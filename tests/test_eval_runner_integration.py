"""Why: the runner is the §20 gate's producer — it must score a REAL
projected build through the real query stack, persist to
builds.eval, and the §14 preflight gate must consume exactly
those numbers: deferred while unscored (never silently passed), blocking on
regression, open otherwise. The eval binding is the fence's only sanctioned
relaxation — its refusals are part of the contract."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
import sqlalchemy as sa
import yaml
from alembic import command
from alembic.config import Config
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.llms import LLM
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.builds.lifecycle import activate, preflight
from core.config import get_settings
from core.eval.golden import load_golden
from core.eval.runner import run_eval
from core.index.indexing import index_build
from core.mcp.policy import load_query_policy
from core.resolve import fingerprints
from core.stores import tables
from core.stores.graph import BuildScopedGraphProjector, graph_driver
from core.stores.repo import BuildScopedWriter, resolve_eval_binding
from core.stores.vectors import BuildScopedVectorProjector, vector_client
from tests.conftest import ensure_project

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(tz=UTC)


class _Embedder:
    async def aget_text_embedding(self, text: str) -> list[float]:
        return [float(len(text)), 1.0, 0.0, 0.0]


class _Llm:
    async def achat(self, messages: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(message=SimpleNamespace(content="{}"))


@pytest.fixture()
def migrated(require_services: None) -> None:
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")


def _engine() -> AsyncEngine:
    dsn = get_settings().postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(dsn, poolclass=NullPool)


@pytest_asyncio.fixture()
async def project(migrated: None) -> AsyncIterator[str]:
    name = f"evalrun-{uuid.uuid4().hex[:10]}"
    yield name
    engine = _engine()
    async with engine.connect() as conn:
        await conn.execute(tables.entities.delete().where(tables.entities.c.project == name))
        await conn.execute(tables.builds.delete().where(tables.builds.c.project == name))
        await conn.commit()
    await engine.dispose()


def _golden_file(tmp_path: Path) -> Path:
    path = tmp_path / "golden.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "cases": [
                    {
                        "question": "Who partners with Acme?",
                        "mode": "semantic",
                        "expects": {
                            "must_contain_entities": ["Acme", "Globex"],
                            "groundedness_min": 0.5,
                        },
                        "min_score": 0.5,
                    },
                    {
                        "question": "Path between Acme and Globex?",
                        "mode": "graph",
                        "expects": {
                            "must_contain_entities": ["Acme", "Globex"],
                            "must_include_relations": [
                                {"src": "Acme", "type": "partners_with", "dst": "Globex"}
                            ],
                            "must_have_valid_paths": True,
                        },
                        "min_score": 0.5,
                    },
                ],
            }
        ),
        "utf-8",
    )
    return path


async def test_run_eval_scores_a_projected_build_and_persists(project: str, tmp_path: Path) -> None:
    engine = _engine()
    qdrant = vector_client()
    driver = graph_driver()
    try:
        async with engine.connect() as conn, driver.session() as session:
            await ensure_project(conn, project)
            build_id: uuid.UUID = (
                await conn.execute(
                    tables.builds.insert()
                    .values(project=project, status="building", started_at=NOW)
                    .returning(tables.builds.c.id)
                )
            ).scalar_one()
            await conn.commit()
            writer = await BuildScopedWriter.for_building_build(conn, project, build_id)

            async def _entity(name: str) -> uuid.UUID:
                entity_id = uuid.uuid4()
                await writer.insert(
                    tables.entities,
                    id=entity_id,
                    type="org",
                    canonical_name=name,
                    entity_key=fingerprints.entity_key("org", name),
                    status="active",
                    review_status="unreviewed",
                    created_by="rule",
                    created_at=NOW,
                    updated_at=NOW,
                )
                await writer.insert_entity_mention(
                    entity_id=entity_id,
                    source_kind="text",
                    source_ref=f"chunk-{entity_id}",
                    surface_form=name,
                    confidence=1.0,
                )
                return entity_id

            acme = await _entity("Acme")
            globex = await _entity("Globex")
            relation_id = uuid.uuid4()
            await writer.insert(
                tables.relations,
                id=relation_id,
                src_entity_id=acme,
                dst_entity_id=globex,
                type="partners_with",
                relation_signature=fingerprints.relation_signature(
                    fingerprints.entity_key("org", "Acme"),
                    "partners_with",
                    fingerprints.entity_key("org", "Globex"),
                ),
                status="active",
                review_status="unreviewed",
                created_by="rule",
                created_at=NOW,
                updated_at=NOW,
            )
            rel_sig = fingerprints.relation_signature(
                fingerprints.entity_key("org", "Acme"),
                "partners_with",
                fingerprints.entity_key("org", "Globex"),
            )
            await writer.insert(
                tables.relation_evidence,
                id=uuid.uuid4(),
                relation_id=relation_id,
                evidence_type="chunk",
                evidence_ref=f"ev-{relation_id}",
                chunk_id=uuid.uuid4(),
                start_offset=0,
                end_offset=25,
                quote="Acme partners with Globex",
                source_uri="s3://docs/a.txt",
                evidence_hash=fingerprints.evidence_hash(
                    rel_sig, f"ev-{relation_id}", "Acme partners with Globex"
                ),
                created_at=NOW,
            )
            await conn.commit()

            vectors = await BuildScopedVectorProjector.for_building_build(
                conn, qdrant, project, build_id
            )
            graph = await BuildScopedGraphProjector.for_building_build(
                conn, session, project, build_id
            )
            await index_build(writer, cast(BaseEmbedding, _Embedder()), vectors, graph)
            await conn.commit()
            await conn.execute(
                tables.builds.update().where(tables.builds.c.id == build_id).values(status="ready")
            )
            await conn.commit()

            golden = load_golden(_golden_file(tmp_path))
            policy = load_query_policy(REPO_ROOT / "projects" / "demo" / "config.yaml")
            report = await run_eval(
                conn,
                qdrant,
                session,
                cast(BaseEmbedding, _Embedder()),
                cast(LLM, _Llm()),
                project,
                build_id,
                golden,
                policy,
            )
            assert len(report.cases) == 2
            semantic_case, graph_case = report.cases
            assert semantic_case.subscores["entity_recall"] == 1.0  # both projected + found
            assert graph_case.subscores["relation_hit_rate"] == 1.0
            # graph entity results carry the SoR canonical name as title
            # (Codex round 3): recall over visible text sees them
            assert graph_case.subscores["entity_recall"] == 1.0
            assert graph_case.subscores["path_validity"] == 1.0  # verified real path
            assert report.passed == 2 and report.failed == 0

            # persisted where the §14 gate reads (one producer, one location)
            row = (
                await conn.execute(
                    sa.select(tables.builds.c.eval).where(tables.builds.c.id == build_id)
                )
            ).one()
            assert row.eval["score"] == pytest.approx(report.score)

            # ---- the §14 gate consumes these numbers ----
            # candidate scored, no active build → gate vacuous (deferred says so)
            check = await preflight(conn, qdrant, session, project, build_id)
            assert check.ok
            assert any("no active build" in d for d in check.deferred)

            # activate it, then gate a WORSE unscored candidate → deferred
            check = await activate(conn, qdrant, session, project, build_id)
            assert check.ok
            await ensure_project(conn, project)
            empty = (
                await conn.execute(
                    tables.builds.insert()
                    .values(project=project, status="ready", started_at=NOW)
                    .returning(tables.builds.c.id)
                )
            ).scalar_one()
            await conn.commit()
            check = await preflight(conn, qdrant, session, project, empty)
            # fail-closed: an unscored candidate against a scored active is
            # REFUSED (a deferral would let the gate's target case promote)
            assert not check.ok
            assert any("no eval score" in f for f in check.failures)

            # score the empty candidate (no data → recall 0) → regression BLOCKS
            await run_eval(
                conn,
                qdrant,
                session,
                cast(BaseEmbedding, _Embedder()),
                cast(LLM, _Llm()),
                project,
                empty,
                golden,
                policy,
            )
            check = await preflight(conn, qdrant, session, project, empty)
            assert not check.ok
            # the per-case bar fires FIRST (both golden cases fail on the
            # empty build), before any regression comparison — same verdict
            # the CLI's exit code gives for this report
            assert any("golden case(s) below their min_score" in f for f in check.failures)
    finally:
        await qdrant.close()
        await driver.close()
        await engine.dispose()


async def test_eval_binding_refuses_unevaluable_builds(project: str) -> None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            await ensure_project(conn, project)
            building: uuid.UUID = (
                await conn.execute(
                    tables.builds.insert()
                    .values(project=project, status="building", started_at=NOW)
                    .returning(tables.builds.c.id)
                )
            ).scalar_one()
            await conn.commit()
            with pytest.raises(LookupError, match="needs ready|active"):
                await resolve_eval_binding(conn, project, building)
            with pytest.raises(LookupError, match="missing"):
                await resolve_eval_binding(conn, project, uuid.uuid4())
    finally:
        await engine.dispose()

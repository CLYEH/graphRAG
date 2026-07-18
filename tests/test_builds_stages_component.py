"""Why: the integration test proves ``default_stages`` runs a real build end to
end, but it can't isolate each adapter's WIRING — that the clean adapter passes
the CONFIG's chunk params (not the defaults), that graph forwards the config's
ontology + proposal policy and refuses an ontology-less text build, that resolve
threads the config's ResolutionConfig, and that each stage report maps into the
right StageResult. These component tests spy on the stage module's deps (no
Postgres/Qdrant/Neo4j/LLM) so the config→stage arg flow and the report→outcome
mapping are pinned in the fast lane, where the integration test can't run.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from core.builds import stages as stages_mod
from core.builds.config import load_build_config
from core.builds.stages import OntologyRequiredError, default_stages
from core.graph.structured import GraphExtractReport
from core.observability.spec import ItemOutcome
from core.registry.store import Source
from core.resolve.resolution import ResolveReport

_BUILD_ID = uuid.uuid4()
_OUTCOME = ItemOutcome(item_kind="document", item_ref="h1", status="cleaned")


class _FakeWriter:
    """Records nothing but the docs it hands back to clean/graph re-reads."""

    def __init__(self, docs: tuple[Any, ...] = (), text_docs: tuple[Any, ...] = ()) -> None:
        self.project = "p"
        self.build_id = _BUILD_ID
        self._docs = docs
        self._text_docs = text_docs

    async def fetch_all(self, table: Any, *where: Any) -> list[Any]:
        # graph's text-doc probe passes a WHERE clause; clean's full read does not.
        return list(self._text_docs) if where else list(self._docs)


def _resolve_report() -> ResolveReport:
    return ResolveReport(**{f.name: 0 for f in ResolveReport.__dataclass_fields__.values()})


@pytest.fixture()
def spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch every stage dependency in the stages module with a spy; return the
    recorded calls. Each adapter is handed this fake writer/projectors."""
    calls: dict[str, Any] = {}
    writer = _FakeWriter()

    async def _for_building_build(conn: Any, project: str, build_id: uuid.UUID) -> Any:
        return writer

    async def _proj(conn: Any, client: Any, project: str, build_id: uuid.UUID) -> Any:
        return SimpleNamespace(kind="projector")

    # patch each class's classmethod at its home module (the same object stages.py
    # imported) — string paths avoid a module-attr-export mypy complaint.
    monkeypatch.setattr(
        "core.stores.repo.BuildScopedWriter.for_building_build", _for_building_build
    )
    monkeypatch.setattr("core.stores.vectors.BuildScopedVectorProjector.for_building_build", _proj)
    monkeypatch.setattr("core.stores.graph.BuildScopedGraphProjector.for_building_build", _proj)

    def _record(name: str, result: Any) -> Any:
        async def fn(*args: Any, **kwargs: Any) -> Any:
            calls[name] = SimpleNamespace(args=args, kwargs=kwargs)
            return result

        return fn

    monkeypatch.setattr(
        stages_mod,
        "ingest_documents",
        _record("ingest", SimpleNamespace(outcomes=(_OUTCOME,), documents=(1,))),
    )
    monkeypatch.setattr(
        stages_mod,
        "extract_structured",
        _record("structured", GraphExtractReport(1, 2, 3, 4, (_OUTCOME,))),
    )
    monkeypatch.setattr(
        stages_mod,
        "extract_documents",
        _record(
            "text",
            SimpleNamespace(
                entities=5,
                relations=6,
                mentions=7,
                evidence=8,
                outcomes=(_OUTCOME,),
                proposals=("prop",),
                discarded=(),
            ),
        ),
    )
    monkeypatch.setattr(stages_mod, "persist_proposals", _record("proposals", None))
    monkeypatch.setattr(stages_mod, "resolve_build", _record("resolve", _resolve_report()))
    monkeypatch.setattr(
        stages_mod,
        "index_build",
        _record(
            "index",
            SimpleNamespace(
                chunks_embedded=1,
                entities_embedded=2,
                entities_projected=3,
                relations_projected=4,
                relations_skipped=5,
                outcomes=(_OUTCOME,),
            ),
        ),
    )
    monkeypatch.setattr(
        stages_mod,
        "summarize_build",
        _record("summarize", SimpleNamespace(communities=2, written=1, outcomes=(_OUTCOME,))),
    )
    calls["_writer"] = writer
    return calls


def _stages(config_raw: dict[str, Any]) -> Any:
    return default_stages(
        load_build_config(config_raw),
        chat_model=SimpleNamespace(name="llm"),  # type: ignore[arg-type]
        embedder=SimpleNamespace(name="embed"),  # type: ignore[arg-type]
        vector_client=SimpleNamespace(name="qdrant"),  # type: ignore[arg-type]
        graph_session=SimpleNamespace(name="neo4j"),  # type: ignore[arg-type]
    )


def test_default_stages_wires_all_six_in_order() -> None:
    stages = _stages({})
    assert [
        callable(getattr(stages, n))
        for n in ("ingest", "clean", "graph", "resolve", "index", "summarize")
    ] == [True] * 6


async def test_clean_passes_the_config_chunk_params_not_the_defaults(
    spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # the wiring the integration test (defaults) can't catch: clean_document
    # must receive config.chunk_max_chars / config.chunk_overlap.
    seen: dict[str, Any] = {}

    async def _clean_doc(writer: Any, doc_id: Any, raw: str, **kwargs: Any) -> list[Any]:
        seen.update(kwargs)
        return [object()]

    monkeypatch.setattr(stages_mod, "clean_document", _clean_doc)
    spy["_writer"]._docs = (SimpleNamespace(id=uuid.uuid4(), raw="x", content_hash="h1"),)

    stages = _stages({"chunking": {"max_chars": 300, "overlap": 40}})
    result = await stages.clean(SimpleNamespace(), "p", _BUILD_ID)

    assert seen == {"max_chars": 300, "overlap": 40}
    assert result.outcomes == (ItemOutcome(item_kind="document", item_ref="h1", status="cleaned"),)
    assert result.detail == {"documents": 1, "chunks": 1}


async def test_graph_forwards_ontology_and_proposal_policy(spy: dict[str, Any]) -> None:
    config = {
        "ontology": {
            "entity_types": ["Person"],
            "relation_types": ["KNOWS"],
            "proposal_policy": "auto",
        }
    }
    stages = _stages(config)
    result = await stages.graph(SimpleNamespace(), "p", _BUILD_ID)

    # extract_documents got the config's TextOntology; persist_proposals got the policy.
    assert spy["text"].args[2].entity_types == ("Person",)
    assert spy["proposals"].kwargs["policy"] == "auto"
    # both stages' outcomes concatenated; proposal count folded into detail.
    assert result.outcomes == (_OUTCOME, _OUTCOME)
    assert result.detail["text"]["proposals"] == 1


async def test_graph_without_ontology_but_with_text_docs_raises(spy: dict[str, Any]) -> None:
    # config-gap guard: no ontology + a text-mime document = loud failure.
    spy["_writer"]._text_docs = (SimpleNamespace(id=uuid.uuid4()),)
    stages = _stages({})
    with pytest.raises(OntologyRequiredError, match="no ontology"):
        await stages.graph(SimpleNamespace(), "p", _BUILD_ID)


async def test_graph_without_ontology_and_no_text_docs_runs_structured_only(
    spy: dict[str, Any],
) -> None:
    stages = _stages({})
    result = await stages.graph(SimpleNamespace(), "p", _BUILD_ID)
    assert "text" not in result.detail  # LLM path skipped
    assert result.detail["structured"]["entities"] == 1
    assert "proposals" not in spy  # persist_proposals not called


async def test_resolve_threads_config_and_maps_counts_to_detail(spy: dict[str, Any]) -> None:
    stages = _stages({"resolution": {"embedding_weight": 0.4}})
    result = await stages.resolve(SimpleNamespace(), "p", _BUILD_ID)

    # resolve_build got (conn, writer, config.resolution); no per-item outcomes.
    assert spy["resolve"].args[2].embedding_weight == 0.4
    assert result.outcomes == ()
    assert result.detail == asdict(_resolve_report())


async def test_index_builds_projectors_and_maps_report(spy: dict[str, Any]) -> None:
    stages = _stages({})
    result = await stages.index(SimpleNamespace(), "p", _BUILD_ID)
    assert result.outcomes == (_OUTCOME,)
    assert result.detail["entities_projected"] == 3
    assert result.detail["relations_skipped"] == 5


async def test_summarize_maps_report(spy: dict[str, Any]) -> None:
    stages = _stages({})
    result = await stages.summarize(SimpleNamespace(), "p", _BUILD_ID)
    assert result.outcomes == (_OUTCOME,)
    assert result.detail == {"communities": 2, "written": 1}


async def test_ingest_pages_sources_and_flatmaps_payloads(
    spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    src = Source(
        id=uuid.uuid4(),
        project="p",
        kind="text",
        uri="file:///x",
        metadata={},
        added_at=datetime(2026, 1, 1),
    )

    seen: dict[str, Any] = {}

    async def _list_sources(
        conn: Any, project: str, *, limit: int, after: Any = None, enabled_only: bool = False
    ) -> Any:
        seen["enabled_only"] = enabled_only
        return ([src], None) if after is None else ([], None)

    monkeypatch.setattr(stages_mod, "list_sources", _list_sources)
    monkeypatch.setattr(stages_mod, "resolve_source", lambda s: iter([SimpleNamespace(raw="doc")]))

    stages = _stages({})
    result = await stages.ingest(SimpleNamespace(), "p", _BUILD_ID)

    # ingest_documents received the flattened payloads; report mapped through.
    assert list(spy["ingest"].args[1]) == [SimpleNamespace(raw="doc")]
    assert result.detail == {"sources": 1, "documents": 1}
    # SRC2: the build must load ONLY enabled sources — a disabled source is
    # excluded from future builds (regression guard: dropping the flag would
    # silently re-ingest disabled corpus).
    assert seen["enabled_only"] is True

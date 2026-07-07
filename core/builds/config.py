"""Build-config loader (BA2c-2a) — ``projects.config`` JSONB → typed §5/§6 config.

The typed targets already validate their OWN business rules at construction:
:class:`~core.graph.ontology.TextOntology` (non-empty type vocabularies),
:class:`~core.graph.ontology.StructuredMapping`/``EntityRule``/``RelationRule``
(non-empty columns, relation endpoints reference declared aliases),
:class:`~core.resolve.resolution.ResolutionConfig` (threshold ordering,
embedding-weight bounds). This loader owns only the layer they don't: pull each
block out of the untrusted JSON by fixed key, type-check every LEAF value (a
``bool`` is not an ``int`` — ``isinstance(True, int)`` is True in Python), and
construct the dataclasses, letting their ``__post_init__`` raise. Every failure
— shape, leaf type, or business rule — is wrapped in one :class:`BuildConfigError`
so a caller (BA2e's build trigger) catches exactly one exception type. Business
rules are NOT restated here (that would be two rule sources that could drift —
the rule-self-consistency lesson); the loader reuses the dataclasses' own
``__post_init__`` and :data:`core.graph.proposals.PROPOSAL_POLICIES`.

``projects.config`` is free-form ONLY at the top level: the API stores and
returns it verbatim (``POST``/``PATCH /projects``), so it may carry keys beyond
build config — the loader reads the keys it knows and IGNORES unknown top-level
ones. Every RECOGNIZED nested block, by contrast, has a closed key set and
rejects unknown keys, so a typo on an optional key (``disambiguator`` for
``disambiguator_column``) fails loud instead of silently disabling the field
(which, for a disambiguator, would collapse distinct same-name rows into one
entity). It also rejects a ``"table"`` field inside a structured mapping: the
mapping KEY is the table name, and a second value could disagree with it (the
composite-identifier lesson — made unrepresentable, not just re-checked).

Not a frozen contract: ``projects.config`` is internal (no ``web``/agent surface
parses it against a shared schema), so it evolves additively via code review —
no ``contracts/`` file, no ``schema_version`` (DR-002 untouched). ``config_hash``
computation belongs with the first real caller (BA2e's trigger, which also owns
the resume-time config-drift check); it is a documented seam, not built here.

Example ``projects.config`` shape (documented here, not schema-frozen)::

    {
      "ontology": {"entity_types": ["Person"], "relation_types": ["WORKS_AT"],
                   "proposal_policy": "review"},
      "structured_mappings": {
        "companies": {"entities": {"co": {"entity_type": "Company",
                       "name_column": "name", "disambiguator_column": "id"}},
                      "relations": []}},
      "resolution": {"auto_merge_threshold": 0.92, "review_threshold": 0.75,
                     "embedding_weight": 0.0, "carry_review": true},
      "chunking": {"max_chars": 1200, "overlap": 200}
    }

An absent block takes its documented default; a PRESENT-but-malformed block
fails loud — an omitted key and a broken key must never collapse to the same
behavior (the per-field-nullability lesson). An explicit ``null`` for a known
block (``{"ontology": null}``) is malformed, not the default: ``null`` is not a
valid block, so it raises — omit the key to take the default (or, for
``ontology``, to declare no ontology). The ``chunking`` numeric relation
(``0 <= overlap < max_chars``) is deliberately NOT re-validated here — only the
leaf types are — so the rule stays owned by ``chunk_text`` alone; a violation
surfaces (loud) when the clean stage runs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from core.clean.chunking import DEFAULT_MAX_CHARS, DEFAULT_OVERLAP
from core.graph.ontology import EntityRule, RelationRule, StructuredMapping, TextOntology
from core.graph.proposals import PROPOSAL_POLICIES
from core.resolve.resolution import ResolutionConfig

#: Hold LLM-proposed types for review unless the config opts into auto-adoption.
#: (Moot when there is no ontology — no text extraction runs — but a sane
#: default keeps ``BuildConfig.ontology_proposal_policy`` always well-formed.)
_DEFAULT_PROPOSAL_POLICY = "review"


class BuildConfigError(ValueError):
    """``projects.config`` is malformed — a shape/leaf-type error the typed
    config objects can't express, or a business-rule violation they raise. One
    type so the caller (BA2e's build trigger) catches exactly one exception."""


@dataclass(frozen=True)
class BuildConfig:
    """The typed, validated config one build runs against — this loader's output
    and ``default_stages``' input (BA2c-2b).

    ``ontology`` is None when the project declares no text ontology; that is
    legal only if the build has no text-mime documents — the graph stage
    enforces that at run time (an ontology-less build with text docs is a config
    gap, not a silent skip)."""

    ontology: TextOntology | None
    ontology_proposal_policy: str
    structured_mappings: Mapping[str, StructuredMapping]
    resolution: ResolutionConfig
    chunk_max_chars: int
    chunk_overlap: int


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BuildConfigError(f"{path} must be an object, got {type(value).__name__}")
    return value


def _str(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise BuildConfigError(f"{path} must be a string, got {type(value).__name__}")
    return value


def _int(value: Any, path: str) -> int:
    # bool is an int subclass in Python — reject it explicitly, else `true`
    # silently becomes 1 (the leaf-type-coercion lesson).
    if isinstance(value, bool) or not isinstance(value, int):
        raise BuildConfigError(f"{path} must be an integer, got {type(value).__name__}")
    return value


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BuildConfigError(f"{path} must be a number, got {type(value).__name__}")
    return float(value)


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise BuildConfigError(f"{path} must be a boolean, got {type(value).__name__}")
    return value


def _str_list(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise BuildConfigError(f"{path} must be an array, got {type(value).__name__}")
    return tuple(_str(item, f"{path}[{i}]") for i, item in enumerate(value))


def _reject_unknown(block: Mapping[str, Any], allowed: set[str], path: str) -> None:
    """Fail loud on unknown keys in a CLOSED nested block. Only the top-level
    ``projects.config`` is free-form; every block the loader recognizes has a
    fixed key set, so a typo on an OPTIONAL key (``disambiguator`` for
    ``disambiguator_column``) must raise, not silently fall to the default and
    disable the field (which, for a disambiguator, collapses distinct same-name
    rows into one entity)."""
    extra = sorted(set(block) - allowed)
    if extra:
        raise BuildConfigError(f"{path} has unknown key(s) {extra}; allowed: {sorted(allowed)}")


def _construct[T](path: str, build: Callable[[], T]) -> T:
    """Run a dataclass constructor, re-wrapping its ``__post_init__``
    ``ValueError`` (the business-rule failure) as a path-prefixed
    ``BuildConfigError`` so callers catch one type."""
    try:
        return build()
    except ValueError as exc:
        raise BuildConfigError(f"{path}: {exc}") from exc


def _load_ontology(raw: Mapping[str, Any]) -> tuple[TextOntology | None, str]:
    if "ontology" not in raw:  # omitted → no ontology; explicit null → _mapping rejects it
        return None, _DEFAULT_PROPOSAL_POLICY
    block = _mapping(raw["ontology"], "ontology")
    _reject_unknown(block, {"entity_types", "relation_types", "proposal_policy"}, "ontology")
    policy = _DEFAULT_PROPOSAL_POLICY
    if "proposal_policy" in block:
        policy = _str(block["proposal_policy"], "ontology.proposal_policy")
        if policy not in PROPOSAL_POLICIES:
            raise BuildConfigError(
                f"ontology.proposal_policy must be one of {list(PROPOSAL_POLICIES)}, got {policy!r}"
            )
    entity_types = _str_list(block.get("entity_types", []), "ontology.entity_types")
    relation_types = _str_list(block.get("relation_types", []), "ontology.relation_types")
    ontology: TextOntology = _construct(
        "ontology",
        lambda: TextOntology(entity_types=entity_types, relation_types=relation_types),
    )
    return ontology, policy


def _load_entity_rule(raw: Any, path: str) -> EntityRule:
    block = _mapping(raw, path)
    _reject_unknown(block, {"entity_type", "name_column", "disambiguator_column"}, path)
    for key in ("entity_type", "name_column"):
        if key not in block:
            raise BuildConfigError(f"{path}.{key} is required")
    disambiguator = block.get("disambiguator_column")
    # type-check leaves BEFORE _construct so a wrong type surfaces once (its own
    # path), not double-prefixed by _construct's business-rule wrapper.
    entity_type = _str(block["entity_type"], f"{path}.entity_type")
    name_column = _str(block["name_column"], f"{path}.name_column")
    disambiguator_str = (
        _str(disambiguator, f"{path}.disambiguator_column") if disambiguator is not None else None
    )
    return _construct(
        path,
        lambda: EntityRule(
            entity_type=entity_type,
            name_column=name_column,
            disambiguator_column=disambiguator_str,
        ),
    )


def _load_relation_rule(raw: Any, path: str) -> RelationRule:
    block = _mapping(raw, path)
    _reject_unknown(block, {"relation_type", "src", "dst"}, path)
    for key in ("relation_type", "src", "dst"):
        if key not in block:
            raise BuildConfigError(f"{path}.{key} is required")
    relation_type = _str(block["relation_type"], f"{path}.relation_type")
    src = _str(block["src"], f"{path}.src")
    dst = _str(block["dst"], f"{path}.dst")
    return _construct(
        path,
        lambda: RelationRule(relation_type=relation_type, src=src, dst=dst),
    )


def _load_structured_mapping(table: str, raw: Any, path: str) -> StructuredMapping:
    block = _mapping(raw, path)
    if "table" in block:
        raise BuildConfigError(
            f"{path} must not carry a 'table' field — the mapping key IS the table "
            f"name ({table!r}); a second value could disagree with it"
        )
    _reject_unknown(block, {"entities", "relations"}, path)
    entities = {
        alias: _load_entity_rule(rule, f"{path}.entities.{alias}")
        for alias, rule in _mapping(block.get("entities", {}), f"{path}.entities").items()
    }
    relations_raw = block.get("relations", [])
    if not isinstance(relations_raw, list):
        raise BuildConfigError(
            f"{path}.relations must be an array, got {type(relations_raw).__name__}"
        )
    relations = tuple(
        _load_relation_rule(rule, f"{path}.relations[{i}]") for i, rule in enumerate(relations_raw)
    )
    return _construct(
        path,
        lambda: StructuredMapping(table=table, entities=entities, relations=relations),
    )


def _load_structured_mappings(raw: Mapping[str, Any]) -> dict[str, StructuredMapping]:
    if "structured_mappings" not in raw:  # omitted → none; explicit null → rejected
        return {}
    return {
        table: _load_structured_mapping(table, mapping, f"structured_mappings.{table}")
        for table, mapping in _mapping(raw["structured_mappings"], "structured_mappings").items()
    }


def _load_resolution(raw: Mapping[str, Any]) -> ResolutionConfig:
    if "resolution" not in raw:  # omitted → defaults; explicit null → rejected
        return ResolutionConfig()
    block = _mapping(raw["resolution"], "resolution")
    _reject_unknown(
        block,
        {"auto_merge_threshold", "review_threshold", "embedding_weight", "carry_review"},
        "resolution",
    )
    kwargs: dict[str, Any] = {}
    for key in ("auto_merge_threshold", "review_threshold", "embedding_weight"):
        if key in block:
            kwargs[key] = _number(block[key], f"resolution.{key}")
    if "carry_review" in block:
        kwargs["carry_review"] = _bool(block["carry_review"], "resolution.carry_review")
    return _construct("resolution", lambda: ResolutionConfig(**kwargs))


def _load_chunking(raw: Mapping[str, Any]) -> tuple[int, int]:
    if "chunking" not in raw:  # omitted → defaults; explicit null → rejected
        return DEFAULT_MAX_CHARS, DEFAULT_OVERLAP
    block = _mapping(raw["chunking"], "chunking")
    _reject_unknown(block, {"max_chars", "overlap"}, "chunking")
    max_chars = (
        _int(block["max_chars"], "chunking.max_chars")
        if "max_chars" in block
        else DEFAULT_MAX_CHARS
    )
    overlap = _int(block["overlap"], "chunking.overlap") if "overlap" in block else DEFAULT_OVERLAP
    return max_chars, overlap


def load_build_config(raw: Mapping[str, Any]) -> BuildConfig:
    """Parse ``projects.config`` into a validated :class:`BuildConfig`.

    Raises :class:`BuildConfigError` for any malformed block (bad shape, wrong
    leaf type, or a business-rule violation the typed objects reject). An empty
    ``{}`` config is valid: no ontology, no structured mappings, default
    resolution and chunking — whether that yields a runnable build depends on
    the project's sources, which the graph stage checks against the ontology."""
    raw = _mapping(raw, "config")
    ontology, proposal_policy = _load_ontology(raw)
    chunk_max_chars, chunk_overlap = _load_chunking(raw)
    return BuildConfig(
        ontology=ontology,
        ontology_proposal_policy=proposal_policy,
        structured_mappings=_load_structured_mappings(raw),
        resolution=_load_resolution(raw),
        chunk_max_chars=chunk_max_chars,
        chunk_overlap=chunk_overlap,
    )

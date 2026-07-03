"""Why: §18's "重跑對得上" (reruns line up) and §27.7's retry-failed-only are
cross-producer agreements — the pipeline writers (C2+), the retry entrypoint,
and Console's "[retry failed only]" button must identify items identically or
a retry re-processes the wrong work (or silently none). These tests pin the
stable-key mapping and the retry boundary semantics.
"""

from __future__ import annotations

from core.observability.spec import (
    ITEM_REF_STABLE_KEYS_MIN,
    ITEM_STATUS_FAILED,
    ITEM_STATUSES_MIN,
    SOURCE_VALIDATION_RUN_KIND,
    ItemOutcome,
    retry_failed_only,
)

# --- frozen vocabularies (§18/§27.7) -------------------------------------------


def test_stable_key_mapping_freezes_design() -> None:
    """§18 verbatim: document=content_hash, entity=entity_key. Repointing a
    kind would orphan every already-recorded item_ref."""
    assert dict(ITEM_REF_STABLE_KEYS_MIN) == {
        "document": "content_hash",
        "entity": "entity_key",
    }


def test_minimum_item_statuses_freeze_design() -> None:
    """§4/§18: default verbosity records exactly failed/skipped — renaming
    either would silently empty retries or the skip accounting."""
    assert ITEM_STATUSES_MIN == ("failed", "skipped")
    assert ITEM_STATUS_FAILED in ITEM_STATUSES_MIN


def test_source_validation_is_the_only_build_unbound_kind() -> None:
    """§27.7: renaming this constant would strand the CHECK constraint's
    literal (they're lockstep-tested) and silently re-bind validation jobs."""
    assert SOURCE_VALIDATION_RUN_KIND == "source_validation"


# --- retry boundary (§27.7) -----------------------------------------------------


def test_only_failed_items_enter_the_retry() -> None:
    """§27.7: the retry input is the *failed* set — skipped items were skipped
    for a reason (unchanged content) and successes must not be redone, or
    "retry failed only" quietly becomes "rerun everything"."""
    items = [
        ItemOutcome("document", "hash-a", "failed"),
        ItemOutcome("document", "hash-b", "skipped"),
        ItemOutcome("entity", "key-c", "ok"),
    ]
    assert retry_failed_only(items) == frozenset({("document", "hash-a")})


def test_an_item_failing_in_several_steps_retries_once() -> None:
    """§27.7 idempotency: dedup by item_ref — a document that failed at both
    clean and graph re-enters the retry exactly once, so retries never fan
    out duplicated work."""
    items = [
        ItemOutcome("document", "hash-a", "failed"),  # failed at clean
        ItemOutcome("document", "hash-a", "failed"),  # failed at graph too
    ]
    assert retry_failed_only(items) == frozenset({("document", "hash-a")})


def test_item_kind_is_part_of_the_identity() -> None:
    """Dedup is by (item_kind, item_ref), not item_ref alone: a document and
    an entity may share a ref string without one's retry swallowing the
    other's."""
    items = [
        ItemOutcome("document", "same-ref", "failed"),
        ItemOutcome("entity", "same-ref", "failed"),
    ]
    assert retry_failed_only(items) == frozenset({("document", "same-ref"), ("entity", "same-ref")})


def test_no_failures_means_an_empty_retry() -> None:
    """A fully-green run must produce an empty retry set — anything else would
    make "[retry failed only]" a destructive rerun button."""
    assert retry_failed_only([ItemOutcome("document", "hash-a", "skipped")]) == frozenset()
    assert retry_failed_only([]) == frozenset()

"""Why: §27.4 evidence spans and §27.2 chunk source refs point back into the
ORIGINAL document text via chunk offsets — `raw[start:end] == chunk.text`
must hold EXACTLY, or a citation two steps downstream quotes the wrong span.
These invariants are property-tested (H4): boundary bugs live in adversarial
inputs (unbreakable runs, whitespace-only text, size == overlap + 1), not in
the polite examples a hand-written test would pick.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.clean.chunking import Chunk, chunk_text

_texts = st.text(max_size=2000)


@st.composite
def _params(draw: st.DrawFn) -> tuple[int, int]:
    max_chars = draw(st.integers(min_value=1, max_value=300))
    overlap = draw(st.integers(min_value=0, max_value=max_chars - 1))
    return max_chars, overlap


@given(text=_texts, params=_params())
def test_offsets_are_exact_and_ordinals_sequential(text: str, params: tuple[int, int]) -> None:
    """The load-bearing invariant: every chunk is literally the slice its
    offsets claim, and ordinals are the gapless sequence reruns rely on."""
    max_chars, overlap = params
    chunks = chunk_text(text, max_chars=max_chars, overlap=overlap)
    for chunk in chunks:
        assert text[chunk.start_offset : chunk.end_offset] == chunk.text
        assert chunk.text  # never an empty chunk row
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))


@given(text=_texts, params=_params())
def test_chunks_cover_the_text_without_gaps_and_always_advance(
    text: str, params: tuple[int, int]
) -> None:
    """Retrieval loses content silently if a byte falls between chunks; the
    chunker also must always move forward, or one adversarial document hangs
    the whole clean step."""
    max_chars, overlap = params
    chunks = chunk_text(text, max_chars=max_chars, overlap=overlap)
    if not text:
        assert chunks == []
        return
    assert chunks[0].start_offset == 0
    assert chunks[-1].end_offset == len(text)
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current.start_offset <= previous.end_offset  # no gap
        assert current.start_offset > previous.start_offset  # strict progress


@given(text=_texts, params=_params())
def test_no_chunk_exceeds_the_window_and_output_is_deterministic(
    text: str, params: tuple[int, int]
) -> None:
    """max_chars is the §23 tunable contract with downstream token budgets;
    determinism is what makes content_hash-keyed reruns line up (§18)."""
    max_chars, overlap = params
    chunks = chunk_text(text, max_chars=max_chars, overlap=overlap)
    assert all(len(chunk.text) <= max_chars for chunk in chunks)
    assert chunks == chunk_text(text, max_chars=max_chars, overlap=overlap)


def test_words_survive_when_a_whitespace_boundary_is_in_reach() -> None:
    """The whitespace preference is behavior worth pinning by example: a
    window that would split mid-word ends at the space instead."""
    text = "alpha beta gamma delta"
    chunks = chunk_text(text, max_chars=12, overlap=2)
    assert chunks[0].text == "alpha beta "
    # and the exact-offset invariant still holds around the snap
    assert text[chunks[1].start_offset : chunks[1].end_offset] == chunks[1].text


def test_invalid_parameters_are_rejected_loudly() -> None:
    """overlap >= max_chars means the window can never advance — an infinite
    loop shipped as configuration; refused at the boundary instead."""
    with pytest.raises(ValueError, match="overlap"):
        chunk_text("abc", max_chars=10, overlap=10)
    with pytest.raises(ValueError, match="max_chars"):
        chunk_text("abc", max_chars=0, overlap=0)


def test_token_count_is_the_documented_heuristic() -> None:
    """len//4 (min 1) — good enough for §19 stats until C5's real tokenizer;
    pinned so a silent change shows up as a failing expectation."""
    (chunk,) = chunk_text("x" * 100, max_chars=200, overlap=0)
    assert chunk == Chunk(0, "x" * 100, 0, 100, 25)

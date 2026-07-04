"""LLM construction behind typed settings (DESIGN §3, C3b).

§3 fixes the LLM abstraction: **LlamaIndex's built-in ``LLM`` base class**
(OpenAI + Claude switchable), default provider OpenAI 🔧 ``gpt-5.4-nano``.
This factory is the single place a concrete provider is constructed — every
consumer (extraction now, resolve/summarize later) takes an
``llama_index.core.llms.LLM`` and stays provider-blind, which IS the
switchability §3 promises. Configuration comes from :mod:`core.config` only;
no module reads ``os.environ`` (guardrail).

Temperature is pinned to 0: pipeline extraction feeds fingerprint-deduped
storage, so run-to-run stability matters more than creativity. Additional
providers (Claude per §3) are additive here — one new branch, consumers
untouched.
"""

from __future__ import annotations

from llama_index.core.llms import LLM
from llama_index.llms.openai import OpenAI

from core.config import get_settings


class LLMNotConfiguredError(RuntimeError):
    """The configured provider cannot be constructed from settings.

    Raised at factory time — a missing key must fail when the pipeline is
    wired, not minutes later on the first chunk's API call.
    """


def chat_model() -> LLM:
    """Build the configured LLM (§3: provider 🔧, model 🔧, key via settings)."""
    settings = get_settings()
    if settings.llm_provider != "openai":
        raise LLMNotConfiguredError(
            f"unsupported llm_provider {settings.llm_provider!r} — 'openai' is "
            "the wired provider; adding one (e.g. Claude, §3) is an additive "
            "branch in core.llm.factory"
        )
    if not settings.openai_api_key:
        raise LLMNotConfiguredError(
            "OPENAI_API_KEY is not set — put it in .env (see .env.example); "
            "core reads it via core.config, never os.environ"
        )
    return OpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        temperature=0.0,
    )

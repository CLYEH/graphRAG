"""The model provider's client exception family (MCP12).

The §3 abstraction layer owns the vendor import: consumers (``core.mcp``,
``core.query``) catch ``LLM_CLIENT_ERRORS`` without ever naming the vendor —
the same one-home discipline as :mod:`core.stores.errors`. OpenAI sits on the
critical path of embedding, NL→SQL, and hybrid routing, so an unhandled
provider exception (a single 429, a network blip) turned EVERY retrieval tool
into a hard MCP ``isError`` with the provider's raw message — upstream error
text (provider identity, model token ceilings) relayed verbatim to an
untrusted caller. Catchers must name only the exception CLASS, never
interpolate ``str(exc)``.
"""

from __future__ import annotations

from openai import OpenAIError

#: every openai client failure (APIConnectionError, RateLimitError,
#: APIStatusError, ...) shares this root — deliberately NOT Exception, so an
#: in-code bug still propagates loud (the ``_bounded`` doctrine).
LLM_CLIENT_ERRORS: tuple[type[BaseException], ...] = (OpenAIError,)

# AGENTS.md

This repo's agent guide lives in **[`CLAUDE.md`](CLAUDE.md)** — read it first (applies to all agents).

TL;DR:
- Design source of truth: `docs/DESIGN.md` (v0.5). Task queue: `TASKS.md`. Loop protocol: `docs/LOOP.md`.
- Definition of done: `uv run poe check-all` is green, then commit.
- Guardrails: Postgres = SoR; all store access via the build-scoped repository (never raw clients);
  `contracts/` is frozen (bump `schema_version` to change); `core` has no HTTP/UI deps.
- LLM default = OpenAI, via `core.config` (never read env directly).

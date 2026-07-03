# graphRAG — agent working guide

Read this before working in this repo. The full design & rationale is the source of
truth: **[`docs/DESIGN.md`](docs/DESIGN.md)** (spec **v0.5, implementation-freeze**).
Work is driven task-by-task per **[`docs/LOOP.md`](docs/LOOP.md)** from **[`TASKS.md`](TASKS.md)**.

## Definition of Done (one task = one PR)
A task is done only when it passes **four gates** and its PR merges (full flow in [`docs/LOOP.md`](docs/LOOP.md)):
1. **Local gates green** — `uv run poe check-all` (backend fmt/lint/type/test + frontend). `check-full` if it touches stores.
2. **Local agent review** — the `code-reviewer` subagent returns `VERDICT: PASS`.
3. **CI green** on the PR (`.github/workflows/ci.yml`).
4. **Codex review** — `chatgpt-codex-connector[bot]` reacts 👍 (`+1`) with no open suggestions.
   👀 = still reviewing (wait); no reaction & no comment = poke it with `@codex review`; a new
   Codex comment = **triage it** per LOOP.md step 7 (must-fix → back to step 3; else
   reply-and-resolve citing the justifying DESIGN §/DR). Unresolved Codex threads block merge
   (GitHub `required_conversation_resolution`); resolve them before merging.
   **No merge without `+1` — no exceptions.** CI green + resolved threads do **not** substitute
   for it. Enforced mechanically by a PreToolUse hook
   (`.claude/hooks/require-codex-approval.sh`) that blocks `gh pr merge` until Codex `+1`.
A failure at gate 2, 3, or 4 sends you back to implementation. Never loosen
`ruff`/`mypy`/`tsconfig`/test configs to pass — fix the code. Push is per-task to a
`task/<id>` branch → PR; never commit straight to `main`.
Backend-only gate: `uv run poe check` · Frontend-only: `uv run poe web-check`.

### Tests — write them with every task
| Tier | When | Command |
|---|---|---|
| unit + contract + component | **every iteration** (in `check-all`) | `uv run poe check-all` |
| coverage (fail-under 85) | every iteration / CI | `uv run poe test-cov` |
| integration (needs services) | task touches stores/pipeline/API | `docker compose up -d && uv run poe check-full` |
| e2e (Console flows) | task touches UI flows | `cd web && npm run test:e2e` (once: `npx playwright install`) |

Mark backend tests: `@pytest.mark.integration` (auto-skips if services down) / `contract` / `eval` / `e2e` / `slow`.
A task is **not done without tests that encode *why*** the behavior matters (DESIGN §24).

## Environment
```bash
uv sync                   # Python env
docker compose up -d      # postgres + neo4j + qdrant + redis
cp .env.example .env       # secrets (OPENAI_API_KEY, ...)
```
Tooling: uv · ruff · mypy (strict) · pytest · poe (backend); oxlint · prettier · tsc · vitest (frontend, in `web/`).

## Agent memory (portable across machines)
Claude Code's auto memory (its own persistent notes — distinct from this CLAUDE.md, which you
write) defaults to a machine-local path under `~/.claude/projects/...`. This repo redirects it
into `.claude/memory/` instead, so it travels with the repo via git — but **the redirect is not
automatic on checkout**; it needs a one-time local setup per checkout/worktree:
- The redirect (`autoMemoryDirectory`) needs an **absolute path**, so it can't live in the shared
  `.claude/settings.json`. Create `.claude/settings.local.json` (gitignored) with:
  ```json
  { "autoMemoryDirectory": "<absolute-path-to-this-checkout>/.claude/memory" }
  ```
  Until this file exists, Claude keeps writing auto-memory to the old machine-local path and
  never touches `.claude/memory/`.
- Once set up, `.claude/memory/*.md` (incl. `MEMORY.md`) are regular tracked files, committed
  like any other file. Memory updates from a session will show up in `git status` — commit them
  (with the task's commit, or standalone) so other machines/worktrees pick them up on their next
  pull.
- Every worktree needs this set independently (pointing at its own `.claude/memory`) — there's no
  cross-worktree sharing of the override. Content still converges normally through git.

## Architecture guardrails (from DESIGN — do not violate)
- **Postgres is the single source of truth.** Neo4j & Qdrant are *derived projections*, tagged with `build_id`.
- **DR-006 — never touch a raw store client** from query/MCP/api layers. All store access goes through the
  build-scoped repository layer, which injects `build_id = active`. This is how "never mix old-version data" is enforced structurally.
- **DR-001 — active build** is decided solely by Postgres `builds.status='active'` (a partial unique index guarantees one).
- **DR-002 — `contracts/` is frozen.** Don't change `openapi.yaml` / `mcp_response.schema.json` without bumping
  `schema_version` and recording it in DESIGN §26.
- **DR-003 — review decisions live in the non-build-scoped `review_ledger`**, keyed by stable fingerprints.
- **Dependency direction:** `api`/`cli`/`projects/*/mcp` → `core`; `web` → `api` OpenAPI contract only; `core`
  has no HTTP/UI dependency. Don't import upward.
- **LLM default is OpenAI** (`gpt-5.4-nano`), behind the abstraction layer — never read `os.environ` directly; use `core.config`.

## Conventions
- Surgical changes: every changed line should trace to the task. Don't refactor unrelated code.
- Match existing style. Add types (mypy strict). Tests encode *why*, not just *what*.
- Secrets never committed; `.env` is gitignored.
- If a task is ambiguous or conflicts with DESIGN, **stop and ask** — don't guess.

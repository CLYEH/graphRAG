# graphRAG

[![CI](https://github.com/CLYEH/graphRAG/actions/workflows/ci.yml/badge.svg)](https://github.com/CLYEH/graphRAG/actions/workflows/ci.yml)

Multi-project **hybrid RAG platform** — clean raw data into a knowledge graph and
expose it to agents over **MCP**, with a Web Console for humans to import, clean,
inspect, review, and query. Each project (A/B/C…) has its own data and its own MCP
server. Retrieval is hybrid: **vector (Qdrant) + graph (Neo4j) + SQL (Postgres)**.

> Full design & decisions: [`docs/DESIGN.md`](docs/DESIGN.md). Current spec: **v0.5 (implementation-freeze)**.

## Stack
- **Core / backend**: Python 3.12, LlamaIndex, Postgres (SoR) + Qdrant + Neo4j, arq + Redis
- **Web Console backend**: FastAPI (`api/`) · **frontend**: React + Vite + TypeScript (`web/`)
- **Tooling**: uv · ruff · mypy · pytest · poe (backend) · eslint/prettier/vitest (frontend)

## Getting started
```bash
uv sync                      # create env + install (Python)
cp .env.example .env         # fill in secrets (OPENAI_API_KEY, ...)
docker compose up -d --wait  # postgres + neo4j + qdrant + redis
uv run poe migrate           # apply Postgres migrations (first run, and after any schema change)
```

## Run it locally
Three processes, one per terminal. All three — the Console alone shows you nothing, and
without the worker a build is only ever *queued*.

```bash
# 1. API (FastAPI). No poe task on purpose: the app is a factory, so call uvicorn directly.
uv run uvicorn api.app:create_app --factory --reload --port 8000

# 2. Build worker (arq). The API enqueues jobs; this is what actually runs them.
uv run poe worker

# 3. Web Console (Vite)  →  open http://localhost:5173
cd web && npm install && npm run dev
```

In dev, Vite proxies `/projects` and `/jobs` to `http://localhost:8000`. If the API is on a
different port, point the Console at it: `VITE_API_PROXY=http://localhost:8010 npm run dev`.

### Three things that look broken but aren't
- **Postgres is published on `15432`**, not 5432 (`.env.example` already points there) — so it
  can coexist with a local Postgres.
- **The Console shows only the *active* build.** A build that finished is not active yet:
  activation runs a preflight that blocks it on an eval regression (DESIGN §14, threshold in
  §20), and the active build is decided solely by Postgres `builds.status='active'` (DR-001).
  So a page with nothing in it usually means *no active build*, not a failed ingest.
- **Project segments in the URL are base64url-encoded** — `/p/ZmUzcWE/inspect`, not
  `/p/fe3qa/inspect`. Navigate from the project switcher rather than typing a project key into
  the address bar, or you'll get "Unknown project".

## Quality gates
```bash
uv run poe check-all         # backend (fmt/lint/type/test) + frontend — the per-iteration gate
uv run poe check-full        # + integration tests (needs: docker compose up -d)
cd web && npm run test:e2e   # Console e2e (once: npx playwright install)
```

## The loop (controlled agent workflow)
Work is driven task-by-task from [`TASKS.md`](TASKS.md); the protocol and guardrails
are in [`docs/LOOP.md`](docs/LOOP.md). One task = one PR, and "done" is four gates, not just a
green test run: **local gates** (`uv run poe check-all`) → **agent code review** → **CI green** →
**Codex approval**. The full flow, including what to do when a gate fails, is in `docs/LOOP.md`.

## Layout
```
core/   shared engine (no HTTP/UI deps)      api/   FastAPI console backend
web/    React console frontend               cli/   graphrag CLI
projects/<name>/  per-project data + config  contracts/  frozen OpenAPI + MCP schemas
docs/DESIGN.md    docker-compose.yml          pyproject.toml
```

# graphRAG

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
docker compose up -d         # postgres + neo4j + qdrant + redis
uv run poe check             # backend quality gates (fmt/lint/type/test)

cd web && npm install && npm run check   # frontend gates
```

## The loop (controlled agent workflow)
Work is driven task-by-task from [`TASKS.md`](TASKS.md); the protocol and guardrails
are in [`docs/LOOP.md`](docs/LOOP.md). Definition of done for any change: **`uv run poe check`
(and `npm run check` in `web/` when the frontend is touched) is green**, then commit.

## Layout
```
core/   shared engine (no HTTP/UI deps)      api/   FastAPI console backend
web/    React console frontend               cli/   graphrag CLI
projects/<name>/  per-project data + config  contracts/  frozen OpenAPI + MCP schemas
docs/DESIGN.md    docker-compose.yml          pyproject.toml
```

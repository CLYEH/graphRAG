---
name: graphrag-architecture
description: graphRAG 專案的核心架構與技術選型定案（monorepo 多專案 hybrid RAG + MCP）
metadata: 
  node_type: memory
  type: project
  originSessionId: 28bf50a0-6391-4a8c-823c-0c81abc2da4a
---

graphRAG（C:\graphRAG，GitHub: CLYEH/graphRAG，預設 branch main）是一套「多專案 hybrid RAG 平台」。

**願景**：共用核心引擎 + 每個專案獨立資料/設定，各自暴露一個 MCP server（Project A → MCP-A，可再加 B/C/D）。Pipeline：raw data → 清洗 → 建圖 → 索引 → 透過 MCP 提供查詢工具給 agent。

**定案（截至 2026-07-02）**：
- 語言：Python
- 骨幹框架：LlamaIndex（PropertyGraph 建圖、實體解析、text-to-SQL、向量、router、LLM/embedding 抽象已含 OpenAI+Claude）
- LLM：抽象層，至少支援 OpenAI + Claude（用 LlamaIndex 內建抽象，可能就不需另疊 LiteLLM）。**預設用 OpenAI**（抽取/推理預設 gpt-5.4-nano，可設定；embedding text-embedding-3-large），仍可切換 Claude
- 儲存：polyglot 三引擎 — Postgres（SQL/結構化）+ Qdrant（向量）+ Neo4j（圖）
- 架構：monorepo，一專案一資料夾一 MCP server process（隔離最清楚）
- 資料型態：結構化 + 非結構化混合
- 圖 schema：混合 ontology（核心 schema + 允許 LLM 提議新型別）
- 檢索：完整 hybrid — vector（語意）+ graph（多跳遍歷 + 全域摘要/community summary）+ SQL（結構化精確查詢）
- 實體解析：要做（結構化實體 ↔ 文件實體對齊）
- 開發策略：使用者選擇「一開始就做完整 hybrid」（我原建議分階段 MVP，被推翻，尊重其決定）
- 規模：未定，設計成可成長
- Source of truth：Postgres 為 SoR（canonical entity registry + 文件/chunks/結構化表）；Neo4j、Qdrant 為 pipeline 單向建置的衍生投影，共用同一 canonical UUID
- MCP 介面：分開工具（semantic_search / graph_query / global_summary / sql_query）+ 一個 hybrid_query router
- **兩個門面共用 core**：(1) agent → 每專案 MCP server；(2) 人 → Web Console。前後端邊界 = OpenAPI 契約（FastAPI 產出 → 前端 codegen typed client），可平行開發
- Web Console 後端：Python / FastAPI；非同步任務用正式佇列 arq（或 Celery）+ Redis
- Web Console 前端：React + Vite + TypeScript；範圍含 匯入/清洗/檢視 + 實體解析審核 + 互動圖譜探索(react-force-graph/Cytoscape) + 查詢測試 playground + Pipeline 狀態儀表板
- 基礎設施：docker-compose = Postgres + Qdrant + Neo4j + Redis

**規格文件**：C:\graphRAG\docs\DESIGN.md，目前為 **v0.5（實作凍結版）**。含治理層 + 決策紀錄 (ADR) §26 + 實作契約凍結 §27。

**v0.5 新增（ChatGPT 三輪 review 收尾）**：
- DR-006 Active build 強制注入：Postgres partial unique index 保證唯一 active build；所有 store 存取經 build-scoped repository 自動注入 build_id，禁裸 client
- DR-007 fingerprint_version：ledger 鍵帶版本，正規化/ontology 變更升版觸發 migration/重審，不靜默誤套
- §27 實作契約凍結：error/warning code enum、SSE event schema、idempotency 表/TTL/衝突、source_refs 依 result_type 最低要求、fingerprint 精確定義 + precedence、relation_evidence quote 上限/hash/prune 存活、eval 補 path_validity/relation_hit_rate/groundedness、Cypher 改參數化模板優先（sqlglot 驗 SQL）
- ChatGPT 判定：v0.5 為開工前最後一輪，之後即可進實作（Track 0 契約凍結先行）

**v0.4 決策紀錄（經 ChatGPT 二輪 review + Claude 判斷）**：
- DR-001 Build activation 一致性：三庫皆標 build_id 並共存多版本，active 唯一真相在 Postgres builds.status，activation=單一 Postgres transaction（天生原子），查詢啟動讀一次 active build_id、三庫照它過濾。取代 v0.3 的 alias/multi-db 切換
- DR-002 契約凍結先行：openapi.yaml + mcp_response.schema.json 為版本化交付物，Track 0 先凍結才開工
- DR-003 審核跨 build 延續：決策存非 build-scoped review_ledger，鍵為穩定 fingerprint（entity_key/relation_signature/merge_key），reject 者排除出投影，避免每次 build 重審
- DR-004 Neo4j 單庫 + build_id property 過濾（Community 相容），不用 multi-database
- DR-005 佇列 arq（非 Celery）
- ChatGPT 精修已納入：MCP response 加 schema_version/typed warnings/權限化 debug/source_refs 必填/排序；relation_evidence 加 chunk_id/offsets/evidence_hash；query guardrail 改 AST 白名單+唯讀角色+row limit、Cypher 禁 CALL/APOC；Console v1 只做 health/jobs/review/playground

**v0.3 新增定案**：
- 任務佇列 arq（定案，非 Celery）
- 採納 build versioning：所有核心表帶 build_id；builds 表；activate=原子切換（Qdrant collection alias + Neo4j multi-database，Postgres 記 active 指標）；rollback=activate 舊版
- observability：pipeline_runs/steps/step_items 三層；item 級預設只記 failed/skipped（verbosity 可調），避免寫入放大
- entities/relations 加 status/review_status/created_by；relation_evidence 獨立表；merge_candidates 加 snapshot/decision 欄位
- 審核 build-scoped，可帶入下個 build

**v0.3 由來**：使用者把 v0.2 丟給 ChatGPT 做 review，Claude 對其建議做「第二意見」後採納高價值項目寫成 v0.3。

**待決（見 DESIGN §25 分級）**：Neo4j 投影隔離最終做法、OpenAPI error code 列舉、split 是否允許、多人 auth、guardrail 實測、eval 門檻、ontology 採納政策、ER 門檻、embedding、Leiden 層級、chunking。

**環境/Harness（已建置 2026-07-02，`uv run poe check-all` 全綠）**：uv+ruff+mypy(strict)+pytest+poe（後端）；React19/Vite8/TS6+oxlint+prettier+vitest（web/）；docker-compose（pg/neo4j/qdrant/redis）；GitHub Actions CI；CLAUDE.md/AGENTS.md 護欄、TASKS.md 佇列、docs/LOOP.md 迴圈協定。**DoD = `uv run poe check-all`**（快 gate）。web-check 用 poe shell 任務（Windows npm=npm.cmd，不能用 poe cmd）。
- **測試分層**：pytest marker（integration[需服務,自動 skip]／contract[schema 驗證,合約未凍前 skip]／eval／e2e／slow）；`poe test`=快(單元+contract)、`test-int`、`test-cov`(fail-under 85)、`check-full`=check-all+整合；前端 vitest(元件,只收 src)+Playwright(e2e,`npm run test:e2e`,不在 fast gate)。原則：service/browser 測試不進 fast loop，用 marker 分流。
- `.claude/settings.json` 權限 allowlist（uv/npm/docker compose/git 免提示）已由使用者授權套用。
- **Loop 流程（8 步，雙 review 閘門，一任務=一分支=一 PR）**：取任務+開 task/<id> 分支 → 界定 → 實作 → 驗證(本地 check) → **code-reviewer subagent**(`.claude/agents/code-reviewer.md`，FAIL 退回步驟3) → commit+push+`gh pr create` → **等 GitHub 硬門檻:CI 綠 + 綁定 Codex review 通過**(任一失敗退回步驟3) → 合併+打勾。DoD = 四道閘門(本地 check → agent review → CI → Codex)。
- **GitHub 硬門檻（已設 2026-07-02）**：scaffold 已 push 到 main（commit 81aae65 + CI badge PR #1）；`main` branch protection 開啟：enforce_admins、required status checks = backend/frontend/integration（strict）、要 PR 才能併、禁 force-push/刪除、linear history、0 human approvals。CI(.github/workflows/ci.yml) 三 job 皆綠；流程已用 PR #1 驗證（CI 綠→squash 合併）。**Codex 已裝並綁定**（bot: `chatgpt-codex-connector[bot]`）。PR #1 上 Codex 反應 👀→👍(+1)=通過。**重點：Codex 用 reaction/留言表態，不發 status check、也不發正式 review**（實測 pulls/1/reviews 空、commits/check-runs 只有 github-actions 三個）。→ GitHub branch protection 無法直接把 Codex 綁成 required check。**Codex 硬門檻定案 = A（loop-enforced + required_conversation_resolution，已在 main 開啟）**。
**Codex PR 反應狀態機（使用者實測）**：發 PR 後只會發生——(1) 👀 eye=審核中→等；(2) 👍 +1=已審完無意見→PASS；(3) 無反應：3-1 且無其他留言=Codex 還沒看到→`@codex review` 催它；3-2 有 Codex 新留言=有意見→退回步驟3。未解決的 Codex 留言由 GitHub required_conversation_resolution 硬擋合併。輪詢：`gh api repos/CLYEH/graphRAG/issues/<pr>/reactions`（找 chatgpt-codex-connector[bot] 的 content=+1）。
- **Codex 判斷改良（PR #2 實作進 LOOP.md）**：verdict 依「**未解決 review thread**」（GraphQL `reviewThreads` isResolved=false）而非留言 count——已解決/歷史留言仍會被 list endpoint 回傳，用 count 會永遠卡在 changes-wanted。**等待態（eyes / 尚未看到）不是失敗**，只有未解決 thread 或新變更請求才退回步驟3。留言要同時看 inline(`/pulls/<pr>/comments`) 與頂層(`/issues/<pr>/comments`)。Codex 不一定 PR open 就自動審（PR #2 需 `@codex review` 催才動）。解決 thread 用 GraphQL `resolveReviewThread` 才能過 required_conversation_resolution。實測：Codex 審「如何審查」的 meta 文件會連環找 nit，收斂靠「硬門檻滿足即合併」而非等到永遠沒意見。
- 下一步：Track 0 契約凍結（contracts/openapi.yaml + mcp_response.schema.json）。

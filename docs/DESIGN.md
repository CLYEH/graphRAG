# graphRAG — 設計規格 (DESIGN.md)

> 狀態：**v0.5（實作凍結版 / implementation-freeze）** · 最後更新 2026-07-03
> 🔧 = 預設值/可調整，🟡 = 待決事項。開發模式：agent 驅動 100% 開發，規格以「完整、可維運、可驗收」為目標。
> v0.5 依 ChatGPT 三輪 review 收尾「實作契約凍結」：active build 強制注入、契約凍到 enum 級、fingerprint 語意 + 版本化、eval 補圖檢索指標、Cypher 驗證策略。細節見 **§27**，決策見 §26。

## 目錄
1 願景 · 2 架構 · 3 技術堆疊 · 4 資料模型 · 5 Pipeline · 6 混合 Schema · 7 實體解析 · 8 檢索層 · 9 MCP · 10 Web Console · 11 部署 · 12 Repo · 13 工作分解
**治理層**：14 Build 版本/Activation · 15 OpenAPI 契約 · 16 MCP Response Contract · 17 審核工作流 + 跨 build 延續 · 18 可觀測性 · 19 品質/Health · 20 評估 · 21 查詢安全 · 22 失敗/降級 · 23 角色權限 · 24 驗收準則 · 25 待決 · 26 決策紀錄 (ADR) · **27 實作契約凍結**

---

## 1. 願景與範圍
多專案 hybrid RAG 平台。共用一個核心引擎，每專案放自己的資料/設定，各自建知識圖與索引。兩個門面共用 `core/`：**agent → MCP server（每專案一個）**；**人 → Web Console（匯入/清洗/檢視/審核/探索/測試/儀表板）**。
```
raw data → 清洗 → 建圖(混合 schema + 實體解析) → 索引(三庫) → 檢索 ├─▶ MCP (agent)  └─▶ Web Console (人)
```
範圍內：混合資料攝取、混合 ontology、實體解析、完整 hybrid 檢索、每專案 MCP、Web Console、CLI、Docker、build 版本、審核工作流、可觀測性、評估、查詢安全、角色權限。範圍外：跨組織多租戶、線上即時串流寫入。

## 2. 系統架構總覽
```
 projects/<name>/            ┌─────────────── core/ (共用引擎) ───────────────┐
   config.yaml  ────────────▶│  ingest → clean → graph → resolve → index →     │
   sources/     ────────────▶│  query        +  三庫 (Postgres/Qdrant/Neo4j)   │
                             └───────┬──────────────────────────────┬──────────┘
                   ┌─────────────────▼─────────────┐        ┌───────▼──────────────┐
        人(操作) → │ Console 後端 (FastAPI)          │        │ MCP server (每專案)   │ ← agent
                   │  api/ + 佇列 (arq+Redis)        │        │ core/mcp             │
                   └─────────────────┬─────────────┘        └──────────────────────┘
                                     │  ← OpenAPI 契約（前後端邊界）
                   ┌─────────────────▼─────────────┐
        人(操作) → │ 前端 SPA (React/Vite/TS) web/   │
                   └───────────────────────────────┘
```
**四個原則**：
1. **Postgres = SoR**；Neo4j / Qdrant 為衍生投影，攜帶相同 `canonical_id` + `build_id`。
2. **前後端邊界 = OpenAPI 契約**，前後端可平行開發。
3. **一切以 build 為版本單位**：三庫資料皆標 `build_id`，查詢只讀 active build（§14）。
4. **契約先凍結後開工**（§15、§16、§26 DR-002）。

## 3. 技術堆疊
| 層 | 選擇 | 理由 |
|---|---|---|
| 語言（後端/核心） | Python | 生態最完整 |
| 骨幹框架 | **LlamaIndex** | PropertyGraphIndex、text-to-SQL、向量、router、LLM/embedding 抽象 |
| LLM 抽象 | LlamaIndex 內建 | OpenAI + Claude 切換 |
| SQL / 結構化 / SoR | **Postgres** | 成熟、text-to-SQL 友善、activation 單一真相 |
| Schema migrations | **Alembic + SQLAlchemy (core)** | autogenerate、asyncpg 相容、生態標準（DR-008） |
| 向量 | **Qdrant** | 效能、payload filter（以 build_id 過濾） |
| 圖 | **Neo4j** | Cypher、演算法；**單一 DB + build_id property 過濾（Community 相容，不用 multi-db）** |
| Console 後端 | **FastAPI** | async、自動 OpenAPI |
| 任務佇列 | **arq + Redis** | 定案（非 Celery） |
| 前端 | **React + Vite + TypeScript** | 生態、圖視覺化成熟 |
| 圖視覺化 | 🔧 react-force-graph / Cytoscape.js | 互動探索 |
| 部署 | Docker Compose | PG + Qdrant + Neo4j + Redis + api + worker |

LLM 預設 🔧：抽取/推理 **OpenAI**（預設 🔧 `gpt-5.4-nano`，可設定）；embedding OpenAI `text-embedding-3-large`；經抽象層可切換（仍支援 Claude）。

## 4. 資料模型（Postgres = SoR）
每專案一個 schema。**所有核心物件帶 `build_id` + 狀態欄；跨 build 穩定身分用 fingerprint（見 §17）。**
```sql
builds(id uuid pk, project text, status text,          -- building|ready|active|failed|archived
       config_hash text, source_hash text,
       started_at, finished_at, activated_at timestamptz, metrics jsonb, eval jsonb)

documents(id uuid pk, project text, build_id uuid, source_uri text, raw text,
          content_hash text, mime text, metadata jsonb, status text, ingested_at timestamptz)
chunks(id uuid pk, document_id uuid, build_id uuid, ordinal int, text text,
       token_count int, start_offset int, end_offset int,
       vector_point_id uuid, metadata jsonb, status text)

entities(id uuid pk, project text, build_id uuid, type text, canonical_name text,
         entity_key text,      -- 跨 build 穩定身分：fpv{N}(norm(type)|norm(name)|disambiguator)，見 §27.3
         attributes jsonb, embedding_point_id uuid,
         status text,          -- active|deprecated|merged|rejected|needs_review
         review_status text,   -- unreviewed|approved|rejected
         created_by text,      -- rule|llm|manual
         created_at, updated_at timestamptz)
entity_mentions(id uuid pk, entity_id uuid, source_kind text,  -- structured|text
                source_ref text, surface_form text, confidence real)

relations(id uuid pk, project text, build_id uuid, src_entity_id uuid, dst_entity_id uuid,
          type text, attributes jsonb, relation_signature text,  -- fpv{N}(src_key|norm(type)|dst_key)，見 §27.3
          status text, review_status text, created_by text, confidence real,
          created_at, updated_at timestamptz)
relation_evidence(id uuid pk, relation_id uuid, build_id uuid, evidence_type text, -- chunk|row|manual
                  evidence_ref text, chunk_id uuid, start_offset int, end_offset int,
                  quote text, source_uri text,  -- 反正規化出處，§27.4 prune 存活（P1 contract 同欄位）
                  evidence_hash text, confidence real, created_at timestamptz)

community_reports(id uuid pk, project text, build_id uuid, level int,
                  title text, summary text, member_entity_ids uuid[], rating real)

-- 審核（§17）
merge_candidates(id uuid pk, project text, build_id uuid, left_entity_id uuid, right_entity_id uuid,
                 score real, features jsonb, status text,   -- pending|approved|rejected|deferred
                 decision text, decided_by text, decided_at timestamptz, reason text,
                 impact jsonb, left_snapshot jsonb, right_snapshot jsonb)
-- 跨 build 延續：非 build-scoped，以 fingerprint 為鍵（§17 / DR-003）
review_ledger(id uuid pk, project text, target_kind text,   -- entity|relation|merge
              target_key text,                              -- entity_key / relation_signature / merge_key
              fingerprint_version int,                      -- 鍵的鑄造版本（§27.3 / DR-007，僅套用同版）
              decision text,                                -- approve|reject|defer|merge|split
              decided_by text, decided_at timestamptz, reason text)

-- 可觀測性（§18）
pipeline_runs(id uuid pk, project text, build_id uuid, kind text, status text,
              config_hash text, source_hash text, created_by text,
              started_at, finished_at timestamptz, metrics jsonb, error jsonb)
pipeline_steps(id uuid pk, run_id uuid, step_name text, status text, started_at, finished_at timestamptz,
               input_count int, output_count int, skipped_count int, failed_count int, metrics jsonb, error jsonb)
-- §6 待審池（C3c）：非 build-scoped 審核工件,穩定鍵 proposal_key = fpv{N}(norm(kind)|norm(type_name))（§27.3 慣例、DR-007 版本化）
-- unique (project, proposal_key)：跨 build 再提案=upsert no-op,rejected 不重開審;決策欄位 IFF 已決(雙向 CHECK)
ontology_proposals(id uuid pk, project text, kind text,        -- entity|relation
                   type_name text, proposal_key text, fingerprint_version int,
                   example text, chunk_ref text,
                   status text,   -- proposed|accepted|rejected（§17）
                   decided_by text, decided_at timestamptz, reason text, created_at timestamptz)

pipeline_step_items(id uuid pk, step_id uuid, item_kind text, item_ref text,  -- item_ref 穩定：content_hash / entity_key
                    status text, message text, error jsonb)  -- 預設只記 failed/skipped（§18 verbosity）
```
**投影規則（皆 build_id 標記，不做跨庫切換）**：
- **Neo4j（單一 DB）**：`(:Entity {canonical_id, build_id, project, type, status,...})`、`[:REL {build_id, type,...}]`；查詢一律 `WHERE n.build_id = $active`。
- **Qdrant（每專案一 collection）**：point payload `{project, build_id, canonical_id, type, text, chunk_id|entity_id}`；查詢 filter `build_id = $active`。

## 5. Pipeline
`1 ingest → 2 clean(切塊🔧) → 3 graph(混合 ontology 抽取) → 4 resolve(實體解析 + 套用 review_ledger) → 5 index(embedding→Qdrant；投影→Neo4j) → 6 summarize(Leiden🔧→ community_reports)`
每次 build 開 `builds` + `pipeline_runs`；每步 `pipeline_steps`；失敗/跳過項記 `pipeline_step_items`。以 `config_hash`+`source_hash`+`content_hash` 判斷可跳過/只重跑失敗項（冪等）。build 完成且 eval 通過 → `ready` →（activate）→ `active`（§14）。

## 6. 混合 Schema / Ontology
每專案 config 定核心 schema。結構化資料規則對映（規則 → entities/mentions/relations/evidence，決定性）；文件用 PropertyGraph 受 schema 引導抽取，允許 LLM 提議新型別 → 待審池，Console 決定採納（🔧 `ontology.proposal_policy: auto|review`）。**mentions 由抽取階段產生**（每個出現的 surface_form/來源只有抽取看得到）；§7 的 resolve 是將既有 mentions 重新指向合併後的 canonical，非另行建立。文件抽取走 §3 的 LlamaIndex `LLM` 抽象 + **自訂 span-capturing prompt/parse，不用現成 PropertyGraphIndex 抽取器**——§27.4 要求 chunk 證據帶逐字 quote + start/end offsets，現成 triplet 抽取器不產 span,無法滿足凍結的溯源最低要求。

## 7. 實體解析
`blocking(type+正規化名) → similarity(字串+embedding 加權) → 高信心自動合併 / 中信心產 merge_candidate / 低信心不合併 → 寫 entities(canonical, entity_key) + entity_mentions`。canonical_id 為三庫共用鍵；`entity_key` 為跨 build 穩定身分。resolve 時先套 `review_ledger`（§17）。🟡 門檻 `auto_merge_threshold`/`review_threshold`。

## 8. 檢索層
| 模態 | 引擎 | 實作 |
|---|---|---|
| semantic | Qdrant | kNN + payload filter（build_id=active） |
| graph | Neo4j | 多跳/路徑；NL→Cypher（§21 guardrail） |
| sql | Postgres | NLSQLTableQueryEngine（唯讀角色、白名單、§21） |
| global | Postgres | community_reports |
| hybrid | router | RouterQueryEngine 選擇+融合，產 routing trace（§16） |

## 9. MCP 介面（每專案一 server）
工具：`semantic_search` · `graph_query` · `global_summary` · `sql_query` · `hybrid_query`（預設入口）· `get_entity` · `list_schema` · `explain_retrieval`。所有 retrieval 工具回傳統一 contract（§16），查詢綁定 active build。Transport 🔧 stdio/http。

## 10. Web Console
### 10.1 後端 API（FastAPI，`api/`）
薄層包 `core/`，不含業務邏輯。REST+JSON、OpenAPI 3；長操作回 `job_id`，狀態經 SSE。端點見 §15。任務由 arq worker 執行。
### 10.2 前端 SPA（React/Vite/TS，`web/`）
OpenAPI codegen typed client。頁面：Project Health 首頁(§19)、匯入、清洗(含抽樣預覽)、檢視(文件/chunks)、圖譜互動探索(三欄：左 search/filter、中 viz、右 節點/邊詳情+evidence+actions；點邊顯示 type/confidence/evidence/來源/created_by/review_status)、實體解析審核(批次操作+impact 排序)、查詢 playground(結果+source_refs+routing trace)、Pipeline 儀表板(build/run/step 進度+失敗 drill-down)。
> **實作順序**（DR & ChatGPT 建議）：Console v1 只做 **health / jobs / review / query playground**；完整圖譜治理 UI 留待 v2，避免範圍膨脹。

## 11. 部署
`docker-compose.yml`：postgres、neo4j、qdrant、redis、api、worker。前端開發 Vite、部署靜態檔。密鑰走 `.env`。

## 12. Repo 佈局
```
graphRAG/
├── core/  llm/ stores/ ingest/ clean/ graph/ resolve/ index/ query/ mcp/ builds/ eval/ observability/ contracts/
├── api/   routers/ schemas/ workers/ deps.py main.py openapi/
├── web/   src/pages/ src/components/ src/api/(codegen)
├── projects/<name>/  config.yaml sources/ mcp_entrypoint.py
├── cli/   graphrag
├── contracts/  openapi.yaml  mcp_response.schema.json  golden.schema.json  query_policy.schema.json   ← 契約凍結交付物 (§15/§16/§20/§21, DR-002)
├── docs/DESIGN.md   docker-compose.yml   pyproject.toml
```
依賴方向：`api`/`cli`/`projects/*/mcp` → `core`；`web` → `api` 契約；`core` 不反向依賴門面。

## 13. 工作分解（四軌，契約先行）
**Track 0 — 契約與治理（最先，凍結後才開工，DR-002）**：P0 OpenAPI 規範 · P1 MCP response contract · P2 Build/activation model · P3 審核狀態機 + carry-forward · P4 Eval contract · P5 Query safety policy · P6 可觀測性 schema。（編號以 `TASKS.md` 佇列為準）
**Track 1 — 核心引擎**：C0 骨架 → C1 儲存&SoR&投影(build_id) → (C2 攝取清洗 ‖ C3 建圖) → C4 實體解析+ledger → C5 索引 → C6 檢索 → C7 全域摘要 → C8 MCP → C9 builds/activate/rollback → C10 eval → C11 observability。
**Track 2 — Console 後端**：BA0 骨架+契約 → BA1 專案/來源 → BA2 arq/jobs/SSE → BA3 檢視 → BA4 清洗 → BA5 審核 → BA6 playground → BA7 health → BA8 builds/activate。
**Track 3 — Console 前端**：FE0 骨架+codegen → FE7 health → FE8 儀表板 → FE5 審核 → FE6 playground →（v2）FE1 匯入 → FE2 清洗 → FE3 檢視 → FE4 圖譜探索。
同步點：Track 0 完成後三軌大幅平行；`web` 只需 BA0 契約即可啟動。

---
# 治理層

## 14. Build 版本 / Activation / Rollback（DR-001）
**模型**：一專案多個 `builds`，至多一個 `active`。**三庫皆保留多個 build 的資料，以 `build_id` 區隔；不做跨庫切換。**
**Activation = 單一 Postgres transaction**：舊 active→archived、新→active、寫 `activated_at`。因為是單庫單 tx，**天生原子**；無需跨 Qdrant/Neo4j 的分散式提交。
**Preflight（activate 前檢查）**：build 為 `ready`、eval ≥ 門檻（§20）、三庫 build_id 計數一致（無 drift，§19）。任一不過則拒絕 activate。
**查詢一致性**：MCP/Console 查詢**啟動時讀一次** `builds` 的 active `build_id`，三庫全部以該 build_id 過濾 → 即使正在 activate 也不會讀到半套（讀到的是切換前或切換後的完整一致版本）。
**Rollback**：activate 一個舊的 `ready/archived` build（同一單 tx 操作）。即時、原子。
**GC**：保留最近 N 個 build（🔧 `retention.keep_builds`）；`graphrag prune` 依 build_id 從三庫刪除逾期資料。
**CLI/API**：`graphrag builds|build|activate|rollback|diff|prune <project>`；對應 REST 見 §15。

## 15. OpenAPI 契約規範（凍結交付物，DR-002）
**Track 0 P0 先產** `contracts/openapi.yaml` + 慣例 + examples，凍結後才開工。
- **成功 envelope**：`{ "data": <payload>, "meta": { "request_id", "build_id", "elapsed_ms" } }`
- **Error**：`{ "error": { "code": <ENUM>, "message", "details", "request_id" } }`；`code` 為凍結列舉（🟡 完整表）。
- **Pagination**：`?limit=&cursor=` → `meta.next_cursor`（cursor-based）。
- **Sorting/Filtering**：`?sort=field:asc&filter[field]=value`。
- **Job**：長操作 `202 {data:{job_id,status}}`；`GET /jobs/{id}/events`(SSE) 事件 `{status, progress, step, message, ts}`。
- **Idempotency**：寫入端點吃 `Idempotency-Key` header，避免重試重複觸發 build/ingest。
- **Auth 佔位**：所有端點 `Depends(auth)`（§23）。
**端點（節錄）**：`/projects[/{p}]` CRUD · `/projects/{p}/sources|ingest|build` · `/projects/{p}/builds[/{b}/activate|rollback]` · `/jobs/{id}[/cancel|/events]` · `/projects/{p}/{documents|chunks|entities|relations|graph/subgraph}` · `/projects/{p}/merge-candidates[/{id}/approve|reject|defer]` · `/projects/{p}/query/{semantic|graph|sql|global|hybrid}` · `/projects/{p}/{health|metrics|eval}`。

## 16. MCP Response Contract（凍結交付物，DR-002）
`contracts/mcp_response.schema.json`，所有 retrieval 工具共用：
```json
{ "schema_version": "1.0", "query": "…", "tool": "hybrid_query", "project": "…", "build_id": "uuid",
  "results": [{ "result_type": "chunk|entity|relation|path|row|community_report",
    "id": "uuid", "title": "…", "text": "…", "score": 0.87, "confidence": 0.91,
    "source_refs": [{ "source_type": "document|chunk|entity|relation|row", "id":"uuid", "source_uri":"…", "metadata":{} }] }],
  "graph_context": { "nodes": [], "edges": [], "paths": [] },
  "warnings": [{ "code": "STORE_UNAVAILABLE", "message": "…" }],   // typed warnings
  "debug": { "stores_used": [], "retrieval_plan": [], "routing_decision": {}, "latency_ms": 0 } }
```
規則：**`source_refs` 必填**（`require_sources`）；**`results` 有明確排序**（score desc, tie-break id）；**`debug` 依 `query_policy.expose_debug` 與呼叫者權限決定是否輸出**；`schema_version` 破壞式變更才升版。router trace 放 `debug.routing_decision`（selected/skipped modes + reason + confidence）。

## 17. 審核工作流 + 跨 build 延續（DR-003）
**狀態**：entity/relation `needs_review → approved|rejected`（另 active|deprecated|merged）；merge_candidate `pending → approved|rejected|deferred`；ontology proposal `proposed → accepted|rejected`。
**跨 build 延續（關鍵）**：審核決策存進 **非 build-scoped 的 `review_ledger`**，鍵為**穩定 fingerprint**（entity=`entity_key`、relation=`relation_signature`、merge=`merge_key=sorted(left_key,right_key)`）。每次 build 的 resolve/index 步驟先套 ledger：
- `reject` 的 key → 從投影中**排除**（不進 production graph），避免同一錯誤每次重抽再審。
- `approve`/`merge` 的 key → 自動採納。
- `defer` → 仍列入待審。
🔧 `resolution.carry_review: true`。🟡 是否允許 `split`（拆分已合併實體）。

## 18. Pipeline 可觀測性
三層 runs/steps/items。`item_ref` 用**穩定鍵**（document=content_hash、entity=entity_key），確保重跑對得上。**只重跑失敗項**須冪等（依 item_ref 去重）。verbosity 🔧 `observability.item_logging: failures(預設)|sampled|all`；retention 🔧 `observability.item_retention_days`。Console 呈現「Build failed at graph · failed docs:3 · chunks:17 · reason:LLM schema validation · [retry failed only]」。

## 19. 品質報告與 Project Health
首頁狀態燈：`Healthy | Needs review | Build failed | Index drift | Eval regression`。指標：active/last-success/last-failed build、source/doc/chunk/entity/relation count、pending review、**projection drift**(PG vs Neo4j/Qdrant 依 build_id 計數對帳)、low-confidence relation、missing-evidence、eval 趨勢。`GET /projects/{p}/health`。

## 20. 評估框架
每專案 `eval/golden.yaml`（question, mode, expects{must_contain_entities, must_cite_sources, answer_regex}, min_score）。評分：entity/source recall、答案相似度、citation 覆蓋。（答案相似度需 golden 增補參考答案欄位——additive schema 演進——之前不發此指標；C10 runner 註記。）回歸門檻 🟡 `eval.regression_threshold`：新 build eval 低於 active 超門檻 → 阻擋 auto-activate、Health 顯示 `Eval regression`。`graphrag eval` / `GET /projects/{p}/eval`。

## 21. 查詢安全政策
`config.query_policy`：`default_mode, max_top_k, max_graph_hops, max_sql_rows, max_latency_ms, require_sources, expose_debug`。
- **text_to_sql**：`enabled, readonly`（**專用 DB 唯讀角色**）, `allowed_tables 白名單`, `blocked_keywords[insert,update,delete,drop,alter,truncate]`, `max_rows`, `timeout_ms`。**執行前 AST 解析驗證**（非僅字串比對）。
- **text_to_cypher**：`readonly`, `allowed_clauses[MATCH,WHERE,RETURN,LIMIT]`, `blocked[CREATE,MERGE,DELETE,SET,REMOVE,CALL]`，**禁 APOC / procedure 呼叫**, `timeout_ms`, `row/hops 上限`。（schema 化＝`contracts/query_policy.schema.json`：`enabled` 承 §27.6「NL→Cypher 選配」語意；row 上限＝`max_rows`；hops 上限＝頂層 `max_graph_hops` 單一來源，不在子塊重複；blocked 與 allowed 重疊時 blocked 優先。）
違規 → 拒絕並回 typed warning。

## 22. 失敗 / 降級行為
- 單一 store 不可用：hybrid 降級為可用模態子集，`warnings` 標示，不整體失敗。
- LLM 抽取失敗：該項記 `pipeline_step_items` failed，build 續跑；步驟 `failed_count > 閾值` 才中止。
- 投影 drift：Health 標示，`graphrag reproject <project> <build>` 重建投影。
- build 中途失敗：`failed`，active 不受影響。
- query 逾時：回部分結果 + warning，不 500。

## 23. 角色與權限
單一 principal（本機/單人），介面預留：所有 API `Depends(auth) → Principal{id, roles}`。角色草案 `viewer|curator|operator|admin`；動作→角色集中於 policy 表。接真實 auth 只換 `auth` 實作。🟡 多人 auth 方案。

## 24. 驗收準則
1. `graphrag build` 產 `ready` build，三庫 build_id 計數一致（無 drift）。
2. 五種查詢皆回符合 §16 contract 且含 source_refs。
3. Console v1（health/jobs/review/playground）可操作；審核 reject 後下個 build 不再出現該項。
4. `graphrag eval` ≥ 門檻；activate 後 MCP 一致。
5. rollback 可還原前一 build 且查詢一致。
6. 單一 store 故障時 hybrid 正確降級。

## 25. 待決事項（分級）
**Blocking（實作前必凍結）**：active build 強制注入機制 · error/warning code enum · SSE event schema · idempotency 語意（表/TTL/衝突）· review fingerprint/ledger 語意 + fingerprint_version · relation_evidence 保存規則 · Cypher 驗證策略 · jobs build_id 邊界。→ **已於 §27 凍結**。
**Blocking（正式使用前）**：是否允許 split · Console 多人 auth · guardrail 對抗測試 corpus · eval 回歸門檻數值 · ontology 採納政策門檻。
**Tunable**：ER 相似度權重/門檻 · embedding 模型（雲 vs 本地）· Leiden 層級 · chunking 策略 · observability verbosity/retention · build retention 數。

## 26. 決策紀錄 (ADR)
**DR-001 Build Activation 一致性**：三庫皆以 `build_id` 標記並共存多版本；**active build 的唯一真相在 Postgres `builds.status`**；activation = 單一 Postgres transaction（天生原子）；查詢啟動讀一次 active build_id，三庫照它過濾；rollback 同理。→ 取代 v0.3 的 alias/multi-db 切換，消除跨庫原子性問題。
**DR-002 契約凍結先行**：`openapi.yaml`、`mcp_response.schema.json`、`golden.schema.json` 與 `query_policy.schema.json` 為版本化交付物，Track 0 先凍結，core/api/web/agent 共用；之後才平行開工。
**DR-003 審核跨 build 延續**：審核決策存非 build-scoped `review_ledger`，鍵為穩定 fingerprint（entity_key/relation_signature/merge_key）；每次 build 套用，reject 者排除出投影 → 不重複重審。
**DR-004 Neo4j 單庫過濾**：採單一 Neo4j database + `build_id` property 過濾（Community 相容、輕量），不用 multi-database。
**DR-005 佇列 arq**：採 arq + Redis（async-native），非 Celery。
**DR-006 Active Build 強制注入**：唯一 active build 以 Postgres partial unique index 保證；所有 store 存取一律經 build-scoped repository 層自動注入 `build_id`，query/MCP 層不得直接拿裸 client → 結構上杜絕「忘了帶 build_id 混到舊版」。
**DR-007 Fingerprint 版本化**：review ledger 的 entity_key/relation_signature/merge_key 帶 `fingerprint_version`；正規化或 ontology 規則變更即升版，升版觸發 migration 或標記重審，不得靜默誤套。
**DR-008 Migration 工具 = Alembic + SQLAlchemy (core)**：Postgres schema 變更一律以 Alembic 管理（表以 SQLAlchemy core 定義、autogenerate 產生 migration、asyncpg 相容）；migration scripts 為版本化交付物，自 P2（`builds` 表 + partial unique index）起隨各任務落地。

---

## 27. 實作契約凍結（開工前凍結項；ChatGPT 三輪 review 收斂）

### 27.1 Active Build Enforcement（DR-006）
- **唯一 active build**：`CREATE UNIQUE INDEX one_active_build ON builds(project) WHERE status='active';`
- **Active lookup**：`active_build(project)` 單查詢，每 request 讀一次並快取。
- **Repository 層強制注入**：定義 `BuildScopedRepo`，所有讀取自動加 `build_id=active`（Postgres `WHERE`、Neo4j `WHERE n.build_id`、Qdrant filter）。query/MCP/Console 層**不得**直接存取裸 store client；寫入一律指定 building 的 build_id。以型別/介面（adapter 需 build_id 參數）+ 測試強制。

### 27.2 契約凍結（enum 級；contracts/ 交付物）
- **error code enum**（additive-only）：`PROJECT_NOT_FOUND, BUILD_NOT_FOUND, BUILD_NOT_READY, NO_ACTIVE_BUILD, VALIDATION_ERROR, JOB_NOT_FOUND, JOB_CONFLICT, IDEMPOTENCY_CONFLICT, QUERY_UNSAFE, QUERY_TIMEOUT, STORE_UNAVAILABLE, RATE_LIMITED, INTERNAL`。
- **warning code enum**：`STORE_UNAVAILABLE, MODE_SKIPPED, PARTIAL_RESULTS, LOW_CONFIDENCE, GUARDRAIL_BLOCKED, TRUNCATED`。
- **SSE event**：`event: job.update|job.done|job.failed`；`data: {job_id, status, step, progress(0..1), message, ts}`。
- **Idempotency**：`idempotency_keys(key pk, project, endpoint, request_hash, response jsonb, status, created_at, expires_at)`；TTL 🔧 24h；同 key+同 request_hash → 回存檔回應；同 key+不同 request_hash → `409 IDEMPOTENCY_CONFLICT`。
- **source_refs 最低要求**（`require_sources` 強制，依 result_type）：chunk→≥1 chunk ref（source_uri+offsets）；entity→≥1 mention（chunk/row）；relation→≥1 relation_evidence；path→每條 edge 皆有 ref；row→table+pk；community_report→member entity refs。

### 27.3 Review Fingerprint & Ledger 語意（DR-003/007）
- `entity_key = fpv{N}( norm(type) | norm(canonical_name) | disambiguator )`（disambiguator＝有穩定外部 id 時採用，**僅去頭尾空白、保留大小寫** — 外部 id 可能區分大小寫；**去空白後為空＝視同未提供**，None／空字串／純空白鑄出同一把鍵）。
- `relation_signature = fpv{N}( src_entity_key | norm(type) | dst_entity_key )`；`merge_key = fpv{N}( sorted(left_key, right_key) )`。
- `norm` 凍結定義＝NFKC → casefold → 摺疊連續空白；hash 輸入以長度前綴編碼組合（杜絕分隔符歧義）。實作＝`core/resolve/fingerprints.py`，與本節逐字對應；任何變更即升 `fingerprint_version`。
- **fingerprint_version**：隨正規化/ontology 變更升版；ledger 僅套用同版（或經 migration 對映）之鍵，否則標記**重審**而非誤套。
- **precedence**：同鍵多筆以 `decided_at` 最新為準；manual(curator) 優先於 auto。
- **套用**：resolve/index 時 reject→排除出投影；approve/merge→採納；defer→留待審。
- **ER 變動**：已審實體之後被 split/merge 導致 fingerprint 改變 → 該 ledger 條目失效並標記重審（不盲目 carry）。

### 27.4 relation_evidence 保存
- `quote` 長度上限 🔧 512 字（存摘句非整段 chunk）。
- `evidence_hash = sha256( relation_signature | evidence_ref | norm(quote) )`（去重 + 穩定身分）。
- **prune 存活**：evidence 反正規化保留 `quote/offsets/source_uri`，即使舊 chunk 被 prune 仍保有出處。
- **offsets 語意（依 evidence_type 分流）**：`chunk` 證據**必有** `start/end offsets`（抽取 span 已知）；`manual`（在 MCP contract 中以 document ref 表示，SourceRefType 無 `manual`）為**刻意無 span** — 人工/文件層級引用只保 `quote + source_uri`；`row` 證據以 `table + pk` 溯源。§16 contract 據此分流強制。

### 27.5 Eval 指標補強（延伸 §20）
新增 GraphRAG 專屬：`path_validity`（回傳路徑為圖上真實路徑）、`relation_hit_rate`（期望關係命中率）、`groundedness`（答案主張是否有引用來源支撐）。`golden.yaml` 的 `expects` 增 `must_include_relations / must_have_valid_paths / groundedness_min`。

### 27.6 查詢 guardrail 策略
- **SQL**：以 `sqlglot` 解析（唯讀角色、單一語句、表白名單、禁 DDL/DML），解析失敗即拒。
- **Cypher**：MCP 圖工具**預設用參數化 query 模板**（neighbors/path/subgraph），不開放自由 NL→Cypher；NL→Cypher 為選配、需經 Cypher parser 限制在允許 clause、禁 `CALL/APOC/寫入`。維護對抗查詢 **test corpus**（§25）。

### 27.7 jobs 邊界
- `pipeline_runs.build_id`：ingest 一律掛在 building 的 build（故 build_id 有值）；純來源驗證 job 可為 null。
- **只重跑失敗項**：輸入＝前次 run 的 failed `item_ref` 集合；輸出邊界＝僅這些項重processed 併回同一 build_id；以 item_ref 去重確保冪等。

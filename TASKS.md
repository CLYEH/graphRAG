# TASKS — graphRAG build queue

The loop consumes the **top unchecked** item (respecting dependencies). One task per
iteration; definition of done = `uv run poe check-all` green, then commit. Protocol:
[`docs/LOOP.md`](docs/LOOP.md). Design: [`docs/DESIGN.md`](docs/DESIGN.md).

Check an item off `[x]` inside its own task PR — the checkoff lands when that PR merges.
Keep items small enough to finish in one loop.

---

## Harness
- [x] Repo skeleton (`core/ api/ cli/ web/`), `pyproject.toml`, uv env
- [x] Quality gates: ruff + mypy(strict) + pytest + poe; `poe check-all` (backend + frontend)
- [x] Frontend scaffold (React/Vite/TS) + oxlint/prettier/vitest
- [x] `docker-compose.yml` (postgres/neo4j/qdrant/redis)
- [x] Test tiers: unit/contract/integration/eval/e2e markers, `test-cov` (85%), conftest service-gating, Playwright scaffold
- [x] CI (`.github/workflows/ci.yml`: backend + coverage + integration + frontend), CLAUDE.md/AGENTS.md, `.env.example`
- [x] H1 harness fixes(CI integration gate、doc-drift、gate-wait、reviewer model、LLM 預設、allowlist、DR-008)
- [x] H2 LOOP.md: Codex suggestion triage rules in step 7 (must-fix vs reply-and-resolve criteria, checkable rationale required for every resolve-without-change, same-class sweep per round; `+1` gate unchanged) + entry points aligned (CLAUDE.md gate 4, /loop prompt, memory)
- [x] H3 harness enforcement & efficiency(watcher、doc-only fast lane、CPU push gates、CI governance job)
- [x] H4 property-based boundary tests: add `hypothesis` (dev dep) + property tests for frozen numeric/boundary rules (`is_eval_regression` first; P5 guardrail limits and C10 scoring as they land) — retro of PR #12's float-boundary must-fix (lesson class 8: 邊界語意 × 表示誤差)
- [x] H5 per-branch review receipts: key `.claude/receipts/review` by branch (or one receipt file per tree hash) so parallel `task/*` + `docs/*` work stops overwriting each other's PASS stamps (push gate fail-closes correctly today but forces a reviewer re-stamp round-trip every time)
- [x] H8 watcher poke→watch race fix + quota probe loop: `watch-codex.sh` bootstrap scans BACKWARD at startup (supersession rule: the latest quota message governs unless a newer +1/review/poke exists) so a limits reply landing seconds after a poke
- [x] H7 quota-aware Codex watcher: `watch-codex.sh` exit `30` when the only fresh bot response is the "reached your Codex usage limits" comment (checked before the exit-10 triage verdict)
- [x] H6 migration `0005`: `pipeline_step_items.item_ref <> ''` CHECK (+ `item_kind <> ''`)
- [x] H9 loop-throughput fixes (3 bottlenecks; all `*.md` → doc fast lane): the dominant cost is the external Codex round-trip, so (1) **cut rounds**
- [x] H13 `watch-codex.sh --anchor <ts>`(2026-07-13,#73 retro;id H12 已退役)
- [x] H11 integration tests must survive a LIVED-IN dev environment (2026-07-13 incident, owner-directed follow-up): the suite assumes pristine services and breaks on real dev-machine state
- [x] H14–H19 harness/loop review 落地(source: `.discuss/harness-loop-review.md`,owner 2026-07-19 排入並優先;C 節=已判定不動的邊界勿重議,D 節=可選實驗待 owner)— 六項依 review 優先序如下(H14–H19 全數 merge,umbrella 隨 H-series retro 收攏)。
- [x] H14 MEMORY.md 索引瘦身(review A4;doc fast lane)— 索引行已膨脹成內容本體(lesson-catalog 行數千字、track5/gov-fe-design 行同病),違反 memory 系統自身「index 一行 hook、內容不進 index」規則且每 session 全量載入=恆常 context 稅。每行縮回「名稱 + 何時該去查」;細節本在各 memory 檔內,用時再 Read。
- [x] H15 push/merge gate 的 fail-open 缺口(review A1)
- [x] H16 re-review diff 視野 + Codex 判定單一實作(review B6+B7;doc fast lane)
- [x] H17 code-reviewer.md 結構重整(review A3)
- [x] H18 CI e2e job(review A2)
- [x] H20 lesson-class 機制化 sweep(owner 2026-07-20 提問「哪些 class 能內化到 loop/harness 而非 prose」;與 H17 的 routing 互補——能機械的機械、剩語意的才進 routing)— 四個獨立切片 H20a–H20d(top-level 列於下;governance lint 只認 column-0 勾稽;a–d 全數 merge,umbrella 隨 H-series retro 收攏)。
- [x] H20a **契約 lint**(class 24/#94):contract ratchet test 掃 openapi.yaml
- [x] MCP1 前端顯示 MCP 網址(owner 2026-07-20 詢問「前端還是看不到 mcp 的網址?」——合約 v1.3 已有 `GET /projects/{project}/mcp`(McpInfo:transport/auth/url,codegen 型別已在 web/src/api/schema.ts),但 API 端點未實作、FE 無顯示面)
- [x] H21 web 測試 gate 的 vitest fan-out 飢餓(#112 期間本機實證:full-suite 預設 worker 數下 RelationReview 的 1s waitFor 確定性餓死——隔離綠/全套紅/CI 綠;workaround=`VITEST_MAX_WORKERS=4`(vitest v4 改名,舊 MAX_FORKS/MAX_THREADS 無效))
- [x] H22 TASKS.md 瘦身 + as-built 歸檔機制(owner 2026-07-23 定案:「a 留摘要 b 放 doc；這件事以後要在 retro 做」)
- [x] H20b **DDL 全稱測試盤點**(class 1 DDL/#17 清單):every-child-FK-has-index、每凍結身分鍵一 unique 等可自 information_schema 機械斷言之項
- [x] H20c **機制化-by-library**(#104–#108 四軸):抽 `useDecisionLock(list, decide)` 鎖述詞 + restore-key helper 共用 hook,新審核/決定面重用而非重推導
- [x] H20d **memory stale-claims warning lint**(class 2):governance job 對 memory 檔「尚餘/待實作 X」而 X 已 [x] 之行發 warning annotation(非 gate)
- [x] H19 merge-gate/governance-check 測試 + receipt GC(review B5+B8)

> **Per-task rule:** one task = one `task/<id>` branch = one PR. It lands with tests for its
> tier, passes local gates + the `code-reviewer` subagent, then merges only after CI **and**
> the bound Codex review are green (see [`docs/LOOP.md`](docs/LOOP.md)).

## Track 0 — Contracts & Governance  *(freeze BEFORE parallel work — DR-002)*
- [x] P0 `contracts/openapi.yaml`: response envelope, error-code enum, cursor pagination, SSE event, idempotency (§15/§27.2)
- [x] P1 `contracts/mcp_response.schema.json`: unified retrieval result + source_refs + debug (§16/§27.2)
- [x] P2 Build/activation model spec + Postgres migrations for `builds` + partial unique index (§14/§27.1) · Alembic setup (DR-008)
- [x] P3 Review state machine + `review_ledger` + fingerprint spec + `fingerprint_version` (§17/§27.3)
- [x] P4 Eval contract: `golden.yaml` schema + metrics incl. path_validity/relation_hit_rate/groundedness (§20/§27.5)
- [x] P5 Query safety policy schema (`query_policy`) + SQL(sqlglot)/Cypher strategy (§21/§27.6)
- [x] P6 Observability schema: pipeline_runs/steps/items + item_ref rules (§18/§27.7)

## Track 1 — Core engine  *(depends on Track 0)*
- [x] C1a PG migrations for core tables (documents/chunks/entities/relations/evidence/reports/merge_candidates; `builds` landed with P2, `review_ledger` with P3, observability with P6)
- [x] C1b **BuildScopedRepo** over Postgres (active-build lookup + build_id injection, DR-006)
- [x] C1c Neo4j adapter + projection repo (build_id-filtered, DR-004)
- [x] C1d Qdrant adapter + projection repo (build_id payload filter)
- [x] C2 Ingest (structured + document connectors) + clean/chunking
- [x] C3a Graph build — structured rule-mapping extraction → entities/mentions/relations/evidence (deterministic, no LLM)
- [x] C3b Graph build — LLM document extraction (schema-guided, §27.4 quote-span evidence) + LLM factory (§3: LlamaIndex 抽象)
- [x] C3c Ontology proposal pool (LLM-proposed new types + `ontology.proposal_policy`)
- [x] C4 Entity resolution + apply `review_ledger`
- [x] C5 Index: embeddings → Qdrant; project entities/relations → Neo4j
- [x] C6a Retrieval: semantic (Qdrant kNN, §16 contract)
- [x] C6b Retrieval: sql (NLSQL + sqlglot guardrail per P5, §27.6)
- [x] C6c Retrieval: graph (parameterized Cypher templates + guardrail per P5, §27.6)
- [x] C6d Retrieval: global (community_reports — needs C7)
- [x] C6e Hybrid router + fusion + routing trace (§8, §16 debug)
- [x] C7 Global summary (Leiden communities + reports)
- [x] C8 MCP server (per project) exposing the tool set
- [x] C8b MCP HTTP transport
- [x] C9 builds/activate/rollback/diff/prune (CLI + core)
- [x] C10 Eval harness runner
- [x] C11 Observability wiring + drift detection

## Track 2 — Console backend (FastAPI + arq)  *(needs Track 0 P0; C-items as they land)*
- [x] BA0 API skeleton + generated OpenAPI matching contract + auth placeholder
- [x] BA1a projects/sources registry — schema + core CRUD
- [x] BA1b projects/sources endpoints — routers + idempotency + opaque cursor
- [x] BA2a jobs table + core job repo + delete-project active-jobs guard
- [x] BA2b builds→projects FK RESTRICT + fixture sweep (close the delete TOCTOU structurally)
- [x] BA2c-1 registry-aware build creation + pipeline orchestrator control flow (six §5 stages injected as a seam; step recording, §22 abort, cooperative cancel, resume; fake stages, hermetic Postgres-only tests)
- [x] BA2c-2a build-config loader — `projects.config` JSONB → typed `TextOntology`/`StructuredMapping`/`ResolutionConfig`/chunk params (reuse dataclass validation, no frozen contract; lenient top-level, strict leaves; unit-tested)
- [x] BA2c-2b sources→connector resolution + `default_stages` + the six stage adapters (shared-conn writer/projectors); component (shared-conn spy) + integration (real stores, fake LLM/embedder) tests
- [x] BA2c-2c two-lane real-LLM test — hermetic + real `chat_model()`/`embedding_model()` over a tiny corpus, key-gated skip-only (no CI secret)
- [x] BA2d-1 **execution lease**(Codex BA2c-1 P2,DB heartbeat-lease)
- [x] BA2d-2 arq worker + Redis wiring
- [x] BA2d-3 **lease reaper**
- [x] BA2e-1 ingest/build triggers + job endpoints (GET /jobs/{id} + cancel)
- [x] BA2e-2 SSE job events
- [x] BA3a inspection: active-binding seam + documents/chunks
- [x] BA3b inspection: entities/relations
- [x] BA3c inspection: graph subgraph
- [x] BA4 cleaning preview/rules
- [x] BA5 merge-candidate review endpoints
- [x] BA6a query playground: binding seam + semantic/sql/global
- [x] BA6b query playground: graph/hybrid
- [x] BA7 health/metrics/eval endpoints
- [x] BA8 builds endpoints

- [x] BA9 canonical source-uri enforcement at the SoR + cancel idempotency (FE1/#70 follow-up, owner 2026-07-13)

## Track 3 — Console frontend (React)  *(needs BA0 contract; v1 = health/jobs/review/playground)*
- [x] ~~H10 browser-QA receipt hook~~
- [x] FE0 app shell + OpenAPI codegen client + project switcher
- [x] FE7 Project Health home
- [x] FE8 Pipeline/jobs dashboard
- [x] FE5 Entity-resolution review UI
- [x] FE6 Query playground UI
- [x] FE3 檢視 Inspect
- [x] FE1 匯入 Import — shipped in #70
- [x] FE2 清洗 Clean
- [x] FE4 圖譜互動探索 Graph explorer

## Track 4 — Console UX 翻新  *(owner-approved 2026-07-14; design source: `.discuss/ux-redesign/PROPOSAL.md` — 12-screenshot audit on real nmmst data. Root diagnosis: the Console leaks "how the system stores" as "what the user sees"; target user is a non-technical knowledge operator. Owner-confirmed pains: 名詞太艱深/沒有判斷上下文/無意義 id/缺操作說明. Phase C's contract bump (eval + upload endpoints) is owner-approved per DR-002 — record in DESIGN §26 when UXC1a lands.)*
- [x] UXA1 Review 頁重設計
- [x] UXA2 總覽 Overview 落地頁
- [x] UXA3 全站翻譯層 sweep
- [x] UXB1 設定 Settings 頁
- [x] UXC1a 契約 v1.2
- [x] UXC1b 後端:eval + upload + stage-1 metadata 傳遞 *(needs UXC1a)*
- [x] UXC2a 評測頁(品質 tab)*(needs UXC1b; UXC2 切片 1/3 — class-25 輪次經濟學:每個新有狀態面一個 PR,先枚舉狀態機再推第一版)*
- [x] UXC2b 上傳 UI(資料 tab)*(needs UXC1b; UXC2 切片 2/3)*
- [x] UXC2c 檢索改名 + 全流程 e2e *(needs UXC2a+UXC2b; UXC2 切片 3/3)*

## Track 5 — 功能完整化與長期維運  *(source: external functional review `.discuss/codex_review/FUNCTIONAL_REVIEW_2026-07-15.md` + hakeguan gap ledger `.discuss/hakeguan/GAPS.md` (both gitignored, local-only); owner 2026-07-15「化作 TASKS」. This is the near-term actionable slice — the larger P2 roadmap is a note at the bottom of this file, promoted to tasks item-by-item only on owner ratification per the GAPS accumulate→ratify→promote protocol. The two former ⚠️ owner-confirm flags (CFG1 direction, SRC2 contract bump) were both approved 2026-07-17 — no items are owner-blocked; contract-touching items ride the CTR1 packaged round (DR-013).)*
- [x] CFG1 (owner 2026-07-17 核准,⚠️ 解除;gateway 形狀同日追加)統一 Console/MCP query-policy SoR (review §P0#2, flagged highest-risk)
- [x] SRC1 xlsx 原生 connector (GAPS G1)
- [x] CTR1 DR-002 打包契約回合(owner 2026-07-17 核准「一個回合議定全部端點」,仿 DR-009/DR-010)
- [x] SRC2 來源生命週期 (GAPS G2)(契約=CTR1/DR-013;runtime 落地:PATCH /sources/{id} soft-disable、enabled 欄 migration 0017、build/ingest 排除停用源、SOURCE_NOT_FOUND)
- [x] GOV1 型別穩定 / 跨型別 resolve (GAPS G4 + full-run DR-003 root cause)
- [x] GOV2 品質治理中心 (review §P1#6 + G4 補強)(契約=CTR1/DR-013,本任務=runtime;因 endpoint+清單+UI 多面切 GOV2-api/GOV2-fe)
- [x] GOV2-api entity/relation approve-reject endpoint(GOV2 切片 A;backend 面)
- [x] GOV2-fe 品質治理中心 UI(GOV2 切片 B;FE 面)
- [x] GOV2-fe-5 **gap-list 兩分頁**(GOV2-fe 收尾片;facet api=#109 GOV2-facet):治理頁加「低信心」「缺證據」兩分頁(消費 `/relations` 的 `filter[confidence]=low`/`filter[evidence]=missing` CLOSED facet)+ Health `low_confidence_relations`/`missing_evidence_relations` 非零深連結
- [x] GOV2-fe-1 entity 審核分頁 + 共用 decide hook + 分頁 a11y(GOV2-fe 第一片,FE 面)
- [x] GOV2-fe-2 relation 審核分頁(GOV2-fe 第二片,FE 面)
- [x] GOV2-fe-3 治理待辦顯示型面板(GOV2-fe 第三片,FE 面;原「發布閘」框架經 Codex #107 P2 修正)
- [x] GOV2-fe-4 審核佇列 robustness follow-up(Codex #105 浮現,FE 面)
- [x] GOV2-facet relation 品質 facet(GOV2 切片 C;backend 面;owner 2026-07-19 D4 核准另立,GOV2-fe 完成後解鎖)
- [x] GOV3 ontology proposal 審核 (review §P1#6)(契約=CTR1/DR-013,本任務=runtime;因 endpoint+UI 兩面切 GOV3-api/GOV3-fe)
- [x] GOV3-api ontology proposal 審核 endpoint(GOV3 切片 A;backend 面)
- [x] GOV3-fe ontology proposal 採納/拒絕 UI(GOV3 切片 B;FE 面,與 GOV2 治理面同落地;owner 2026-07-19 定案分頁式治理頁)
- [x] GOV4 merge-candidates `Filter` fail-loud (GAPS O4)
- [x] QP1 自然語言 → 自動安全 graph plan (review §P0#3)
- [x] SS1a server-side 篩選 facets(SS1 切片 1/2,owner 2026-07-17 核准拆分)
- [x] SS1b server-side 搜尋 + totals + sort(SS1 切片 2/2;契約=CTR1/DR-013,本任務=runtime;因跨 backend/FE/perf ≥2 面,再切 SS1b-api/SS1b-fe)
- [x] SS1b-api server-side `q` + totals(SS1b 切片 A;backend 面)
- [x] SS1b-fe Graph 頁改接 server-side `q`(SS1b 切片 B;FE 面)
- [x] SS2 引用卡片 resolve (review §P1#8)
- [x] RB1 retry attempt / lineage (review §P1#7)(契約=CTR1/DR-013,本任務=runtime;因 drill-down 讀 + retry 編排 + UI 多面切 RB1-api/RB1-retry/RB1-fe)
- [x] RB1-api step/item drill-down 讀端點(RB1 切片 A;backend 讀面)
- [x] RB1-retry retry attempt 編排(RB1 切片 B;orchestrator+worker;owner 2026-07-18 核准再切 core/skip 兩片,因發現「重用成功產物+只重跑失敗項」需跨庫 clone + 逐項 compute-skip,且與 §27.7「併回同一 build_id」舊語衝突——凍結契約(新 build+parent_build_id+父不可變)為準,§27.7 散文順修)。(umbrella 收攏:B-1 retry-core #100、B-2 retry-skip #103 皆已 merge——見各自條目。)
- [x] RB1-retry-core lineage + 端點 + documents clone(RB1 切片 B-1)
- [x] RB1-retry-skip 逐項 compute-skip + graph-layer 重用(RB1 切片 B-2;owner 2026-07-19 定案 pin 父設定 + v1 最小刀)
- [x] RB1-fe 失敗恢復 UI(RB1 切片 C;FE 面)

## Track 6 — MCP agent 可用性  *(source: `.discuss/MCP_AGENT_REVIEW_2026-07-23.md` (gitignored, local-only) — 27 條實測發現、28 項建議,視角=「接上這個 MCP、要靠它回答終端使用者問題的 agent」;owner 2026-07-23「請依優先順序開成 TASKS」ratify. 排序即優先序:P0 會讓 agent 講出**有信心的錯話**(最高),P1 讓 agent 拿得到內容,P2 把靜默失敗變訊號,P3 可發現性(成本最低),P4 效能與併發. 對外 auth/授權/撤銷/稽核**不在此 Track** —— 已存在於下方路線圖「正式對外:auth/整合/部署」,本 review 只是替它補上實證(未帶 header 即可建立/刪除專案;`openapi.yaml:34-36` 宣告 bearerAuth 但 runtime 零強制;DNS-rebinding 防護在設 `0.0.0.0` 的那一刻自動關閉且無法打開),升級為任務仍待 owner 批准.)*

### P0 — 會導致 agent 給出錯誤答案
- [x] MCP2 warning 分類正確性(review 第 1/4/13 條:seed 未解析警告、輸入錯誤與 store 故障分流、STORE_UNAVAILABLE 具名 store)
- [x] MCP3 global_summary 的「未經查詢比對」必須可見(review 第 2 條:LOW_CONFIDENCE 誠實警告 + community_report refs 上限 8)
- [x] MCP4 域外信心訊號:實測定案**刻意不提供**(DESIGN §22 記錄;semantic/hybrid 工具描述誠實聲明「分數是回應內排序,不是可答性」;擴大 battery 實測無門檻可分——域外 0.6144 高於三個域內 0.4992/0.5065/0.5176)

### P1 — agent 拿不到可用內容
- [x] MCP5 MCP 面補原文取回工具:`get_chunk`(chunk UUID→text+provenance)+ `get_document`(document UUID→來源+完整 raw),自省形狀比照 `get_entity`,零契約變更;graph_query 描述指引 evidence ref→get_chunk;mention ref(`chunk:{hash}:{ordinal}`)以具名錯誤指出「不同形狀、尚不可解」(MCP7 缺口);DESIGN §9 工具清單同步。
- [x] MCP6 短查詢也要拿得到 chunk:semantic 預設改雙型別檢索+各保底 `top_k//2` 名額(稀缺型別不佔空位);新增 `point_type` 參數("chunk"/"entity",壞值 GUARDRAIL_BLOCKED);entity title 帶本體型別後綴「名稱 (TYPE)」(自由字串零契約變更,SoR enrich)。實測修復:`票價`/`票價多少?`/`海科館全票` 從 0 chunk/8 entity → **4 chunk/4 entity**,同名 entity 可辨(海科館 (FACILITY) vs (ORGANIZATION))。
- [x] MCP7 entity mention ref 可解化(**DR-014**:`mcp_response.schema.json` 1.0→1.1):兩段 join 把 `chunk:{hash}:{ordinal}` 解成 chunk UUID+source_uri+quote+offsets(get_chunk 直接可解);單一 seam `core/query/mentions.py` 供 semantic/graph/get_entity;不可解丟棄、§16 面 cap 8(get_entity 不 cap=真回復路徑);負向契約 pin;真資料驗證通過。
- [x] MCP8 hybrid 定位修正:**移除 LLM selector**(實測 1,525ms=半數延遲且為漏選元兇)固定跑所有可用模態;**global 退出融合**(未經查詢比對,每呼叫 MODE_SKIPPED 指向 global_summary);**confidence=來源模態原始分數**(RRF 只管排序);工具描述誠實改寫。實測:report 0 格、4 chunk/4 entity、conf 帶真 cosine、海科館全票 3,078→1,384ms。
- [x] MCP9 瀏覽/列舉/分頁與名稱解析:新增 `list_entities`(q 子字串+字元-AND 模糊回退,`match` 具名精度、主館→主題館 實測命中)/`list_chunks`(具名截斷 preview)/`list_reports` 三個分頁自省工具,游標釘 build+filters+match(class 31);93 report/442 chunk 全數可走(原 top_k=20 牆);get_entity 描述指引模糊者用 list_entities;零契約變更。
- [x] MCP10 引用出處終端可呈現:純目錄來源新增 `<name>.meta.json` sidecar 慣例——context(title/attributes.source_url 等)由 connector 組成 DR-010 envelope 落 `documents.metadata`,經既有 metadata_exposure allowlist 投影進 chunk source_refs;system/schema_version 不可偽造、孤兒/壞 JSON loud fail;上傳來源既有 API 擷取不變。DESIGN §5 記慣例。

### P2 — 靜默違背 agent 的明確指令
- [x] MCP11 顯式 graph 參數必須強制執行:半套 graph_*(缺 template+entity 完整對)由 `_incomplete_graph_invocation_payload` 於 binding 前 loud 拒絕(GUARDRAIL_BLOCKED、n=0、點名已給/缺少/「你的 invocation 沒跑」),絕不靜默改跑 QP1;graph_hops 改 None 預設使單給可偵測;原第一半缺陷(selector 丟顯式參數)已被 MCP8 決定性 fan-out 結構性修除,nmmst 實測顯式 invocation 確實執行。DESIGN §8 記。
- [x] MCP12 錯誤族與信封補齊:PG 連線層例外併入 STORE_CLIENT_ERRORS 單一 map;NoActiveBuildError→typed NO_ACTIVE_BUILD warning(v1.2,DR-015);LLM 供應商族 typed 降級不轉交上游原文;查詢 4000 字上限;gateway 每請求 preflight(5s)——policy 壞 503 帶可行動文字、已刪 404、registry 掛 503 快速,取代 200+空串流。
- [x] MCP13 warning 語意一致性:top_k 超限 clamp 必發 TRUNCATED(四工具共用 helper);explain_retrieval 於 expose_debug 關閉時事前拒絕(GUARDRAIL_BLOCKED 單義=零產出,省整條管線+LLM 花費);introspection error 一律配 typed error_code(INVALID_INPUT/NOT_FOUND/QUERY_TIMEOUT/STORE_UNAVAILABLE/NO_ACTIVE_BUILD,全站 sweep)。DESIGN §27.2 記。

### P3 — 可發現性(成本最低、回報直接)
- [x] MCP14 工具面 metadata 補完:全參數 Field description;封閉詞彙上 enum(template/graph_template/point_type);六個信封工具 outputSchema 接上凍結契約(僅廣告面,runtime 轉換不動);描述去 §/內部代號;server 自述(instructions/website_url/版本改報 graphrag 套件版,非 SDK 版);空的 prompts/resources 能力取消宣告(handler 反註冊)。全 metadata,執行路徑零變更。
- [x] MCP15 限制事前可發現:list_schema 回應併入 session 實際 policy 區塊——max_top_k/max_graph_hops/對帳後 max_sql_rows+max_graph_rows/max_latency_ms/sql+graph timeout_ms/expose_debug/sql_enabled/max_response_bytes(顯式 null=無上限)/query 4000/browse 200/q 64,騎全部分支;非 operative 欄位(default_mode、cypher 開關)寧缺勿列。純新增,不動工具詞彙。

### P4 — 效能與併發(若要讓多個 agent 同時使用)
- [x] MCP16 事件迴圈解阻塞:store/model client 改每 server 一份共享(首 session 於 asyncio.to_thread 建構——Qdrant 建構子實測 1,302ms 迴圈停滯不再凍住併發;後續 session 僅讀 policy,實測 1587ms→196ms),session 不再各建各關(累積問題一併除);policy 仍逐 session(DR-012)且先於 client(#93 R5);關閉權在 owner(gateway host 驅逐/關機、run_server 結束)經 _graphrag_close_shared。
- [x] MCP17 gateway 水平擴充與 session 生命週期:serve-mcp 改 factory(`create_app`);`--workers>1` REFUSED(需 affinity LB,process-sticky+共用 socket);idle 注入 1800s + 絕對年齡上限 7200s(鍵 `(project,session_id)`)封頂過期 policy 快照,過齡誠實 404 reconnect+server 端釋放;/health 回非敏感 `{"status":"ok"}`;刪專案撤銷沿 MCP12 preflight。
- [x] MCP18 查詢延遲(review 第 25 條)— (b) 跨請求 embedding 快取:query embedder 包 bounded LRU(每實例一份,非 gateway 級預算;`embedding_cache_size` 預設 128,多專案主機應調降;0 停用),重複問題(導覽:票價/開放時間/交通)命中即跳過 ~1s OpenAI 往返(佔 semantic ~88%);ingestion 不快取。(c) selector 已由 MCP8 移除。(a) 模態併發需 per-mode PG 連線隔離,owner 定案改列 follow-up。

## Track 7 — 對外品質(黑箱 QA)  *(source: `.discuss/BLACKBOX_QA_2026-07-26.md`(gitignored,local-only)— 19 個 agent 對 live Console/API/MCP 做**外部**黑箱探測:8 維度並行(契約符合性/輸入驗證/session 生命週期/答案品質/分頁游標/誠實降級/Console HTTP/跨介面一致性)+ 每維度一個對抗式查證 agent 專責推翻該維度發現;37 筆原始發現 → 21 筆通過查證 → 綜整 **19 條缺陷(7 P2 / 12 P3,無 P0/P1)**。owner 2026-07-26「請將它們成立TASKS」ratify。排序即優先序:先補**核心承諾**(引文可稽核、預設入口答得出來),再消滅**靜默錯誤**(靜默丟參數/靜默 clamp),最後收框架層與生命週期衛生。**駁回項不立案**——serverInfo `0.0.0.0` 版號(importlib 忠實回報 pyproject 佔位版號)、REST/MCP 計數差(merged 墓碑,設計如此)、LIKE/SQL/CRLF 注入與 Vite 機密曝露(全數守住)、session 偽造/跨專案重放隔離(Codex #137 擔心的逃逸確認已關)皆已查證為正確行為。**Host-guard 未執法不立案**:那是 `mcp_http_host=0.0.0.0` 綁定的既知後果(SDK rebinding 防護僅在 localhost 綁定自動開),屬 §23 auth round。)*

### 核心承諾(P2)
- [ ] QA1 引用 quote 逐字化(D2)— entity mention/source_ref 的 `quote` 現為 LLM 正規化的 canonical name 而非擷取自原文的 span:166 個 mention 稽核中 16 個(~10%)的 quote 在被引 chunk 全文**找不到**(例 entity「全票（票價）」quote=「全票（票價）」,chunk 4bce4527 實文為「問:請問潮境智能海洋館全票多少錢？答:全票300元。」);relation 的 chunk evidence 則 24/24 逐字吻合,證明凍結契約「the same auditable shape as relation chunk evidence」的本意就是逐字。因 offsets 依 DR-014 是 chunk 層級,quote 是**唯一的 mention 層級稽核錨點**。修法:抽取時捕捉真實文字 span 存為 `surface_form`(`core/graph/documents.py:322` 現為 `surface_form=name`);退路是明標語意為 canonical name 並修契約措辭 + bump `schema_version`(DR-002)。做 groundedness 驗證的 agent 會因此**拒絕正確答案**。
- [ ] QA2 hybrid_query 零文字頁(D1)— 文件明載的預設入口對真實遊客問題可回傳 20/20 全 `result_type=entity`、`text` 全空的頁面,把所有直接回答的 chunk 擠掉且**無任何警告**(`hybrid_query {"query":"有導覽解說嗎？"}` → rank0「銀眼魚」confidence=1.0;同題 `semantic_search` 有 10 個直接回答的 chunk,hybrid 20 筆裡一個都沒有)。單模式工具嚴格優於旗艦融合工具。修法:融合時保 chunk 配額(如 semantic 的 half-page 保證),或至少發警告揭露 chunk 位移。

### 靜默錯誤(P2)
- [ ] QA3 未知參數靜默丟棄(D3)— MCP 靜默接受並忽略拼錯/未知的工具參數,以**乾淨成功**回傳錯誤資料:`semantic_search {"query":"票價多少","topk":3}` → 20 筆(非 3)、warnings=[];`list_entities {"entity":"票價資訊","limit":3}` → 3 個毫不相干的 entity、`error_code=null`;`graph_query …"hop":2` → 靜默退回 hops=1。REST 對同一 typo 正確回 `400 extra_forbidden`。MCP 的呼叫者正是最會打出似是而非參數名的 LLM agent——filter 被丟掉後 agent 以為在查單一 entity,實際在 1405 筆語料的未過濾切片上推理。修法:inputSchema 加 `additionalProperties:false` 或 server 端對未知 key 發警告/拒絕,對齊 REST。
- [x] QA4 REST 靜默 clamp(D4)— `policy.top_k()` 的 min() 夾在 REST facade 無揭露、MCP 有(同產品兩套說法);TRUNCATED 訊息上移 `core/mcp/policy.py` 成單一來源,兩表面共用,REST 於 `_run_mode` 單點注入(semantic/global/sql/hybrid 全掃;graph 本就拒收 top_k),nil-build 不謊報。契約 enum 已含 TRUNCATED,免 bump。

### 框架層與型別化錯誤(P3,建議一次修掉)
- [x] QA5 字串/框架層驗證一次收口(D5/D9/D10/D11/D12/D14)— 單一述詞 `unstorable_string_reason` 貫穿 MCP 查詢/introspection、core graph 種子(兩 facade 同答)、REST `q`/`filter`(400 非 500)、gateway 路徑(不再假報 503)。框架層於 dispatch seam 依**階段**分類(先驗參數再 dispatch,tool body 例外不再誤貼 INVALID_INPUT);拒絕回各工具**廣告的**形狀。
- [ ] QA6 型別化錯誤一致性與診斷措辭(D8/D13)— (a) `get_entity` 查無此名回**成功**(`error_code=null`、`entities=[]`),而同族的 `get_chunk`/`get_document` 查無回 `NOT_FOUND`;initialize instructions 為 get_*/list_* 定義的是單一 taxonomy,按文件以 error_code 分支的消費者會把落空查詢(改名、過期引文)讀成成功。(b) 任何三段式垃圾 cursor 被誤診為「cursor was minted for a different build (the active build changed)」——什麼都沒變;根因是 split-on-pipe 先於形狀驗證,第一段未驗 UUID 就當 build id 比對。「active build changed」在 DR-001 系統是操作上有份量的宣稱,會引發不必要的 build 稽查。

### 生命週期與衛生(P3)
- [ ] QA7 專案刪除的終局與 uploads 清理(D7/D16)— 跑過一次 build 的專案**永遠無法刪除**:REST 拒絕並指示「prune them first」,但 `graphrag prune {p} --keep 0` 吐**未捕捉的 traceback**(`lifecycle.py:797 ValueError: keep must be >= 1 …would drop the active build`,該 build 甚至從未 activate),`--keep 1` 又保住唯一 build;契約 52 個 op 無 build 刪除端點,實測唯一出路是手改 Postgres 列——**錯誤訊息指向一條做不到的補救路**。本機曾累積 194 個專案(189 個 gclone-*/retry- 殘留,已於 2026-07-26 清除)即此缺陷的實景。另:上傳的 managed-corpus 檔案在專案刪除後**殘留磁碟**(`data/uploads/{p}/`),無任何 API 可列舉或移除——無上限磁碟成長 + 資料保留意外。修法:允許非 active build 走 `--keep 0`(ValueError 接成 typed REFUSED),DELETE 專案時一併清 uploads 目錄。
- [ ] QA8 cursor 打標與資源綁定(D6/D15)— (a) `/relations` 鑄造**未打標的 legacy cursor** 且不綁定任何 filter:`filter[confidence]=low` 的 cursor 換到無 filter 重放 → 200、**靜默跳過 32 列**;該 cursor 也被 `/entities`、`/documents` 全部接受。不需偽造——每個 relations 頁面都在發一張到處有效的 token,相容孔因端點持續鑄造舊 token 而永遠關不掉。(b) REST cursor 的 scope fingerprint **未含資源別**:`/entities` 與 `/documents` 在同 context 算出相同 tag,cursor 互餵 → 200 + 靜默重複列。修法:`/relations` 改走 `encode_sorted_cursor`/`_scope_fingerprint`,並把資源別納入 fingerprint;順帶檢查 `/builds/{id}/steps/{step}/items`(用同一未打標 decode,同根因嫌疑,QA 期間無可達資料未能證實)。
- [ ] QA9 Console 打磨(D17/D18/D19)— (a) 根路徑導向 `projects[0]`(created_at DESC),operator 一開門就掉進最新建立的空殼專案;`ProjectSwitcher` 是**無搜尋的裸 `<select>`**(其註解假設「project counts…are small」已失真),SPA 全無刪除入口。改法:落地改為最近使用/持久化選擇的專案 + switcher 加搜尋。(b) 經 proxy 的**尾斜線**請求 307 轉址到後端 origin `http://127.0.0.1:8010`(API 由 Host header 派生 Location,Vite proxy 改寫 Host 卻不回寫 Location)——破壞 same-origin 契約,LAN 使用者跟隨轉址會打到**自己**的機器,並對每個客戶端揭露內部後端位址。(c) 服務出的 document shell 未品牌化且語言標錯:`<title>web</title>`、`<html lang="en">`,實際渲染繁中 UI——每個分頁/書籤都叫「web」,lang 錯標讓螢幕閱讀器與翻譯工具誤處理整個 UI。

### 盲區補測
- [ ] QA10 寫入路徑輸入驗證掃描 — 本輪 QA 全程唯讀,**所有變更路徑的輸入驗證完全未測**,報告明列為「最該看的下一個洞」:`POST /projects`、`/sources`、`/ingest`、`/uploads`、`/build`、`PATCH /sources`、`activate`/`retry`/`rollback`、review 核決端點。以可拋棄的 qaprobe 專案演練畸形/邊界/型別/unicode 輸入,確認每條寫入路徑都給型別化拒絕而非 500/靜默接受。順帶補其他盲區:開一個 sql-enabled 的 qaprobe lane 測 NL-to-SQL 輸入面(nmmst policy 停用故整個表面未測)、在可拋棄環境做 store-down 演練(`STORE_UNAVAILABLE`/`PARTIAL_RESULTS`/`QUERY_TIMEOUT` 三條降級路徑本輪皆無法覆蓋)。**這三塊目前是空白,不是綠燈。**

---

## 路線圖 — P2 擴張與正式營運  *(functional review §P2 + roadmap; NOT queued tasks — the accumulate-in-ledger → owner-ratify → promote-to-task protocol from GAPS applies. Detailed here so nothing is lost; promoted to Track 5 tasks item-by-item on owner sign-off.)*
- **連接器擴充** (review §P2#9) — text is `.txt/.md` only, structured is CSV only; no URL/DB connector (`core/ingest/connectors.py`). Staged: PDF/Office/HTML/OCR (keep page/layout position) → S3/Azure Blob → HTTP crawler + incremental site update → DB incremental sync → scheduled build + change detection + connector health. Data model should split content-blob from source-occurrence so identical content across sources keeps all provenance. (SRC1 xlsx = the first slice.)
- **正式對外:auth/整合/部署** (review §P2#10) — bearer token is extracted but NOT validated; anonymous passes (`api/auth.py`); MCP HTTP has no transport guard (`core/config.py`). OIDC/API-key + RBAC + project membership + MCP service token; mutation/review/activate audit + rate limit; build-level JSONL/CSV/GraphML export + manifest; job/build/review webhooks; full API/worker/web images + Compose + readiness/migration/secrets/TLS/limits; structured logs + Prometheus/OTel + alerts + 3-store backup-restore drill. **Spec gap:** DESIGN §11 (部署) says Compose should include API/worker but `docker-compose.yml` has only the 4 stores; README still needs 3 manual terminals.
- **答案合成** (review §P1#5) — **direction decided 2026-07-15: retrieval-first** (QueryResult stays ranked hits; the rename ships in UXC2). Re-open only if the product later targets human knowledge workers needing cited synthesized answers + claim-level eval.
- **metadata 下游階段** (review §P0#4 rules 4/5/8/9/10, after UXC1b's stage-1) — chunk-local metadata population (clean/chunking), filterable-field indexing + search-BY-metadata (with SS1), embedding participation (opt-in per project), auto-extract namespace + provenance, optional entity/relation promotion.
- **O3 job 詞彙 verify** (GAPS O3) — terminal state is `done` not `succeeded`; confirm the contract enum matches the impl, upgrade to a task only if they diverge.
- **查詢效能後續(MCP18 defer,P4)** — MCP18 只落地 (b) 跨請求 embedding 快取;兩項 gateway-resource 收尾待 owner 重估:(1) **hybrid 模態併發執行** — 現循序 `for mode in selected:`(端到端 3,078ms≈各模態耗時和),真併發需 per-mode PG 連線隔離(模態共用單一 asyncpg 連線,同連線併發會炸)+改寫釘住循序假設的 §21/§22/mode-order 測試;(2) **query 快取的 gateway 級預算** — per-instance LRU 隨 `McpGateway._apps` 保留的專案數累加、無 gateway 級上限(已調小預設 128 緩解即時風險),Codex 建議的跨 server 共享 byte/entry 預算會動到 per-instance 隔離模型。完整分析(連線隔離、測試清單、shared LRU vs per-instance)見 `.claude/memory/graphrag-open-followups.md`。

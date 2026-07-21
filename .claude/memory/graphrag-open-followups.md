---
name: graphrag-open-followups
description: 尚未立案的懸置 follow-ups 集中帳(2026-07-17 memory 大掃除時自四個已刪檔抽出)
metadata: 
  node_type: memory
  type: project
  originSessionId: d673e708-e836-4b8a-8fc7-cb33527c5fc3
  modified: 2026-07-21T05:52:05.504Z
---

散落在已刪除記憶檔裡仍然「活著」的 follow-ups,集中一處(狀態以 TASKS.md/
GitHub 為準;立案或了結後從本檔劃掉):

- **useCancelJob 無 Idempotency-Key**(FE8 殘留,owner deferred):Console 寫入
  的 retry-safe 一致性缺一角;FE5 同類已修。小任務量級。
- **ontology-configuration UI**(FE1 殘留,owner 未授權):Import 頁只 surface
  「缺 ontology」不提供收集表單;UXB1 蓋了「編輯已設定 ontology」,首次
  設定的引導仍缺。
- **UXC2 codegen follow-up**:`format: binary` 經 openapi-typescript 產出
  `string[]`(上傳欄位型別不精確);等 codegen 工具鏈升級或手寫 override。
- **教學文件位置**(reference):`.discuss/tutorial/`(gitignored,owner 指定)
  — TUTORIAL.md + 8 截圖 + 可重現語料;產品首次端到端實證(2026-07-13)。
- **repo 衛生**:#93 已把 `projects/{museum,nmmst}/eval/golden.yaml` commit
  (owner 可 revert;已在 PR 揭露)並刪除 per-project `config.yaml`/
  `mcp_entrypoint.py`(CFG1)。現剩 untracked `data/`(runtime 產物):
  .gitignore 或清除未決。
- **policy.py 殘留 nit**(#93 reviewer 非阻塞):`load_query_policy` 的
  `text=` 參數已無 caller 使用(worker 走 registry 路徑後孤兒化)+docstring
  該段落過時 — 小清理任務量級。
- **server.py dispose 不對稱**(#93 reviewer 非阻塞,先於 CFG1 即存在):
  lifespan 中 client factory(qdrant/neo4j/embedder/llm)建構失敗時 engine
  不 dispose(NullPool 故無實質洩漏);policy 失敗路徑已修(R5),此半邊
  順手補即可。
- **config 樂觀併發 / version token**(DR-002 級,#84 R10 起立案,#97 R3 新增
  參與者):`PATCH /projects` 整欄覆寫 config 無版本檢查,跨寫者(別 tab/CLI、
  或 GOV3 accept 寫 ontology)在一次 save 的 read 與 PATCH 之間互相 clobber=
  版本 token 凍結契約缺口。GOV3 accept 已在鎖下 atomic read-modify-write,但
  後續版本無感的 PATCH 仍可蓋掉被接受型別(proposal 終態 accepted 但型別不在
  config)。真解=config version/If-Match 樂觀併發,跨全部 config 寫者,自成一
  DR-002 任務。
- **MCP auth**:CFG1 gateway 不帶 auth(owner 2026-07-17 預設同意);對外
  曝露後 §23 placeholder 會變真需求,屆時是 DR-002 相關 owner 決策
  (凍結 enum 無 auth 錯誤碼)。
- **MCP 部署後對外開放**(owner 2026-07-21:「之後如果佈署之後要能開」——
  僅記需求,未定方案):屆時配套=(i)綁定非 loopback(`serve-mcp --host`/
  `GRAPHRAG_MCP_HTTP_HOST`)+(ii)`GRAPHRAG_MCP_PUBLIC_HOST` 設對外位址
  (Console 廣告面,MCP1 已做)+(iii)防火牆/隧道擇一(區網/Tailscale/
  cloudflared 之別=給誰用)+(iv)**auth 前置**(公網裸奔 auth:none 不可,
  接上一條)。
- **空 stages 的 run_build 本體 ~4s**(H20b 期間實測,#115):orchestrator 對
  「六個 no-op stage」的 build 也要 4 秒(疑似 per-stage 連線/交易開銷或
  store client 建構),曾把既有 lock 測試的 5s 等待預算壓到確定性餓死
  (該測試已放寬至 30s 上限)。值得一次 profiling:若是 eager 建構
  (class 13)或逐 stage 重連,修掉能讓整個 integration tier 提速。
- **GOV2 gap-list FE 片**(facet api 已於 #109 落地〔GOV2-facet:`filter[confidence]=low`/
  `filter[evidence]=missing`,述詞與 §19 gauge 同 `LOW_CONFIDENCE_BELOW` 常數〕;剩 FE):
  治理頁加「低信心」「缺證據」兩分頁(重用 RelationReview 模式)+ Health `TAB_FOR_COUNT`
  的 `low_confidence_relations`/`missing_evidence_relations` 深連結。**測試必斷言同送
  `filter[status]=active`+facet 兩參數**(facet 正交,gauge parity 靠組合——#109 gate-2 nit
  明記)。落地後 GOV2 umbrella 的完成準則(每個非零品質訊號皆深連結至可行動清單)即閉合。
- **RB1-retry-skip 的 entanglement 保守退全導**(#103 R3/R4 follow-up):目前若父有
  「同時被失敗與非失敗 doc 觸及」的實體(或關係),整個重試退回全部重導(fork-C
  紀律),放棄 compute-skip 省成本。真正精細解=只「額外重抽糾纏的成功 docs」(而非
  全建置),既修 first-write-wins 部分 scalar 又保省成本;需算糾纏 doc 集合並讓 clone
  排除之。已在 `core/builds/retry.py::graph_entangles_failed_docs` docstring 記為
  future slice。
- **legacy request schema 的 additionalProperties:false 閉合**(H20a 掃出,DR-002 級):
  8 個 v1.0 時代 request schema(BuildRequest/IngestRequest/ProjectCreate/ProjectUpdate/
  QueryRequest/ReviewDecisionRequest/SourceCreate/uploads multipart inline)在 schema 文本
  沉默開放;其中 7 個 JSON model runtime 已 `extra="forbid"`(閉合=純 schema-text 對齊,
  無行為變更),**multipart uploads 例外**:無 model,uploads.py 只讀 files/metadata、
  忽略未知 form part=兩面皆開,閉合時須同步加 runtime 拒絕(行為變更)。閉合=凍結
  契約編輯:版本 bump + DESIGN §26 + 縮 test_contracts.py 的 ratchet pin,自成一
  DR-002 任務,owner 決策。
- **候選-scoped 發布 preflight**(GOV2-fe-3 #107 Codex R1 浮現,DR-002 級):Health 的
  review/confidence/evidence 計數為 active-build scoped,無 per-build facet——故 Console
  無法對「即將上線的候選 build」做品質預檢(GovernanceBacklog 已誠實改述上線中知識庫)。
  若 owner 想要真正的發布前品質檢查,需契約新增 per-build health/counts facet
  (如 `GET /builds/{id}/health` 或 health 帶 build 參數),自成 DR-002 任務。
- **run-level 失敗成因未曝露到 Console**(RB1-fe #102 P1+step-error 兩輪浮現,
  DR-002 級):`pipeline_runs.error`(整個 run 於「步驟之外」崩潰的權威成因)
  沒有任何讀端點曝露,且 `Build` schema 無 `job_id`、無 jobs 清單/build→job
  查詢端點,故失敗建置的 job id 從 Console 這條流「取不到」。RB1-fe 已把逐步驟
  `BuildStep.error` 與逐項結果都呈現,並把 run-level 說明誠實界定為「唯一仍未
  呈現者」;真解需後端契約變更(RB1-api 加 run-error 投影欄位,或 build→job
  lookup + `GET /jobs` 清單),自成 DR-002 任務。RB1-fe 說明已標 (RB1-api)。

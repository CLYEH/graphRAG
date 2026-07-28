---
name: graphrag-open-followups
description: 尚未立案的懸置 follow-ups 集中帳(2026-07-17 memory 大掃除時自四個已刪檔抽出)
metadata: 
  node_type: memory
  type: project
  originSessionId: d673e708-e836-4b8a-8fc7-cb33527c5fc3
  modified: 2026-07-27T04:39:36.994Z
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
- **push-gate 盲點:驗工作樹、不驗 outgoing commit**(2026-07-22 #116 實證):
  已審修正未 commit 時,工作樹 hash 與 receipt 相符→push 放行,但推出去的
  HEAD 不含修正(當場發生一次,一分鐘內補正;gate-2 之後兩輪都以人工
  flag 防守)。可機械化:push gate 加一條「`git status --porcelain` 除
  untracked 外必須乾淨」(有 uncommitted tracked 變更即擋),H-task 量級。
- **filterable 保留名 `status` 的 config-time 回饋**(SS1b gate-2 nit):documents
  端點對 schema 宣告 filterable 的 `status` 屬性靜默讓位給 lifecycle facet
  (安全預設,已文件化),但專案 owner 得不到「此欄位在 documents 清單不可達」
  的回饋——宜在 metadata_schema 載入/驗證時警告或拒絕保留名,小任務量級。
- **PageMeta.total 的 estimate 路徑**(SS1b 收尾時 owner 定案「維持精確」):
  契約已預留 `total_estimated`;觸發條件出現時(COUNT 顯著變慢/表過十萬列級)
  再實作 planner 估計路徑,Console 改顯「約 N 筆」;同一 scale 路徑順帶:
  0020 的 GIN 建索引改 CONCURRENTLY、COALESCE timestamp keyset 補 functional
  index(目前規模皆不需要,gate-2 nit 記帳)。
- **metadata 搜尋的 Qdrant 半邊**(rule 8;SS1b 只落 Postgres 清單面):
  retrieval 時以 metadata 過濾 chunk 候選(Qdrant payload index)屬查詢管線
  面,需另立任務設計 store filters 與 query API 的接法。
- **空 stages 的 run_build 本體 ~4s**(H20b 期間實測,#115):orchestrator 對
  「六個 no-op stage」的 build 也要 4 秒(疑似 per-stage 連線/交易開銷或
  store client 建構),曾把既有 lock 測試的 5s 等待預算壓到確定性餓死
  (該測試已放寬至 30s 上限)。值得一次 profiling:若是 eager 建構
  (class 13)或逐 stage 重連,修掉能讓整個 integration tier 提速。
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

- ~~relations/review default cursor 未綁 query scope(SS1a-era)~~ — **QA8 已結清**(#146, 28c44bc):實際範圍是九個鑄造端而非原記的兩處,as-built 見該 PR。殘留只剩下一條(legacy 分支待拆)。

- **`decode_scoped_id_cursor` 的 legacy 1-item 分支待拆(QA8 收尾)**:QA8 已停止所有無 tag 鑄造,但相容分支本身沒有出口——手工造的 `["<uuid>"]` token 至今仍被六個 id listing 接受(builds、build-step-items、relations、merge-candidates、ontology-proposals、documents/entities 預設序)。「讓在途 token 老化」只有在有人真的拆掉分支時才成立,故立此帳。影響有限(非授權繞道:build/project 範圍來自 repo 與路徑,不來自 cursor;後果是同一個已授權 listing 內的自傷式錯頁),故非阻斷級。順手時機:老化窗口後任何動 `api/pagination.py` 的任務。

- **corpus 目錄的大小寫/正規化別名(Codex #149 r6,P1,已實測)**:`projects.name` 是**大小寫敏感**的 Postgres 主鍵,但預設 NTFS(與 APFS)的目錄名是**大小寫不敏感**的,故 `p` 與 `P` 是兩個合法且不同的專案,卻共用**同一個** corpus 目錄。實測(QA10a 期間):`safe_project_subdir(base,'p')` 與`(base,'P')` 回傳**同一路徑**,且 `P` 讀得到 `p` 的檔案;因此刪掉任一個都會detach + rmtree 掉另一個活專案的語料——**資料毀損,非孤兒**。macOS 的 NFC/NFD 正規化是同一類的第三種等價(`é` 兩種寫法)。
  **非本次回歸**:QA10a 之前就存在,新增的守衛都是純字串規則,看不到跨列衝突。
  **為何沒在 QA10a 修**:正解需要**跨列**判定(建立時查大小寫/正規化等價的既有專案),而要真正無競態則需要 `lower(name)` 的 functional unique index(migration)——與本任務的純字串邊界驗證是不同機制。只加一個查詢而不加索引,會把「一定壞」換成「並發建立時才壞」並附帶一份虛假的安全感。
  **結構性消除**:corpus 目錄改以**不可變 project id**命名(見上一條 #145 的generation-unique layout 討論)——id 不會有大小寫或正規化等價,故該類別整個消失。本條讓那個提案的價值上升:先前只為了 recreate 競態,現在還解掉活專案互毀。
  **在那之前的症狀(給值班的人)**:兩個僅大小寫不同的專案,其一被刪除後,另一個的下一次 build 會 loud fail 於 `core/ingest/connectors.py` 的`NotADirectoryError`,指名那個已消失的路徑。

- **`reject_unsafe_corpus_path` 在 uploads 端仍在 event loop 上(Codex #149 r6 P2 的姊妹)**:該 helper 會做 `Path.resolve()`,而 `upload_corpus_dir` 可能是網路掛載;掛載卡住時會擋住整個 worker 的 event loop。QA10a 在 `POST /projects` 的新呼叫點已改成`asyncio.to_thread`,但 `api/routers/uploads.py` 的既有呼叫點沒動(非本任務範圍,且該端點後續本來就要做檔案 I/O)。**判準記著**:把阻塞呼叫包成 sync helper 只是讓 lint 看不到,不是把它移出 loop——兩件事別混為一談。**同一呼叫點的第二個缺口(Codex #149 r7 的孿生,同樣未修)**:該呼叫在 `run_idempotent` **之前**,而它會讀檔案系統,故帶 Idempotency-Key 的**重試**可能因為暫時性掛載錯誤或目錄已變成 symlink 而拿到新的 400,而不是照約定原樣重播已存的回應。projects 端已把同一個 helper 移進 `produce` 修掉;uploads 端**刻意未動**——移進 `produce` 會把「名字不合法」的拒絕推到**整個 body 緩衝之後**,那是真實的取捨(fail-closed vs 正確重播),值得自成一個任務而非在 review 輪次順手改。順手時機:下一個動 uploads 端點的任務,兩個缺口一起處理。

- **REST 的 inspect 面沒有任何 §21 wall-clock deadline(QA10b,經 gate-2 更正後的真問題)**:`api/` 全庫沒有 `asyncio.timeout`/`TimeoutError`,所以 `QUERY_TIMEOUT → 504` 這個映射在 REST 面**沒有自己的 emitter**——它只用於指名 framework/proxy 自己拋的 504(`code_for_framework_status`,`tests/test_api_skeleton.py:101,304` 有釘)。MCP 的 introspection 工具有 `_introspection_timeout`(`core/mcp/server.py:794-806`,DESIGN §292 指名),REST 的對應面(`api/routers/inspect.py`)沒有。**owner 問題**:inspect 類的長查詢該不該有自己的 deadline,還是刻意交給 proxy 逾時?
  **我原本記的是錯的,留著記錄以免重犯**:初稿寫「同一逾時條件 MCP 發 QUERY_TIMEOUT、REST 發 PARTIAL_RESULTS,是跨介面不一致」。錯在拿**共用的** query 路徑去比**不同的** introspection 路徑——`api/routers/query.py:146` 就是呼叫 `core.mcp.server.run_bounded_query`(query.py:4-7 明寫「one machinery, two facades」),而 timeout→`PARTIAL_RESULTS` 出自`run_bounded_query` 自己的 `except TimeoutError`(server.py:681-696),**MCP 工具走同一條**。沒有分歧。DESIGN §245 也早已定調「query 逾時:回部分結果 + warning,不 500」。同輪我還誤稱那是死映射、誤稱 Console 看不到該碼——實際上 `web/src/api/queries.ts` 把 `QUERY_TIMEOUT` 放進 `SCOPE_NEUTRAL`,正是為 proxy-504。

- **NL-to-SQL 輸入面已實測(QA10b,需自建結構化語料才測得到)**:nmmst 測不到,因為它是**文件語料**、`STRUCTURED_MIME` 列為零,於是每張白名單表都渲染成`- halls()`,模型照抄寫出 `FROM halls()`,sqlglot 解析成 `exp.Func`,`_reject_side_effects` 在白名單/JOIN/聚合/注入檢查**之前**就擋掉——量到的是「空 schema 一律拒絕」。故另建 3 列 CSV(`structured` kind + `metadata.table/pk_column`,指向**檔案**而非目錄)→ build → 補 golden.yaml → eval → activate,才真正跑得到這個面。
  **結果(七個輸入,全部 200、無 500、無靜默執行)**:良性查詢正常回列;`drop table halls; --` **沒有破壞任何東西**(事後 3 列完好);`' UNION SELECT * FROM projects --` 回 0 列;**白名單守住**——`列出 projects 表的所有資料` 與直接送 `SELECT * FROM projects` 都只回`halls` 的列,registry 資料從未外洩;寫入意圖(`把…樓層改成 9`)被 `GUARDRAIL_BLOCKED`;RTL override 字元不炸。
  **真正的發現是功能性過度阻擋,不是訊息品質(gate-2 把我的診斷倒過來,已修)**:sqlglot 的 `exp.And`/`exp.Or` 是 **`exp.Func` 的子類**,所以 `_reject_side_effects` 的`find_all(exp.Func)` 會匹配到布林連接詞本身——**每一個多條件 WHERE 都被拒**。無需 DB/LLM 即可重現:`validate_sql("SELECT * FROM t WHERE a='x' AND b='y'", ...)` → `GuardrailBlocked`,而單條件 WHERE 通過。後果:§8 的 sql 模式**答不出任何需要兩個條件的問題**,而 `core/query/sql.py` 的 `_RULES` 正好明文要模型「Narrow with WHERE」——**prompt 要求的形狀就是守衛拒絕的形狀**。已修(`exp.Cast | exp.Connector` 一併跳過;連接詞不取鎖、不改設定、不非確定,不是這個檢查存在的理由),並補上 accept-surface 測試。
  **我原本把它記成「守衛擋對了、只是訊息指名錯節點」**——那是反的:守衛擋錯了,訊息指名的正是它拒絕的那個節點。這個誤判會讓它被當成 cosmetic 排期,而模式一直是壞的。
  **`tests/test_sql_guard.py` 的 accept 面沒有任何雙條件 WHERE 案例**,所以 1542 支測試在一個壞掉的守衛上全綠。已補 AND/OR/三條件/含 CAST 四例,並寫明理由。
  **七個輸入的結論需按此重讀**:白名單那條仍然成立(`_single_allowed_table` 是獨立檢查,UNION 案結構上就死),但「寫入意圖被 GUARDRAIL_BLOCKED」那條**是被連接詞 bug 擋的**,不能拿來當「守衛判斷寫入意圖正確」的證據——修好之後需要重測那一項。
  **殘留**:`qa10b-sql` 這個測試專案留在 dev 庫(DELETE 回 400 `ProjectHasBuildsError`——QA7 的刻意設計),檔案(CSV/golden)已刪。dev 庫本就有數十個`gclone-*`/`retry-*` 測試殘留,故未強行清除。

- **delta-review receipt 被 harness 自動誤旗(#125 期間兩次)**:gate-2 persistent
  reviewer 依 SendMessage delta-review 協議自查 diff 後蓋章,harness 的 security
  heuristic 兩度標為「無真審查的自我蓋章」。誤報(輸出含具體查證),但訊號值得
  機械化補強:候選 H-task=write-review-receipt.sh 記錄 verdict 摘要/輪次 context,
  或 LOOP.md 明文 delta-review 協議供 heuristic 對齊。owner 知悉後再立案。

- **`_SelectorLLM` fixture 殘黨(MCP8 #128 retro 發現)**:`tests/test_query_hybrid_integration.py`
  的 fake LLM class 名與 docstring 仍描述已移除的 LLM 選模行為(「Routes to global +
  semantic」)——實際上 MCP8 後 deps.llm 只有 sql/NLSQL 路徑會碰。行為無誤(fake 從未被
  諮詢),純命名/註解過期。順手時機:下一個動 hybrid 整合測試的任務改名為 `_FakeLLM`
  並修 docstring。
- **MCP session 透明 policy 刷新 transport(2026-07-25, MCP17 #137 r5)**:gateway 的 max-age 逾齡以 404 終止 session,與 SDK idle reaper 同契約——pinned MCP Python client 收 404 不自動重連(保留舊 id、後續全 404),故長跑 agent 的 policy 撤銷需客戶端主動開新 transport。真正透明的「session 到齡自動重連並帶新 policy」需自訂 transport 或 SDK 支援,屬產品層決策(§23 auth round 一併考量)。現況:idle+max-age 皆終止式,DESIGN §9 已誠實記載。
- **gateway routing 的 rebinding 確認洩漏(2026-07-25, MCP17 #137 r11 gate-2)**：未認證 /mcp routing 的 404(not-in-registry)vs child-421(rebinding 拒)狀態差,可讓 DNS-rebinding 頁「確認猜中的專案名」——比已修的 /health 枚舉弱,但屬未認證 §23 routing 固有,唯 gateway 層自帶 rebinding 驗證可閉。留待 §23 auth round 與 gateway 認證一併處理。
- **hybrid 模態併發執行(2026-07-26, MCP18 owner 定案 defer)**:hybrid 現以循序 `for mode in selected:` 跑各模態(端到端實測 3,078ms≈各模態耗時和),模態彼此獨立本可併發——但 semantic/graph/sql 共用單一 asyncpg 連線,同連線併發協程會炸(asyncpg 單連線不可並發),故真併發需 per-mode PG 連線隔離(各自 build-scoped、DR-006 再驗、清理)+改寫釘住循序假設的 §21 deadline/§22 degradation/mode-order 測試(`tests/test_query_hybrid.py` 的 `test_the_whole_call_shares_one_wall_clock_deadline`、`test_auto_planned_graph_runs_at_its_mode_order_position`)。MCP18 owner 定案先落地 (b) embedding 快取、(a) 併發改列 follow-up 待重估(風險 vs 收益)。附:QP1 auto-plan(`distinct_active_entity_names` DB 往返 ~766ms)於模態前循序跑,可與不需 graph_params 的 semantic/sql 併發。核心 `core/query/hybrid.py:185` 循序迴圈;house style 並發用 asyncio(core)/anyio task group(gateway)。
- **query embedding 快取的 gateway 級預算(2026-07-26, MCP18 #138 Codex r1 P1 defer)**:MCP18 (b) 的 embedding LRU 每 query-embedder 實例一份(`embedding_cache_size` 預設 128),`McpGateway._apps` 保留每個已掛載專案的 server+cache 至刪除,故多專案 gateway 的 aggregate=N×size×~100KB/vec(text-embedding-3-large 3072 floats)、無 gateway 級上限。本回合採 Codex 首帖(調小預設+operator 調降旋鈕)已解即時耗盡風險;Codex 次帖「跨 server 共享 byte/entry 預算」會動到 per-instance 隔離模型,與上條併發同屬 gateway-resource follow-up,待 owner 重估時一併設計(shared LRU/global accountant vs 保持 per-instance)。各 cache 已各自 LRU 有界,aggregate 隨 operator 佈建的專案數而非流量成長。
- **QA7 之前刪掉的專案,其 uploads 仍留在磁碟且無端點可見(2026-07-27, QA7 #145 gate-2)**:QA7 讓 DELETE 一併清 `data/uploads/{p}/`,但**修法之前**已刪除的專案留下的目錄仍在——DELETE 對不存在的專案先回 404(`api/routers/projects.py`),清理根本走不到。**現況非破口**:那是不可達的殘留狀態,不是活路徑會再產生的洩漏(新刪除已被涵蓋)。刻意不在 404 路徑上清理:那會變成一個「對任意名稱都能刪目錄」的無界破壞原語。D16 另提過「殘留語料應可經端點看見/移除」,若要做就是新增 inventory/清理端點的獨立任務(契約新增,DR-002 級),不是 QA7 的範圍。
**同條另記(2026-07-27, #145 Codex r2 + gate-2 覆核後**修正**)**:原先記的「recreate 微競態」**已消除,不是縮小**——最終設計在**刪除交易內**把目錄 rename 成唯一 tombstone(`.deleting-*`),此時 `projects.name` 主鍵讓並行 INSERT 必須等待該交易(gate-2 對 live PG 實測:INSERT 阻塞直到交易結束),故重用名字時舊目錄已不在該名字下,清理只碰自己 rename 出來的路徑。**連帶作廢**先前記的「durable cleanup queue 會放大競態」警告——危險來自**以名字定址**,而帶 tombstone **路徑**的 sweeper 是安全的;留著會把未來的人擋在正確設計之外。**仍在的殘留(較窄)**:(a) commit 後 rmtree 失敗→回 204(刪除確實成功,報 500 會謊報)+ 留下一個 inert 的 `.deleting-*`,僅靠 `api/routers/projects.py` 的 warning log 可發現;(b) `_reattach_upload_dir` 在名字已被占用時放棄,故「commit 失敗」與「並行 upload 的 mkdir」對撞時,活專案的位元組會留在 tombstone 而其 corpus 目錄為空——**未毀損、可人工復原**。(c) **硬殺(SIGKILL/worker 終止)落在 rename 與 commit 之間**(#145 Codex r4):rename 已永久生效但交易回滾,專案與其 managed sources 存活、語料卻停在 `.deleting-*` 之下——in-process 的 `except` 依定義救不了硬殺。**設計本身已可對帳**:tombstone 名字即 `.deleting-{project}-{uuid}`,故 sweeper 可逐一解析出專案名並以 Postgres 為準裁決——專案還在→rename 回去(復原);專案不在→刪除(完成清理)。**資料未毀損、可機械復原**,只差那支 sweeper。**症狀(給值班的人,不只給寫 sweeper 的人)**:該專案下一次 build **立刻 loud fail**——`core/ingest/connectors.py:110` 的 `NotADirectoryError: document source root <root> is not a directory` 會指名缺的正是那個路徑,而對應的 `.deleting-<project>-<uuid>` 就在同一層目錄;managed source 的註冊清單雙向權威,故絕不會安靜地少 ingest 幾份文件。另:**重試那次 DELETE 即可收斂**——corpus 目錄已不在原名下,detach 回 None、列刪除照常 commit,狀態退化成上面的殘留 (a)(良性孤兒)。為何仍優於前兩版:files-before-refusal 會在**被拒絕的請求**上毀資料;files-after-commit 有 recreate 競態會**刪掉活專案的語料**;本版最壞情況是「可辨識、可對帳的錯置」。**generation-unique layout 的急迫性因此下降**(安全那半已由結構達成);仍開放的是`.deleting-*` 與 pre-fix 孤兒的 **inventory/sweeper**。

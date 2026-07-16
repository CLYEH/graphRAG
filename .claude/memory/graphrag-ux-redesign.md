---
name: graphrag-ux-redesign
description: "owner 2026-07-14 核准開工:Track 4(UXA1-3/UXB1/UXC1-2)已排入 TASKS.md;Phase C 的 DR-002 契約 bump(eval+upload 端點)同日核准;提案=.discuss/ux-redesign/PROPOSAL.md"
metadata: 
  node_type: memory
  type: project
  originSessionId: a5de076e-8823-47d9-a44a-b3fd7b14df4e
---

**Owner 兩個急件(2026-07-14)**:(1) 前端要能走完完整流程;(2) UX designer 視角
全面重審前端——「目前的前端畫面太過技術了,非技術人員無法操作」。
確認痛點原話:「各式名詞太艱深、沒有足夠上下文讓人判斷、過多對人沒有意義的 id、
操作上需要一些說明」。

**已交付**:`.discuss/ux-redesign/PROPOSAL.md` — 完整 UX 審查(12 張真實資料截圖,
`.discuss/ux-redesign/ux-audit-screens.spec.ts` 可重跑——需先複製到 web/e2e/ 執行)+ 翻新設計 + 路線圖。
鐵證:Review 頁 Candidate 欄顯示 UUID 截斷碼(連名字都沒有);Playground 結果頁
高 17,450px(裸 UUID 溯源牆);Health 數字爆版。

**核心診斷**:工程師的資料庫檢視器 vs 館方知識管理員的工作台——翻譯層缺失
(UUID 永不見人/欄名變人話/狀態變下一步/JSON 摘要化)。

**路線圖(owner 2026-07-14 全數核准,Track 4 已排入 TASKS.md)**:
Phase A=純 FE(UXA1 Review 重設計、UXA2 總覽落地頁+activate 按鈕、UXA3 全站翻譯層);
Phase B=UXB1 設定頁表單(既有 PATCH);Phase C=UXC1 契約 v1.2(**eval+upload 端點,
DR-002 bump 已核准**,§26 記錄隨 UXC1 PR 落地)+UXC2 評測頁/上傳 UI。
主線斷頭現況:config=curl、eval=CLI(無端點)、activate=curl(見 [[graphrag-sample-data-hakeguan]]
的 G5 與 .discuss/hakeguan/GAPS.md)——A2/B1/C 依序補齊。

**UXA1 ✅ merged(PR #76,2026-07-14,37692bb)**:Review 頁重設計上線。
12 輪 Codex/16 P2 全修——新有狀態互動面的狀態機被逐輪枚舉(教訓入
[[graphrag-loop-paused-pr5]] class 20)。最終架構:mutation+凍結+index 全在頁面層,
CaseCard 只留哨兵查詢與 UI;write gate = deciding||scopeFrozen||queueRefreshing||
scopeChecking(全 isFetching 基準)。下一個:UXA2(總覽落地頁+activate 按鈕)。

**UXA2 ✅ merged(PR #77,2026-07-14,3b38536)**:總覽落地頁上線——落地/切換/
建案後全導到總覽;四步 checklist=伺服器狀態投影;activate 按鈕(含更新卡:
嚴格較新的 ready 才算更新,防降級);§14 拒絕從 details.failures 全文呈現+
missing-score 專屬 CLI 提示(`eval --build <id> -- <project>`,shell 三層安全)。
3 輪 Codex/4 P2+2 reviewer blocker(教訓入 class 21:mock 對齊真實錯誤信封)。
下一個:UXA3(全站翻譯層 sweep)。

**UXA3 ✅ merged(PR #78,2026-07-15,c329850)——Phase A 完成**:全站翻譯層上線。
9 頁 chrome 無裸 UUID/snake_case(chromeInvariant.test 機械 pin:TreeWalker 掃
文字節點、排除 <details> 祖先);原始識別碼只住兩處=hover title 與「進階」folds;
前綴洩漏(id.slice(0,8))由 per-component 測試另層 pin。3+1 輪 Codex/4 P2
(詞元數、建立者、review link gate 可行動分量、drift 診斷剪裁)+本地 reviewer
首輪 FAIL(oracle 未蓋的 3 頁全漏 UUID)——教訓入 [[graphrag-loop-paused-pr5]]
class 22(翻譯即斷言/oracle 閉合路由表)。下一個:UXB1 設定頁表單。

**UXB1 ✅ merged(PR #79,2026-07-15,cd2bf4e)——Phase B 完成**:設定頁上線——
ontology/chunking/query_policy 三塊 config 做成表單、蓋在既有 PATCH 上。
**10 輪 Codex 全 P2(UXA 之後最貴的 FE 任務)**:主病根=PATCH 對 config **什麼都不驗**,
每個子塊各自晚炸(build config-load / query 400),表單的 client 有效性鏡像是唯一飛行前
守衛——手寫/畸形塊 save「成功」而 build/query 持續拒=**靜默磚**。三塊各一輪硬化
(policy R1/R2、ontology R4、chunking R8):client 鏡像 pin 到真 validator(雙 gate 共用
corpus)、missing/malformed=未存狀態故修復免 dummy edit、salvage 不得產 validator 拒的值、
未動欄位從 fresh read 導出(class 17)。R9=跨專案抽屜滲漏(`key={project}` remount)、
R10=同頁併發存丟更新(`useIsMutating` 頁級鎖;跨寫者仍是版本 token 缺口 DR-002)。
教訓入 [[graphrag-loop-paused-pr5]] class 23。下一站:Phase C UXC1——契約 v1.2
(eval+upload 端點,DR-002 bump 已核准,§26 記錄隨 PR 落地),改凍結契約=先確認 owner
仍要這條路後開工。

**2026-07-15 — 兩份文件化作 TASKS(owner 指示)**:讀 `.discuss/hakeguan/GAPS.md` + 外部功能
評估 `.discuss/codex_review/FUNCTIONAL_REVIEW_2026-07-15.md`,經 owner 拍板轉成任務。三個岔路
owner 定案:**(1) UXC1 拆成 UXC1a(契約 v1.2:eval+upload+通用文件 metadata envelope)/UXC1b
(stage-1 metadata 傳遞)**——review §P0#4 只堅持「upload 契約要能帶 metadata」,故 envelope 語意
(system/context/governance 三 namespace + 專案自訂 schema + exposure allowlist,安全邊界設計進契約)
進 UXC1a,實作只做 capture→persist→snapshot→enrich-on-read;下游(chunk-local/搜尋索引/embedding/
自動抽取/entity 升級)進路線圖。**(2) 答案合成定調 retrieval-first**(不做合成,「問答測試→檢索測試」
改名併進 UXC2)。**(3) P2 用 A 方案**(近期切片寫成正式任務、P2 大包只留詳細路線圖註記,不拆任務——
「進佇列≠會做,完整度靠執行不靠佇列長度;通用/維運最不能靠大量淺任務」)。**新增 Track 5 功能完整化與
長期維運**:CFG1(⚠️統一 Console/MCP config SoR,推翻 07-10 決策,待確認)/SRC1(xlsx connector)/
SRC2(⚠️來源生命週期,DR-002,待確認)/GOV1(型別漂移根病:跨型別 resolve)/GOV2(治理中心)/
GOV3(ontology proposal 審核)/GOV4(merge Filter fail-loud)/QP1(NL→自動 graph plan)/SS1(server-side
搜尋)/SS2(引用卡片 resolve,修 UXA3 過度宣稱)/RB1(retry lineage)。GAPS 對照表已回填帳本頂。
**驗證發現**:UXA3 checkoff 宣稱 source_refs 成 resolved cards,實際 `QueryResults.tsx:94-108` 只折疊沒
解析名字(→SS2);Console/MCP config 雙源是 07-10 刻意設計(→CFG1 需再確認才動)。

**UXC1a ✅ merged(PR #80,2026-07-15,7e85b13)——Phase C 起點**:契約 v1.2 上線——
`contracts/openapi.yaml` info.version 1.1→1.2,新增 2 路徑(`POST /projects/{project}/uploads`
uploadDocuments、`POST …/builds/{build_id}/eval` runBuildEval)+ 通用文件 metadata envelope
(system server-owned/immutable、context 封閉核心 `{title, document_type}`+開放 `attributes` 袋、
governance、schema_version;input 只收 context/governance,exposure allowlist 為 runtime 策略走
query_policy 前例)。DR-010 記入 DESIGN §26。**純契約層(0 runtime)**:mcp_response.schema.json
不動(SourceRef.metadata 已寬鬆),web/src/api/schema.ts 每輪機械重生(`npm --prefix web run gen:api`)。
**5 輪 Codex(契約 5 面各一輪)**:主病根=保證寫進散文卻沒結構凍結——manifest 缺 `minItems:1`
(可表示「全部靜默丟棄」)、rejected 缺 `required:[reason]+minLength`、envelope component 定義卻沒
`$ref`(dead)、`original_filename` optional 使 per-file 關聯失效、context prose 說 project-defined 與
`additionalProperties:false` 矛盾。教訓入 [[graphrag-loop-paused-pr5]] **class 24**(散文承諾必須結構凍結;
已把「guarantee→結構 pin」矩陣寫進 code-reviewer.md §8)。**UXC2 follow-up(Codex reply-resolve)**:
`format:binary` 在 openapi-typescript 出 `string[]`,FE `File`/`Blob` 對不上=消費端 job,UXC2 需 gen
transform 或 wrapper(runtime openapi-fetch 送 FormData 不看編譯型別,契約語意正確)。
**下一站:UXC1b**——後端實作三端點 + stage-1 metadata 傳遞(capture→validate→persist(documents.metadata)
→snapshot 入 build→讀時 enrich source_ref.metadata(chunk→document 走 Postgres SoR)→經 allowlist 曝露);
integration test 驅 upload→build→eval→activate,metadata 跨三個無關文件情境驗證,無 meeting-specific 路徑。

**UXC1b ✅ merged(PR #81,2026-07-16,2822458)**:後端三端點 + stage-1 metadata 傳遞上線
(upload multipart→受管 file:// 來源、eval async job(接受時 pin 輸入指紋+dispatch 驗漂移+
讀一次共用位元組)、metadata capture→persist→snapshot→enrich-on-read→allowlist)。
**29 輪 Codex/45 threads/35 triage——史上最貴的已合併 PR**(超越 C6b 19 輪):三個新面捆一包
互乘了未掃格。~10 輪全在 eval handler 的寫入授權(lease/終態/取消/handoff),T27 加的漂移
守衛自身在 T35 被抓 TOCTOU。教訓入 [[graphrag-loop-paused-pr5]] **class 25**(寫入授權矩陣=
class 17 寫入側對偶;修法即新面;跨 ≥2 新面的任務計畫期就切片)——防線已入 code-reviewer §7。
**下一站:UXC2**——評測頁+上傳 UI+檢索改名(需 UXC1b✅);記得帶上 UXC1a 的 format:binary
codegen follow-up(FE `File`/`Blob` wrapper)。

**UXC2 切成 2a/2b/2c(docs 6721a64,class-25 切片先例)。UXC2a ✅ merged(PR #82,
2026-07-16,bba0321)**:品質頁上線——golden-set 評測(UXC1b 端點+job SSE+逐題
通過/未過表+終態 invalidate ["builds"] 餵總覽 step ③ 同讀取);總覽三處 CLI 交接改連
品質頁(帶 ?build=<id>)。**2 輪 Codex 3 P2 + 本地 reviewer 連續 2 FAIL 推翻我的修法**
(streamTerminal 殘留事件回歸+revert-probe 假綠)——教訓入 [[graphrag-loop-paused-pr5]]
**class 26**(隱含預設不跨時間與交接;殘留狀態 gate 身分;invalidation 探針 stub=世界
模型+mutation 實證;probe 還原用 temp copy 勿 git checkout)。真瀏覽器 QA:museum 真實
eval 3/3 通過、step ③ 自動翻綠。**下一站:UXC2b 上傳 UI(資料 tab)+ format:binary
codegen follow-up;之後 UXC2c 檢索改名+全流程 e2e。**

**UXC2b ✅ merged(PR #83,2026-07-17,f6c767a)**:匯入頁上傳區上線——拖放/多檔
multipart(FormData cast=format:binary follow-up 落地)、逐檔誠實 manifest(已接受/
已退回+拒因逐字)、受管來源自動入列、必填 metadata 逐檔輸入(產生用鏡像+留空省略鍵)、
config 未知 fail-closed、metadata 編輯輪換冪等鍵。3 輪 Codex 各 1 P2,無新 class——
三個 finding 全是「同頁 sibling 已有紀律、新 section 沒繼承」(gatesLoaded 在隔壁
component、欄位編輯重鑄鍵在上方表單)+ config-driven 變體沒枚舉(required-schema
專案=保證被拒死路)。詳 [[graphrag-loop-paused-pr5]] #83 條。真瀏覽器 QA:museum
真實上傳 .txt 落盤+.exe 逐字拒因。**下一站:UXC2c 檢索改名+全流程 e2e(Phase C 收尾)。**

**UXC2c ✅ merged(PR #84,2026-07-17,a1b79c0)——Track 4 全數完成**:「問答測試」→
「檢索測試」、nav「問答」→「檢索」(Playground 面;Settings「問答安全」=刻意邊界,
含 Settings.tsx:101「下一次問答」一句,待 owner 決定是否延伸);capstone e2e=完整
無終端機旅程(建專案→上傳→設定 ontology→建置→評測→上線→檢索,world-state stub,
旗標掛在 job 被觀測到終態的邊界上)。2 輪 Codex 各 1 P3:1 修(class-26 精煉:世界
旗標掛觀測邊界非觸發點)、1 以執行證據 decline(playwright glob 語意——讀源碼+實測
regex,詳 [[graphrag-loop-paused-pr5]] #84)。**Console UX 翻新(UXA1-3/UXB1/UXC1a-b/
UXC2a-c)九項全 merge;下一步=Track 5(SRC1 xlsx connector 為首個非 ⚠️ 項;CFG1/SRC2
待 owner 確認)。**


---
name: graphrag-lesson-classes
description: Lesson-class 目錄 + 常備規則 + owner 定案(LOOP step 8 retro 的比對基準)——即時狀態一律查 TASKS.md/GitHub;per-PR 全文敘事已於 2026-07-20 裁剪,史料在 git history(前身 graphrag-loop-paused-pr5.md)
metadata:
  node_type: memory
  type: project
  originSessionId: d673e708-e836-4b8a-8fc7-cb33527c5fc3
  modified: 2026-07-21T02:09:53.583Z
---

# 使用方式(step 8 / prep 協定)

- **即時狀態**(PR、任務、額度)一律查 `TASKS.md` 與 GitHub;本檔只留跨任務可重用的教訓。
- **開工前(prep)**:按任務型別讀命中的 class(各 class 開頭有「何時比對」)。教訓存在≠教訓被套用——catalog 是開工前讀物,不是事後對照(#74 以身試法:class 17 原文預言了那顆 bug,我照樣寫了事件計數器)。接手他人/舊 session 的主張先查證再動工,一半是過時的(#87);接手 stale summary 時以 repo 現況為準(#67)。
- **step 8 retro**:每個 finding 先比對 class 目錄──命中=在該 class 追加一行 sub-point(附 PR 錨點);未命中=開新 class(**編號穩定,只增不改號**)。**不再寫 per-PR 敘事段**(2026-07-20 裁剪定案);輪次經濟學、任務史、路線圖一律不進本檔。新 class 若可機械化,**優先立 H-task 做成 hook/CI/lint/共用 helper**(mechanize-first,owner 2026-07-20 方向),prose 是最後手段。
- 每個 class 標注機械化狀態:`[已機械化:<機制>]`／`[可機械化→H<n>]`／`[pattern]`(可抽共用 helper/測試模板)／`[prose]`(不可約簡的判斷)。

# 常備規則(S 系列)

- **S1 狀態警語**:本檔任何「目前狀態」敘述在下次 push 後即過期;只信 TASKS.md/GitHub。
- **S2 Codex 三管道**:「changes wanted」走 `pulls/N/reviews`(inline threads),不是 issue comments;`+1` 是 issue reaction;**另有乾淨 comment(「no major issues」)也算過**(#39)——兩種過關格式,乾淨 review 後絕不再 poke。[已機械化:watch-codex.sh(exit 0/10/20/30、--anchor)+ require-codex-approval.sh]
- **S3 壓輪次**:Codex 一輪傾向只給一條;收到任何 finding,主動掃整個 diff 的同類問題一次修完(#33:模板/參數類 sibling 同輪掃)。**升級對策(owner 2026-07-20,#112 實證)**:遠端連續擠牙膏時改用本地 codex-rescue 對全 diff 批次審查(一輪 8-10 條,相當遠端 8-10 輪),修完批次、理想上本地審到乾淨才回去 poke 遠端。[部分機械化:codex-rescue 批次審查]
- **S4 review race**:push 後 thread 可能仍指舊 head;先對現 head 驗證 thread 內容再 triage;已修者回覆指向 commit 並 resolve。[部分機械化:merge gate 要求 +1 晚於 head commit]
- **S5 doc-only fast lane**:純 `*.md` → `docs/<id>` → doc-reviewer PASS 蓋章 → push → CI 綠 → ff main;**鐵序:CI 綠 → push main → 才刪分支**(先刪=孤兒 commit+CI 取消,#88)。並行注意:doc lane 先落地會讓等待中 PR 需 rebase/新 +1——快 +1 的 PR 前後,doc push 要排序(PR #15)。[已機械化:push-gate hook 雙 lane]
- **S6 ops 衛生**:背景鏈第一條指令必是 `cd <絕對 repo 根>`(繼承 cwd 曾整鏈靜默死,#92;bash cwd 會漂,#84);GitHub 分支改名會**關閉** open PR——先改名再開 PR,分支名須等於 TASKS.md `<id>`(#23);pipeline 出口碼:`cmd | tail` 回傳 tail 的碼、被 kill 的 pytest 讓鏈繼續——先擷取出口碼再接管線(#73);寫非 ASCII 經任何工具層後,讀回**檔案位元組**驗證(heredoc 會毀 backslash/全半形,#77/#85);vitest full-suite fan-out 餓死 timing-sensitive 測試(隔離綠/全套紅/CI 綠,#112)——durable fix 已落地(H21:vite.config.ts `maxWorkers: min(4, max(cores-1,1))`;`VITEST_MAX_WORKERS` 不再需要,且 >4 的覆寫會被 viteConfig.test.ts pin 響亮擋下——env var 是 config 之後才套用的旁路);「跨首次 settle 的 waitFor」要顯式 timeout(規則在 fe.md checklist);git-bash 的 /tmp 與 Windows python 的 /tmp 是不同目錄——跨工具傳檔用絕對路徑(#112 差點掉了 980 行測試檔,git HEAD 救回)。[prose/pattern]
- **S7 TASKS.md 勾稽**:任務勾稽隨該任務 PR;**triage 改了設計就同步改已勾稽任務的驗收文字**(任務帳=呈現面,#105)。[部分機械化:governance-check.sh 勾稽 lint]
- **S8 receipt 樹衛生**:content-addressed receipt 覆蓋 tracked+untracked;雜物(test-results/ 等)會讓章失效——root .gitignore 收乾淨(#75);**註解/純文字修改也改 tree**,PASS 後任何編輯都要重章(#106)。[已機械化:push-gate 精確樹比對 + H15 gates-receipt]
- **S9 額度逃生閥**:Codex 額度用盡且 gates 全綠+小幅已驗證 delta 時,由 **owner 本人**決定是否親自 merge——是 owner 決策,不是 agent 例外。

# Owner 定案(重寫/裁剪時不可遺失)

- **2026-07-03 merge 授權**:agent 可自行 `gh pr merge`;誤 merge 由 PreToolUse hook 機械防止(+1 晚於 head、無 eyes 殘留、無未解 thread、查詢失敗即擋);hook 擋下時不得繞過;無 +1 路徑一律 stop-and-ask。
- **2026-07-03 Codex P2/P3 判讀政策**(H2 已寫入 LOOP step 7,以該文為準):必要=違反凍結保證/矛盾/誤導/真 bug;不必要→附可查證逐項理由 reply-and-resolve;說不清楚=修;模糊=stop and ask。
- **2026-07-03 post-merge retro + retro routing**(LOOP step 8):doc-only follow-up 走 fast lane;機械強制走 H-task。
- **CI-first**(#23 起;canonical 在 [[graphrag-ci-before-codex]]):push 後先修 CI 再 triage Codex;eyes 在看=絕不 poke。
- **收斂指令**(#39,常備):輪次拉長時,逐條判 must-fix vs reply-and-resolve、劃清任務邊界,不追角落案例。
- **連線所有權**(#32 R8):per-phase transactions、loaned-clean。
- **2026-07-12 H10 DROP**:browser-QA 收據 hook 全案撤銷(±24 輪後 owner 砍);browser-QA=honest-agent 紀律(證據進 PR body,owner/Codex 抽查),不做 hook。
- **2026-07-13 fresh-state-before-write 協議**:對 PR 的每個寫入(comment/resolve/poke/merge)前,先以腳本重查現況、條件仍成立才送——用程式判斷,不用 LLM 讀感。
- **契約散文為裁決基準**(#98):gate-2 與 Codex 對「是否 bug」不合時,凍結契約的 prose(DR-002 SoR)是 tiebreaker。
- **GovernanceBacklog 僅供參考**(#107):品質治理待辦不擋上線。
- **2026-07-20 mechanize-first + catalog 裁剪**:lesson 能機械化就機械化(H20 清查);catalog 不再累積 per-PR 敘事。
- **2026-07-20 主動回報**:等待/長流程期間每次查完狀態要主動回報進度給 owner,不悶著查;Codex 擠牙膏時先本地批次審查(見 S3)。

# Lesson classes(step 8 比對目錄;編號=歷史原編號,只增不改)

**Class 1 — 契約/DDL 驗值**(何時比對:動 schema、DDL、任何驗證器)[可機械化→H20:DDL 全稱測試 + 契約 lint]
Schema/DDL 只驗結構不驗值=漏洞:每欄位要 type/range/length/non-empty;no-op 值(false/0/空/缺鍵/顯式 null/空白)要禁或給語意;跨欄位矛盾對要枚舉;「schema 表達不了」須以嘗試證明。CHECK 從例外側寫;IFF 要顯式雙分支(合取弱化假分支;2^n 角落展開,#27);寬鬆/嚴格邊界判準=「這層的 key 是資料還是 schema」——自由格式邊界容忍、封閉巢狀 schema 拒未知鍵(#47)。信任邊界是 per-COLUMN 不是 per-store(SoR 裡無約束的 ref 欄=不可信,先落地再發射);協定極限(PG 32767 binds)也是值域(#35)。門控狀態變更的 boolean 要 `strict=True`("false" 字串須 400,#95)。不可信輸入做值樹掃描:每葉 ×{缺、錯型、空、空白、詞彙外、找不到}一次掃完;join/dedup 鍵須用 store 自己的身分函數、拼接鍵要無損編碼(#25、#23)。同一設定被兩個消費層讀=值域**按層分**:`mcp_http_port=0` 對 bind 合法(OS 挑臨時埠)、對廣告 URL 非法(`:0` 無人可撥)——驗證放各消費層(display resolver fail-loud),絕不放 Settings Field(把兩層值域的交集強加全層=禁掉合法 bind)(#113)。**全稱測試**(「每個子表 FK 都有索引」)必須從 information_schema/目錄枚舉實例,否則假綠(#17)。migration 收緊要在同 alembic txn 調和既存列;fresh-DB CI 對此結構性盲——測法=downgrade→塞舊資料→upgrade(#45、#27);retrofit 父表三面:孤兒清理+backfill+寫入 TOCTOU/FK(#42)。emission 側 nullability:optional 非 nullable 欄位以「省鍵」編碼 NULL;矩陣雙面(request 解析 × response 發射)× 每欄位(#55)。開放/封閉詞彙判準=DDL 有無 CHECK,政策跟著 SoR 走(#92)。

**Class 2 — 入口一致性**(何時比對:改規則/改名/加成員/加錯誤)[部分機械化→H20(d) stale-claims lint;餘 H17 checklist]
規則改了但舊措辭活在其他入口——grep 全部 tracked *.md;凍結集合加成員→grep 既有成員名;雙詞彙→兩者都收斂;新領域錯誤→掃所有 consumer 的翻譯層。行為變更掃**行為 pin**(斷言常數),不只 prose(#89);退役假 affordance/警語→grep 它所有實例(標頭註解、inline、UI 文案,#101);絕不重抄凍結 schema 欄位清單——引用 schema(#53)。UI 可見字串改名必掃 `web/e2e/*.spec.ts`(Playwright 在本地 check-all/push gate 之外;CI 僅 web-touching PR 的非 required `e2e` job 跑它——H18,#104)。改清單排序一次連動五件事:order × keyset × cursor × sort-affordance × index(#99)。

**Class 3 — 規則自洽 × 單一來源**(何時比對:寫 meta-rule、重用既有概念)[prose]
Meta-rule 不得自相矛盾——驗證每個被允許的分支都能產出規則要求的證據;業務規則單一來源。寫服務既有概念的查詢前,先 grep 既定述詞(別重新發明佇列定義,#59)。新建 diff/path gate 前先讀在庫 sibling gate(push gate、governance-check)的旗標與模式——#111 兩輪 Codex(rename 摺疊要 `--no-renames`、capture-first 防 SIGPIPE)全是 sibling 已解教訓的重導輪。

**Class 4 — 工具語意實測**(何時比對:依賴 CLI/API/庫的邊緣行為)[prose;宣稱須執行驗證]
rename folding、分頁預設、text≠value、locale、statement_timeout 範圍、accessible-name 計算——先實測再依賴。對「工具預設值」的宣稱同樣要對**安裝的 dist** 驗證:vitest maxWorkers 預設是 `max(cores-1,1)` 非 per-core,「CI 不受影響」的推理錯了兩輪、修正措辭又寫回舊詞一輪(#114;本地 codex 直接 rg 進 node_modules 查實作裁決)。時鐘**穩定度等級**(txn/statement/call)是正確性契約的一部分:PG `now()` 是 transaction-stable,逐決策序要 `clock_timestamp()`(#59);排序時戳單一時鐘源,絕不混 app clock(#38)。asyncio.timeout 只在 await 邊界搶佔——純 CPU 掃描要注入檢查點(#89);邊界規則按書寫系統分派(Latin 詞界/CJK 包含;isalnum 對 CJK 誤判,#89)。「anchor」的同秒 tie 語意分兩種:anchor 是對話方=回覆(未處理);anchor 是事件自身時戳=自己(已處理)(#29/#90)。截斷識別字只對 opaque 定寬域安全;複合/變長 id 顯示全長(#69)。FastAPI sync-def dep 走 threadpool→check-then-set 不原子,用 async def(#57)。

**Class 5 — 檢查者/消費者分岔**(何時比對:寫任何「檢查」「鏡像」「fixture」)[pattern:共用述詞/共用語料]
Checker 探測的≠consumer 實際做的——checker 參數從 consumer 原始碼導出;資料交接完整性(skip 分支掉交接,#22);list-vs-detail 欄位、fresh-DB CI vs 有料 DB。定義重用要掃**每一軸**(述詞、scope、排序——統一一軸≠統一定義,#62);共用 helper 擁有不變量,surface 綁定的教訓不會轉移到晚生 surface(#60);指向環境的 fixture 要**自我驗證**(斷言解析後設定真指向宣稱處,#91);fixture 須供給被檢查的性質(#35);rowcount 當正確性輸入→兩端都驗(n 與 0,#100);多型契約值的 consumer 先讀**所有** producer 的發射面(#88);hermetic 假件按 prompt 前綴分派、skip-only 契約證明(CI 無鑰綠+本地真跑,#49;兩 lane 紀律見 [[graphrag-ba-real-llm]]);api 端點與 CLI 警告要對同一問題(廣告位址)給同一答案→共用 resolver 抽到 core(#113:CLI 曾自算並報錯位址);config-value pin 探測的是**原始 config 文本**、consumer 吃的是**解析後值**——分岔面要逐一補 guard:truthiness 解析(0=缺席=退回預設)、env var 在 config 解析後無條件覆寫(pin 恆綠、pool 卻跑 16)、空字串 env=未設(guard 要鏡射 runtime 真值判定,否則誤紅)(#114)。

**Class 6 — spec/實作逐字對齊**(何時比對:實作 DESIGN/契約條文、裁判/評分面)[部分機械化:lockstep 契約測試]
凍結 spec 與實作逐字互查;versioned 凍結物變更⇒bump;DESIGN vs docstring 並排 diff。裁判/評分 surface 先寫完整語意 spec(身分模型、雙 store 一致性、退化、可比性)再動工(#39)。

**Class 7 — 執行級驗證**(何時比對:infra shell/hook/腳本、任何「機制」類產出)[pattern:執行級測試(test_receipts.py 模式)]
純推理 review 連過兩輪沒發現收據機制根本跑不動(git 拒零位元 index)——infra 腳本必須「跑起來」才算 review 過,並以執行級測試伴隨(subprocess 真跑 bash/hook);工具碼與產品碼受同等 class 1/8 審查(#29)。(#37 R2、#84)

**Class 8 — 邊界語意 × 表示誤差**(何時比對:threshold、浮點、JSONB null)[pattern:對抗值 fixture 套件 + property-based(H4)]
精確門檻被浮點表示破壞;Python None→JSONB 是 `'null'` 字面量不是 SQL NULL(`none_as_null=True`/`sa.null()`,#43);結構性 CHECK 能抓 ORM 表示 bug 而行為測試抓不到;fake-conn 測試繞過 SQL ORDER BY=假綠(#40)。對抗性不可表示值+顯式容差。

**Class 9 — 防護面完整性 × 過度阻擋**(何時比對:寫任何 deny/guard)[部分機械化:reviewer routing→checklists/guards.md]
Deny-guard 的面有 sibling 建構子 × 巢狀深度——全枚舉;每條 deny 配正向 accept pin;以值/字串判別不以型別。讀側:每個投影→輸出值「驗證於 SoR 否則 DROP」(不 raise),drop 計入 PARTIAL_RESULTS(#31);不可信選擇輸出 partial-valid=whole-fail(整答案 breadth-first fallback,#36)。allowlist 參數 guard 讓未來 facet 免 bump 即可加(#87);公共 core seam 自我強制簽名的 scope 參數(「unused param」warning=洩漏訊號,#99);對抗性威脅模型決定值域大小,並在 review 中點名該威脅模型(#21)。

**Class 10 — 綁定時檢查 ≠ 不變量(TOCTOU)**(何時比對:多 commit 編排、鎖、並發 admin 面)[pattern:in-window 探針]
綁定時驗證不保之後——recheck 摺進變更語句或持對鎖;交錯在真 infra 上測;**每個 commit 邊界都是 crash/race 窗**——逐一問「這裡崩了/並發落地會怎樣」(#46)。鎖測試要探**保護窗內**(monkeypatch 暫停中途)——完成後探測被偶發鎖遮蔽(#44);txn-ownership seam 模式(`*_in_caller_txn`:preflight+lock+promotion 同 caller txn,#63);compose-in-one-txn 端點三序:廉價 guard 先於昂貴工作、replay 先於存在性 gate、強鎖先於弱鎖(#100);replay/衝突判定先於任何 scope-row precheck(scope 列合法消失而 key 活著,#53);state-machine × 並發面開工前做 operation × state × interleaving 矩陣(#38)。

**Class 11 — 請求級不變量掃完整生命週期**(何時比對:cap/budget/idempotency 跨段)[prose]
Per-segment cap≠request 級不變量——枚舉生命週期每段,交接剩餘預算;圍籬面的旁路入口要帶同強度能力證明(ActiveBinding/InitVar-sealed,#37)。

**Class 12 — 框架生命週期 × 自有 SoR/liveness**(何時比對:任何 framework dispatch/retry/timeout/streaming 任務)[pattern:lease/reaper 原語已抽出]
先讀框架原始碼列「出口清單」再蓋上去;liveness 自己擁有(DB lease+reaper),框架 timeout 只當鬆後盾;恢復通道自身是生命週期(per-generation 決定性 id、key 保留、索引掃描);config 快照 pin 在 **job 建立**(INSERT 內 scalar subquery),不是首次 dispatch(佇列延遲窗,#51);yield-dep 活到 response 完成——streaming 抱著 txn 一路(#54);best-effort 背景工作(heartbeat/cleanup)例外經 finally 的 await 遮蔽主結果——除 cancellation 外全部收容(#50);啟用新模式/transport→重審既有碼的模式相依假設(per-session lifespan 覆寫 module slot,#58);框架錯誤路徑窮舉(handler 優先序、隱藏序列化失敗、side-channel headers,#41);anyio cancel scope 綁 task——host-task-per-child(同 task 進出 lifespan);startup.complete=綠燈語意(資源就緒才送,#93)。

**Class 13 — eager dependency acquisition**(何時比對:DI provider、資源工廠)[pattern:must-not-acquire 測試]
DI 解析先於 handler 邏輯——provider 解析期零 I/O;資源只在需要它的分支內取;以 raising-provider + must-not-acquire 測試 pin。

**Class 14 — 錯層/無界表面**(何時比對:守任意字串→受限 sink;lint 要「判定」任意語言/schema 時)[prose;drop 判準=owner call]
**#112 決定性實證**:lint 判「任意 JSON Schema 是否接納物件」=無界 denylist——22 遠端輪+2 本地批次共 18 條 finding 不收斂(否定/動態 scope/條件代數/恰一組合學…每修一批冒一批);答案=反轉為**嚴格 allowlist recognizer**(只認契約實用子集(實測僅 12 關鍵字),子集外一律 fail-loud「先擴充再用」)——構造上無沉默路徑,重構後 2 輪內收斂。判準:發現自己在為 judging 邏輯逐案補語意特判時,就是該反轉的訊號。
**#113 第二實證(bind/位址)**:wildcard bind 廣告修法首版又用拼法列舉(`_WILDCARD_BINDS` + `":" in host` 代理)——gate-2 重現未列舉拼法(`0:0:0:0:0:0:0:0`)照樣廣告、`[::1]` 被二次加括號;改以**值自身性質**判定(`ipaddress` 解析、`is_unspecified` 蓋全部拼法、`isinstance(IPv6Address)` 取代冒號代理)後一次收斂。同一判準的第二種措辭:「在列舉一個值的拼法」=該改問「這個值的性質」。
無界面(bash×git 文法、任意字串)枚舉是錯的工具——移到「已解析」seam(有限 allowlist>無界 denylist);不是每個面都有下游 seam(誠實殘量);為 interpreter 步驟建模,不列舉拼寫;知道何時整個機制該 DROP(H10)。無界值→受限 sink:第一輪就用 opaque 全編碼(base64url),絕不逐 hazard 轉義(#65);多 sink × decode 所有權:自己 decode→encode、server decode→拒絕(#66);dossier 裡「庫會處理」的編碼宣稱必須執行驗證(探真 URL/bytes,#66)。

**Class 15 — 「等價/已處理」紅旗 × gate 取捨 × decline 證據**(何時比對:想寫「server 已處理」「兩視圖收斂」時;decline Codex 意見時)[prose]
「兩導出視圖收斂」「server 已處理」是紅旗;client 鏡像 gate 只在失敗 LATE 或 SILENT 時建——INSTANT+LOUD+零成本失敗是被認可的 UX;decline 要可查證且獨立驗證──**執行過的證據**(讀已裝庫源碼+跑起來貼結果),對決直覺是羅生門(#84);反駁誤報用可查證證據鏈:源碼+既有判別測試+docstring(#93)。

**Class 16 — 有界表面的收斂法 × 雙 gate 同集**(何時比對:兩個 gate/suite 該接受同一集合時)[pattern:共用語料 fixture(canonical_file_uri.json 模式)]
證明面有界=讀 CONSUMER 源碼列其結構決策;兩 gate 須收同集——唯共用語料雙 suite 同讀可證;純 differential 對共有缺陷盲→加 display==read oracle;絕不弱化自己的 oracle 讓它閉嘴;唯一合法 gate 不對稱=只有 worker 知道的知識,且須朝安全方向 fail loud。最強修法=**只留一個 gate**(公共 wrapper+lazy import 重用;parity 語料仍可能漏角,#97)。

**Class 17 — 快取信任述詞 × 決策面四軸**(何時比對:任何 FE 快取渲染、審核/決策 UI)[可機械化→H20:useDecisionLock 共用 hook]
快取渲染的正確性=「此快取仍由已 settle、成功、同 scope 的答案背書嗎」——先寫述詞,枚舉全矩陣(fetch kind × in-flight/settled × success/error × scope 存亡 × consumer);additive 詞彙→allowlist;述詞算一次全頁共用。決策面四軸:**鎖述詞**(=clean-settled-load 的否定:`isPending || isFetching || isError`——error 絕不解鎖安全 gate,配重試,#106)×**gate 入口**(inline 動作與 confirm 按鈕都要)×**key 粒度**(見下)×**retry 語意**(retry-the-goal=全 refetch 重導 inputs;retry-the-input=重放 stale cursor+pin,#108)。idem-key 三型:decided-once→決定性 `${id}:${verb}`;可循環再決→每邏輯操作一把(首試鑄造、失敗保留、成功清除);**絕不** per-click、絕不跨循環決定性(#68/#108);re-mint 觸發集=server 請求雜湊的完整輸入清單(#83)。debounce 標籤/計數 key 在 APPLIED 值不是原始輸入(#101);react-query 分頁渲染 key 在 data 存在性不是 isSuccess、初載與下一頁錯誤分開(#102);隱藏失敗警告要無條件(#102);list DTO 刻意省 detail 欄→detail 惰性載入;修一個 stale-race→grep 同 hook 家族所有 sibling 元件(#106);決策 UI 必須讓操作者**看得到**決策對象,每個決策入口 × 每個資料未就緒態都 gate(#106);「後端可重複」不豁免 UI 確認——若該可重複性在 UI 不可達(#105);新終局決策 UI 沿用 sibling 流程的確認步(#104);轉瞬回饋徽章(「已複製」)是**關於某值的事實**——存 `{value, outcome}`、渲染以 `outcome.value === 現值` 比對導出,不用 effect 重置:refetch 原地換值與 click→promise-resolve 競態同一機制收掉(#113 R4)。

**Class 18 — 契約物件的投影疊層**(何時比對:動 openapi→codegen→TS 消費鏈)[部分機械化:typetest `@ts-expect-error` + strict-flag tripwire]
一個 schema 活在五面(schema 文本/runtime 驗證器/生成 client 型別/型別指派語意/compiler flags)——各自驗證各自 pin;codegen 同 PR 內 regen。

**Class 19 — 多 query 頁面的導出 DAG**(何時比對:N query 的 FE 頁面)[prose]
N 個 query 的正確性=其蘊含圖——哪個失敗證明其他快取已死(頁級裁決)vs 局部事實 vs 導出值依賴;動工前畫 DAG 逐邊裁決;判別 pin 構造成「被測 gate 是唯一能擋的」。

**Class 20 — 寫入生命週期宿主 × keyed 子元件瞬時卸載**(何時比對:FE mutation 回呼放哪)[pattern:頁級宿主]
Mutation 回呼綁在會瞬時 re-key/unmount 的元件上=靜默蒸發——完整寫入生命週期(arm/disarm/advance/error/pending)住在不卸載層(頁);sentinel 狀態機先枚舉完;**一個 useMutation observer 只追一個 mutation**——並發 `mutate()` 使前一個脫鉤(per-row 回呼不可靠;全池 isPending 鎖或 `mutateAsync().finally()`,#104);in-flight 清理測試讓 mutation 兩向 settle(#104);新功能面要枚舉 config 驅動的變體(no-schema/optional/REQUIRED),同頁 sibling 元件的 props 先掃(#83)。

**Class 21 — mock 對齊真實錯誤信封**(何時比對:mock 錯誤處理)[pattern:自整合測試取信封 fixture]
先讀 server/整合測試的實際信封再 mock;可複製 CLI 提示是三層安全問題(quoting/`--`/argparse)。

**Class 22 — 翻譯即斷言 × oracle 閉合路由表**(何時比對:顯示層翻譯、聚合 gauge、面板標題)[prose]
翻譯 label 即斷言語意/單位——讀 producer;同形異義碰撞;聚合 gauge≠可行動計數,「X 佇列」的分母=spec 整個狀態機(#40);不變量 oracle 閉合全路由表(抽樣=假綠);會 crash 的檢查不是 gate(`&&`/`set -e` fail-closed);**面板標題/框架/元件名=scope 斷言**——逐計數讀 producer 確認真 scope,混 scope 分組誠實標題、空組不渲染(#107)。

**Class 23 — 寫入端不驗證的靜默磚化**(何時比對:寫 config/自由 JSONB 的 UI)[pattern:per-block 四硬化模板]
寫入端點不驗證時,每個 config 子塊在不同的「之後」各自炸——client 鏡像是唯一 preflight;每塊四硬化:鏡像 pin 到真 server 驗證器(共用語料)、缺/壞塊=unsaved 態、salvage 不產驗證器拒絕值、未觸欄位取自 fresh read(缺=尊重並發刪除)。跨身分 keyed remount;同頁並發 save→頁級鎖(≠跨寫者 version token——DR-002 缺口在帳上)。

**Class 24 — 散文承諾由 schema 結構強制**(何時比對:動 contracts/ 任何檔)[可機械化→H20:契約 lint(additionalProperties:false 等)]
契約 prose 的每個 never/always/must/only 要結構 pin(oneOf+required+minLength、minItems、false-schema);定義而未引用的 component=死件;固定語意 request body 一律 `additionalProperties:false`(新增時全枚舉一次,#94);廣告的狀態必須可由端點自身路由形狀產生(無空頭承諾,#94);開工前跑「每條 prose 保證→結構 pin」矩陣;accepted-and-ignored 參數是契約謊言(#60)。

**Class 25 — async job handler 寫入授權矩陣 × 修法即新面**(何時比對:worker/handler 寫入路徑;review 中新增機制時)[pattern:lease-authority 述詞共用]
「我仍持寫權嗎」述詞導出一次(row lock:status active ∧ lease mine),枚舉**每個**寫入點一遍套用;review 中新加的機制(pin/guard)在 push 前對自己重跑 catalog(檢查與消耗共享一讀/一鎖);規劃期見 ≥2 個真新面→提議切片。預先承諾資源+async consumer→枚舉每條 consumer 拒收路徑,各自 finalize/回收(或別預先承諾,#100);「重用工件」=**不再導出**(跳過 stage),不是「重跑+去重」(#100)。

**Class 26 — 隱含預設不跨時間/交接 × 探針紀律**(何時比對:預設值跨 async/頁面交接;寫任何 mutation-probe)[pattern:世界態 stub + probe 模板]
導出預設只在導出當下有效——跨 async job/頁面交接要物化(pin id/連結帶 id);殘留狀態 gate 身分(`job_id === jobId`)。**探針紀律**(全 probe 類教訓集中此處):stub 建模世界狀態(旗標)不是呼叫次數,旗標掛在被保護的觀察邊界不是觸發動作;probe 先斷言突變已落地(probe-of-probe,#82);雙向 probe(舊 stub+guard-off 須轉綠=證明先前假綠;新 stub+guard-off 須轉紅,#93);probe 輸出要看到 FAILED、輸入要能判別兩實作(#89);fixture 用新舊行為相異的值(空白 vs 空串,#61);決定性宣稱的 fixture 要判別(排列變異圖,#34);oracle 種子從 SPEC 公式鑄造,絕不從實作查表複製(#86);同一 key 政策的兩實作各要自己的判別測試、恢復後逐點 grep-count(sed 會過匹配,#108);probe 存還原走暫存副本,絕不 `git checkout --`(#82/#67);pin 過渡行為的 hermetic 測試有生命週期——實作到位時刻意反轉(#87);「assess and pin」陷阱:把錯行為 pin 進測試≠正確(錯優先序要全面掃,#57)。

**Class 27 — 可達集/參與集變更 → 重推導守衛前提**(何時比對:擴大或縮小任何集合)[prose]
擴大可達集使「舊集合結構性保證」的守衛前提失效(三個各自正確的元件聯合一致——移一個,第三個靜默壞);判斷下沉到值、無法證明時 fail-closed。窄化對偶:縮小參與集→同步每個述詞評估點(executor+所有預測 gate;gate 用子集、顯示用全集,#95)。選擇性跨 store 重用=枚舉**忠實性**hazard(resolve 跑過沒、config pin 消耗時確認、chunk-id remap、first-write-wins 糾纏、oracle 略去非決定欄),每個 hazard 的安全方向=退回全導出(#103)。

**Class 28 — 身分切段在未解碼表示上做**(何時比對:路徑/複合身分解析)[pattern:raw-path 切段 helper]
身分段在 `raw_path`/編碼形上切,逐段 unquote;解碼值含分隔符=視為未知(404);pin 需要兩個身分並存的輸入。

**Class 29 — 同一身分的兩投影同源單讀**(何時比對:record vs pin、gauge vs facet 成對出現)[pattern:單常數 + rekey-after-produce]
一個身分的兩投影(record/pin、gauge/facet)須源自單一鎖定讀——構造上相同(lock-then-read、產出後 rekey;絕不為修 race 重排鎖序→死鎖);「同源」是普遍原則:一個 source **且**一條讀徑——grep 掉其他 setter 旋鈕(沒人用的「彈性」=parity 缺口,#109)。key 組成本身是斷言(內嵌 id=蓄意分離);「身分無條件、狀態有條件」級聯原則(#28);idempotency 錨定的導出值=集合的純函數,不是抓取順序(#34)。

**Class 30 — FE dossier 讀到 worker 消費層**(何時比對:FE 任務 prep)[prose]
FE dossier 必須讀到 worker 實際消費層(connectors/stages/config),不只 api/schemas;run-gate 判準=「會不會讀操作者登記的路徑」,不是「不會失敗」(#70)。

# 裁剪紀錄

2026-07-20:本檔取代 `graphrag-loop-paused-pr5.md`(254KB→本檔;class 編號沿用歷史原編號)。裁掉:per-PR 敘事全文(#17–#109)、輪次經濟學、路線圖尾註、watcher 手工輪詢時代機制(已由 watch-codex.sh/--anchor/機械 hook 取代)、已完結 H-task 立案敘事、逐任務 scope 定案史(TASKS.md/git 為準)。完整史料:`git log --follow -- .claude/memory/graphrag-loop-paused-pr5.md`。

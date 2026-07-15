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


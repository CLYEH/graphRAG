# Memory Index

- [graphRAG 架構定案](graphrag-architecture.md) — 多專案 hybrid RAG 平台：Python + LlamaIndex + Postgres/Qdrant/Neo4j，一專案一 MCP，DESIGN.md v0.5（實作凍結版）
- [graphRAG 工作方式](graphrag-working-style.md) — agent 全開發追求最完整、使用者握定案權、可與 ChatGPT 交叉討論
- [Codex +1 才能 merge](codex-plus-one-merge-gate.md) — 合併硬門檻=Codex 👍(+1),無例外;已用 PreToolUse hook 機械性強制
- [Loop 流程教訓 + lesson classes](graphrag-loop-paused-pr5.md) — watcher 三管道;Codex 意見先判讀;同類一次掃完;**LOOP step 8 post-merge retro 比對本檔的 19 個 lesson classes**(契約驗值/入口一致性/規則自洽/工具語意/檢查者分岔/spec 對齊/執行級驗證/邊界語意×表示誤差/防護面完整性×過度阻擋/綁定時檢查≠不變量TOCTOU/請求級不變量掃全生命週期/框架生命週期機制×自有SoR-liveness/eager-acquisition 耦合非依賴路徑/錯層×無界表面-知道何時丟棄機制/等價-已處理論證紅旗×gate失敗時機判準/讀消費者源碼關閉有界表面×雙gate-parity共用corpus×別調弱自己的oracle/快取信任述詞-枚舉快取狀態機-additive-enum配allowlist/契約投影疊層-schema×runtime×codegen×指派語意×編譯旗標各自pin/多query頁面-導出DAG×verdict位置×判別pin構造);即時狀態查 TASKS.md/GitHub;class17寫入側=快取世界觀滲入write path(spread/fallback/驗證全對同一次fresh read導出)
- [CI 先於 Codex](graphrag-ci-before-codex.md) — push 後先 `gh pr checks` 修 CI 紅燈再 triage Codex;等 Codex 一律 `scripts/watch-codex.sh`(review 管道對土製輪詢隱形,#72 重演 PR#5);**PR 寫入前同腳本 fresh-state check**(owner 協議:程式判斷非 LLM 讀;+1 須晚於 head);切片分支名對到子項(C3a→task/C3a)
- [BA 階段真實 LLM 測試](graphrag-ba-real-llm.md) — .env 有真 key,BA 可用真實 API call;harness 兩 lane(快速 hermetic vs 真實 LLM 在 integration/e2e,本地 pre-push,CI fail-loud);只在動到 model 的任務花呼叫
- [初期落地場景:博物館導覽](graphrag-goal-museum-guide.md) — 外部 no-code agent 平台經 MCP(HTTP)接本平台當場所知識服務;C8b=整合縫;raw data 已到位(海科館 xlsx,v2 後再動);硬約束=不失一般性
- [FE 瀏覽器操作測試](graphrag-fe-browser-qa.md) — FE 任務 e2e 綠後、開 PR 前,用 Claude in Chrome 真瀏覽器走 UI 流程(console/截圖證據附 PR);補充驗證步非機械 gate(owner 2026-07-11 定案)
- [v2 前端開放](graphrag-v2-frontend-scope.md) — v1 全綠後 owner 2026-07-12 開放 v2 FE(FE1-4);FE1/FE3 建於既有端點、FE2 卡 BA4(契約無端點→DR-002 先問 owner)、FE4 部分;改凍結契約=DR-002 gate 停下問
- [海科館真實資料](graphrag-sample-data-hakeguan.md) — nmmst 全量 425 列完成(1409實體/1158關係,eval 4:4;DR-003 驗證 2/3,型別漂移=主病根,催生 G5→Track 4);缺口帳本=.discuss/hakeguan/GAPS.md(G1-G5+O1-O4);job 終態=done 非 succeeded
- [教學先於海科館](graphrag-tutorial-before-hakeguan.md) — ✅ 完成(.discuss/tutorial/,owner 指定 gitignored 位置);全產品首次端到端實證;**遺留:museum 專案在 dev 庫會踩 H11 全表計數測試**;下一站海科館
- [DR-002 清洗回合](graphrag-dr002-cleaning-round.md) — owner 2026-07-13 核准為 cleaning 加端點；只缺「預覽」(POST clean/preview，document_id|text 擇一、一次一份)，rules 用既有 PATCH /projects/{project}；FE4 不需契約變更
- [FE1 PR#70 教訓](graphrag-fe1-pr70-codex-quota.md) — Codex quota 有限(長 finding-chain 耗盡,恢復後 @codex review 手動觸發);FE 鏡射後端實際行為非契約示意;gate fail-closed(isFetching);後端 _local_path 硬化已完成(BA9/#71);ontology UI follow-up 仍待 owner
- [UX 翻新兩急件](graphrag-ux-redesign.md) — owner 2026-07-14 核准開工:Track 4(UXA1-3/UXB1/UXC1-2)已入 TASKS.md;Phase A 純FE/B 設定頁/C=契約 v1.2(eval+upload,DR-002 bump 已核准,§26 隨 UXC1 落地);提案=.discuss/ux-redesign/PROPOSAL.md(Review 頁只顯示 UUID=鐵證)

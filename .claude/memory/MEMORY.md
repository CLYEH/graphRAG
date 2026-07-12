# Memory Index

- [graphRAG 架構定案](graphrag-architecture.md) — 多專案 hybrid RAG 平台：Python + LlamaIndex + Postgres/Qdrant/Neo4j，一專案一 MCP，DESIGN.md v0.5（實作凍結版）
- [graphRAG 工作方式](graphrag-working-style.md) — agent 全開發追求最完整、使用者握定案權、可與 ChatGPT 交叉討論
- [Codex +1 才能 merge](codex-plus-one-merge-gate.md) — 合併硬門檻=Codex 👍(+1),無例外;已用 PreToolUse hook 機械性強制
- [Loop 流程教訓 + lesson classes](graphrag-loop-paused-pr5.md) — watcher 三管道;Codex 意見先判讀;同類一次掃完;**LOOP step 8 post-merge retro 比對本檔的 15 個 lesson classes**(契約驗值/入口一致性/規則自洽/工具語意/檢查者分岔/spec 對齊/執行級驗證/邊界語意×表示誤差/防護面完整性×過度阻擋/綁定時檢查≠不變量TOCTOU/請求級不變量掃全生命週期/框架生命週期機制×自有SoR-liveness/eager-acquisition 耦合非依賴路徑/錯層×無界表面-知道何時丟棄機制/等價-已處理論證紅旗×gate失敗時機判準);即時狀態查 TASKS.md/GitHub
- [CI 先於 Codex](graphrag-ci-before-codex.md) — push 後先 `gh pr checks` 修 CI 紅燈再 triage Codex;CI 快、Codex 慢,別串起兩個等待;切片任務分支名要對到子項(C3a→task/C3a)
- [BA 階段真實 LLM 測試](graphrag-ba-real-llm.md) — .env 有真 key,BA 可用真實 API call;harness 兩 lane(快速 hermetic vs 真實 LLM 在 integration/e2e,本地 pre-push,CI fail-loud);只在動到 model 的任務花呼叫
- [初期落地場景:博物館導覽](graphrag-goal-museum-guide.md) — 外部 no-code agent 平台經 MCP(HTTP)接本平台當場所知識服務;C8b=整合縫;raw data 待 owner 提供;硬約束=不失一般性
- [FE 瀏覽器操作測試](graphrag-fe-browser-qa.md) — FE 任務 e2e 綠後、開 PR 前,用 Claude in Chrome 真瀏覽器走 UI 流程(console/截圖證據附 PR);補充驗證步非機械 gate(owner 2026-07-11 定案)
- [v2 前端開放](graphrag-v2-frontend-scope.md) — v1 全綠後 owner 2026-07-12 開放 v2 FE(FE1-4);FE1/FE3 建於既有端點、FE2 卡 BA4(契約無端點→DR-002 先問 owner)、FE4 部分;改凍結契約=DR-002 gate 停下問
- [FE1 PR#70 教訓](graphrag-fe1-pr70-codex-quota.md) — Codex quota 有限(長 finding-chain 耗盡,恢復後 @codex review 手動觸發);FE 鏡射後端實際行為非契約示意;gate fail-closed(isFetching);後端 _local_path 硬化 + ontology UI 兩 follow-up 待 owner

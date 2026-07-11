# Memory Index

- [graphRAG 架構定案](graphrag-architecture.md) — 多專案 hybrid RAG 平台：Python + LlamaIndex + Postgres/Qdrant/Neo4j，一專案一 MCP，DESIGN.md v0.5（實作凍結版）
- [graphRAG 工作方式](graphrag-working-style.md) — agent 全開發追求最完整、使用者握定案權、可與 ChatGPT 交叉討論
- [Codex +1 才能 merge](codex-plus-one-merge-gate.md) — 合併硬門檻=Codex 👍(+1),無例外;已用 PreToolUse hook 機械性強制
- [Loop 流程教訓 + lesson classes](graphrag-loop-paused-pr5.md) — watcher 三管道;Codex 意見先判讀;同類一次掃完;**LOOP step 8 post-merge retro 比對本檔的 13 個 lesson classes**(契約驗值/入口一致性/規則自洽/工具語意/檢查者分岔/spec 對齊/執行級驗證/邊界語意×表示誤差/防護面完整性×過度阻擋/綁定時檢查≠不變量TOCTOU/請求級不變量掃全生命週期/框架生命週期機制×自有SoR-liveness/eager-acquisition 耦合非依賴路徑);即時狀態查 TASKS.md/GitHub
- [CI 先於 Codex](graphrag-ci-before-codex.md) — push 後先 `gh pr checks` 修 CI 紅燈再 triage Codex;CI 快、Codex 慢,別串起兩個等待;切片任務分支名要對到子項(C3a→task/C3a)
- [BA 階段真實 LLM 測試](graphrag-ba-real-llm.md) — .env 有真 key,BA 可用真實 API call;harness 兩 lane(快速 hermetic vs 真實 LLM 在 integration/e2e,本地 pre-push,CI fail-loud);只在動到 model 的任務花呼叫
- [初期落地場景:博物館導覽](graphrag-goal-museum-guide.md) — 外部 no-code agent 平台經 MCP(HTTP)接本平台當場所知識服務;C8b=整合縫;raw data 待 owner 提供;硬約束=不失一般性

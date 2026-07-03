---
name: graphrag-loop-paused-pr5
description: loop 狀態 — H1(#6)已 merge;P1(PR #5)修完 Codex 意見等 fresh +1;下一個任務 P2
metadata: 
  node_type: memory
  type: project
  originSessionId: 3cfe1f1b-4e4e-4d2f-940f-12a9f9d7def4
---

graphRAG build loop 狀態(2026-07-03 更新):

- **P0(PR #4)、H1(PR #6)已 merge**。H1 = 外部審查後的 harness 修正(fail-loud CI integration gate、文件漂移、gate-wait pipelining、reviewer→opus、LLM 預設→`gpt-5.4-nano`、allowlist、CI dedupe/concurrency/qdrant pin、DR-008)。
- **P1(PR #5)等 fresh Codex +1**:Codex 額度恢復後給了一條 inline 建議(chunk offsets 只 required 沒型別),已修(`44dd119`:`{integer, minimum: 0}` + 2 rejection 測試)、thread 已 resolve、main 已 merge 進來解 TASKS.md 衝突(head `6fb117d`)、已重新 `@codex review`。+1 後 merge → 下一個任務 **P2**(build/activation + Alembic setup,DR-008)。
- **教訓(監聽 Codex)**:Codex 的「changes wanted」走 **`pulls/N/reviews`(PR review + inline threads)**,不是 issue comment;+1 走 issue reaction。watcher 必須同時輪詢 reactions、reviews、comments 三個管道 —— 腳本在 scratchpad `watch-codex-pr5.sh` 可參考。
- **教訓(merge)**:auto-mode 分類器會擋 agent 自己 `gh pr merge`(即使 +1 已確認);由使用者跑 merge 指令即可(hook 會驗證)。
- 慣例備忘:TASKS.md 勾選一律含在該任務 PR 內;PR 等 gates 期間可從 main 開下一個獨立 task 分支(LOOP.md 已明文)。

相關:[[codex-plus-one-merge-gate]]、[[graphrag-architecture]]

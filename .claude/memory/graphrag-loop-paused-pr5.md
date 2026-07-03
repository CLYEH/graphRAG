---
name: graphrag-loop-paused-pr5
description: loop 流程教訓(Codex 三管道監聽、merge 權限、TASKS.md 慣例)——PR/任務即時狀態一律查 TASKS.md 與 GitHub,不要信任本檔的狀態敘述
metadata: 
  node_type: memory
  type: project
  originSessionId: 3cfe1f1b-4e4e-4d2f-940f-12a9f9d7def4
---

**PR/任務的即時狀態請一律查 `TASKS.md` 與 GitHub(`gh pr view`/`gh pr list`)。這份筆記只保留跨任務可重用的流程教訓,任何「目前狀態」敘述在下次 push/merge 後就會過期,不可當作現況依據。**

- **教訓(監聽 Codex)**:Codex 的「changes wanted」走 **`pulls/N/reviews`(PR review + inline threads)**,不是 issue comment;+1 走 issue reaction。watcher 必須同時輪詢 reactions、reviews、comments 三個管道。
- **教訓(merge)**:auto-mode 分類器會擋 agent 自己 `gh pr merge`(即使 +1 已確認);由使用者跑 merge 指令即可(hook 會驗證)。
- 慣例備忘:TASKS.md 勾選一律含在該任務 PR 內;PR 等 gates 期間可從 main 開下一個獨立 task 分支(LOOP.md 已明文)。

歷史脈絡(2026-07-03 撰寫時的快照,僅供追溯,不代表現況):當時 P0(PR #4)、H1(PR #6)已 merge,P1(PR #5)正在等 fresh Codex +1。

相關:[[codex-plus-one-merge-gate]]、[[graphrag-architecture]]

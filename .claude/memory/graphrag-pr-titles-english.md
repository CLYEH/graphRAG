---
name: graphrag-pr-titles-english
description: PR 標題一律用英文(owner 2026-07-17 指示);內文/commit 訊息維持慣例不受此限
metadata: 
  node_type: memory
  type: feedback
  originSessionId: d673e708-e836-4b8a-8fc7-cb33527c5fc3
---

Owner 2026-07-17:「以後 PR 標題請用英文」。

**Why:** owner 的明確偏好(PR 列表可讀性/工具相容性)。

**How to apply:** `gh pr create --title` 一律英文(簡潔祈使句,格式 `<TASK-ID>: <english summary>`);PR body 與 commit message 語言維持既有慣例(zh-tw 為主)不受此限。發現既開 PR 標題是中文時,以 `gh pr edit --title` 更正。

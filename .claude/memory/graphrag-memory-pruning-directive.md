---
name: graphrag-memory-pruning-directive
description: "owner 2026-07-17 指示:retro 附帶整份專案 memory 重新 review,去蕪存菁(過時的拿掉、重要的保存)"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: d673e708-e836-4b8a-8fc7-cb33527c5fc3
---

Owner 2026-07-17:「下一輪的 retro 要再加上:自己的專案 memory 整個再重新
review,去蕪存菁,哪些過時的拿掉,哪些重要的保存。」

**Why:** memory 是每個 session 的世界觀來源;過時的狀態敘述會誤導未來
工作(#90 retro 的 doc-review 已實際抓到 CFG1/SRC2 stale 矛盾一例),
冗餘敘事拉高 recall 成本。

**How to apply:** 執行一輪完整盤點:逐檔判定 keep / compress / delete —
(a) 純歷史敘事且事實已活在 DESIGN/TASKS/lesson catalog 者 → 刪或壓成
一行;(b) 帶「現況」語氣的過時狀態 → 更新或刪;(c) 活躍協議與 owner
決策 → 保留;(d) lesson catalog(graphrag-lesson-classes)的 classes
為承重結構,保留。之後的 retro 維持這個衛生習慣(發現 stale 即修,
不必每輪全掃)。

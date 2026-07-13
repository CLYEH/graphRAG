---
name: graphrag-tutorial-before-hakeguan
description: owner 2026-07-13 指示:v2 完成後、海科館資料適配之前,先寫手把手圖文教學(操作截圖)+ 自產/上網找測試資料
metadata:
  type: project
---

**Owner 指示(2026-07-13,FE4 進行中時下達)**:在進入 [[graphrag-sample-data-hakeguan]] 的資料適配**之前**,先完成一份**使用教學文件**:

- **測試資料**:自己產生或上網找(不是海科館資料——那是之後的事)。
- **形式**:手把手(step-by-step)、**圖文並茂**——要有實際操作截圖(Claude in Chrome 走真 UI 拍)。
- **對象**:讓使用者知道怎麼用這個系統(從建專案 → 註冊來源 → ingest/build → activate → Console 各頁 → 查詢/MCP 的完整流程)。

**實作備忘**:
- 截圖是 .png → **doc fast lane 走不了**(push-gate 只允許 *.md);教學文件+圖檔要走 task branch + PR,或先問 owner 是否放寬 doc lane 對 docs/tutorial/ 圖檔的限制。
- 全流程需要 worker(`poe worker`)跑真 build + 真 LLM key(.env 有);§20 eval gate 會擋 activate——教學裡要嘛帶 golden.yaml 讓 eval 過,要嘛誠實記載如何處理(別像 FE3 QA 那樣直接改 DB,教學要教正路)。
- 順序:FE4 → 本教學 → 海科館適配。

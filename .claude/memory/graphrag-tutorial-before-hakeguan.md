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

**✅ 已完成(2026-07-13)**:owner 改指示放 **`.discuss/tutorial/`**(gitignored,免 PR)。
產出=`TUTORIAL.md` + 8 張截圖(img/)+ 可重現語料(corpus/,三篇虛構海洋博物館展廳文)。
教學走的全是正路且**首次端到端驗證了整個產品**:museum 專案 → PATCH config(ontology/chunking/query_policy)→
file:// 來源 → 真 LLM build(~90s,30 實體/19 關係,evidence 引文齊)→ golden.yaml + `graphrag eval`(3/3, 1.0)→
§14 preflight 下 activate → 六頁 Console + hybrid 查詢(社群報告有效)。

**遺留現場(下個任務注意)**:
- dev 庫裡留著 **museum 專案 + 2 個 build**(active fa84a39b + ready 372225fa)供 owner 探索——
  **會踩 H11 的全表計數整合測試**;跑 `check-full` 前要嘛清掉 museum,要嘛先修 H11。
- `projects/museum/`(config.yaml + eval/golden.yaml)是 untracked repo 檔;要嘛提交(教學資產)要嘛 gitignore。
- 三個 dev 程序(uvicorn/worker/vite)留著跑,owner 可直接逛。

**下一站:海科館適配**(見 [[graphrag-sample-data-hakeguan]];已知缺 xlsx connector)。

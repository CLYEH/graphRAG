---
name: graphrag-sample-data-hakeguan
description: owner 2026-07-13 提供海科館真實知識資料(.discuss/sample_data，5 份 xlsx)；v2 完成後才動手：拿真實資料跑通全流程、找出缺的功能
metadata: 
  node_type: memory
  type: project
  originSessionId: a5de076e-8823-47d9-a44a-b3fd7b14df4e
---

**Owner 交辦（2026-07-13，明說「不用現在做」）**：v2 全部完成後，拿**真實場域資料**實際跑這個 repo 的功能，**看缺什麼再補**。這是 [[graphrag-goal-museum-guide]] 那個落地場景的 raw data，之前一直缺、現在到位了。

**資料位置**：`C:\graphRAG\.discuss\sample_data`（184K，5 份 **.xlsx**）
- `2-1.基礎服務知識庫.xlsx`
- `2-2.導覽內容知識庫.xlsx`
- `海科館_2-2.導覽內容知識庫_區域探索廳_1150612.xlsx`
- `海科館_2-2.導覽內容知識庫_研究典藏館_0617_omnigfix.xlsx`
- `國立海洋科技博物館__iMuseum_FAQ.xlsx`

場域＝**國立海洋科技博物館（海科館）**。內容形狀：基礎服務（開放時間/票價/交通這類 FAQ 型）＋導覽內容（展廳/展品敘述型）＋FAQ。這正好對上 museum-guide 場景要的「場所知識服務」。

**已經看得到的缺口（第一眼，未深入）**：
- **沒有 xlsx connector**。`core/builds/sources.py` 只 wire 了兩種 kind：`text`（讀 file:// 目錄下的 .txt/.md）與 `structured`（讀單一 CSV，需 metadata 的 `table`/`pk_column`）。xlsx 完全沒有路徑 → 要嘛先轉 CSV/txt，要嘛新增一種 kind（新 connector）。這是「缺什麼功能」清單的第一項，幾乎確定。
- FAQ 型（Q/A 成對）與敘述型（長文）**混在同一批資料**裡，切塊策略未必同一組參數合用 → 可能牽動 chunking 參數（見 [[graphrag-dr002-cleaning-round]]）或需要 per-source 設定。
- ontology 需要為「博物館」領域定義（展品/展廳/服務/票種…），目前 ontology 只能從專案 config 塞、還沒有 UI（ontology-configuration UI 仍是 owner 未決的 follow-up，見 [[graphrag-fe1-pr70-codex-quota]]）。

**動手時機**：v2（FE3 → BA4 → FE2 → FE4）全綠之後。不要提早開，會跟 v2 的任務搶範圍。

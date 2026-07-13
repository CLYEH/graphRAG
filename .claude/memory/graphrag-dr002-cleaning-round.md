---
name: graphrag-dr002-cleaning-round
description: owner 2026-07-13 拍板開 DR-002 回合為 cleaning 加預覽端點；形狀已定案（單一 POST clean/preview，document_id 或 text 擇一，一次一份）
metadata: 
  node_type: memory
  type: project
  originSessionId: a5de076e-8823-47d9-a44a-b3fd7b14df4e
---

**Owner 決策（2026-07-13）：核准開一個 DR-002 回合，為 cleaning 新增端點**，解鎖 BA4 → FE2。這是 v2 最後一組被卡的任務（見 [[graphrag-v2-frontend-scope]]）。

**調查結論（決定了這個回合有多小）**：
- 這個 repo 的「清洗」= **切塊**（`core/clean/` 只有 `chunking.py`），旋鈕只有 `max_chars` / `overlap`。
- **`PATCH /projects/{project}` 已存在** → 寫入 chunking 參數（TASKS 說的 "rules"）**不需要新端點**，FE2 用現有的。
- 契約真正缺的只有 §10.2 的**「含抽樣預覽」**：無法在不跑 build、不落地的前提下看到切塊結果。
- 順帶確認 `GET /projects/{project}/graph/subgraph` 已存在 → **FE4 不需要任何契約變更**。

**凍結的端點形狀（owner 選定）**：
```
POST /projects/{project}/clean/preview
body: { max_chars, overlap, document_id | text }   # 兩種來源擇一
→ { data: { chunks: [{ordinal, text, start_offset, end_offset, token_count}] },
    meta: { build_id }  }                          # build_id 只有走 document_id 時有意義
```
- **兩種來源都收**：`document_id` 讓 owner 在真實語料上比較新舊參數；`text` 讓第一次 build 之前就能試（此 pipeline 的 ingest 與 build 是同一條，第一次 build 前沒有任何 document）。
- **一次一份**（不做多份取樣）：§10.2 只說「含抽樣預覽」沒規定份數；一次一份就足以回答「這組參數會怎麼切我的文件」，且避免截斷語意（取哪 N 份/回應上限/是否分頁）把端點表面撐大。
- 切塊是**純函式**（`chunk_text` 決定性、不碰 store、不呼叫 LLM）→ 預覽完全不落地，不寫任何資料。

**DR-002 回合的機械要求**（CLAUDE.md / DESIGN §26）：改 `contracts/openapi.yaml` 必須 bump `schema_version`（目前 `1.0`）並把變更記進 DESIGN §26。這是 v0.5 實作凍結以來第一次動契約。

---
name: graphrag-stale-stack-invalidates-qa
description: "黑箱 QA 或給連結前必查:五層 stack 每一層的 process 啟動時間都要晚於 HEAD,否則證據無效"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: bbc79866-0db1-40f8-b323-82b7e715b259
  modified: 2026-07-29T02:47:47.763Z
---

這個 repo 的本地 stack 有**五層**,不是一層。「服務有沒有在跑 / 更新到最新」必須五層都查:

1. docker compose ×4(postgres / neo4j / qdrant / redis)
2. API — `uvicorn api.app:create_app --factory`(起時無 `--reload`,**不會自己更新**)
3. MCP gateway — `python -m cli.main serve-mcp`
4. Console — `vite`
5. **arq worker** — `arq api.workers.build_worker.WorkerSettings`(最容易被整個忘掉)

**Why:** 2026-07-29 只查了 docker 就回報「服務原本是停的、已啟動」,漏掉另外四層。API/gateway/Console 當時正在跑但落後 main 29 小時(中間 merge 了 QA9/QA10/QA10b),worker 從未啟動。使用者拿這個環境做完整輪黑箱測試,開出 15 個 issue,重驗後大量是假陽性(#152 project name 未驗證 → QA10 已修;#156 NUL byte 500 → 已修;#159 nil build_id → 已修)。更嚴重的是 worker 沒跑讓所有 job 永遠卡在 `queued`,直接把 #151 誤判成 P0「專案永久無法刪除」—— 補上 worker 後 queue 20 秒排空、DELETE 回 204。**環境不完整會製造出看起來像 P0 的假缺陷。**

**How to apply:**
- 回答「服務是否最新」一律用機械證據,不用宣稱:`Get-Process` 的 `StartTime` 對比 `git log -1 --format='%cI'`,每個 process 都要晚於 HEAD;再加一條行為證據(例:`POST /projects {"name":"a/b"}` 回 400 代表 QA10 驗證生效,回 201 代表是舊碼)。
- 給使用者連結前先跑這個檢查。給出舊 server 的連結 = 讓對方浪費一整輪 QA。
- 收到黑箱 QA 產出的 issue,**先驗環境再讀內容**;stack 有任何一層舊於 HEAD,整批證據作廢、重跑後才進 LOOP。
- 對 job/queue 類的症狀(「卡住」「永遠不完成」「無法刪除」),先問「worker 在跑嗎」再懷疑程式。
- 探測寫入端點會**真的**觸發工作:`POST /projects/<p>/build` 空 body 回 202 就是真的開始重建、真的燒 OpenAI 額度。拿真實專案(如 nmmst)當 control probe 前先想清楚。

參見 [[graphrag-lesson-classes]] 的嚴重性標錯條目、[[graphrag-sample-data-hakeguan]](dev 真實資料)、[[graphrag-fe-browser-qa]]。

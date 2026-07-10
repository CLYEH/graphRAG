---
name: graphrag-ba2e-prep
description: BA2e 進度——BA2e-1(triggers+GET/cancel)已 merge(PR #53);剩 BA2e-2 SSE 的凍結合約摘要與可沿用 harness 指路
metadata: 
  node_type: memory
  type: project
  originSessionId: 24544557-4e13-47f3-89e2-819678d8de00
---

BA2e 切片進度(2026-07-10 更新):**BA2e-1 已 merge(PR #53,5 輪 Codex 全 must-fix;retro 見 [[graphrag-loop-paused-pr5]] #53 條)**;剩 **BA2e-2 = SSE**。

**BA2e-2(SSE)凍結合約形狀(contracts/openapi.yaml,DR-002)**:
- `GET /jobs/{job_id}/events` → `text/event-stream`,event 名 `job.update|job.done|job.failed`(§27.2);payload=**JobEvent**(required: job_id,status,step,progress,message,ts)——**全凍結形狀恆在**,step/message 是 null 而非缺欄;stream 在 `job.done`/`job.failed` 後結束。
- 設計約束:**SSE 輪詢 jobs row(Postgres SoR),絕不讀 arq**(worker `keep_result=0`,arq result 恆空——刻意的)。

**BA2e-1 落地後可沿用的 harness/模式**:
- `api/routers/jobs.py` 已有 GET/cancel;SSE endpoint 加在同 router。
- 整合測試 fixture:`tests/test_jobs_api_integration.py` 的 `api` 4-tuple(client, conn, enqueued spy, queue_touches)——savepoint-per-request、arq_redis_provider override、enqueue_build spy。
- 但注意:SSE 是長輪詢 stream,savepoint-per-request 的單連線 harness 對「stream 期間 worker 併發改 row」可能不夠——SSE 讀取用獨立短查詢(每 poll 一個 conn/txn)較貼近生產;測試設計時先想清楚。
- `job_dto`/`JobEvent` 欄位重疊但不同(JobEvent 無 kind/project/error,多 ts)——寫新的 `job_event_dto`,勿複用。
- class-12 sibling 窗已由 BA2e-1 關閉(enqueue-in-band + reaper queued-sweep `find_unenqueued_jobs`,grace 🔧 `job_enqueue_grace_seconds`=120s)。

**開工前先掃(#53 retro 的教訓,SSE 尤其相關)**:
- 凍結 schema 欄位直接讀 openapi.yaml,勿信註解複述(#53 R1)。
- SSE 是「請求級不變量」重災區(class 11):stream 生命週期(連上→輪詢→終態→關閉)每段對照 JobEvent 形狀與終止語意;job 在 stream 期間被 CASCADE 刪掉(專案刪除)→ stream 該怎麼終止?(#53 R2 的 sibling:scope row 可合法消失)。
- 連線資源:SSE 佔連線時間長——db 連線取得點(每 poll 取還 vs 佔用 request 連線)= #53 R3 候選 class 的近親。

相關:[[graphrag-loop-paused-pr5]](#53 retro、class 12)、[[graphrag-architecture]]

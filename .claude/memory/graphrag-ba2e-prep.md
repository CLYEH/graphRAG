---
name: graphrag-ba2e-prep
description: BA2e(triggers + job endpoints + SSE)開工前研究——凍結合約形狀摘要、切片提議、設計約束(class-12 sibling 窗、SSE 讀 SoR 不讀 arq)
metadata: 
  node_type: memory
  type: project
  originSessionId: 24544557-4e13-47f3-89e2-819678d8de00
---

BA2e 開工前研究(2026-07-09,PR #52 quota 等待期間完成;開工時直接用,不必重讀合約)。

**凍結合約形狀(contracts/openapi.yaml,DR-002)**:
- `POST /projects/{project}/ingest`(IngestRequest)與 `POST /projects/{project}/build`(BuildRequest)→ **202 JobAcceptedResponse**,帶 `Idempotency-Key` param,有 409 分支。
- `GET /jobs/{job_id}` → JobResponse;`POST /jobs/{job_id}/cancel` → 202 JobAcceptedResponse(也走 Idempotency-Key)。
- `GET /jobs/{job_id}/events` → `text/event-stream`,payload=**JobEvent**:event 名 `job.update|job.done|job.failed`(§27.2);**全凍結形狀恆在**——`step`/`message` 是 null 而非缺欄(required: job_id,status,step,progress,message,ts),SSE 消費者不分支缺欄。

**切片提議(開工時先向 owner 確認)**:BA2e-1 = triggers(create_job + enqueue_build)+ GET /jobs/{id} + cancel;BA2e-2 = SSE。

**設計約束**:
- **class-12 sibling 窗(#52 retro 已寫進 TASKS.md BA2e 行)**:trigger 的 create_job→enqueue_build 之間是 unmarked crash 窗——job 已 commit `queued` 但從未進 arq,arq 與 lease reaper 都看不見,永久卡住。切片必須關掉或可復原(候選:reaper 加掃「never-leased `queued` 且超過寬鬆 grace」;或 enqueue 掛進 idempotent retry 路徑)。
- **SSE 輪詢 jobs row(Postgres SoR),絕不讀 arq**:worker 已設 `keep_result=0`(#52 R4,arq result 無人消費的教義已機械化)——任何想讀 arq job result 的設計都會拿到空值,這是刻意的。
- enqueue 用現成 `api/workers/build_worker.py::enqueue_build`(`_job_id=str(job_id)` 冪等第一線);worker 端 dedup/recovery 已由 BA2d 全包(lease 包住整個 dispatch + reaper)。
- Idempotency-Key 儲存/replay 機制 BA1b 已建好,triggers 直接沿用;409 語意對齊 BA1b 現行 handler。

相關:[[graphrag-loop-paused-pr5]](class 12 與 #51/#52 retro)、[[graphrag-architecture]]

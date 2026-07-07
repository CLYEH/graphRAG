---
name: graphrag-ba2-registry-fk
description: "FK builds.project→projects.name 已於 BA2b(#45)加上(RESTRICT);剩 build-creation 走 registry=BA2c"
metadata: 
  node_type: memory
  type: project
  originSessionId: 24544557-4e13-47f3-89e2-819678d8de00
---

**狀態(BA2a #44 + BA2b #45 後)**:第 2 項 FK **已完成**——BA2a 先加 `delete_project` 的
`FOR UPDATE` 鎖(關 count-then-delete 窗),BA2b 加 `builds.project → projects.name`(RESTRICT)
FK + supporting index + reconcile-before-ALTER(0010 在 ADD CONSTRAINT 前重跑 backfill,見
[[graphrag-loop-paused-pr5]] #45 的「收緊型 migration 遮蔽」教訓)。**剩第 1 項**:build-creation
變 registry-aware(建 build 前確保 projects 有該 name)=**BA2c**(orchestrator + registry-aware
build 建立)。下方為原始 deferral 紀錄(歷史)。

BA1a(#42) delete_project 的 guard 是 count-then-delete **TOCTOU**：並發「為 project X 建 build」
插入 builds 若落在 count(=0) 與 delete 之間,會把 build 留在已刪除、可重用的 project name 下
(builds.project 是裸 text 無 FK)。Codex round-3 P2 指出,已 reply-resolve 為 BA2 deferral。

**結構性修法(BA2 做)**：
1. build-creation 變 registry-aware——建 build 前先確保 projects 有該 name(BA2 的 ingest/build
   trigger 本就會先建/驗 project)。
2. 加 FK `builds.project → projects.name`(ON DELETE RESTRICT):
   - 建 build 需 project 存在(referential integrity),
   - 刪 project 有 builds 時 DB 直接擋(RESTRICT)+ row lock → 消除 TOCTOU,不靠 app 端 count。
   - BA1a #42 的 migration backfill(從 builds∪review_ledger∪ontology_proposals∪pipeline_runs
     回填 projects)已讓既有資料滿足此 FK,**FK 的前置條件已備妥**。
**為何 BA1a 不做**：retrofit FK 會讓所有「直接插 builds 而無 projects row」的既有 build/pipeline
測試全掛(build 子系統早於 registry、尚未 registry-aware)——超出 BA1a(schema+CRUD)邊界。
see [[graphrag-loop-paused-pr5]] 的完整性鏈教訓 · [[graphrag-ba-real-llm]]

---
name: graphrag-ba3-prep
description: BA3(inspection endpoints)開工前研究——凍結合約 9 ops 摘要、repo/graph 讀取面、active-binding 佈線缺口、cursor/DTO 清單、切片提議與 3 個 owner 問題
metadata: 
  node_type: memory
  type: project
  originSessionId: 1ca14cc5-3e0a-461e-ae04-ca06338ee1f2
---

BA3 開工前研究(2026-07-10,PR #54 quota 等待期間完成;開工時直接用,細節再查 dossier 指到的行號)。

**凍結合約(contracts/openapi.yaml)——9 ops,全 GET、tags=[inspect]、綁 active build**:
- list+get × {documents(411/434), chunks(451/474), entities(491/514), relations(531/554)};list 帶 Limit/Cursor/Sort/Filter,path id 全 uuid。
- getSubgraph(571):query params `entity_id`(必填 uuid)+`hops`(int min 1 default 1,受 `query_policy.max_graph_hops` cap)+Limit → SubgraphResponse(data=GraphContext required [nodes,edges])。
- **合約沒有 reports inspection endpoint**(無 CommunityReport schema、無 path)——TASKS.md BA3 行寫的「reports」未被合約凍結;要嘛從 BA3 剔除、要嘛走 DR-002 升版(owner 問題)。
- 錯誤分支全走 `default: Error`(inspect ops 無顯式 409);NO_ACTIVE_BUILD 在 enum(409)。
- required 欄位:Document[id,build_id,source_uri](`raw` 僅 detail GET 回);Chunk[id,document_id,build_id,ordinal,text];Entity[id,build_id,type,canonical_name,entity_key,status];Relation[id,build_id,src_entity_id,dst_entity_id,type,status](optional evidence[] required [id,evidence_type])。

**核心讀取面(DR-006)**:
- `BuildScopedRepo`(core/stores/repo.py:331):唯一泛用讀 `fetch_all(table,*where)`;先例 core/query/global_reports.py:67。scoped 表:documents/entities/relations/community_reports/merge_candidates(project+build)、chunks/relation_evidence(build-only)。
- active 解析:`resolve_active_binding(conn,project)→ActiveBinding`(repo.py:193)/`NoActiveBuildError`→映 NO_ACTIVE_BUILD;構造走 `for_active_build` 或 `bound_to(conn,binding)`。
- **subgraph 走 Neo4j**:`BuildScopedGraphRepo`(core/stores/graph.py:200,neighbors/shortest_path/edges_among)+ 編排 `core/query/graph.py:graph_query`(驗 graph.build_id==repo.build_id);API 需要 Neo4j session seam(api/deps 目前只有 engine+arq)——鏡射 MCP `ProjectContext.bound()`(core/mcp/context.py:52:一次 resolve_active_binding、全 store 共用 binding)。
- **API 佈線缺口**:api/ 目前完全沒碰 active-build;`success()` 的 build_id 預設 None、`response_meta` 只帶 request_id+elapsed——BA3 要在 db_conn 上 resolve binding 並把 build_id 傳進 success()。

**cursor/DTO 待建**:
- 現有 decoder 只有 project/source 兩個;BA3 每資源要新 keyset decoder。keyset 候選:documents **無 created_at**(用 ingested_at nullable 或 id)、chunks 用 (document_id,ordinal) 唯一但跨文件不唯一(可用 id)、entities/relations 有 created_at(nullable)→(created_at,id)、community_reports 無 created_at(level,id)。nullable keyset 欄位要小心(NULLS ordering=#40 教訓)。
- DTO 全缺:document/chunk/entity/relation/graph-context;Document.raw 僅 detail 回(list 不含)。
- Sort/Filter:BA1b 先例=`reject_unsupported_query`(非預設 sort/任何 filter → VALIDATION_ERROR),BA3 沿用直到真的做 sort/filter。

**切片提議(開工時向 owner 確認)**:BA3a=active-binding seam+envelope build_id 佈線+documents+chunks;BA3b=entities+relations(+evidence);BA3c=subgraph(Neo4j session seam+hops policy cap)。或兩片(PG 檢視/subgraph)。
**Owner 問題三則**:(1)切片;(2)「reports」不在凍結合約——剔除或 DR-002 升版?(3)DESIGN:154 Console v1 只做 health/jobs/review/playground(FE3/FE4 檢視/圖譜=v2)——BA3 是後端(DESIGN:175 明列 Track 2),照做?還是隨 v1 縮?

**開工前掃(retro 教訓對照)**:讀側=「不受信投影 value tree」不適用(讀 SoR)但 **subgraph 讀 Neo4j 投影=#31/#33 的 read/emit 覆驗面全套**(值 corrupt/status/邊存在/可達性);meta.build_id 佈線=class 2 入口一致性(每個 inspect 響應同一 binding);詳 [[graphrag-loop-paused-pr5]]。

相關:[[graphrag-loop-paused-pr5]](#54 條=BA2 家族收官)、[[graphrag-architecture]]

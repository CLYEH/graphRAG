---
name: graphrag-track5-owner-approvals
description: owner 2026-07-17「全部同意」— CFG1(#93)+DR-002 打包契約回合 CTR1(#94/DR-013)+SS1a(#92)皆已 merge,餘各任務 runtime;⚠️ 旗標解除
metadata: 
  node_type: memory
  type: project
  originSessionId: d673e708-e836-4b8a-8fc7-cb33527c5fc3
---

Owner 2026-07-17 對 Track 5 待決事項回覆「全部同意」,三組核准全數生效。
**執行狀態(2026-07-18)**:第 1 組 CFG1 已落地(PR #93,DR-012);第 3 組
SS1a 已落地(PR #92)。第 2 組 DR-002 打包契約回合=**CTR1 已 merge**(PR #94,
DESIGN §26 DR-013,openapi v1.2→1.3):SRC2/GOV2/GOV3/SS1b/RB1 端點 + MCP 資訊
端點(`GET /projects/{p}/mcp`)一回合議定完畢,contract + test + codegen only。
**各任務 runtime 落地**:SRC2(#95)、SS1b-api(#96)、GOV3-api(#97 ontology
proposal accept/reject)、GOV2-api(#98 entity/relation approve-reject)、
RB1-api(#99 build step/item drill-down)、RB1-retry-core(#100 lineage+端點+
documents clone;Codex 5 輪:retry 跳 live-source ingest/0-doc 拒/set-based clone/
job-guard 先於 clone+gates 入 produce+FOR UPDATE 鎖序/preflight 失敗終結子 build)
皆已 merge。SS1b-fe(#101 Graph 左欄 client-side 過濾→伺服器 q 搜尋+精確 total+
debounced+maxLength=256;browser QA 過)亦已 merge=SS1b 收官(#96+#101)。
RB1-fe(#102 RunsTable 失敗診斷:step/item 下鑽+安全 retry+lineage)亦已
merge=RB1 FE 切片收官;**Codex 9 輪**(R4 note-suppression+CSS-cascade、
filter[status]、P1 job-id 假承諾、step-error、next-page-error),每輪 fix+
mutation-probe 判別測試+re-stamp,教訓見 lesson catalog #102。
RB1-retry-skip(#103 選擇性 graph-layer clone+compute-skip:成功 doc 圖層重用、只
重抽失敗 doc;owner 定案 pin 父設定+v1 最小刀)亦已 merge=**RB1 線完全收官**;
**Codex 4 輪**(fork-C post-resolve 退全導、config-pin 強制+chunk_id remap、relation
first-write-wins entanglement、entity entanglement subsumes),教訓見 lesson catalog #103。
GOV3-fe(#104 本體提案採納/拒絕 UI + 分頁式治理頁〔審核→治理〕+ Health
`pending_ontology_proposals` 深連結)亦已 merge=**GOV3 線收官**(api #97 + fe #104);
**Codex 3 輪**(R1 單值 deciding 讓相反動詞競態、R2 Set 因 react-query 單-observer
併發卸離 strand、R3 §17 終態動作加確認步驟對齊 ReviewCases),教訓見 lesson catalog #104。
GOV2-fe 因規模拆 fe-1/2/3/4;**GOV2-fe-1**(#105 entity 審核分頁 + 共用
`useDecideReviewTarget` + 分頁 WAI-ARIA + Health `needs_review_entities` 深連結)亦已 merge;
**Codex 2 輪**(R1 三 finding:隨機鍵重試雙記 ledger→決定性鍵、reversible 在 UI 不可達→排除加
確認、佇列讀盡→reply-resolve;R2 TASKS.md 驗收準則漂移),教訓見 lesson catalog #105——
**reversible→無確認+隨機鍵是過度聰明設計,被反推回 sibling §17 模式(決定性鍵+排除確認)**。
**GOV2-fe-2**(#106 relation 審核分頁)亦已 merge;**Codex 5 輪**全 P1/P2——relation 比 entity 豐富
(需 src→type→dst 名稱解析 + evidence 懶載),「決定前須見所決之物」不變量逐面硬化:list 端點省略
evidence→懶載 detail、名稱解析+決定 gate 至名載完(pending+error 皆鎖+重試)、每決定入口(含確認鈕)
皆 gate、決定後 refetch 窗鎖定(queue.isFetching,回補 EntityReview+ProposalPool grep-all),教訓見
lesson catalog #106。**GOV2-fe-3**(#107 GovernanceBacklog 治理待辦顯示型面板)亦已 merge;
**Codex 2 輪**皆 scope-honesty(「發布閘」框架→counts 為 active-build scoped 故改框治理待辦、
混 scope 訊號依真實 scope 分組〔上線中知識庫/全專案〕),教訓見 lesson catalog #107。
**GOV2-fe-4**(#108 已排除/復原視圖 + 增量分頁)亦已 merge=**GOV2-fe 全四片收官**;
**Codex 2 輪**(R1 鎖述詞含 error 臂 + pin-retry 改全量 refetch;R2 復原 idem-key 每邏輯
復原一把),教訓見 lesson catalog #108。
SS1b/GOV3/RB1 皆切 api/fe;RB1 另切 RB1-retry,再切 core/skip(全數 merge)。
尚餘 low-confidence/missing-evidence 清單(另待 `/relations` facet api 任務)、
relation 影響/impact 抽屜(useSubgraph,follow-up)、
候選-scoped 發布 preflight(需 per-build health facet=契約變更,owner 決);
Console MCP URL/健康顯示 = GOV2-fe 後續接 `GET /mcp` 端點。

1. **CFG1 方向確認**:推翻 2026-07-10 雙源決策 — query-policy 統一單一 SoR
   (採建議:Postgres `projects.config`,MCP 啟動時讀 DB,`config.yaml` 退場)
   + 通用 serve-mcp(廢除每專案手寫 entrypoint)+ Console
   顯示 MCP URL/連線健康/可複製 Agent 設定。⚠️ 旗標解除,loop 可取。
   **Owner 2026-07-17 追加的驗收形狀**:單一 gateway process 服務「全部」
   專案,URL = `http://<host>:<port>/mcp/<project_name>`(path-per-project,
   一 port 多專案);建完專案(不需重啟)即可經 streamable HTTP 連上——
   gateway 從 registry(Postgres SoR)動態解析專案,新專案 lazy-mount。
   §9「一專案一 MCP server」語意保留為「每專案一個邏輯 server 實例,
   掛在同一 gateway 下」,DESIGN 措辭隨 CFG1 修訂。專案名的 path 限制
   沿用 Console 的 isPathAddressable 規則(含 `/`、`.`、`..` 者不可達)。
2. **DR-002 打包契約回合核准**(仿 DR-009/DR-010):GOV2(entity/relation
   審核端點+列表+publish gate)、GOV3(proposal pool 採納/拒絕端點)、RB1
   (BuildRequest retry 欄位+steps/items 端點)、SS1 search 半邊(`q` 參數
   +total/estimate)、SRC2(soft-disable+不可變 URI,GAPS option 2)——
   **一個回合議定全部端點**:先出契約 PR(schema+DESIGN §26 新 DR),再逐
   任務落地 runtime。SRC2 ⚠️ 旗標解除。
3. **SS1a filters-only 切片核准**:SS1 拆兩半 — filter facets(GOV4
   allowlist 機制+既有 Sort 參數,免 DR-002)先做;search 半邊入打包回合。

**How to apply:** loop 取任務時 CFG1/SRC2 不再等 owner;契約回合開工時引用
本核准(TASKS.md 的 ⚠️ 註記在各該任務 PR 內順手改掉,遵守 checkoff lint:
不得重寫已勾稽行,未勾稽行的文字修改無妨)。

---
name: graphrag-track5-owner-approvals
description: owner 2026-07-17「全部同意」— CFG1(#93 已 merge)+DR-002 打包回合(待做)+SS1a(#92 已 merge);⚠️ 旗標解除
metadata: 
  node_type: memory
  type: project
  originSessionId: d673e708-e836-4b8a-8fc7-cb33527c5fc3
---

Owner 2026-07-17 對 Track 5 待決事項回覆「全部同意」,三組核准全數生效。
**執行狀態(2026-07-18)**:第 1 組 CFG1 已依驗收形狀落地(PR #93 merge,
DESIGN §26 DR-012;`graphrag serve-mcp` 起單一 gateway);第 3 組 SS1a 已落地
(PR #92)。**尚待執行的只剩第 2 組 DR-002 打包契約回合**(含 Console MCP
URL/健康顯示——需要曝露 gateway 設定的端點,隨打包回合議定)。

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

---
name: graphrag-goal-museum-guide
description: 初期落地場景——博物館導覽 agent:外部 no-code agent 平台經 MCP(HTTP)接本平台當場所知識服務;資料已建置(nmmst 全量,connector 已上線);硬約束=不失一般性
metadata: 
  node_type: memory
  type: project
  originSessionId: 1ca14cc5-3e0a-461e-ae04-ca06338ee1f2
---

owner 2026-07-10 闡述初期目標:
- owner 有一個 agent no-code 平台,可接外部 MCP(**HTTP**);平台上調教的 agent 可輸出成 API 嵌進應用程式,成為對話應用。
- 正在幫**博物館**建置導覽 agent:本 graphRAG 專案=場所知識的 serving 服務,經 **MCP over HTTP** 接給該 agent 使用 → **C8b(MCP HTTP transport)是主要使用場景的整合縫**,不只是 nice-to-have(owner 已定案排在 BA3c 之後)。
- **資料已落地**:海科館全量 425 列已建置(xlsx connector=SRC1 #85 已上線;nmmst 專案 1409 實體/1158 關係),見 [[graphrag-sample-data-hakeguan]]。
- **硬約束:不失一般性**——平台未來要支援更多不同性質的專案;針對博物館資料的強化一律走 per-project config(§6 ontology/mapping)與可插拔 connector(C2),不寫死領域邏輯。
- 衍生注意:MCP HTTP 對外曝露後,§23 auth placeholder 會變成真需求(外部平台呼叫的認證);凍結 enum 無 auth 錯誤碼,屆時是 DR-002 相關的 owner 決策。

**Why**: 排序與取捨要對齊真實落地(博物館 agent 經 HTTP MCP 取知識);一般性是 owner 的明示邊界。
**How to apply**: 涉及 MCP transport/auth/connector/ontology 的任務,先對照此場景;領域強化永遠 config 化。

相關:[[graphrag-architecture]]、[[graphrag-working-style]](C8b 插隊定案史料:PR #57,見 git history 的舊 lesson catalog)

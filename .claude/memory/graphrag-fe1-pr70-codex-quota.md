---
name: graphrag-fe1-pr70-codex-quota
description: FE1 PR
metadata: 
  node_type: memory
  type: project
  originSessionId: 1ca14cc5-3e0a-461e-ae04-ca06338ee1f2
---

**Codex quota 是有限資源 (2026-07-12 實測):** PR #70(FE1)的長 finding-chain(26 個 findings、19 輪本地 re-review)把 Codex code-review 額度用盡,bot 直接留言 "You have reached your Codex usage limits"。限流期間 push 不會觸發 auto-review;額度恢復後要留言 `@codex review` 手動觸發。對策:prep dossier 越完整、單輪修越徹底(同類一次掃完),消耗輪數越少。

**FE1 finding 主軸(retro 素材,26 個 findings:25 修+1 可查核 decline,全數真實):** FE 必須鏡射「後端實際行為」,契約的示意文件不可信 — source kinds 實際只有 text/structured(非 file/directory/url/database)、ingest 與 build 是同一條 full pipeline(只差 job kind 標籤)、text 需 file:// 目錄、structured 需 table/pk_column metadata、ontology 缺失 → graph 階段死。uri gate 收斂到:canonical `file:///`、無 host/query/hash、decoded path 單一前導斜線、驗證「存儲原文」不可 trim(Python urlparse 保留尾端空白,瀏覽器 URL/trim 會吃掉 → display/read 分歧)。run gate 的正確標準不是「不會失敗」而是「會讀到 operator 註冊的那個路徑」(silent wrong-data 比 loud failure 更糟)。所有 react-query gate 要 fail-closed:data 存在不夠,`isFetching` 期間(冷載入、invalidation refetch、refocus refetch)一律關閉(class 10 TOCTOU 的 stale-data-during-refetch 變體)。

**兩個 owner 未決 follow-up:** (1) 後端 `_local_path`(core/builds/sources.py)應對「顯示路徑≠實讀路徑」的 file uri 直接 raise(非 file scheme、netloc 非空、decoded path 以 // 開頭、malformed escape、邊緣空白),Console gate 蓋不到 CLI/API/MCP 觸發的 build — 本地 reviewer 兩度建議。(2) ontology-configuration UI(FE1 只 surface 缺 ontology、不收集)。

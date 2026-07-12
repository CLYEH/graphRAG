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

**Follow-up 狀態:** (1) 後端 `_local_path` 硬化 — **已完成**(BA9 / PR #71,2026-07-13 merge:`7859ede`)。收斂遠超原提案:不只 raise,還把 SoR 與 Console 的接受集用**共用 corpus**(`tests/fixtures/canonical_file_uri.json`,pytest + vitest 同讀)機械綁定,並用「讀 `nturl2path`/`unquote` 原始碼列三軸」取代逐拼法枚舉 — 見 [[graphrag-loop-paused-pr5]] 的 lesson class 16。(2) ontology-configuration UI(FE1 只 surface 缺 ontology、不收集)— **仍未決,owner 未授權**。

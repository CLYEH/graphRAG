---
name: graphrag-open-followups
description: 尚未立案的懸置 follow-ups 集中帳(2026-07-17 memory 大掃除時自四個已刪檔抽出)
metadata: 
  node_type: memory
  type: project
  originSessionId: d673e708-e836-4b8a-8fc7-cb33527c5fc3
---

散落在已刪除記憶檔裡仍然「活著」的 follow-ups,集中一處(狀態以 TASKS.md/
GitHub 為準;立案或了結後從本檔劃掉):

- **useCancelJob 無 Idempotency-Key**(FE8 殘留,owner deferred):Console 寫入
  的 retry-safe 一致性缺一角;FE5 同類已修。小任務量級。
- **ontology-configuration UI**(FE1 殘留,owner 未授權):Import 頁只 surface
  「缺 ontology」不提供收集表單;UXB1 蓋了「編輯已設定 ontology」,首次
  設定的引導仍缺。
- **UXC2 codegen follow-up**:`format: binary` 經 openapi-typescript 產出
  `string[]`(上傳欄位型別不精確);等 codegen 工具鏈升級或手寫 override。
- **教學文件位置**(reference):`.discuss/tutorial/`(gitignored,owner 指定)
  — TUTORIAL.md + 8 截圖 + 可重現語料;產品首次端到端實證(2026-07-13)。
- **repo 衛生**:#93 已把 `projects/{museum,nmmst}/eval/golden.yaml` commit
  (owner 可 revert;已在 PR 揭露)並刪除 per-project `config.yaml`/
  `mcp_entrypoint.py`(CFG1)。現剩 untracked `data/`(runtime 產物):
  .gitignore 或清除未決。
- **policy.py 殘留 nit**(#93 reviewer 非阻塞):`load_query_policy` 的
  `text=` 參數已無 caller 使用(worker 走 registry 路徑後孤兒化)+docstring
  該段落過時 — 小清理任務量級。
- **server.py dispose 不對稱**(#93 reviewer 非阻塞,先於 CFG1 即存在):
  lifespan 中 client factory(qdrant/neo4j/embedder/llm)建構失敗時 engine
  不 dispose(NullPool 故無實質洩漏);policy 失敗路徑已修(R5),此半邊
  順手補即可。
- **MCP auth**:CFG1 gateway 不帶 auth(owner 2026-07-17 預設同意);對外
  曝露後 §23 placeholder 會變真需求,屆時是 DR-002 相關 owner 決策
  (凍結 enum 無 auth 錯誤碼)。

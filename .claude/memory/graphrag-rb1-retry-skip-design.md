---
name: graphrag-rb1-retry-skip-design
description: RB1-retry-skip(TASKS.md line 140,RB1 切片 B-2)owner 定案設計:pin 父設定 + v1 最小刀 + 選擇性 clone 關鍵正確性洞見;**已實作並 merge(#103,Codex 4 輪)** — 設計如下,實際加了 fork-C/config-pin/chunk_id-remap/entanglement 四道守衛(見 lesson catalog #103)
metadata:
  node_type: memory
  type: project
  originSessionId: d673e708-e836-4b8a-8fc7-cb33527c5fc3
---

RB1-retry-skip 是 RB1-retry 線最後一片(retry-core #100 已 merge)。目標:重試時
只重跑「失敗文件」的 graph LLM 抽取,複製「成功文件」的圖層產物,省 LLM 成本、
消漂移、凍語料。`retry_failed_only()`(`core/observability/spec.py:73`)首次接上
production。

**Owner 2026-07-19 兩項定案**(AskUserQuestion):
1. **設定處理 = A 綁定父設定**(凍語料完備):重試子 build 用「父建置的設定」重跑,
   非現行 `projects.config`。因子 clone 的成功產物是用父設定抽的,失敗文件若用現行
   設定重抽會混兩份設定(chunk/ontology 漂移 → source_ref ordinal 對不上 =
   四不像建置)。**實作找到**:設定是 pin 在 **jobs.config_snapshot**(每 job 一份,
   `create_job` 於建 job 時擷取 `projects.config`;worker 走 `capture_config_snapshot`
   fallback)。retry 端點目前經 `create_job_exclusive` 擷取「現行」config → 需改讓子
   retry job 帶「父的」config_snapshot。取父設定兩路:(a)查父 build 的 build-kind job
   之 config_snapshot(**jobs 無刪除**故存活,但一 build 可有多 job〔eval job 也帶
   build_id〕須濾 build/retry kind);(b)新增 `builds.config_snapshot` 欄(migration,
   1:1 不歧義,較穩健)。傾向 (b) 但 (a) 免 migration——實作時定。
2. **範圍 = A v1 最小刀**:只跳過成功文件的 graph LLM 抽取;resolve/index/summarize
   照全跑(重嵌入/重摘要,其投影/報告 per-build 未 clone);且**只在父「於 graph 階段
   失敗」時**做 clone+skip,更後階段失敗退回 retry-core 全部重導。更寬(clone
   Qdrant/Neo4j 投影+community_reports 讓 index/summarize 也跳)留後續切片。

**關鍵正確性洞見(Plan agent 追碼確認,實作必守)**:抽取是「逐項 commit」的——
失敗文件在父其實已「部分寫入」圖層(失敗前跑完的 chunks)。故「clone 整張父圖 + 只
重抽失敗文件」是**錯的**:重抽失敗文件會在已 clone 的部分列旁「新增漂移列」= 鬼影。
clone 必須**選擇性**:只 clone「成功文件」可歸屬的 text 產物,失敗文件整份重抽 fresh。
歸屬不變量已驗:relation 的每條 evidence 對應之文件必也 mention 兩端點(text 逐 chunk
`accepted_keys` 重置)→ 用 `entity_key`/`relation_signature`(每 build 凍結身分)當
id-remap 橋,clone 可全 server-side、記憶體有界(承 #100 P2 原則),FK 恆可滿足。

**檔案級計畫**(Plan agent 產出,完整版在本 session transcript):
- `core/observability/reads.py`:新 `latest_run_graph_failed_items(conn,project,build_id)`
  讀父「最新 run 的 graph step、status=failed、item_kind=document」→ 經 `retry_failed_only`
  濾出 `failed_docs` content_hash 集合(graph-step-scoped:index 失敗的 doc graph 是好的)。
- `core/builds/retry.py`:`CloneCounts` 加 entities/relations/mentions/evidence;新
  `clone_graph_artifacts(...failed_content_hashes)` 四條 idempotent server-side
  `INSERT..SELECT`(entities→mentions→relations→evidence,以 `<> ALL(:failed)` 空集安全、
  `NOT EXISTS` 去重;只 clone text-attributed,structured 讓 `extract_structured`
  重建〔確定性、無 LLM〕)。
- `core/graph/documents.py`:`extract_documents(..., extract_only: frozenset[str]|None=None)`,
  mime 過濾後 `if extract_only is not None and doc.content_hash not in extract_only: continue`
  (`None`=一般 build 全抽,不變)。reused doc 建議記 `ItemOutcome("document",hash,"skipped")`。
- `core/builds/stages.py`:`_is_retry_build`→`_retry_parent(conn,build_id)->uuid|None`;
  `_graph_stage`:若 retry 且「父於 graph 失敗」→ 讀 failed_docs、`clone_graph_artifacts`
  (在 extract 前,讓 `BuildGraphState.preload` 見 clone)、`extract_documents(extract_only=failed_docs)`。
- clean 不變(續重 chunk 全部);resolve/index/summarize 不變(全跑,per-item self-skip)。
- **不 clone** `vector_point_id`/`embedding_point_id`(Qdrant/Neo4j 點 per-build 未 clone,
  clone point-id 會假稱「已嵌入」)。cloned `relation_evidence.chunk_id` 懸置容忍
  (非 FK,offset 為 document-absolute,raw 原樣 clone)。

**測試計畫(encode why)**:extract_only 只抽 B(fake LLM call-count 證 A 未呼);
latest_run_graph_failed_items 只回最新 run 的 failed;整合:2-doc(A 成功 B 失敗)retry →
子只重抽 B、A 產物 = 父(fresh id)、**merged 子圖 == 全新 rebuild 圖**(entity_key/
signature/evidence_hash 集相等,fake LLM 確定性);clone 冪等跑兩次無 unique 違反;
空 failed set(父於 index 失敗)→ 零重抽、graph LLM 未呼。mutation-probe 每 must-fix。

五個 fork:A(config,選定 pin)、B(scope,選定 v1)已定;C(父於 graph 後失敗→退回全導,
v1 採此)、D(chunk_id 懸置,容忍)、E(reused 記 skipped)採建議預設。

實作時遵 §5/§18/§27.7;預期如 retry-core 多輪 Codex(深端點+跨庫 clone+多失敗態)。
關聯:[[graphrag-track5-owner-approvals]] [[graphrag-open-followups]]。

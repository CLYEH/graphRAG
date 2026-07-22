---
name: graphrag-gov-fe-design
description: GOV2-fe+GOV3-fe 治理中心 owner 2026-07-19 定案:分頁式治理頁 + 兩 gap 清單延後另立 api 任務 + 發布閘顯示型;**GOV3-fe 已 merge(#104,Codex 3 輪);GOV2-fe 拆 fe-1/2/3/4;fe-1 entity(#105)、fe-2 relation(#106)、fe-3 治理待辦面板(#107,Codex 2 輪→scope-honesty:GovernanceBacklog 依真實 scope 分組)、fe-4 已排除/復原視圖+增量分頁(#108,Codex 2 輪→鎖述詞含 error 臂+retry 改全量 refetch+idem-key 每邏輯復原一把)皆已 merge=**GOV2-fe 全四片收官**;GOV2-facet(#109 relation 品質 facet)已 merge;gap-list 片=#119 GOV2-fe-5 已 merge(GOV2 umbrella 收攏);尚餘 relation 影響抽屜+候選-scoped preflight(follow-ups)**
metadata:
  node_type: memory
  type: project
  originSessionId: d673e708-e836-4b8a-8fc7-cb33527c5fc3
---

GOV2-fe + GOV3-fe = Console 治理中心(§17 四種審核類型:合併/實體/關聯/本體提案)。
契約與 codegen 皆凍結就緒(`web/src/api/schema.ts` 已含 approve/reject entity/relation、
listOntologyProposals + accept/reject)——**純 runtime FE**,不動 contracts、不動 codegen。

**Owner 2026-07-19 三定案(AskUserQuestion)**:
1. **頁形式=A 分頁式治理頁**:把現有「審核」頁擴成分頁(合併/實體關聯審核/本體提案
   〔+日後 低信心/缺證據〕)。route 保持 `review`,nav 標籤 審核→治理
   (`web/src/components/AppShell.tsx:21`)。分頁狀態入 URL `?tab=`,供 Health 深連結。
2. **兩 gap 清單=A 先做能做的、延後另立 api 任務**(歷史決策;api 已於 #109 GOV2-facet
   落地,現僅剩 FE 消費):本次出 GOV3 提案 + 實體/關聯審核 + Health 深連結 + 發布閘面板;
   低信心/缺證據清單延後另立 SS1a-式 api facet 任務——該任務已完成,見下。
3. **發布閘=A 顯示型建議面板**:後端 preflight 實際「不擋」待審/缺證據/抽取失敗
   (只擋 §19 drift + §20 eval gate,`core/builds/lifecycle.py:445-503`)。故發布閘為
   **顯示型/建議**——於上線控制旁列品質數當警告+深連結,不擋上線(上線鈕仍照傳伺服器
   §14 裁決,不可假擋)。

**Plan agent 驗證的關鍵碼事實**:
- **實體/關聯決定後端「可反覆」**(`api/routers/review.py:447-449`;`core/resolve/decisions.py:140`)
  ——re-decide append ledger、無終態。**但(Codex #105 修正,見 [[graphrag-lesson-classes]] #105)
  「後端可反覆」≠「UI 該省確認」**:反覆性在 GOV2-fe-1 的 UI **不可達**(決定即移除列、無已決定
  視圖),故不能靠它省確認。最終落地=**退回 sibling §17 模式**:決定性 `${id}:${verb}` idem-key
  (queue-decided-once→重放不雙記 ledger,非隨機鍵)+ 破壞性「排除(reject)」加確認步驟
  (role=alertdialog)、非破壞「保留(approve)」行內。完整已決定/復原視圖 = GOV2-fe-4。
- **審核佇列 filter=`filter[status]=needs_review`**(Health 同述詞算 needs_review_*,
  佇列/gauge parity)。`/entities` 支援 `q`、`/relations` **不支援**(送 q→400)。
- **本體提案池**預設佇列 `proposed`、**非 build-scoped**(全專案)、欄位全在列上
  (type_name/kind/example/chunk_ref),accept 改 projects.config.ontology(須 invalidate
  project config 快取)。
- **兩 gap = 已解(#109 GOV2-facet)**:`/relations` allowlist 現含 `confidence`/`evidence`
  兩 CLOSED facet(`low`/`missing`,免契約 bump);述詞與 §19 gauge 同讀
  `LOW_CONFIDENCE_BELOW` 常數(同源單讀,可覆寫參數已移除)。**FE 消費=#119 GOV2-fe-5 已 merge**:兩分頁 +
  Health/Overview 雙深連結;測試斷言同送 `filter[status]=active`+facet(facet 正交,gauge
  parity 靠組合)已釘住。
- 無 review_ledger 讀端點→實體/關聯的 per-decision audit 僅能顯示回傳 DTO 的
  review_status/updated_at(全歷史缺,小 gap,接受)。

**切片(RB1-式增量,各自 vitest 綠 + 瀏覽器 QA)**:
1. GOV3-fe 本體提案池(最小、全服務):`useOntologyProposals`+`useDecideOntologyProposal`
   + ProposalPool 分頁;順手加分頁 host 骨架到 ReviewQueue.tsx。**✅ 已 merge #104**
   (連同切片 3 Health 深連結;Codex 3 輪教訓見 [[graphrag-lesson-classes]] #104:UI 字串
   改名掃 e2e spec、react-query 單-observer 併發卸離→整池鎖、終態動作沿用姊妹流確認步驟)。
2. GOV2-fe 實體/關聯審核佇列 → **實際拆 fe-1/2/3/4**:
   - **GOV2-fe-1 entity 審核 = ✅ 已 merge #105**(Codex 2 輪;教訓見 [[graphrag-lesson-classes]] #105:
     reversible→無確認+隨機鍵是過度聰明設計,被 Codex 三刀反推回 sibling 模式=決定性鍵+排除確認;
     同頁 intro 散文與確認警語矛盾要對齊;triage 改設計即改 TASKS.md 已勾稽準則;共用 hook 無-caller
     分支以 renderHook 釘契約)。共用 `useEntityReviewQueue` + `useDecideReviewTarget`(entity+relation
     皆走此 hook,決定性鍵,已含 relation 分支+契約測試)。
   - **GOV2-fe-2 relation 審核 = ✅ 已 merge #106**(Codex **5 輪**全 P1/P2;教訓見
     [[graphrag-lesson-classes]] #106):relation 比 entity 豐富——per-row RelationRow 以 useEntity
     解析 src→type→dst 名稱(決定 gate 至兩名載完,pending+error 皆鎖+重試)+ confidence + evidence
     引文懶載自 detail(**list 端點省略 evidence**)。沿用 fe-1 修正後模式(`useDecideReviewTarget`
     kind="relation" 決定性鍵、排除加確認、整池鎖)+ **決定後 refetch 窗鎖定(queue.isFetching,
     ReviewCases 既有 queueRefreshing 樣式)**。此 refetch-race 亦回補 EntityReview + ProposalPool
     (grep-all-instances,三審核面齊)。Health `needs_review_relations`→`?tab=relation`。
     **元教訓:豐富上下文的決定面,「操作者看得到所決之物」須在每個決定入口 × 每個資料未就緒態強制。**
   - evidence 引文已於 #106 懶載落地(useRelation);**影響/impact 抽屜(useSubgraph,重用
     ReviewCases 模式)移出 fe-4,列為獨立 follow-up**。
3. Health 深連結:ProjectHealth.tsx `COUNT_LABELS` 各非零訊號 →治理分頁(count>0 才連結)。
   `TAB_FOR_COUNT` 映射:`pending_ontology_proposals`→proposals(#104)、`needs_review_entities`→entity
   (#105)、`needs_review_relations`→relation(#106)皆已落地;`low_confidence_relations`→low-confidence、`missing_evidence_relations`→missing-evidence(#119)——五訊號全接。
4. GOV2-fe-3 = ✅ 已 merge #107(Codex 2 輪皆 scope-honesty,見 [[graphrag-lesson-classes]] #107):
   原「發布閘」框架被修正——Health counts 為 active-build scoped(提案池 project-wide),故面板
   改名 **GovernanceBacklog**、依真實 scope 分組(上線中知識庫/全專案,各組非零才渲染)、標題
   「品質治理待辦(僅供參考,不影響上線)」;不擋上線。**候選-scoped 發布 preflight 需 per-build
   health facet(契約變更)= 新 follow-up,owner 決**。
5. **GOV2-fe-4 審核佇列 robustness = ✅ 已 merge #108**(Codex 2 輪,見
   [[graphrag-lesson-classes]] #108):(a)已排除/復原視圖(已排除者可 restore;idem-key
   每邏輯復原一把——首擊 mint、失敗沿用、成功清)+(b)兩 review 佇列 useInfiniteQuery
   增量分頁(build-pin 騎 pageParam)皆已落地;(c)relation 影響/impact 抽屜(useSubgraph)
   **移出 fe-4,列為獨立 follow-up**(見 [[graphrag-track5-owner-approvals]])。

**測試紀律**:mutation+invalidate probe(approve 打對端點+invalidate 佇列+health;
proposal accept 另 invalidate project config);**filter 正確性**(兩 fetcher 都斷言帶
filter[status]=needs_review、relations 不帶 q——否則假綠,FE3 教訓);verb-rides-URL;
UXA3 譯詞 Record-keyed(chromeInvariant sweep 無 raw UUID/snake_case);false-affordance
(缺證據誠實佔位、低信心若走 client 標「僅已載入」、發布閘不假擋);deep-link routing;
build-scope pin 失敗即炸。關聯:[[graphrag-track5-owner-approvals]] [[graphrag-fe-browser-qa]]。

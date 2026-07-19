---
name: graphrag-gov-fe-design
description: GOV2-fe+GOV3-fe 治理中心(TASKS.md 125/128)owner 2026-07-19 定案:分頁式治理頁 + 兩 gap 清單延後另立 api 任務 + 發布閘顯示型;**GOV3-fe 切片 1+3 已 merge(#104,Codex 3 輪);尚餘 GOV2-fe 切片 2/4**
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
2. **兩 gap 清單=A 先做能做的、延後另立 api 任務**:本次出 GOV3 提案 + 實體/關聯審核 +
   Health 深連結 + 發布閘面板;低信心/缺證據清單**後端目前無法提供**(見下),另立
   SS1a-式 api facet 任務,日後切片補上。
3. **發布閘=A 顯示型建議面板**:後端 preflight 實際「不擋」待審/缺證據/抽取失敗
   (只擋 §19 drift + §20 eval gate,`core/builds/lifecycle.py:445-503`)。故發布閘為
   **顯示型/建議**——於上線控制旁列品質數當警告+深連結,不擋上線(上線鈕仍照傳伺服器
   §14 裁決,不可假擋)。

**Plan agent 驗證的關鍵碼事實**:
- **實體/關聯決定「可反覆」**(`api/routers/review.py:447-449`;`core/resolve/decisions.py:140`)
  ——re-decide append ledger、無終態(不同合併的一次性),故用「清單+行內動作」輕量 UI,
  非合併的兩步確認。
- **審核佇列 filter=`filter[status]=needs_review`**(Health 同述詞算 needs_review_*,
  佇列/gauge parity)。`/entities` 支援 `q`、`/relations` **不支援**(送 q→400)。
- **本體提案池**預設佇列 `proposed`、**非 build-scoped**(全專案)、欄位全在列上
  (type_name/kind/example/chunk_ref),accept 改 projects.config.ontology(須 invalidate
  project config 快取)。
- **兩 gap**:`/relations` filter allowlist 僅 `{type,status,review_status}`——**無
  confidence/evidence facet**;Health 有算數(confidence<0.5、NOT EXISTS evidence)但無
  列端點。低信心可 client-side 部分(confidence 在列上,但=Graph 假承諾、無法對上 Health
  真數);缺證據**完全無法** client-side(evidence 只在明細)。→ 需小 api facet 任務
  (filter[confidence]/filter[evidence],**免契約 bump**,SS1a 先例)。已 flag 於
  [[graphrag-open-followups]]。
- 無 review_ledger 讀端點→實體/關聯的 per-decision audit 僅能顯示回傳 DTO 的
  review_status/updated_at(全歷史缺,小 gap,接受)。

**切片(RB1-式增量,各自 vitest 綠 + 瀏覽器 QA)**:
1. GOV3-fe 本體提案池(最小、全服務):`useOntologyProposals`+`useDecideOntologyProposal`
   + ProposalPool 分頁;順手加分頁 host 骨架到 ReviewQueue.tsx。**✅ 已 merge #104**
   (連同切片 3 Health 深連結;Codex 3 輪教訓見 [[graphrag-loop-paused-pr5]] #104:UI 字串
   改名掃 e2e spec、react-query 單-observer 併發卸離→整池鎖、終態動作沿用姊妹流確認步驟)。
2. GOV2-fe 實體/關聯審核佇列:`useReviewEntities`(帶 q)/`useReviewRelations`(不帶 q)
   + `useDecideEntity`/`useDecideRelation`(verb rides URL、per-attempt random idem-key、
   onSuccess invalidate 佇列+`["health",project]`);行內 approve/reject + 明細抽屜
   (evidence via useRelation、impact via useSubgraph,重用 ReviewCases 模式)。
3. Health 深連結:ProjectHealth.tsx `COUNT_LABELS` 各非零訊號 →治理分頁(count>0 才連結)。
   **部分於 #104**:`TAB_FOR_COUNT` 映射,`pending_ontology_proposals`→`?tab=proposals`
   已落地;entity/relation/低信心/缺證據各項待 GOV2-fe 補入 `TAB_FOR_COUNT`。
4. 發布閘顯示型面板(Overview.tsx ActivateControl 旁)。**待 GOV2-fe**

**測試紀律**:mutation+invalidate probe(approve 打對端點+invalidate 佇列+health;
proposal accept 另 invalidate project config);**filter 正確性**(兩 fetcher 都斷言帶
filter[status]=needs_review、relations 不帶 q——否則假綠,FE3 教訓);verb-rides-URL;
UXA3 譯詞 Record-keyed(chromeInvariant sweep 無 raw UUID/snake_case);false-affordance
(缺證據誠實佔位、低信心若走 client 標「僅已載入」、發布閘不假擋);deep-link routing;
build-scope pin 失敗即炸。關聯:[[graphrag-track5-owner-approvals]] [[graphrag-fe-browser-qa]]。

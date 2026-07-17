---
name: graphrag-sample-data-hakeguan
description: 海科館適配進行中(nmmst 專案)；pilot 30 列全綠、缺口帳本在 .discuss/hakeguan/GAPS.md(G1-G4)；owner 定案=缺口先累積、夠量才開 task
metadata: 
  node_type: memory
  type: project
  originSessionId: a5de076e-8823-47d9-a44a-b3fd7b14df4e
---

**Owner 交辦（2026-07-13，明說「不用現在做」）**：v2 全部完成後，拿**真實場域資料**實際跑這個 repo 的功能，**看缺什麼再補**。這是 [[graphrag-goal-museum-guide]] 那個落地場景的 raw data，之前一直缺、現在到位了。

**資料位置**：`C:\graphRAG\.discuss\sample_data`（184K，5 份 **.xlsx**）
- `2-1.基礎服務知識庫.xlsx`
- `2-2.導覽內容知識庫.xlsx`
- `海科館_2-2.導覽內容知識庫_區域探索廳_1150612.xlsx`
- `海科館_2-2.導覽內容知識庫_研究典藏館_0617_omnigfix.xlsx`
- `國立海洋科技博物館__iMuseum_FAQ.xlsx`

場域＝**國立海洋科技博物館（海科館）**。內容形狀：基礎服務（開放時間/票價/交通這類 FAQ 型）＋導覽內容（展廳/展品敘述型）＋FAQ。這正好對上 museum-guide 場景要的「場所知識服務」。

**已經看得到的缺口（第一眼，未深入）**：
- **沒有 xlsx connector**。`core/builds/sources.py` 只 wire 了兩種 kind：`text`（讀 file:// 目錄下的 .txt/.md）與 `structured`（讀單一 CSV，需 metadata 的 `table`/`pk_column`）。xlsx 完全沒有路徑 → 要嘛先轉 CSV/txt，要嘛新增一種 kind（新 connector）。這是「缺什麼功能」清單的第一項，幾乎確定。
- FAQ 型（Q/A 成對）與敘述型（長文）**混在同一批資料**裡，切塊策略未必同一組參數合用 → 可能牽動 chunking 參數（見 [[graphrag-dr002-cleaning-round]]）或需要 per-source 設定。
- ontology 需要為「博物館」領域定義（展品/展廳/服務/票種…），目前 ontology 只能從專案 config 塞、還沒有 UI（ontology-configuration UI 仍是 owner 未決的 follow-up，見 [[graphrag-fe1-pr70-codex-quota]]）。

**動手時機**：v2（FE3 → BA4 → FE2 → FE4）全綠之後。不要提早開，會跟 v2 的任務搶範圍。

**偵察完成(2026-07-13,教學任務後)**——五份 xlsx 的實際結構(讀自 sharedStrings,UTF-8 正常):

| 檔案 | 結構 | 內容 |
|---|---|---|
| 2-1.基礎服務知識庫 | 編號/主題/**問題**/**答案** | 服務 FAQ(票價、場租、餐飲…) |
| 2-2.導覽內容知識庫 | 編號/**標題**/**內容詳情**/時效資訊/相關連結/**位置**/分類 | 展品敘事(海祭、達悟族拼板舟…) |
| iMuseum_FAQ | 編號/主題/**問題**/**答案** | 參觀 FAQ(開放時間、吉祥物北火熊…) |
| 2-2_區域探索廳 | 同 2-2 schema | 單館別匯出 |
| 2-2_研究典藏館(omnigfix) | 同 2-2 schema | 館藏物種(圓眼燕魚、花尾鷹䱵…含分布/棲地) |

**適配要點**:
- 本質=**逐列文件 + 結構化欄位**:「位置」(如「研究典藏館 1F 海洋人文區」)天然對應 LOCATION 實體、
  「分類」「時效資訊」是 metadata;內容詳情是抽取主體(展品/族群/儀式/物種實體密度高)。
- **缺口確認:無 xlsx connector**。兩條路:(a) 前處理腳本 xlsx→逐列 .md/.txt(帶 metadata 標頭),
  先用既有 text connector 跑通(快、不動契約);(b) core/ingest/connectors.py 新增 xlsx kind + 欄位映射
  config(正規解,可能需要 openpyxl 依賴 + TASKS 新任務)。建議先 (a) 驗證抽取品質再決定是否投資 (b)。
- ontology 草案:EXHIBIT/LOCATION/SPECIES/ETHNIC_GROUP/EVENT/FACILITY;LOCATED_IN/BELONGS_TO/
  PRACTICED_BY/EXHIBITS。FAQ 類(2-1/iMuseum)更適合純 semantic 檢索,圖譜價值在 2-2 導覽內容。
- 教學的 museum 專案(.discuss/tutorial)就是這個場景的迷你彩排——同一套流程直接放大。

**適配實跑(2026-07-13~14,pilot 完成)**:
- 專案 `nmmst`(display 海科館導覽);前處理=`.discuss/hakeguan/preprocess.py`
  (xlsx→每列一 .txt,`--pilot N` 取每檔前 N 列;防禦模板千空列/壞 dimension/float 編號)。
- **Pilot 30 列全綠**:build ~5min → 124 實體/98 關係(0 缺證據)/15 社群報告;
  golden eval 4/4(`projects/nmmst/eval/golden.yaml`);已 activate。
  Review 流程首次真實觸發:3 合併候選=2 併(全名簡稱/館廳混用)+1 駁(生物區vs人文區),
  決策入 review_ledger——全量 build 要驗 DR-003 自動套用。
- **缺口帳本=`.discuss/hakeguan/GAPS.md`**(G1 xlsx connector/G2 source 生命週期 API/
  G3 上游截斷/G4 抽取雜訊 + 觀察 O1-O3)。**owner 2026-07-14 定案:缺口的來龍去脈
  記在 .discuss 累積,「還有很多需要討論的」,等累積夠了再開 TASKS 任務——別急著開 task。**
- `projects/nmmst/`(config.yaml/eval/golden.yaml/mcp_entrypoint.py)與 museum 同樣 untracked,
  doc-lane commit 時別被 `git add -A` 掃進去。
- 工具教訓:`GET /jobs/{id}` 終態字彙是 **done**(非 succeeded);entities 列表欄位是
  `canonical_name`、merge-candidate 快照欄位是 `name`;jobs 端點在頂層(非 project-scoped);
  merge-candidates 是 active-build-scoped(build ready 時查=0)且固定只回 pending+deferred
  (Filter 參數契約有、實作延後,帶了會被靜默忽略)。
- **全量 425 列完成(2026-07-14)**:410 文件(15 份=上游重複列,去重正確)→ 1409 實體/
  1158 關係/93 報告,eval 4/4,已 activate;55 個 pending 候選**留給 owner 審**。
  **DR-003 驗證 2/3**:approve 吸收+reject 抑制都成立;`區域探索館↔廳` 因 LLM 型別漂移
  (EXHIBIT→FACILITY/LOCATION)fingerprint 對不上而重浮——規模化主病根=型別不穩定
  (同名實體劈成 3-4 型別、EVENT 12→264 不成比例),已記入 GAPS.md G4。
- **G5(owner 2026-07-14 回饋)**:web「光看了不會操作」——主線有三個 UI 洞
  (config=curl、eval=CLI 無端點、activate=無按鈕),詳 GAPS.md G5。

**SRC1 ✅ merged(PR #85,2026-07-17,580b889)**:xlsx 原生 connector 上線——每列渲染
text/plain 文件(【label】title/編號行/extra 行/body)、#row=<id> 溯源、欄位對應住
source.metadata(title/body 必填,id/extras/label 選填,全 fail-loud);髒資料防禦全數
落地(全形/半形標頭附註、謊報 dimension+預編號模板尾列停掃、float 編號、撞號/歧義標頭
大聲失敗、同內容異列以作者編號入文保身分)。4 輪 Codex 各 1 P2+本地 reviewer 抓「全形
regex 其實是半形」假綠對(codepoint dump 實證),詳 [[graphrag-loop-paused-pr5]] #85。
實資料 QA:區域探索廳 19 列 0 LLM 花費全數落 SoR。preprocess.py 就此退役;G1 關閉。

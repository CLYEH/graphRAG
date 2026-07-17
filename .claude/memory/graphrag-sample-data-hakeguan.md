---
name: graphrag-sample-data-hakeguan
description: 海科館(nmmst)真實資料:全量 425 列已建置(1409 實體/1158 關係);缺口帳本 GAPS.md 已全數化作任務或了結
metadata: 
  node_type: memory
  type: project
  originSessionId: d673e708-e836-4b8a-8fc7-cb33527c5fc3
---

**nmmst 專案 = 海科館全量真實資料**(owner 的博物館導覽場景,見
[[graphrag-goal-museum-guide]]):425 列 xlsx → 1409 實體/1158 關係,
eval 4:4。原始 5 份 xlsx 在 `.discuss/sample_data`(gitignored)。

**全量實測的兩個主發現**(已催生任務並了結/立案):
- **型別漂移=主病根**:同一真實事物被 LLM 跨 build 標成多型別 → DR-003
  carry-forward 1/3 白審 → **GOV1(#86)以 DR-011 修畢**(ledger 鍵
  type-free 化+跨型別 resolve)。
- **缺口帳本** `.discuss/hakeguan/GAPS.md`(G1-G5+O1-O4)2026-07-15 化作
  TASKS:G1→SRC1✅#85(xlsx connector)、G2→SRC2(07-17 已核准入 DR-002
  打包回合)、G4→GOV1✅/GOV2、O4→GOV4✅#87;G3=資料方 action、
  G5=Track 4 已覆蓋、O2=H11。

**操作備忘**:job 終態=`done`(非 succeeded);dev 庫的 nmmst/museum 資料
會踩全表計數 integration 測試(=H11 任務要修的事,測試該 scope 自己的
專案)。教學文件在 `.discuss/tutorial/`(產品首次端到端實證)。

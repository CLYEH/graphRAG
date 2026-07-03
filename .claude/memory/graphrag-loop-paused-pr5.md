---
name: graphrag-loop-paused-pr5
description: loop 流程教訓(Codex 三管道監聽、merge 權限、TASKS.md 慣例)——PR/任務即時狀態一律查 TASKS.md 與 GitHub,不要信任本檔的狀態敘述
metadata: 
  node_type: memory
  type: project
  originSessionId: 3cfe1f1b-4e4e-4d2f-940f-12a9f9d7def4
---

**PR/任務的即時狀態請一律查 `TASKS.md` 與 GitHub(`gh pr view`/`gh pr list`)。這份筆記只保留跨任務可重用的流程教訓,任何「目前狀態」敘述在下次 push/merge 後就會過期,不可當作現況依據。**

- **教訓(監聽 Codex)**:Codex 的「changes wanted」走 **`pulls/N/reviews`(PR review + inline threads)**,不是 issue comment;+1 走 issue reaction。watcher 必須同時輪詢 reactions、reviews、comments 三個管道。
- **教訓(merge)**:auto-mode 分類器會擋 agent 自己 `gh pr merge`(即使 +1 已確認);由使用者跑 merge 指令即可(hook 會驗證)。
- **Codex P2/P3 意見判讀政策(使用者 2026-07-03 定案;已由 H2 寫入 LOOP.md step 7,以該文為準)**:P2/P3 級意見先判讀必要性——**必要**=違反 DESIGN 凍結保證/契約或文件內部矛盾/會誤導未來實作/真 bug;**非必要**=超出 DESIGN 文字的假設性強化、風格偏好、無互通性依據的凍結要求 → 回覆給出**該準則對應的可查核理由**(LOOP step 7 逐條列出:無條文要求/無行為差異則明述;可調項引 🔧 條文並指出無互通性依據;過度收緊引定義該合法情境的 §/DR)後 resolve 不修;講不出理由=該修。判讀模糊時 stop and ask。+1 門檻不變。
- **教訓(壓 review 輪次)**:Codex 傾向一輪只給一條;收到意見後要**主動掃整個 diff 的同類問題**一次修完,否則會一輪一條擠牙膏(P1 因此走了 7 輪)。
- **教訓(review race)**:push 後 Codex 可能仍對「舊 head」留 thread、隨後才對新 head +1;判讀 thread 前先確認它評的內容是否已被現 head 修掉,是則回覆指向修正 commit 後 resolve。
- **Codex 額度用盡時的收斂路徑(使用者行使過,H3/PR #9)**:規則的逃生閥是「stop and ask」——當其他 gates 全綠、剩餘未經 Codex 的 delta 小且已被本地執行級審查驗證時,由**使用者本人**決定 merge(從 web UI 或自己的終端;本機 hook 只認 +1,會擋 agent)。這是 owner 決定,不是 agent 例外。
- **教訓(執行級審查)**:純推理的 code review 連過兩輪都沒發現收據機制根本跑不起來(git 拒絕零位元組 index);審 shell/hook 這類基礎設施時 reviewer 要**實際執行**,並補執行級測試(tests/test_receipts.py 模式)。
- 慣例備忘:TASKS.md 勾選一律含在該任務 PR 內;PR 等 gates 期間可從 main 開下一個獨立 task 分支(LOOP.md 已明文)。
- **doc-only fast lane(H3 起)**:純 `*.md` 變更不開 PR、不經 Codex —— `docs/<id>` 分支 → doc-reviewer(sonnet)PASS → CI 綠 → fast-forward 進 main;push-gate hook 機械擋非 .md。等 Codex 用 `scripts/watch-codex.sh <pr>`(exit 0=+1/10=有意見/20=timeout),不要手寫 watcher。

歷史脈絡(2026-07-03 更新的快照,僅供追溯,不代表現況):P0/P1 契約與 H1–H3 harness(fail-loud gates、triage 規則、watcher/doc lane/收據/governance)皆已 merge;當時正要開工 P2(build/activation + Alembic)。

相關:[[codex-plus-one-merge-gate]]、[[graphrag-architecture]]

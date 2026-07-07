---
name: graphrag-ci-before-codex
description: "push 後先查/修 CI(gh pr checks)再 triage Codex——CI 快、Codex 慢,別把兩個等待串起來"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 4822fc72-b943-4955-806c-12c697183042
---

**開 PR / push 後,先 `gh pr checks <pr>` 查 CI 並修好紅燈,再去等/triage Codex。**

CI(GitHub Actions:governance / backend / frontend / integration)通常數十秒~1 分鐘就回;Codex 常要數分鐘。watcher(`scripts/watch-codex.sh`)只輪詢 Codex 三管道、**不盯 CI**。若先卡在 Codex 意見、把 CI 紅燈晾著,等於把兩個等待串起來、拖慢整體進度。

**Why:** 使用者 2026-07-04 指出「CI 通常比 codex 早回,先修 CI 會加速」。CI 是確定性且快速的 gate,早修早綠;而 CI 紅燈常是機械性小錯(governance 分支名/checkoff 不符、lint、format),幾秒可修,不該讓它跟慢的 Codex 排隊。

**How to apply:** push → 立刻(或 ~60s 後)`gh pr checks <pr>` → 有紅燈**先修**(governance / lint / test / cov)→ 綠了再啟 Codex watcher。特別記住 **governance job** 規則:`task/<id>` 分支必須恰好勾掉 TASKS.md 的 `<id>` 項且不勾別項——**切片任務時分支名要對到子項**(C3a 的 PR 走 `task/C3a`,不是 `task/C3`),否則 `scripts/governance-check.sh` 會擋。相關 [[graphrag-loop-paused-pr5]]。

**Watcher 必須 fail-stop on CI red(2026-07-06,C8/PR#37 教訓——owner 抓到,非流程抓到;trace 錨點=commit `82b7983` 訊息)**:watcher 腳本只等「非 pending」就進 Codex 段,印了 `integration fail` 卻沒停——CI-first 規則被腳本邏輯繞過,連紅 5 push 沒人修。規則:任何 push 後的 watcher,CI 段見 `fail` 立即 `exit 1` 回報,**絕不**進入 Codex 監看;Codex 段輪詢中也要每輪重查 CI,紅了就停。另一半教訓:**本機綠≠CI 綠**——本機 `.env` 有 OPENAI_API_KEY、CI 沒有,吃 key 的測試(進真 lifespan/factory)本機恆綠 CI 恆紅;測 seam 的測試要 fake 掉 vendor client factory,或以 `env -u` 驗證無 key 也綠。

**切片分支的 governance checkoff（BA1a #42 教訓）**：`scripts/governance-check.sh` 會硬性要求 `task/<id>` PR 在 TASKS.md **恰好** check off 自己那條 `- [x] <id> `（且不得夾帶別項）。切片任務時(BA1→BA1a/BA1b)必須先把 TASKS.md 拆成對應子項並勾掉當前子項,否則 CI governance 直接紅(6s 就 fail)。開切片分支的第一步就把 TASKS.md 子項建好+勾好,別等 CI 才發現。改 TASKS.md 會動到 working tree → receipt 失效,要重新 stamp 才能 push。

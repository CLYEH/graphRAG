---
name: codex-plus-one-merge-gate
description: "絕對規則——PR 合併的硬門檻是 Codex 👍(+1),沒有任何例外"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 28bf50a0-6391-4a8c-823c-0c81abc2da4a
---

**合併 PR 的硬門檻 = Codex `chatgpt-codex-connector[bot]` 對「當前 head commit」反應 👍(+1)。沒有例外。**

在 Codex 還是 👀(eyes,審核中)、或最新 commit 尚未拿到 +1 之前,**一律不准 merge**。CI 綠、對話已解決、thread 我自己 resolve 掉——這些**都不能替代 +1**。每次 push 後要等**針對新 commit 的新 +1**(舊 commit 的 +1 不算)。

**Why:** 使用者的受控 loop 完整性建立在「每一次合併都經 Codex 核准」。我曾在 PR #2 上,趁 Codex 還 👀 審最後一個 commit(`1bc1472`,恰是改寫 Codex 判斷邏輯者)時就合併,理由是「硬門檻已滿足、避免無限 nit」——這個 rationalization 是**無效的**,直接違反規則,還把「收斂例外」寫進了 LOOP.md。

**How to apply:** LOOP step 7 只有一個放行條件——Codex 對 head commit +1。若 Codex 持續留言,依 LOOP step 7 的 **suggestion triage** 處理(必要才修;非必要給出 step 7 要求的可查核理由後 resolve),直到它 +1;若它遲遲不 +1,**停下來問使用者**,絕不自行合併。不要再引入任何「gates 滿足即合併」的捷徑。相關:[[graphrag-architecture]]。

**機械性強制（已實作,PR #3 併入 main，commit d583695）:** `.claude/hooks/require-codex-approval.sh`（**bash**,非 pwsh——跨 Windows git-bash / Linux / macOS;使用者可能換到 Mac,BSD date 無 `-d`,故時間戳改用 gh 內建 jq 的 `fromdateiso8601`,不依賴 `date`）。PreToolUse hook,matcher `Bash|PowerShell`,註冊於 `.claude/settings.json`,命令 `bash "$CLAUDE_PROJECT_DIR/.claude/hooks/require-codex-approval.sh"`。行為:攔 `gh pr merge`(number/url/branch/當前分支、flag 任意順序、`-R`)與 `pulls/<n>/merge`;要求 `chatgpt-codex-connector[bot]` 反應 `+1` **且該 +1 晚於 head commit**(committer.date);eyes/無 +1/stale/未解決 Codex thread(GraphQL 分頁,login 用 startswith 兼容 [bot])一律 deny;fail-closed。Hook **新 session 啟動才載入**(故 PR #3 自身能合併)。
**已知限制（使用者拍板接受）:** 無法把「乾淨 +1 reaction」真正綁到 head SHA——`Commit.pushedDate`=null、乾淨 approval 無 commit_id;故用 committer.date 當 freshness proxy(對誠實 agent 足夠,能擋 push 新 commit 後的 stale +1;擋不住蓄意 backdate)。另 out-of-scope 邊緣:`gh pr merge --body "..." <ref>`(引號值)、跨 repo `gh api PUT`。**教訓:Codex 審安全控制會無止盡找邊緣 nit,收斂不能靠等到永遠沒意見**（PR #3 當時由使用者拍板「API 限制內已足夠即合併」收尾;此收斂機制現已制度化為 LOOP step 7 的 suggestion triage）。

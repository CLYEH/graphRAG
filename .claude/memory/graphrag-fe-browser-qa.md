---
name: graphrag-fe-browser-qa
description: "FE 階段流程定案(owner 2026-07-11):Playwright e2e 綠燈之後、開 PR 之前,加一步 Claude in Chrome 真瀏覽器操作測試"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 1ca14cc5-3e0a-461e-ae04-ca06338ee1f2
---

FE(Console 前端)任務的 DoD 在 e2e 之後多一步:**Claude in Chrome 真瀏覽器操作測試**(owner 2026-07-11 定案)。

**Why:** Playwright e2e 是確定性的機械 gate,但斷言不到視覺層(版面壞掉、loading 體感、錯誤訊息可讀性);owner 要求 agent 在真 Chrome 裡親手走一遍任務的 UI 流程做 dogfooding。

**How to apply:**
- 時機:FE 任務 local gates(含 `cd web && npm run test:e2e`)全綠之後、開 PR 之前。發現的問題回實作修完、重跑 gates,再走 PR。
- 工具:`mcp__claude-in-chrome__*`(先 ToolSearch 一次載入 core set:tabs_context_mcp / navigate / computer / read_page / tabs_create_mcp,視需要加 read_console_messages / read_network_requests / gif_creator)。
- 內容:走該任務的 UI 主流程 + 邊界(空狀態、錯誤路徑);讀 browser console 確認無錯誤;截圖(必要時 GIF)附進 PR body 當證據。
- 前置:本機 stack 跑起來(docker compose + API + `npm run dev`)、Chrome 掛 Claude 擴充、localhost 站點權限。FE 首個任務先做連通確認。
- 定位:**補充驗證步,不取代 Playwright e2e 這個 DoD gate**(CI 跑不了——agent session 限定)。「有沒有跑過」改由 **honest-agent 落實**(非機械 gate):PR body 必附截圖/GIF + browser console 證據,供 **owner 與 Codex 抽查**。**機械強制曾以 H10(browser-QA receipt hook)施作,已 DROP**(owner 2026-07-12,PR #64 closed unmerged):把 push-gate 擴成攔 `gh pr create`/FE push 引出了無界的命令文法表面(~24 輪),owner 喊停改回靠紀律;詳 TASKS.md H10 與 [[graphrag-lesson-classes]] class 14。日後若要機械強制,做最小版(只在 FE 分支的 `git push` 檢查 receipt,不碰 PR verb)。

相關:[[graphrag-working-style]]

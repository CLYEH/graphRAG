---
name: graphrag-ba-real-llm
description: BA 階段可用真實 OpenAI API call 測試；.env 有真 key，harness 分兩 lane
metadata:
  type: project
---

使用者於 2026-07-07 產生 `.env`（含真實 OpenAI key，`sk-proj…`，model `gpt-5.4-nano`，
已 gitignored），**同意在 BA(Track 2) 階段用真實 API call 進行測試**。key 經 `core.config`
載入（勿直接讀 os.environ，勿 commit .env）。

**How to apply（harness 兩 lane）:**
- 快速/coverage suite 保持 hermetic（無真實呼叫、決定性、免費）——CI coverage gate 跑這個。
- 真實 LLM 測試只在 `integration`/`e2e` lane：本地 pre-push 跑（有 key），仿 `require_services`
  ——有 key 則跑、無 key 則 skip，但 **CI 要 fail-loud** 不可假綠。
- 成本紀律（全域 Rule 6）：nano model、小 fixture、少案例、per-task token budget；可 record-replay。
- **只在任務真的動到 model 時才花呼叫**：BA1a/BA1b(schema/CRUD/routers)=0 呼叫；BA2+(pipeline/query)才加。
- 已定案的常態:key 為 local-only(CI 跑 hermetic + service-integration,無真實 LLM lane);真實 LLM 驗證屬本地 pre-push 與真資料 QA。

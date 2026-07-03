---
name: graphrag-working-style
description: graphRAG 專案的工作方式偏好：agent 全開發、追求最完整、使用者握定案權、可與 ChatGPT 交叉討論
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 28bf50a0-6391-4a8c-823c-0c81abc2da4a
---

在 graphRAG 專案（見 [[graphrag-architecture]]），使用者的工作方式偏好：

- **agent 驅動 100% 開發，不受人力限制**：規格與實作以「最完整、可維運、可驗收」為目標，不要為了省人力而砍功能或過度精簡。
- **但仍避免 runtime 反模式**：例如可觀測性不要每筆成功項目都寫 log（寫入放大），這類是執行期成本考量，與人力無關，仍要做正確取捨。
- **定案權在使用者**：Claude 可以提建議、可以直接跟使用者的 ChatGPT 對話交叉討論設計，但最終決策一律由使用者拍板，不可自行定案。
- **ChatGPT 交叉審查流程**：使用者會把 DESIGN.md 丟給 ChatGPT review；Claude 讀取（用 claude-in-chrome 開分享連結）、做「第二意見」判斷該採納/精簡/反對，再更新規格。

**Why**：使用者要的是高完整度的平台級設計，且重視多方交叉檢驗。
**How to apply**：偏向完整與嚴謹（不要用「精簡團隊」當理由砍範圍）；重大取捨與定案要回報給使用者確認；需要第二意見時可經瀏覽器與 ChatGPT 討論，但把決定權留給使用者。

# LOOP — controlled agent workflow

How agents make changes here safely. Guardrails: [`CLAUDE.md`](../CLAUDE.md). Queue:
[`TASKS.md`](../TASKS.md). Design: [`DESIGN.md`](DESIGN.md).

## Per-iteration protocol — one task = one branch = one PR
Two review gates guard every task: a **local agent review** before push, and the
**GitHub gates** (CI + bound Codex review) before merge. A failure at either sends the
loop back to step 3.

1. **Pick** the top unchecked task in `TASKS.md` (respect deps). If ambiguous or in
   conflict with `DESIGN.md`, **stop and ask** — don't guess.
   Then branch off latest main: `git switch main && git pull && git switch -c task/<id>`.
2. **Scope** the change to that task only (surgical; no unrelated refactors).
3. **Implement** following the guardrails in `CLAUDE.md`, with tests for the tier.
4. **Verify (local gates)** — run until green (tier that matches the change):
   ```bash
   uv run poe check-all        # fast: fmt/lint/type + unit/contract (py) + component (web)
   uv run poe check-full       # + integration (first: docker compose up -d)
   cd web && npm run test:e2e  # UI flows (first: npx playwright install)
   ```
5. **Agent review (local gate)** — run the `code-reviewer` subagent on the diff.
   **VERDICT: FAIL → back to step 3** (fix, then re-verify + re-review).
6. **Commit → push → open PR** (one task, one PR):
   ```bash
   git commit -m "<id>: <summary>"
   git push -u origin task/<id>
   gh pr create --fill --base main
   ```
7. **Wait for GitHub gates** on the PR — all must be satisfied:
   - **CI** green — required checks `backend` / `frontend` / `integration` (GitHub-enforced).
   - **Codex review** — after a PR opens, judge `chatgpt-codex-connector[bot]` by its
     **reaction** plus its **unresolved review threads**. Do **not** use a raw comment
     `length` as the verdict: the list endpoints keep returning historical comments after
     they're resolved, so a count would latch "changes-wanted" forever. Only these states,
     and the two pending ones are **not** failures:

     | state | signal | loop action |
     |---|---|---|
     | reviewing | 👀 `eyes` reaction, no unresolved threads | **wait** (pending — not a failure) |
     | not seen yet | no reaction, no Codex comment/thread | comment `@codex review`, then **wait** |
     | approved | 👍 `+1` reaction | **PASS** |
     | changes wanted | ≥1 **unresolved** Codex review thread (or a top-level change-request comment) | **back to step 3** |

     ```bash
     # reaction verdict: +1 = approved, eyes = still reviewing
     gh api repos/CLYEH/graphRAG/issues/<pr>/reactions \
       --jq '[.[]|select(.user.login=="chatgpt-codex-connector[bot]")|.content]'
     # changes-wanted = UNRESOLVED Codex review threads (ignores resolved/historical ones)
     gh api graphql -f query='{repository(owner:"CLYEH",name:"graphRAG"){pullRequest(number:<pr>){
       reviewThreads(first:50){nodes{isResolved comments(first:1){nodes{author{login}}}}}}}}' \
       --jq '[.data.repository.pullRequest.reviewThreads.nodes[]
              |select(.isResolved==false and .comments.nodes[0].author.login=="chatgpt-codex-connector")]|length'
     ```
   - **Conversations resolved** — GitHub blocks merge (`required_conversation_resolution`)
     until every Codex thread is addressed and resolved (PR UI, or
     `gh api graphql` → `resolveReviewThread`).
   **Merge requires Codex `+1` on the head commit — no exceptions.** `eyes` / "not seen yet"
   are pending states (wait/poke, never a failure); unresolved threads or a fresh
   change-request → back to step 3. **CI green + resolved conversations do NOT substitute for
   `+1`.** This is enforced mechanically: `.claude/hooks/require-codex-approval.ps1`
   (PreToolUse) blocks any `gh pr merge` until Codex has reacted `+1`. If Codex never `+1`s,
   **stop and ask** — never merge around it.
8. **Merge & advance** — only after Codex `+1` on the head commit (the hook enforces it):
   merge the PR, delete the branch, `git switch main && git pull`, check off the item in
   `TASKS.md`, return to step 1.

## Testing tiers (what runs when)
| Tier | Runs | Marker / location | In fast loop? |
|---|---|---|---|
| unit | pure logic, no I/O | py: unmarked · web: `src/**/*.test.tsx` | ✅ `check-all` |
| contract | payloads vs frozen schemas | py: `@pytest.mark.contract` | ✅ (skips until `contracts/` exists) |
| coverage | fail-under 85 | `poe test-cov` | ✅ / CI |
| integration | real stores via docker | py: `@pytest.mark.integration` (auto-skips if down) | ❌ `check-full` / CI |
| eval | retrieval-quality golden set | py: `@pytest.mark.eval` | ❌ on demand |
| e2e | Console flows (Playwright) | `web/e2e/*.spec.ts` | ❌ `npm run test:e2e` |

Every task lands with the tests for its tier. Keep the fast loop fast — don't put
service-dependent or browser tests in `check-all`; use the markers.

Never weaken `ruff`/`mypy`/`tsconfig`/tests to go green. Push is manual (not part of the loop).

## Running the loop autonomously
Two options — both use the same protocol above:

- **Claude Code `/loop`** — recurring self-paced runs. Suggested prompt:
  > Do the next unchecked task in TASKS.md following docs/LOOP.md's 8-step protocol:
  > branch `task/<id>`, implement with tests, run `uv run poe check-all` until green,
  > then run the `code-reviewer` subagent — if it FAILs, fix and re-review. Then commit,
  > push, and `gh pr create`. Wait for CI green **and** Codex to react `+1` (no exceptions —
  > a hook blocks merge otherwise); if Codex comments, fix on the same branch and re-review.
  > Only once CI is green and Codex has `+1`'d, merge, check the task off, next. If Codex
  > never `+1`s, stop and ask — don't merge around it.
  > If a task is ambiguous or conflicts with DESIGN.md, stop and ask instead of guessing.

- **ralph-loop plugin** — for continuous autonomous iteration; point it at the same prompt.

Start with 1–2 supervised iterations before letting it run unattended, so you can confirm
the gate + commit rhythm behaves as expected.

## Permissions
Agents run the harness commands (uv/npm/docker compose/git) without a prompt each time via
[`.claude/settings.json`](../.claude/settings.json).

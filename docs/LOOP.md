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
   uv run poe check-full       # + integration (first: docker compose up -d --wait)
   cd web && npm run test:e2e  # UI flows (first: npx playwright install)
   ```
5. **Agent review (local gate)** — run the `code-reviewer` subagent on the diff
   (`doc-reviewer` for doc-only changes taking the fast lane).
   **VERDICT: FAIL → back to step 3** (fix, then re-verify + re-review).
   A PASS stamps a **receipt** binding the verdict to a content hash
   (`.claude/hooks/write-review-receipt.sh`); the push gate
   (`.claude/hooks/require-push-gates.sh`) recomputes the hash and blocks the push if
   anything changed after the PASS — and re-runs `poe check` itself. Steps 4–5 are
   therefore CPU-verified at push time, not taken on faith.
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
     | changes wanted | ≥1 **unresolved** Codex review thread (or a top-level change-request comment) | **triage each suggestion** (below): must-fix → step 3; else reply-and-resolve |

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
     **Waiting is standardized:** run `bash scripts/watch-codex.sh <pr>` (background it) —
     it polls all three Codex channels (reactions / PR reviews / comments; a review-only
     "changes wanted" is invisible to the other two) and exits `0`=+1 approved,
     `10`=new response to triage, `20`=timeout (poke `@codex review`). Don't hand-roll
     one-off watchers.
   - **Conversations resolved** — GitHub blocks merge (`required_conversation_resolution`)
     until every Codex thread is addressed and resolved (PR UI, or
     `gh api graphql` → `resolveReviewThread`).
   - **Codex suggestion triage** — when Codex leaves inline suggestions, classify each
     one BEFORE acting. Do not blanket-accept (P1 took 7 one-comment rounds that way)
     and do not blanket-resolve.

     **Must fix (back to step 3)** if ANY of:
     1. P0/P1 badge, or an un-badged explicit change request.
     2. Violates a guarantee DESIGN freezes — you can cite the exact §/DR it breaks
        (e.g. §27.2 provenance minimums, §27.4 audit/prune survival, DR-001…008).
     3. Internal inconsistency — the same concept constrained/stated differently in two
        places (incl. docs/memory contradicting a frozen DR).
     4. Breaks a real producer/consumer — accepts payloads consumers can't process, or
        rejects payloads the design says are legitimate.
     5. A genuine bug (wrong logic, false-green test).

     **Reply-and-resolve without changing (P2/P3 only)** if ANY of:
     1. Hardening beyond what DESIGN's text guarantees (no violated § can be cited).
     2. Style preference with no behavioral difference.
     3. Freezing a 🔧 tunable with no interoperability rationale.
     4. Would make a case DESIGN defines as legitimate unrepresentable (over-tightening).

     **Hard rules:** a resolve-without-change reply MUST name the criterion it invokes
     and give that criterion's checkable rationale — 1: state that no frozen § mandates
     the suggested guarantee; 2: state that behavior/rendering is identical; 3: cite
     the § marking the value 🔧 tunable and note the suggestion offers no
     interoperability rationale for freezing it; 4: cite the §/DR defining the case the
     suggestion would make unrepresentable. An unreasoned "resolved" is banned — if you
     can't articulate the rationale, treat the suggestion as must-fix. If the call is
     genuinely ambiguous, stop and ask the user.
     And whichever way a suggestion
     is triaged, sweep the whole diff for the same class of issue and settle it in one
     round. This triage changes nothing about the `+1` gate below — resolving threads
     never substitutes for a fresh `+1` on the head commit.

   **Merge requires Codex `+1` on the head commit — no exceptions.** `eyes` / "not seen yet"
   are pending states (wait/poke, never a failure); unresolved threads or a fresh
   change-request → triage above (must-fix → step 3, else reply-and-resolve with a
   checkable rationale). **CI green + resolved conversations do NOT substitute for
   `+1`.** This is enforced mechanically: `.claude/hooks/require-codex-approval.sh`
   (PreToolUse) blocks any `gh pr merge` until Codex has reacted `+1`. (The hook is local,
   honest-agent enforcement — it guards merges issued from this repo's agent sessions;
   GitHub itself does not check the Codex reaction, so the web UI could bypass it. Known,
   accepted tradeoff.) If Codex never `+1`s, **stop and ask** — never merge around it.

   **Don't idle while gates run:** while a PR waits on CI/Codex, you may pick the next
   task whose dependencies are already met and start it on its own branch **off latest
   `main`** (never off the waiting branch). Each PR still merges only when its own gates
   clear; if the waiting PR gets Codex feedback, finishing it takes priority over new work.
8. **Merge & advance** — only after Codex `+1` on the head commit (the hook enforces it):
   merge the PR (its TASKS.md checkoff rode in it), delete the branch,
   `git switch main && git pull`.

   **Post-merge retro (owner rule, 2026-07-03):** before returning to step 1, sweep the
   merged PR's review findings — every Codex thread AND every local-reviewer blocker —
   and classify each against the **lesson classes** catalogued in
   `.claude/memory/graphrag-loop-paused-pr5.md`:
   - **Repeated class** ⇒ the existing prevention isn't biting — strengthen it (sharpen
     the reviewer checklist / hook / CI check that should have caught it).
   - **New class** ⇒ add it to the catalog, and when it is mechanically preventable,
     file an `H<n>` harness task in TASKS.md.
   - Nothing new ⇒ note nothing and move on; the retro is a sweep, not a ceremony.

   **Retro routing (owner rule, 2026-07-03):** apply the retro's own follow-ups by lane —
   if they touch only `*.md` (catalog entries, reviewer-checklist sharpening, LOOP/CLAUDE
   clarifications), take the **doc-only fast lane**: no PR, no Codex review requested.
   Only mechanical enforcement (hooks / CI / scripts — any non-`.md` file) becomes an
   `H<n>` task through the full PR + Codex lane.

   **Compact before the next task (owner rule, 2026-07-03):** once the retro is done,
   compact the session context (`/compact`) so every task starts on a fresh, small
   context — the agent asks the owner to run it when it cannot itself. Then return
   to step 1.

## Doc-only fast lane (no PR, no Codex)
Codex auto-reviews every PR the moment it opens, so doc-only work in a PR burns review
quota against zero code risk. Owner decision (2026-07-03): a change where **every changed
file is `*.md`** skips the PR entirely:

1. Branch `docs/<id>` off latest `main`.
2. Local gates as usual (`uv run poe check-all` — cheap; nothing executes Markdown).
3. Run the **`doc-reviewer`** subagent (not `code-reviewer`). `VERDICT: PASS` stamps the
   review receipt the push gate requires.
4. Push the branch — CI runs on `docs/**` pushes — wait for green, then fast-forward main:
   `git push origin docs/<id>:main`.

Enforcement is mechanical, not honor-based:
- `.claude/hooks/require-push-gates.sh` (PreToolUse) treats `docs/*` branch pushes and any
  direct-to-main push as this lane: it blocks them when a non-`*.md` file is outgoing, or
  when the content doesn't match a reviewer receipt.
- Branch protection still requires green required checks on the pushed SHA (statuses are
  per-SHA, so the `docs/**` CI run satisfies them). "Require a pull request" was lifted to
  enable this lane — code changes still go through PRs because the push gate refuses them
  here.

Anything touching code/config/contracts/workflows/hooks — any non-`.md` file — takes the
full lane: task branch → PR → CI + Codex `+1`.

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

Never weaken `ruff`/`mypy`/`tsconfig`/tests to go green.

## Running the loop autonomously
Two options — both use the same protocol above:

- **Claude Code `/loop`** — recurring self-paced runs. Suggested prompt:
  > Do the next unchecked task in TASKS.md following docs/LOOP.md's 8-step protocol:
  > branch `task/<id>`, implement with tests, run `uv run poe check-all` until green,
  > then run the `code-reviewer` subagent — if it FAILs, fix and re-review. Then commit,
  > push, and `gh pr create`. Wait for CI green **and** Codex to react `+1` (no exceptions —
  > a hook blocks merge otherwise); if Codex comments, triage each suggestion per step 7
  > (must-fix → fix on the same branch and re-review; else reply-and-resolve with the
  > step-7 checkable rationale).
  > Only once CI is green and Codex has `+1`'d, merge (the checkoff rode in the PR), run
  > the step-8 post-merge retro, compact the session (`/compact`), then take the next
  > task. If Codex never `+1`s, stop and ask — don't merge around it.
  > If a task is ambiguous or conflicts with DESIGN.md, stop and ask instead of guessing.

- **ralph-loop plugin** — for continuous autonomous iteration; point it at the same prompt.

Start with 1–2 supervised iterations before letting it run unattended, so you can confirm
the gate + commit rhythm behaves as expected.

## Permissions
Agents run the harness commands (uv/npm/docker compose/git) without a prompt each time via
[`.claude/settings.json`](../.claude/settings.json).

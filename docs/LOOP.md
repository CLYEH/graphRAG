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
   A green `poe check` / `poe web-check` stamps a **gates receipt**
   (`.claude/receipts/gates-<kind>-<tree>`, via `scripts/stamp_gates_receipt.py` as
   the sequence's final step) that the push gate later verifies — no extra command
   to remember, but note that editing ANYTHING afterwards invalidates the stamp,
   so the last green run must be on the exact content you push.
   **FE tasks only (owner 2026-07-11):** after the gates above (incl. Playwright
   e2e) go green, run the **Claude in Chrome browser pass** — the agent drives a
   real Chrome (`mcp__claude-in-chrome__*`) through the task's UI flows on the
   local stack, checks the browser console for errors, and captures screenshots/
   GIF evidence that goes into the PR body. Findings → back to step 3. This is a
   supplementary verification step (agent-session only, not CI-runnable), so it
   does not replace the Playwright gate. It is an **honest-agent step** — the
   pass runs and its screenshots / console evidence go into the PR body, where
   the owner and Codex audit it — **not** a push-gate hook (mechanical
   enforcement was attempted as H10 and dropped; see TASKS.md H10 for why).
5. **Agent review (local gate)** — run the `code-reviewer` subagent on the diff
   (`doc-reviewer` for doc-only changes taking the fast lane).
   **VERDICT: FAIL → back to step 3** (fix, then re-verify + re-review).
   A PASS stamps a **receipt** binding the verdict to a content hash
   (`.claude/hooks/write-review-receipt.sh`); the push gate
   (`.claude/hooks/require-push-gates.sh`) recomputes the hash and blocks the push if
   anything changed after the PASS — and requires the step-4 gates receipts for the
   same hash. Steps 4–5 are therefore CPU-verified at push time, not taken on faith.
   **Known boundary (H15): PreToolUse hooks fail OPEN** — only exit 2 blocks; a
   hook that times out or crashes lets the command through silently. Mitigation:
   both hooks now do only fast local/API work (receipt lookups, one paginated gh
   query) under explicit 120s timeouts, so a timeout means something is
   pathologically wrong, not that the suite was slow. CI + branch protection stay
   the server-side backstop for anything a local hook misses.
   **The local review is the pre-push adversarial pass — spend the round HERE, not
   on Codex.** Run the `code-reviewer` routed checklist matrix that matches the
   change (`.claude/agents/checklists/*.md`, routed by diff content) to
   COMPLETION before the first push (input-position × level for parsers/validators;
   commit-boundary × crash/race for multi-commit; operation × state × interleaving
   for state machines), sweeping each class's siblings in the same pass. A class
   Codex later finds that the matching sweep would have caught is a missed pre-push
   pass, not a new discovery — and every extra Codex round is an external
   round-trip that dominates wall-clock (BA2c-2a: 2 rounds, both one class a
   position apart — one matrix pass covers both).
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
     # ILLUSTRATION ONLY — never re-implement verdict logic from this snippet.
     # Authoritative implementations: .claude/hooks/require-codex-approval.sh
     # (merge gate) fully PAGINATES reviewThreads (hasNextPage loop) and is the
     # sole authority for the unresolved-thread verdict; scripts/watch-codex.sh
     # owns waiting/triage — its thread count is a single first:100 page shown
     # in the poll log for information only (not part of its exit-code logic).
     # This sketch (first:50, unpaginated) is weaker than both.
     gh api repos/CLYEH/graphRAG/issues/<pr>/reactions \
       --jq '[.[]|select(.user.login=="chatgpt-codex-connector[bot]")|.content]'
     gh api graphql -f query='{repository(owner:"CLYEH",name:"graphRAG"){pullRequest(number:<pr>){
       reviewThreads(first:50){nodes{isResolved comments(first:1){nodes{author{login}}}}}}}}' \
       --jq '[.data.repository.pullRequest.reviewThreads.nodes[]
              |select(.isResolved==false and .comments.nodes[0].author.login=="chatgpt-codex-connector")]|length'
     ```
     **Waiting is standardized:** run `bash scripts/watch-codex.sh <pr>` (background it) —
     it polls all three Codex channels (reactions / PR reviews / comments; a review-only
     "changes wanted" is invisible to the other two) and exits `0`=+1 approved,
     `10`=new response to triage, `20`=timeout (poke `@codex review`), `30`=Codex is out
     of review quota (its only fresh response is the usage-limits message) — stop waiting
     and re-poke after the quota window resets instead of polling for nothing. Don't
     hand-roll one-off watchers. **After triaging an event WITHOUT a poke** (the normal
     flow: fix + resolve, the push auto-triggers the re-review), pass that handled
     event's timestamp to the next watch — `watch-codex.sh <pr> --anchor <iso-ts>` (the
     watcher prints it in its exit-10 RESULT line) — otherwise the bootstrap treats the
     last poke as the triage-ack and every later watch exits 10 forever on the same
     already-handled event (H13).

     **Never poke while `eyes` is showing (owner mandate).** `eyes` means Codex is
     actively reviewing; an `@codex review` then only burns its quota and produces a
     duplicate pass. So before *any* poke — including on a watcher `20`=timeout —
     re-query the reactions and unresolved threads, and poke **only** when there is
     genuinely no `eyes`, no `+1`, and no Codex thread/comment/review that landed after
     your last push. A push **auto-triggers** a Codex re-review, so **never poke right
     after pushing**: wait for `eyes` to appear, then wait for the verdict. Two watcher
     failure modes that have caused false pokes, both to guard against: (1) a `20`=timeout
     can be a *stale-review bootstrap* re-flagging an already-triaged response — confirm
     the flagged response is genuinely new (submitted after your last push) before acting;
     (2) never compare timestamps across mixed offsets (a `…Z` UTC time vs a `…+08:00`
     local time) — normalize to one zone or the lexical compare silently mis-orders them.
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
     round. When a round sends you back to step 3, fix ALL of that round's findings
     (plus that same-class sweep) BEFORE returning to step 5 — each fix changes the
     tree hash and voids the prior receipt, so the re-review/re-stamp is once per
     Codex round, not once per finding. This triage changes nothing about the `+1`
     gate below — resolving threads never substitutes for a fresh `+1` on the head commit.

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
   `.claude/memory/graphrag-lesson-classes.md`:
   - **Repeated class** ⇒ the existing prevention isn't biting — strengthen it (sharpen
     the reviewer checklist / hook / CI check that should have caught it).
   - **New class** ⇒ add it to the catalog, and when it is mechanically preventable,
     file an `H<n>` harness task in TASKS.md.
   - Nothing new ⇒ note nothing and move on; the retro is a sweep, not a ceremony.
   Record the retro as **class-entry updates only** (a sub-point with the PR anchor on
   the matching class, or a new stable-numbered class); per-PR narrative blocks are
   retired (owner, 2026-07-20) — round counts, task history, and roadmaps stay out of
   the catalog. Mechanize-first: a preventable class becomes a hook/CI/lint/helper
   (`H<n>`), prose is the fallback.

   **Retro routing (owner rule, 2026-07-03):** apply the retro's own follow-ups by lane —
   if they touch only `*.md` (catalog entries, reviewer-checklist sharpening, LOOP/CLAUDE
   clarifications), take the **doc-only fast lane**: no PR, no Codex review requested.
   Only mechanical enforcement (hooks / CI / scripts — any non-`.md` file) becomes an
   `H<n>` task through the full PR + Codex lane. Then return to step 1.

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
| e2e | Console flows (Playwright) | `web/e2e/*.spec.ts` | ❌ `npm run test:e2e` · CI `e2e` job (non-required, web-touching PRs — H18) |

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
  > the step-8 post-merge retro, then take the next task. If Codex never `+1`s, stop and
  > ask — don't merge around it.
  > If a task is ambiguous or conflicts with DESIGN.md, stop and ask instead of guessing.

- **ralph-loop plugin** — for continuous autonomous iteration; point it at the same prompt.

Start with 1–2 supervised iterations before letting it run unattended, so you can confirm
the gate + commit rhythm behaves as expected.

## Permissions
Agents run the harness commands (uv/npm/docker compose/git) without a prompt each time via
[`.claude/settings.json`](../.claude/settings.json).

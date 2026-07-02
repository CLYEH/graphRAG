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
   - **Codex review** — after a PR opens, poll `chatgpt-codex-connector[bot]`'s reaction
     **and** comments. Only these states occur:

     | signal on the PR | meaning | loop action |
     |---|---|---|
     | 👀 `eye` reaction | Codex is reviewing | **wait** |
     | 👍 `+1` reaction | reviewed, no comments | **PASS** |
     | no reaction **and** no Codex comment | hasn't seen it yet | comment `@codex review` to poke it, then wait |
     | a new Codex comment / suggestion | wants changes | **back to step 3** |

     ```bash
     # verdict = Codex reaction (+1 = pass, eyes = reviewing)
     gh api repos/CLYEH/graphRAG/issues/<pr>/reactions \
       --jq '[.[]|select(.user.login=="chatgpt-codex-connector[bot]")|.content]'
     # "has comments?" must check BOTH streams — Codex may leave inline review comments
     # OR a top-level PR comment; /pulls/.../comments returns only the former.
     gh api repos/CLYEH/graphRAG/pulls/<pr>/comments \
       --jq '[.[]|select(.user.login=="chatgpt-codex-connector[bot]")]|length'   # inline review comments
     gh api repos/CLYEH/graphRAG/issues/<pr>/comments \
       --jq '[.[]|select(.user.login=="chatgpt-codex-connector[bot]")]|length'   # top-level PR comments
     ```
   - **Conversations resolved** — GitHub blocks merge (`required_conversation_resolution`)
     until every Codex suggestion thread is addressed and resolved (PR UI, or
     `gh api graphql` → `resolveReviewThread`).
   **Codex leaves suggestions / no 👍 → back to step 3.** Fix on the same branch, push,
   resolve threads, let Codex re-review. (CI is GitHub-hard; Codex's *verdict wait* is
   loop-enforced; Codex's *unresolved comments* are GitHub-hard.)
8. **Merge & advance** — merge the PR, delete the branch, `git switch main && git pull`,
   check off the item in `TASKS.md`, return to step 1.

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
  > push, and `gh pr create`. Wait for CI + the bound Codex review to pass; if either
  > fails, fix on the same branch. When both are green, merge, check the task off, next.
  > If a task is ambiguous or conflicts with DESIGN.md, stop and ask instead of guessing.

- **ralph-loop plugin** — for continuous autonomous iteration; point it at the same prompt.

Start with 1–2 supervised iterations before letting it run unattended, so you can confirm
the gate + commit rhythm behaves as expected.

## Permissions
Agents run the harness commands (uv/npm/docker compose/git) without a prompt each time via
[`.claude/settings.json`](../.claude/settings.json).

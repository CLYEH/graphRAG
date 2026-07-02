# PreToolUse guard: block any PR merge unless Codex reacted +1 on the PR.
# Rule (no exceptions): merge is allowed only when chatgpt-codex-connector[bot]
# has reacted 👍 (+1). Still-reviewing (👀 eyes), no reaction, or any unresolved
# Codex review thread => BLOCK. See memory: codex-plus-one-merge-gate.
#
# Wired via .claude/settings.json PreToolUse (matcher: Bash|PowerShell).
# Fails CLOSED: if a merge is detected but Codex approval can't be verified, it blocks.

$ErrorActionPreference = 'Stop'

function Allow { exit 0 }

function Deny([string]$reason) {
  $out = [ordered]@{
    hookSpecificOutput = [ordered]@{
      hookEventName            = 'PreToolUse'
      permissionDecision       = 'deny'
      permissionDecisionReason = $reason
    }
  }
  Write-Output ($out | ConvertTo-Json -Compress -Depth 6)
  exit 0
}

# --- read the tool command from stdin (PreToolUse payload) ---
try {
  $raw = [Console]::In.ReadToEnd()
  $payload = $raw | ConvertFrom-Json
  $cmd = [string]$payload.tool_input.command
} catch {
  # Can't parse payload -> not our business, don't block normal work.
  Allow
}
if ([string]::IsNullOrWhiteSpace($cmd)) { Allow }

# --- only engage on merge commands ---
$isMerge = ($cmd -match 'gh\s+pr\s+merge') -or ($cmd -match 'pulls/\d+/merge')
if (-not $isMerge) { Allow }

try {
  if ($env:CLAUDE_PROJECT_DIR) { Set-Location $env:CLAUDE_PROJECT_DIR }

  $repo = (gh repo view --json nameWithOwner -q .nameWithOwner) 2>$null
  if (-not $repo) { Deny 'Codex-gate: cannot resolve the repo to verify Codex +1 -> merge blocked.' }

  # PR number: explicit arg, or the current branch's PR.
  $pr = $null
  if ($cmd -match 'gh\s+pr\s+merge\s+(\d+)') { $pr = $matches[1] }
  elseif ($cmd -match 'pulls/(\d+)/merge') { $pr = $matches[1] }
  if (-not $pr) { $pr = (gh pr view --json number -q .number) 2>$null }
  if (-not $pr) { Deny 'Codex-gate: cannot determine the PR number -> merge blocked.' }

  # Codex reaction on the PR (issue-level).
  $react = (gh api "repos/$repo/issues/$pr/reactions" `
      --jq '[.[]|select(.user.login=="chatgpt-codex-connector[bot]")|.content]|join(",")') 2>$null
  if ($null -eq $react) { $react = '' }

  # Unresolved Codex review threads (GraphQL; variables avoid string-escaping).
  $owner = $repo.Split('/')[0]
  $name = $repo.Split('/')[1]
  $q = 'query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:50){nodes{isResolved comments(first:1){nodes{author{login}}}}}}}}'
  $unresolved = (gh api graphql -f query=$q -F owner=$owner -F name=$name -F number=$pr `
      --jq '[.data.repository.pullRequest.reviewThreads.nodes[]|select(.isResolved==false and .comments.nodes[0].author.login=="chatgpt-codex-connector")]|length') 2>$null

  if ($react -match 'eyes') {
    Deny "Codex-gate: Codex is still reviewing PR #$pr (reaction: eyes). Wait for +1 before merging. No exceptions."
  }
  if ($react -notmatch '\+1') {
    Deny "Codex-gate: Codex has NOT approved PR #$pr (no +1 from chatgpt-codex-connector[bot]; reaction='$react'). Poke '@codex review', wait for +1, then merge."
  }
  if ($unresolved -and ([int]$unresolved) -gt 0) {
    Deny "Codex-gate: PR #$pr has $unresolved unresolved Codex review thread(s). Address + resolve them, keep Codex +1, then merge."
  }

  # Codex +1, not reviewing, no unresolved threads -> allow the merge.
  Allow
}
catch {
  Deny "Codex-gate: verification error ($($_.Exception.Message)) -> merge blocked (fail-closed)."
}

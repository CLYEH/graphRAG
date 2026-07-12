import { useState } from "react";

import { useDecideMergeCandidate, useMergeCandidates } from "../api/queries";

import type { MergeCandidate, MergeCandidateStatus, ReviewVerb } from "../api/queries";

// MergeCandidateStatus (DESIGN §17) → the shared status-badge tone.
const TONE: Record<MergeCandidateStatus, string> = {
  pending: "info",
  approved: "ok",
  rejected: "bad",
  deferred: "warn",
};

// Approved / rejected are terminal: BA5 refuses any further transition under the
// lock, and the frozen contract has no illegal-transition error, so the UI is the
// only thing stopping a pointless POST — disable every action on these.
const TERMINAL: ReadonlySet<MergeCandidateStatus> = new Set(["approved", "rejected"]);

export function CandidatesTable({ project }: { project: string }) {
  const { data: candidates, isPending, isError, error } = useMergeCandidates(project);

  if (isPending) return <p className="runs__muted">Loading review queue…</p>;
  if (isError)
    return (
      <p className="runs__muted runs__muted--error">
        Could not load review queue: {error instanceof Error ? error.message : "unknown error"}
      </p>
    );
  if (candidates.length === 0) return <p className="runs__muted">No merge candidates to review.</p>;

  return (
    <table className="runs review__table">
      <thead>
        <tr>
          <th>Candidate</th>
          <th>Status</th>
          <th>Score</th>
          <th>Decision</th>
        </tr>
      </thead>
      <tbody>
        {candidates.map((c) => (
          <CandidateRow key={c.id} candidate={c} project={project} />
        ))}
      </tbody>
    </table>
  );
}

function fmt(ts: string | null | undefined): string {
  return ts ? ts.replace("T", " ").replace(/\..*$/, "").replace("Z", " UTC") : "—";
}

function blob(value: MergeCandidate["impact"]): string {
  return value ? JSON.stringify(value) : "—";
}

function CandidateRow({ candidate, project }: { candidate: MergeCandidate; project: string }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const decide = useDecideMergeCandidate(project);

  // §17 transitions (enforced under the lock in BA5): pending → any verb;
  // deferred → approve/reject only (never re-defer a deferred pair);
  // approved/rejected are terminal. Mirror that here so the UI never offers a
  // transition the server will reject.
  const terminal = TERMINAL.has(candidate.status);
  const canDefer = candidate.status === "pending";

  const submit = (verb: ReviewVerb) =>
    decide.mutate({
      candidateId: candidate.id,
      verb,
      reason: reason.trim() === "" ? null : reason.trim(),
    });

  return (
    <>
      <tr className="runs__row" onClick={() => setOpen(!open)} aria-expanded={open}>
        <td className="runs__id">{candidate.id.slice(0, 8)}</td>
        <td>
          <span className={`runs__badge runs__badge--${TONE[candidate.status]}`}>
            {candidate.status}
          </span>
        </td>
        <td>{candidate.score.toFixed(3)}</td>
        {/* the decision controls own their clicks — toggling the detail row on a
            button/input press would be a surprise */}
        <td className="review__decide" onClick={(e) => e.stopPropagation()}>
          <input
            className="review__reason"
            aria-label="Decision reason"
            placeholder="reason (optional)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <button
            type="button"
            onClick={() => submit("approve")}
            disabled={terminal || decide.isPending}
          >
            Approve
          </button>
          <button
            type="button"
            onClick={() => submit("reject")}
            disabled={terminal || decide.isPending}
          >
            Reject
          </button>
          <button
            type="button"
            onClick={() => submit("defer")}
            disabled={!canDefer || decide.isPending}
          >
            Defer
          </button>
          {decide.isError && (
            <span className="review__error">
              Decision failed:{" "}
              {decide.error instanceof Error ? decide.error.message : "unknown error"}
            </span>
          )}
        </td>
      </tr>
      {open && (
        <tr className="runs__detail">
          <td colSpan={4}>
            <dl>
              <div>
                <dt>candidate id</dt>
                <dd>{candidate.id}</dd>
              </div>
              <div>
                <dt>left entity</dt>
                <dd>{candidate.left_entity_id}</dd>
              </div>
              <div>
                <dt>right entity</dt>
                <dd>{candidate.right_entity_id}</dd>
              </div>
              <div>
                <dt>build</dt>
                <dd>{candidate.build_id}</dd>
              </div>
              <div>
                <dt>impact</dt>
                <dd>{blob(candidate.impact)}</dd>
              </div>
              <div>
                <dt>features</dt>
                <dd>{blob(candidate.features)}</dd>
              </div>
              <div>
                <dt>left snapshot</dt>
                <dd>{blob(candidate.left_snapshot)}</dd>
              </div>
              <div>
                <dt>right snapshot</dt>
                <dd>{blob(candidate.right_snapshot)}</dd>
              </div>
              {candidate.decided_by && (
                <div>
                  <dt>decided by</dt>
                  <dd>
                    {candidate.decided_by}
                    {candidate.decided_at ? ` · ${fmt(candidate.decided_at)}` : ""}
                  </dd>
                </div>
              )}
              {candidate.reason && (
                <div>
                  <dt>reason</dt>
                  <dd>{candidate.reason}</dd>
                </div>
              )}
            </dl>
          </td>
        </tr>
      )}
    </>
  );
}

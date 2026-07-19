import { useState } from "react";

import { useDecideOntologyProposal, useOntologyProposals } from "../api/queries";

import type { OntologyProposal, ProposalVerb } from "../api/queries";

// kind → operator words; keying on the contract enum makes a new value a type
// error, not a silently-english label (the UXA3 translation layer).
const KIND_LABEL: Record<OntologyProposal["kind"], string> = {
  entity: "實體型別",
  relation: "關聯型別",
};

function message(error: unknown): string {
  return error instanceof Error ? error.message : "unknown error";
}

// GOV3-fe: the ontology-proposal pool — LLM-observed types not in the configured
// ontology, awaiting review. Accept adds the type to the project's ontology (next
// build reads it); reject excludes it. A list (not the merge queue's one-at-a-time
// walk) because a proposal is a single terminal choice, not a per-pair adjudication.
export function ProposalPool({ project }: { project: string }) {
  const proposals = useOntologyProposals(project);
  const decide = useDecideOntologyProposal(project);
  // Which row is awaiting an explicit confirm, and for which verb. accept/reject
  // are §17-TERMINAL (a re-decide 409s) and irreversible from here — reject drops
  // the type, accept mutates the configured ontology — so a single inline misclick
  // must NOT commit them; the first click only arms a confirm. The sibling
  // merge-review flow gates its terminal verbs behind the same confirm for the
  // same reason (ReviewCases). One row confirms at a time.
  const [confirming, setConfirming] = useState<{ id: string; verb: ProposalVerb } | null>(null);

  const onConfirm = () => {
    if (!confirming) return;
    decide.mutate({ proposalId: confirming.id, verb: confirming.verb, reason: null });
    setConfirming(null);
  };

  // lock the decision controls while a decision posts AND while the pool refreshes
  // after one: a resolved POST clears decide.isPending before the invalidated GET
  // drops the decided proposal, so a second decision in that window re-hits the
  // now-terminal proposal and 409s into a spurious "決定失敗" (Codex #106 P1d — the
  // same stale-while-revalidate guard as the entity/relation review tabs). 取消
  // stays usable to back out of a confirm.
  const locked = decide.isPending || proposals.isFetching;

  if (proposals.isPending) return <p className="review__line">載入提案…</p>;
  if (proposals.isError)
    return (
      <p className="review__line review__line--error">無法讀取提案:{message(proposals.error)}</p>
    );
  if (proposals.data.length === 0) return <p className="review__line">目前沒有待審的本體提案。</p>;

  return (
    <ul className="proposals">
      {proposals.data.map((p) => (
        <li key={p.id} className="proposals__row">
          <div className="proposals__head">
            <span className="proposals__type">{p.type_name}</span>
            <span className="proposals__kind">{KIND_LABEL[p.kind]}</span>
          </div>
          {p.example ? <p className="proposals__example">例:{p.example}</p> : null}
          {p.chunk_ref ? (
            <p className="proposals__src" title={p.chunk_ref}>
              首見來源:{p.chunk_ref}
            </p>
          ) : null}
          {confirming?.id === p.id ? (
            <div className="proposals__confirm" role="alertdialog" aria-label="確認決定">
              <p>
                {confirming.verb === "accept"
                  ? "採納後會把這個型別加入本體,下次建置生效。"
                  : "拒絕後這個型別會被排除。"}
                此決定<strong>無法復原</strong>,確定嗎?
              </p>
              <button
                type="button"
                className={confirming.verb === "accept" ? "proposals__accept" : "proposals__reject"}
                disabled={locked}
                onClick={onConfirm}
              >
                {confirming.verb === "accept" ? "確定採納" : "確定拒絕"}
              </button>
              <button type="button" disabled={decide.isPending} onClick={() => setConfirming(null)}>
                取消
              </button>
            </div>
          ) : (
            <div className="proposals__actions">
              {/* First click ARMS the confirm above (terminal-action guard); the
                  buttons also lock while ANY decision posts (decide.isPending), so
                  exactly one mutation is ever in flight — react-query owns the
                  pending lifecycle (no observer-detach stranding a row) and the
                  opposite-verb race can't form (Codex #104 P2). */}
              {/* HONEST label: 採納 mutates the project's configured ontology, so a
                  future extraction stores this type — say so, don't just "accept" */}
              <button
                type="button"
                className="proposals__accept"
                disabled={locked}
                onClick={() => setConfirming({ id: p.id, verb: "accept" })}
              >
                採納(加入本體)
              </button>
              <button
                type="button"
                className="proposals__reject"
                disabled={locked}
                onClick={() => setConfirming({ id: p.id, verb: "reject" })}
              >
                拒絕
              </button>
            </div>
          )}
        </li>
      ))}
      {decide.isError ? (
        <li className="review__line review__line--error">決定失敗:{message(decide.error)}</li>
      ) : null}
    </ul>
  );
}

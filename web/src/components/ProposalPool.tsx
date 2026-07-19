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
// build reads it); reject excludes it. A list with inline actions (not the merge
// queue's one-at-a-time flow) because a proposal decision is a single terminal
// choice, not a per-pair adjudication.
export function ProposalPool({ project }: { project: string }) {
  const proposals = useOntologyProposals(project);
  const decide = useDecideOntologyProposal(project);

  const onDecide = (proposalId: string, verb: ProposalVerb) => {
    decide.mutate({ proposalId, verb, reason: null });
  };

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
          <div className="proposals__actions">
            {/* Whole-pool lock while ANY decision is in flight (decide.isPending),
                not a per-row flag: a single useMutation observer tracks one
                mutation, and a second concurrent mutate() would detach it from the
                first — the first's onSettled would never fire and its row would
                stay stuck disabled (Codex #104 P2). Locking the pool keeps exactly
                one decision in flight, so react-query owns the pending lifecycle
                and decide.isError reliably reports the one failure. A proposal
                POST is sub-second; one-at-a-time matches the merge queue. */}
            {/* HONEST label: 採納 mutates the project's configured ontology, so a
                future extraction stores this type — say so, don't just "accept" */}
            <button
              type="button"
              className="proposals__accept"
              disabled={decide.isPending}
              onClick={() => onDecide(p.id, "accept")}
            >
              採納(加入本體)
            </button>
            <button
              type="button"
              className="proposals__reject"
              disabled={decide.isPending}
              onClick={() => onDecide(p.id, "reject")}
            >
              拒絕
            </button>
          </div>
        </li>
      ))}
      {decide.isError ? (
        <li className="review__line review__line--error">決定失敗:{message(decide.error)}</li>
      ) : null}
    </ul>
  );
}

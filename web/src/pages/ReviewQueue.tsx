import { CandidatesTable } from "../components/CandidatesTable";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import "./ReviewQueue.css";

// FE5 Entity-resolution review (DESIGN §17): the curator's merge-candidate queue
// for the active build. Same project-addressability guards as the other pages —
// a "." / ".." / slash-bearing key can't ride the single `{project}` path segment.
export function ReviewQueue() {
  const project = useActiveProject();

  if (project === undefined) return <p className="review__line">Unknown project.</p>;
  if (!isPathAddressable(project))
    return (
      <p className="review__line review__line--error">
        Project &quot;{project}&quot; isn&apos;t addressable over the API — its key contains
        &quot;/&quot; or is &quot;.&quot; / &quot;..&quot;, which a URL path segment can&apos;t
        carry.
      </p>
    );

  return (
    <section className="review">
      <h1 className="review__title">Entity Review</h1>
      <CandidatesTable project={project} />
    </section>
  );
}

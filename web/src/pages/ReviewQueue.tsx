import { ReviewCases } from "../components/ReviewCases";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import "./ReviewQueue.css";

// FE5 Entity-resolution review (DESIGN §17), reworked by UXA1: the curator's
// merge-candidate queue for the active build, one case at a time with the
// decision's basis on screen. Same project-addressability guards as the other
// pages — a "." / ".." / slash-bearing key can't ride the single `{project}`
// path segment.
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
      <h1 className="review__title">實體審核</h1>
      <p className="review__intro">
        建置時系統發現這些名字<strong>可能指同一個東西</strong>,但不敢自行決定。
        請逐案確認:同一個就合併,不同的就分開;拿不準先跳過。
      </p>
      <ReviewCases project={project} />
    </section>
  );
}

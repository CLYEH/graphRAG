import { useMemo, useState } from "react";

import {
  PolicyMissingError,
  isScopeNeutral,
  useEntities,
  useEntity,
  useRelation,
  useSubgraph,
} from "../api/queries";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import "./Graph.css";

import type { Entity, GraphContext, Relation } from "../api/queries";
import type { ReactNode } from "react";

// FE4 圖譜互動探索 (DESIGN §10.2): three columns — left search/filter over
// entities, middle subgraph viz, right node/edge detail (an edge shows
// type/confidence/evidence/來源/created_by/review_status). Read-only over the
// ACTIVE build (DR-006 via the inspect endpoints).
//
// Facts the page is built around (read from api/routers/inspect.py):
// * the subgraph REQUIRES query_policy in project config — its absence is a
//   NAMED condition with configuration guidance, not a generic failure;
// * hops beyond the policy ceiling are REJECTED (not clamped) — the page sends
//   what the operator chose and surfaces the server's own rejection;
// * entity lists have NO server-side search — the left filter runs over LOADED
//   pages only and says so (FE3's false-affordance lesson);
// * Relation.evidence[] rides ONLY the detail GET — clicking an edge fetches.
//
// Known gap (deliberate v2 scope): the SVG node/edge selection is pointer-only
// (no keyboard handlers) — a11y-complete graph navigation is a later pass. The
// §10.2 "actions" on the detail column are governance verbs, which live on the
// Review page (FE5) — this page stays read-only so there is one write surface.

type Selection = { kind: "node" | "edge"; id: string };

function message(error: unknown): string {
  return error instanceof Error ? error.message : "unknown error";
}

export function Graph() {
  const project = useActiveProject();

  if (project === undefined) return <p className="graph__line">Unknown project.</p>;
  if (!isPathAddressable(project))
    return (
      <p className="graph__line graph__line--error">
        Project &quot;{project}&quot; isn&apos;t addressable over the API — its key contains
        &quot;/&quot; or is &quot;.&quot; / &quot;..&quot;, which a URL path segment can&apos;t
        carry.
      </p>
    );

  return <GraphBody project={project} />;
}

function GraphBody({ project }: { project: string }) {
  const [centerId, setCenterId] = useState<string | undefined>(undefined);
  const [hops, setHops] = useState(1);
  const [selected, setSelected] = useState<Selection | null>(null);
  const list = useEntities(project);
  const sub = useSubgraph(project, centerId, hops);

  // Page-wide scope death (Codex, #75): a scope-gone answer on the ENTITY list
  // means the whole page's world is dead — leaving the middle/right columns
  // rendering their cached subgraph/detail would show the old build's graph
  // beside a line saying there is no active build. One predicate, one verdict,
  // all three columns (the class-17 rule applied at page scope, not per column).
  const keepsRows = list.isFetchNextPageError && isScopeNeutral(list.error);
  if (list.isError && !keepsRows)
    return (
      <section className="graph">
        <h1 className="graph__title">Graph</h1>
        <p className="graph__line graph__line--error">
          Could not load entities: {message(list.error)}
        </p>
      </section>
    );
  // The build-SPLICE verdict is page-wide for the same reason (Codex, #75): a
  // load-more that SUCCEEDS from a different build proves the world changed
  // under the page — the cached subgraph/detail describe the old build just as
  // much as the spliced list would.
  const pages = list.data?.pages ?? [];
  if (new Set(pages.map((p) => p.buildId)).size > 1)
    return (
      <section className="graph">
        <h1 className="graph__title">Graph</h1>
        <p className="graph__line graph__line--error">
          The active build changed while loading entities — the page would mix two builds. Reload to
          see a single build.
        </p>
      </section>
    );

  // Selection is reconciled against the CURRENT subgraph by comparison, not by
  // clearing on events (Codex, #75; the FE2 lesson): shrinking hops, recentering
  // or a refetch can all leave `selected` pointing at a node/edge the displayed
  // graph no longer contains — the right column must not render detail for
  // something that is not on screen, whatever path removed it.
  const graph = sub.data?.graph;
  const visibleSelection =
    selected === null || graph === undefined
      ? null
      : selected.kind === "node"
        ? graph.nodes.some((n) => n.id === selected.id)
          ? selected
          : null
        : graph.edges.some((e) => e.id === selected.id)
          ? selected
          : null;

  return (
    <section className="graph">
      <h1 className="graph__title">Graph</h1>
      <p className="graph__hint">
        Explore the <strong>active build&apos;s</strong> knowledge graph: pick an entity, walk its
        neighborhood, click nodes and edges for the fields behind them.
      </p>
      <div className="graph__columns">
        <EntityColumn
          list={list}
          keepsRows={keepsRows}
          centerId={centerId}
          onCenter={(id) => {
            setCenterId(id);
            setSelected({ kind: "node", id });
          }}
        />
        <VizColumn
          project={project}
          sub={sub}
          centerId={centerId}
          hops={hops}
          onHops={setHops}
          selected={visibleSelection}
          onSelect={setSelected}
          onCenter={setCenterId}
        />
        <DetailColumn project={project} selected={visibleSelection} />
      </div>
    </section>
  );
}

// ---- left: entity list + honest client-side filter ---------------------------

function EntityColumn({
  list,
  keepsRows,
  centerId,
  onCenter,
}: {
  list: ReturnType<typeof useEntities>;
  keepsRows: boolean;
  centerId: string | undefined;
  onCenter: (id: string) => void;
}) {
  const [filter, setFilter] = useState("");

  if (list.isPending) return <div className="graph__col">Loading entities…</div>;
  // Same cached-rows discipline as the FE3 lists: a refetch re-verifies the
  // active build, so until it answers the rows are unverified; a next-page
  // fetch extends the pinned build and keeps the list. (The scope-gone isError
  // case never reaches here — GraphBody fails the WHOLE page closed on it.)
  if (list.isFetching && !list.isFetchingNextPage)
    return <div className="graph__col">Loading entities…</div>;

  // (the build-splice case never reaches here — GraphBody fails the page closed)
  const rows = (list.data?.pages ?? []).flatMap((p) => p.rows);
  if (rows.length === 0) return <div className="graph__col">No entities in the active build.</div>;

  const needle = filter.trim().toLowerCase();
  const shown = needle
    ? rows.filter(
        (e) =>
          e.canonical_name.toLowerCase().includes(needle) || e.type.toLowerCase().includes(needle),
      )
    : rows;

  return (
    <div className="graph__col" aria-label="entities">
      <label className="graph__field">
        filter
        <input
          type="search"
          value={filter}
          placeholder="name or type…"
          onChange={(e) => setFilter(e.target.value)}
        />
      </label>
      {/* the API has no entity search — say exactly what this filter covers */}
      <p className="graph__muted">
        Filters the {rows.length} loaded entit{rows.length === 1 ? "y" : "ies"} only — load more to
        widen it.
      </p>
      <ul className="graph__entities">
        {shown.map((e) => (
          <li key={e.id}>
            {/* the subgraph endpoint only accepts ACTIVE seeds (repo.active_entity_ids
                — Codex, #75): a merged/rejected row is still real build content worth
                LISTING, but clicking it could only 404, so it is disabled and says why */}
            <button
              type="button"
              className={`graph__entity${e.id === centerId ? " graph__entity--center" : ""}`}
              disabled={e.status !== "active"}
              title={
                e.status !== "active"
                  ? `status ${e.status} — only active entities can seed a subgraph`
                  : undefined
              }
              onClick={() => onCenter(e.id)}
            >
              <span className="graph__entity-name">{e.canonical_name}</span>
              <span className="graph__entity-type">
                {e.status !== "active" ? `${e.type} · ${e.status}` : e.type}
              </span>
            </button>
          </li>
        ))}
        {shown.length === 0 && <li className="graph__muted">No loaded entity matches.</li>}
      </ul>
      {keepsRows && (
        <p className="graph__line--error">Could not load more entities: {message(list.error)}</p>
      )}
      {list.hasNextPage && (
        <button
          type="button"
          className="graph__more"
          disabled={list.isFetchingNextPage}
          onClick={() => list.fetchNextPage()}
        >
          {list.isFetchingNextPage ? "Loading…" : "Load more entities"}
        </button>
      )}
    </div>
  );
}

// ---- middle: the subgraph, laid out radially by BFS depth --------------------

type Positioned = {
  x: number;
  y: number;
  id: string;
  label: string;
  type: string | null;
};

const W = 640;
const H = 480;

/** Deterministic radial layout: the center entity sits at the origin, every
 *  other node on a ring at its BFS depth, spread evenly by angle. Pure so the
 *  tests can assert placement, and honest about disconnected leftovers (nodes
 *  the edge set never reaches sit on an outer ring rather than vanishing). */
export function radialLayout(graph: GraphContext, centerId: string): Positioned[] {
  const depth = new Map<string, number>([[centerId, 0]]);
  const queue = [centerId];
  while (queue.length > 0) {
    const cur = queue.shift() as string;
    const d = depth.get(cur) as number;
    for (const e of graph.edges) {
      for (const next of [e.dst === cur ? e.src : e.src === cur ? e.dst : null]) {
        if (next && !depth.has(next)) {
          depth.set(next, d + 1);
          queue.push(next);
        }
      }
    }
  }
  const maxDepth = Math.max(1, ...depth.values());
  const unreachable = graph.nodes.filter((n) => !depth.has(n.id));
  const rings = new Map<number, string[]>();
  for (const n of graph.nodes) {
    const d = depth.get(n.id) ?? maxDepth + 1;
    rings.set(d, [...(rings.get(d) ?? []), n.id]);
  }
  const ringRadius = Math.min(W, H) / 2 / (maxDepth + (unreachable.length > 0 ? 2 : 1));
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const out: Positioned[] = [];
  for (const [d, ids] of rings) {
    ids.forEach((id, i) => {
      const angle = (2 * Math.PI * i) / ids.length;
      const r = d * ringRadius;
      const n = byId.get(id);
      out.push({
        id,
        x: W / 2 + r * Math.cos(angle),
        y: H / 2 + r * Math.sin(angle),
        label: n?.label ?? id.slice(0, 8),
        type: n?.type ?? null,
      });
    });
  }
  return out;
}

function VizColumn({
  project,
  sub,
  centerId,
  hops,
  onHops,
  selected,
  onSelect,
  onCenter,
}: {
  project: string;
  sub: ReturnType<typeof useSubgraph>;
  centerId: string | undefined;
  hops: number;
  onHops: (h: number) => void;
  selected: Selection | null;
  onSelect: (s: Selection) => void;
  onCenter: (id: string) => void;
}) {
  const positioned = useMemo(
    () => (sub.data && centerId ? radialLayout(sub.data.graph, centerId) : []),
    [sub.data, centerId],
  );

  if (centerId === undefined)
    return <div className="graph__col graph__muted">Pick an entity on the left to start.</div>;

  return (
    <div className="graph__col" aria-label="subgraph">
      <label className="graph__field graph__hops">
        hops
        <input
          type="number"
          min={1}
          value={hops}
          onChange={(e) => {
            const v = Number(e.target.value);
            if (Number.isInteger(v) && v >= 1) onHops(v);
          }}
        />
      </label>

      {/* class-17 discipline: a refetch re-verifies the active build — until it
          answers, the cached subgraph is unverified and does not render */}
      {sub.isFetching ? (
        <p className="graph__muted">Loading subgraph…</p>
      ) : sub.isError ? (
        sub.error instanceof PolicyMissingError ? (
          <div className="graph__policy">
            <p className="graph__line--error">
              This project has no <code>query_policy</code> configured, and the graph endpoints are
              §21-governed — there is no default to fall back to.
            </p>
            <p className="graph__muted">
              Configure it once via <code>PATCH /projects/{project}</code> with a{" "}
              <code>query_policy</code> block (see DESIGN §21), then reload.
            </p>
          </div>
        ) : (
          <p className="graph__line--error">Could not load the subgraph: {message(sub.error)}</p>
        )
      ) : sub.data && positioned.length > 0 ? (
        <>
          {sub.data.buildId && <p className="graph__muted">Active build {sub.data.buildId}</p>}
          <svg
            viewBox={`0 0 ${W} ${H}`}
            className="graph__svg"
            role="img"
            aria-label="entity neighborhood"
          >
            {sub.data.graph.edges.map((e) => {
              const a = positioned.find((p) => p.id === e.src);
              const b = positioned.find((p) => p.id === e.dst);
              if (!a || !b) return null;
              const isSel = selected?.kind === "edge" && selected.id === e.id;
              return (
                <g key={e.id}>
                  <line
                    x1={a.x}
                    y1={a.y}
                    x2={b.x}
                    y2={b.y}
                    className={`graph__edge${isSel ? " graph__edge--selected" : ""}`}
                    onClick={() => onSelect({ kind: "edge", id: e.id })}
                  />
                  <text
                    x={(a.x + b.x) / 2}
                    y={(a.y + b.y) / 2 - 4}
                    className="graph__edge-label"
                    onClick={() => onSelect({ kind: "edge", id: e.id })}
                  >
                    {e.type}
                  </text>
                </g>
              );
            })}
            {positioned.map((n) => {
              const isSel = selected?.kind === "node" && selected.id === n.id;
              const isCenter = n.id === centerId;
              return (
                <g
                  key={n.id}
                  transform={`translate(${n.x}, ${n.y})`}
                  className="graph__node"
                  onClick={() => onSelect({ kind: "node", id: n.id })}
                  onDoubleClick={() => onCenter(n.id)}
                >
                  <circle
                    r={isCenter ? 14 : 10}
                    className={`graph__dot${isSel ? " graph__dot--selected" : ""}${isCenter ? " graph__dot--center" : ""}`}
                  />
                  <text y={-16} className="graph__node-label">
                    {n.label}
                  </text>
                </g>
              );
            })}
          </svg>
          <p className="graph__muted">
            {sub.data.graph.nodes.length} node{sub.data.graph.nodes.length === 1 ? "" : "s"} ·{" "}
            {sub.data.graph.edges.length} edge{sub.data.graph.edges.length === 1 ? "" : "s"} ·
            double-click a node to re-center
          </p>
        </>
      ) : (
        <p className="graph__muted">The neighborhood is empty at {hops} hop(s).</p>
      )}
    </div>
  );
}

// ---- right: node / edge detail ------------------------------------------------

function DetailColumn({ project, selected }: { project: string; selected: Selection | null }) {
  const entity = useEntity(project, selected?.kind === "node" ? selected.id : undefined);
  const relation = useRelation(project, selected?.kind === "edge" ? selected.id : undefined);

  if (!selected)
    return <div className="graph__col graph__muted">Click a node or an edge for detail.</div>;

  const q = selected.kind === "node" ? entity : relation;
  return (
    <div className="graph__col" aria-label={`${selected.kind} detail`}>
      <h2 className="graph__subtitle">{selected.kind === "node" ? "Entity" : "Relation"}</h2>
      {/* FE3's Detail discipline verbatim: loading, error, fields are a three-way
          exclusive chain — fields render only from a settled, successful answer */}
      {q.isFetching ? (
        <p className="graph__muted">Loading…</p>
      ) : q.isError ? (
        <p className="graph__line--error">{message(q.error)}</p>
      ) : selected.kind === "node" && entity.data ? (
        <EntityFields e={entity.data} />
      ) : selected.kind === "edge" && relation.data ? (
        <RelationFields r={relation.data} />
      ) : null}
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="graph__fieldrow">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function blob(value: unknown): string {
  return value && Object.keys(value as object).length > 0 ? JSON.stringify(value, null, 2) : "—";
}

function EntityFields({ e }: { e: Entity }) {
  return (
    <dl className="graph__fields">
      <Field label="canonical_name">{e.canonical_name}</Field>
      <Field label="type">{e.type}</Field>
      <Field label="entity_key">{e.entity_key}</Field>
      <Field label="status">{e.status}</Field>
      <Field label="review_status">{e.review_status ?? "—"}</Field>
      <Field label="created_by">{e.created_by ?? "—"}</Field>
      <Field label="attributes">
        <pre className="graph__pre">{blob(e.attributes)}</pre>
      </Field>
    </dl>
  );
}

// §10.2 names the edge fields explicitly: type/confidence/evidence/來源/
// created_by/review_status — every one is here, and evidence quotes carry
// their denormalized source_uri (來源) which survives chunk pruning (§27.4).
function RelationFields({ r }: { r: Relation }) {
  return (
    <dl className="graph__fields">
      <Field label="type">{r.type}</Field>
      <Field label="confidence">{r.confidence ?? "—"}</Field>
      <Field label="status">{r.status}</Field>
      <Field label="review_status">{r.review_status ?? "—"}</Field>
      <Field label="created_by">{r.created_by ?? "—"}</Field>
      <Field label="evidence">
        {r.evidence && r.evidence.length > 0 ? (
          <ul className="graph__evidence">
            {r.evidence.map((ev) => (
              <li key={ev.id}>
                <span className="graph__evidence-type">{ev.evidence_type}</span>
                {ev.quote && <blockquote className="graph__quote">{ev.quote}</blockquote>}
                {ev.source_uri && <span className="graph__evidence-src">{ev.source_uri}</span>}
              </li>
            ))}
          </ul>
        ) : (
          "—"
        )}
      </Field>
    </dl>
  );
}

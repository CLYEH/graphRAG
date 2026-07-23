import { useEffect, useState } from "react";

import { isScopeNeutral, useChunk, useChunks, useDocument, useDocuments } from "../api/queries";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import "./Inspect.css";

import type { Chunk, Document, InspectPage } from "../api/queries";
import type { ReactNode } from "react";

// FE3 檢視 (DESIGN §10.2): browse what the ACTIVE build actually contains — the
// documents that were ingested, and the chunks they were split into. Read-only: it
// answers "what did this build actually produce?" before anyone trusts a query over it.
//
// Scope is the spec's: §10.2 names this page 檢視(文件/chunks). Entity/relation detail
// with evidence is the spec'd content of a DIFFERENT page — 圖譜互動探索 (FE4: "點邊顯示
// type/confidence/evidence/來源") — so it is deliberately NOT here; building it in both
// would leave two edge-evidence surfaces to keep in step.
//
// Every list is build-scoped by the repository layer (DR-006) and every response names
// the build that served it. The page pins that build across pages and fails loud on a
// swap rather than showing a corpus spliced from two builds.

type Tab = "documents" | "chunks";

const TABS: { id: Tab; label: string }[] = [
  { id: "documents", label: "文件" },
  { id: "chunks", label: "段落" },
];

// A row's id is enough to fetch its detail; the LIST rows omit the heavy field
// (Document.raw) entirely, so a click is a real fetch, not a local expand.
type Selection = { tab: Tab; id: string };

export function Inspect() {
  const project = useActiveProject();
  const [tab, setTab] = useState<Tab>("documents");
  const [selected, setSelected] = useState<Selection | null>(null);

  if (project === undefined) return <p className="inspect__line">Unknown project.</p>;
  if (!isPathAddressable(project))
    return (
      <p className="inspect__line inspect__line--error">
        Project &quot;{project}&quot; isn&apos;t addressable over the API — its key contains
        &quot;/&quot; or is &quot;.&quot; / &quot;..&quot;, which a URL path segment can&apos;t
        carry.
      </p>
    );

  function select(tab: Tab, id: string) {
    setSelected((current) => (current?.id === id ? null : { tab, id }));
  }

  return (
    <section className="inspect">
      <h1 className="inspect__title">Inspect</h1>
      <p className="inspect__hint">
        What the <strong>active build</strong> contains. Select a row to see the fields the list
        omits.
      </p>

      <div className="inspect__tabs" role="tablist" aria-label="Inspect sections">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`inspect-tab-${t.id}`}
            aria-controls={`inspect-panel-${t.id}`}
            aria-selected={tab === t.id}
            className={`inspect__tab${tab === t.id ? " inspect__tab--active" : ""}`}
            onClick={() => {
              setTab(t.id);
              setSelected(null); // a selection from the other tab has no meaning here
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div
        role="tabpanel"
        id={`inspect-panel-${tab}`}
        aria-labelledby={`inspect-tab-${tab}`}
        className="inspect__panel"
      >
        {tab === "documents" ? (
          <DocumentsTab
            project={project}
            selected={selected}
            onSelect={select}
            onDeselect={() => setSelected(null)}
          />
        ) : (
          <ChunksTab project={project} selected={selected} onSelect={select} />
        )}
      </div>
    </section>
  );
}

// ---- the paged list shell, shared by the two tabs ---------------------------

type ListProps<T> = {
  label: string;
  /** search-aware empty text: zero MATCHES is not an empty build (SS1b R3) */
  emptyMessage?: string;
  columns: { header: string; cell: (row: T) => ReactNode }[];
  query: {
    data?: { pages: InspectPage<T>[] };
    isPending: boolean;
    isFetching: boolean;
    isError: boolean;
    isFetchNextPageError: boolean;
    error: unknown;
    hasNextPage: boolean;
    isFetchingNextPage: boolean;
    fetchNextPage: () => void;
  };
  selectedId?: string;
  onSelect: (id: string) => void;
  detail: ReactNode;
};

function message(error: unknown): string {
  return error instanceof Error ? error.message : "unknown error";
}

function PagedList<T extends { id: string }>({
  label,
  emptyMessage,
  columns,
  query,
  selectedId,
  onSelect,
  detail,
}: ListProps<T>) {
  const {
    data,
    isPending,
    isFetching,
    isError,
    isFetchNextPageError,
    error,
    hasNextPage,
    isFetchingNextPage,
    fetchNextPage,
  } = query;

  if (isPending) return <p className="inspect__muted">Loading {label}…</p>;

  // A background REFETCH (focus/reconnect) exists to re-ask which build is active — so until
  // it answers, the cached pages are exactly the thing being verified. react-query serves
  // them anyway (stale-while-revalidate) with isError false, which would show build A's rows,
  // still clickable, after build B was activated in another tab — until the refetch settles,
  // or indefinitely on a hung request (Codex, #72). Same fail-closed rule as everywhere else
  // on this page: unverified rows don't render. A next-page fetch is the one fetch that does
  // NOT re-open the question for the rows already on screen — it extends the pinned build,
  // and the splice/scope guards below judge its answer — so it must not blank the table.
  if (isFetching && !isFetchingNextPage) return <p className="inspect__muted">Loading {label}…</p>;

  // react-query KEEPS the cached pages on any failed fetch, so `isError` beside a populated
  // cache is not one situation. The rows stay showable under exactly ONE condition: the build
  // that served them is still the active build. So THAT — not "which request failed" — decides.
  //   * a failed REFETCH (focus/reconnect) — the cached rows are unverified against the
  //     server's current answer; fail closed (the stale-data-during-refetch trap of #70).
  //   * a failed "load more" — keep the rows ONLY when the failure says nothing about the
  //     binding (transport, or a scope-neutral code: store down, throttled, 500, timeout).
  //     Discarding a good table over one flaky page would be a worse failure than the one
  //     reported. But a load-more can ALSO return NO_ACTIVE_BUILD or PROJECT_NOT_FOUND, and
  //     those prove every row on screen belongs to a build that no longer exists — keeping
  //     them because "it was only a load-more" would leave a vanished corpus on display.
  // Note the symmetry this restores: the list already fails closed when a swap makes page 2
  // arrive from a DIFFERENT build. A swap that leaves NO active build (or takes the project
  // with it) is the same event, and must not land on the opposite branch.
  const keepsRows = isFetchNextPageError && isScopeNeutral(error);
  if (isError && !keepsRows)
    return (
      <p className="inspect__muted inspect__muted--error">
        Could not load {label}: {message(error)}
      </p>
    );

  const pages = data?.pages ?? [];
  // Each request re-resolves the active build, so a build activated between page 1 and
  // page 2 would splice two different corpora into one table. Show none of it: a spliced
  // list is wrong data, which this platform treats as strictly worse than a loud failure.
  const builds = new Set(pages.map((p) => p.buildId));
  if (builds.size > 1)
    return (
      <p className="inspect__muted inspect__muted--error">
        The active build changed while loading {label} — the pages below would come from two
        different builds. Reload to see a single build.
      </p>
    );

  const rows = pages.flatMap((p) => p.rows);
  // An empty table really does mean an empty build: no active build answers 409, not a
  // 200 with no rows, so this cannot mistake an outage for an empty corpus.
  if (rows.length === 0)
    return <p className="inspect__muted">{emptyMessage ?? `No ${label} in the active build.`}</p>;

  return (
    <>
      <table className="inspect__table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.header}>{c.header}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.id}
              className={row.id === selectedId ? "inspect__row--selected" : undefined}
            >
              {columns.map((c, i) => (
                <td key={c.header}>
                  {i === 0 ? (
                    <button
                      type="button"
                      className="inspect__rowbtn"
                      aria-pressed={row.id === selectedId}
                      onClick={() => onSelect(row.id)}
                    >
                      {c.cell(row)}
                    </button>
                  ) : (
                    c.cell(row)
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>

      {/* the SAME predicate that let the rows survive the guard above — reusing it (rather
          than restating the condition) is what keeps the two from ever disagreeing */}
      {keepsRows && (
        <p className="inspect__muted inspect__muted--error">
          Could not load more {label}: {message(error)}
        </p>
      )}

      {hasNextPage && (
        <button
          type="button"
          className="inspect__more"
          onClick={() => fetchNextPage()}
          disabled={isFetchingNextPage}
        >
          {isFetchingNextPage ? "載入中…" : "載入更多"}
        </button>
      )}

      {detail}
    </>
  );
}

// ---- detail panel -----------------------------------------------------------

// Values come straight from ingested documents — they are DATA, never markup or
// instructions. React escapes them; nothing here interpolates them into HTML.
function Detail({
  title,
  fields,
  isFetching,
  isError,
  error,
}: {
  title: string;
  fields?: [string, ReactNode][];
  isFetching: boolean;
  isError: boolean;
  error: unknown;
}) {
  return (
    <aside className="inspect__detail" aria-label={`${title} detail`}>
      <h2 className="inspect__detail-title">{title}</h2>
      {/* Same trap as the list, one component over, in both of its states. A failed REFETCH
          raises isError while react-query keeps the previous `data` — and while a refetch is
          still IN FLIGHT it serves that `data` with isError false, so either way the OLD
          build's id/source_uri/raw would render as if current (under the error line, or while
          the 404 that will disown it is still on the wire — a hung request shows it forever).
          Loading, error, and fields are mutually exclusive: fields render only from a settled,
          successful answer. Fail closed here rather than in each tab, so a tab added later
          cannot re-open it. (isFetching covers the initial load too — the query only runs
          with a selected id, so pending means fetching.) */}
      {isFetching ? (
        <p className="inspect__muted">Loading…</p>
      ) : isError ? (
        <p className="inspect__muted inspect__muted--error">{message(error)}</p>
      ) : (
        fields && (
          <dl className="inspect__fields">
            {fields.map(([label, value]) => (
              <div key={label} className="inspect__field">
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        )
      )}
    </aside>
  );
}

function blob(value: unknown): string {
  return value && Object.keys(value).length > 0 ? JSON.stringify(value, null, 2) : "—";
}

function text(value: string | null | undefined): ReactNode {
  return value ? <pre className="inspect__pre">{value}</pre> : "—";
}

// The last path segment of a source uri — the human name of the file; the
// full uri stays reachable (hover + detail) but stops being the row identity.
function basename(uri: string): string {
  const trimmed = uri.replace(/[/\\]+$/, "");
  const seg = trimmed.split(/[/\\]/).pop();
  return seg && seg !== "" ? seg : uri;
}

function fmt(ts: string | null | undefined): string {
  return ts ? ts.replace("T", " ").replace(/\..*$/, "").replace("Z", " UTC") : "—";
}

// ---- the two tabs -----------------------------------------------------------

//: SS1b documents search — mirrors the Graph page's server-side entity search
const DOC_SEARCH_MAX = 256; // the contract's q maxLength

// re-declared from Graph.tsx (page-local on purpose: a pages/ cross-import
// would couple two routes for three lines)
function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(id);
  }, [value, delayMs]);
  return debounced;
}

type TabProps = {
  project: string;
  selected: Selection | null;
  onSelect: (tab: Tab, id: string) => void;
  /** clear the selection outright (no toggle) — search changes use this (R5) */
  onDeselect?: () => void;
};

function DocumentsTab({ project, selected, onSelect, onDeselect }: TabProps) {
  const id = selected?.tab === "documents" ? selected.id : undefined;
  const [search, setSearch] = useState("");
  // debounced + trimmed: the QUERY the shown rows answer; q rides the hook's
  // queryKey so a new search restarts from page 1 (SS1b, the Graph pattern)
  const q = useDebounced(search.trim(), 300) || undefined;
  const list = useDocuments(project, q);
  const total = list.data?.pages[0]?.total;
  const detail = useDocument(project, id);
  const doc = detail.data;

  return (
    <>
      <label className="inspect__search">
        搜尋
        <input
          type="search"
          value={search}
          maxLength={DOC_SEARCH_MAX}
          placeholder="source_uri…"
          onChange={(e) => {
            setSearch(e.target.value);
            // R5: a selection made under one search means nothing under
            // another — the row may drop out of the new result set while its
            // detail pane lingers as if it belonged to the filtered rows
            if (id) onDeselect?.();
          }}
        />
      </label>
      {/* the count is the server's exact total for THIS search — page 1's
          meta.total, never a loaded-rows count. Gated on the SAME settled-
          success condition PagedList shows rows under (SS1b R3 / class 17):
          during a refetch, or beside a failed one, the cached total is
          unverified against the current build and must not linger. The label
          CARRIES the applied term (the GraphBody pattern, R6): while the
          debounce is pending the box already shows the new input, so the
          count must say which query it answers. */}
      {q && total !== undefined && !list.isFetching && !list.isError ? (
        <p className="inspect__hint">
          符合「{q}」的文件:{total} 筆
        </p>
      ) : null}
      <PagedList<Document>
        label="documents"
        emptyMessage={q ? `沒有符合「${q}」的文件。` : undefined}
        columns={[
          {
            header: "文件",
            // the row leads with the file NAME — the full uri (a filesystem path
            // in disguise) rides the hover title and the detail pane (UXA3)
            cell: (d) => <span title={d.source_uri}>{basename(d.source_uri)}</span>,
          },
          { header: "類型", cell: (d) => d.mime ?? "—" },
          { header: "狀態", cell: (d) => (d.status === "ingested" ? "已匯入" : (d.status ?? "—")) },
          { header: "匯入時間", cell: (d) => fmt(d.ingested_at) },
        ]}
        query={list}
        selectedId={id}
        onSelect={(rowId) => onSelect("documents", rowId)}
        detail={
          id && (
            <Detail
              title="Document"
              isFetching={detail.isFetching}
              isError={detail.isError}
              error={detail.error}
              fields={
                doc && [
                  ["id", doc.id],
                  ["source_uri", doc.source_uri],
                  ["content_hash", doc.content_hash ?? "—"],
                  [
                    "metadata",
                    <pre key="metadata" className="inspect__pre">
                      {blob(doc.metadata)}
                    </pre>,
                  ],
                  // `raw` is detail-only — the list omits the key entirely, which is the
                  // whole reason a row click fetches.
                  ["raw", text(doc.raw)],
                ]
              }
            />
          )
        }
      />
    </>
  );
}

function ChunksTab({ project, selected, onSelect }: TabProps) {
  const id = selected?.tab === "chunks" ? selected.id : undefined;
  const list = useChunks(project);
  const detail = useChunk(project, id);
  const chunk = detail.data;

  return (
    <PagedList<Chunk>
      label="chunks"
      columns={[
        { header: "序號", cell: (c) => c.ordinal },
        {
          header: "所屬文件",
          // an id is not a name — words on the surface, id on hover (UXA3)
          cell: (c) => <span title={c.document_id}>(懸停看識別碼)</span>,
        },
        { header: "內容開頭", cell: (c) => c.text.slice(0, 80) },
        { header: "詞元數(估計)", cell: (c) => c.token_count ?? "—" },
      ]}
      query={list}
      selectedId={id}
      onSelect={(rowId) => onSelect("chunks", rowId)}
      detail={
        id && (
          <Detail
            title="Chunk"
            isFetching={detail.isFetching}
            isError={detail.isError}
            error={detail.error}
            fields={
              chunk && [
                ["id", chunk.id],
                ["document_id", chunk.document_id],
                ["ordinal", chunk.ordinal],
                ["offsets", `${chunk.start_offset ?? "—"} … ${chunk.end_offset ?? "—"}`],
                ["vector_point_id", chunk.vector_point_id ?? "—"],
                ["text", text(chunk.text)],
              ]
            }
          />
        )
      }
    />
  );
}

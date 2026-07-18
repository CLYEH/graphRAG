import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { focusManager } from "@tanstack/react-query";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Import } from "./Import";
import { api } from "../api/client";
import {
  job,
  project,
  projectRoute,
  renderWithProviders,
  source,
  sseResponse,
  stubPost,
  stubPostError,
  stubSources,
} from "../test-utils";

import type { Project, Source } from "../api/queries";

const JOB_ID = "0c9f7a3e-2f65-4f0a-8a2b-7d1e9c4b5a6f";
const META = { request_id: "0", build_id: null, elapsed_ms: 1, next_cursor: null };

afterEach(() => {
  vi.restoreAllMocks();
});

function renderImport(route: string) {
  return renderWithProviders(
    <Routes>
      <Route path="/p/:project/import" element={<Import />} />
    </Routes>,
    { route },
  );
}

// Routes the two GETs the Import page fires: the projects list (RunPipeline reads
// the active project's config for the ontology gate) and the source list.
function stubImportGets(proj: Project, srcs: Source[]) {
  return vi
    .spyOn(api, "GET")
    .mockImplementation(((path: string) =>
      Promise.resolve(
        path === "/projects"
          ? { data: { data: [proj], meta: META }, error: undefined }
          : { data: { data: srcs, meta: META }, error: undefined },
      )) as never);
}

describe("Import", () => {
  it("registers a text source with a per-attempt idempotency key and clears the form", async () => {
    stubSources([]);
    // first attempt fails (a lost 201 looks the same client-side), the retry
    // succeeds — the key must be IDENTICAL across the two calls so the server
    // replays the committed row instead of duplicating it; it must NOT be
    // uri-derived (uri isn't unique server-side; a stable natural key would
    // suppress an intentional re-registration — edits mint a fresh key instead)
    const post = vi
      .spyOn(api, "POST")
      .mockResolvedValueOnce({
        data: undefined,
        error: { error: { code: "STORE_UNAVAILABLE", message: "down", details: null } },
      } as never)
      .mockResolvedValue({
        data: { data: source({ uri: "file:///data/corpus/", kind: "text" }), meta: META },
        error: undefined,
      } as never);
    renderImport(projectRoute("acme", "import"));

    const uri = screen.getByLabelText("uri");
    fireEvent.change(uri, { target: { value: "file:///data/corpus/" } });
    fireEvent.click(screen.getByRole("button", { name: "登記來源" }));
    await screen.findByText(/登記失敗/);
    fireEvent.click(screen.getByRole("button", { name: "登記來源" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));

    type Call = [string, { params: { header: { "Idempotency-Key": string } }; body: unknown }];
    const [path, first] = post.mock.calls[0] as Call;
    const [, second] = post.mock.calls[1] as Call;
    expect(path).toBe("/projects/{project}/sources");
    expect(first.body).toEqual({ uri: "file:///data/corpus/", kind: "text" });
    const key = first.params.header["Idempotency-Key"];
    expect(key).toBeTruthy();
    expect(key).not.toContain("corpus"); // random per attempt, not uri-derived
    expect(second.params.header["Idempotency-Key"]).toBe(key);
    // the form clears so the next source starts fresh (and with it, a fresh key)
    await waitFor(() => expect(uri).toHaveValue(""));
  });

  it("requires table + pk_column for a structured source and sends them as metadata", async () => {
    stubSources([]);
    const post = stubPost(source({ uri: "file:///data/rows.csv", kind: "structured" }));
    renderImport(projectRoute("acme", "import"));

    fireEvent.change(screen.getByLabelText("uri"), { target: { value: "file:///data/rows.csv" } });
    fireEvent.change(screen.getByLabelText("kind"), { target: { value: "structured" } });

    // read_csv_rows needs table + pk_column, so the submit must stay blocked until
    // they're supplied — else the build fails on missing structured metadata
    expect(screen.getByRole("button", { name: "登記來源" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("table"), { target: { value: "documents" } });
    fireEvent.change(screen.getByLabelText("pk_column"), { target: { value: "id" } });
    fireEvent.click(screen.getByRole("button", { name: "登記來源" }));

    await waitFor(() => expect(post).toHaveBeenCalled());
    const [, init] = post.mock.calls[0] as [string, { body: unknown }];
    expect(init.body).toEqual({
      uri: "file:///data/rows.csv",
      kind: "structured",
      metadata: { table: "documents", pk_column: "id" },
    });
  });

  it("requires the xlsx column mapping and sends it typed (extra_columns as a list)", async () => {
    // SRC1: the mapping (which column is the title/body) rides the source's
    // metadata — resolve_source fails a build without title/body_column, so
    // the submit stays blocked until both are supplied; the optional keys ride
    // only when non-blank, and extra_columns is a comma-split LIST (the
    // backend rejects a bare string loud)
    stubSources([]);
    const post = stubPost(source({ uri: "file:///data/guide.xlsx", kind: "xlsx" }));
    renderImport(projectRoute("acme", "import"));

    fireEvent.change(screen.getByLabelText("uri"), {
      target: { value: "file:///data/guide.xlsx" },
    });
    fireEvent.change(screen.getByLabelText("kind"), { target: { value: "xlsx" } });

    expect(screen.getByRole("button", { name: "登記來源" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/title_column/), { target: { value: "標題" } });
    expect(screen.getByRole("button", { name: "登記來源" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/body_column/), { target: { value: "內容詳情" } });
    fireEvent.change(screen.getByLabelText("id_column"), { target: { value: "編號" } });
    fireEvent.change(screen.getByLabelText(/extra_columns/), {
      target: { value: "位置, 分類, " },
    });
    fireEvent.change(screen.getByLabelText("label"), { target: { value: "導覽" } });
    fireEvent.click(screen.getByRole("button", { name: "登記來源" }));

    await waitFor(() => expect(post).toHaveBeenCalled());
    const [, init] = post.mock.calls[0] as [string, { body: unknown }];
    expect(init.body).toEqual({
      uri: "file:///data/guide.xlsx",
      kind: "xlsx",
      metadata: {
        title_column: "標題",
        body_column: "內容詳情",
        id_column: "編號",
        extra_columns: ["位置", "分類"],
        label: "導覽",
      },
    });
  });

  it("blocks a uri the backend would misread, before POSTing", async () => {
    stubSources([]);
    const post = stubPost(source());
    renderImport(projectRoute("acme", "import"));

    const uri = screen.getByLabelText("uri");
    const add = () => screen.getByRole("button", { name: "登記來源" });
    // _local_path reads only urlparse(uri).path, so each of these registers a
    // source the build then misreads or rejects — refuse them at the source:
    // a non-file scheme (unwired), a host-bearing file uri (host silently
    // dropped → wrong path), an empty-path file uri (resolves to the worker's
    // cwd), and query/hash-bearing uris (silently stripped → the worker reads a
    // different path than the stored uri displays)
    for (const bad of [
      "https://x/doc",
      "file://nas/corpus",
      "file://",
      "file:///?x",
      "file:///data/corpus?old",
      "file:///a#frag",
      // four slashes parse to a "//"-leading path that url2pathname reinterprets
      // as a UNC authority — the worker can't read the displayed path
      "file:////nas/corpus",
      // the backend DECODES before resolving, so an encoded leading slash lands
      // as "//" (server root / UNC) despite a clean-looking raw pathname
      "file:///%2F",
      "file:///%2Fdata",
      // a malformed percent-escape decodes differently than the backend reads it
      "file:///%zz",
      // encoded separators survive URL parsing (%2F stays literal in pathname)
      // and only materialize on the backend's decode — the filesystem then
      // resolves the sprung "//../.." to a different tree than displayed
      "file:///safe/%2F..%2F..%2Fetc",
      // an embedded NUL can't name a real file on any supported OS
      "file:///data/%00corpus",
      // dot segments — raw or encoded — are resolved by the filesystem to a
      // different tree than the stored uri appears to name; the gate checks the
      // BACKEND-derived raw path, because the browser normalizes these out of
      // url.pathname before any pathname-based check could see them
      "file:///data/../etc",
      "file:///data/%2e%2e/etc",
      // literal C0 controls: Python's urlsplit strips tab/LF/CR at ANY position
      // before .path, so "\t.." displays as a segment but READS as a traversal
      // (newline variants are untypeable here — the input element itself strips
      // them — but stored uris carrying them hit the same shared gate)
      "file:///tmp/\t../etc",
      "file:///data\tcorpus",
      // encoded separators hide the segment boundary from the display (no
      // filesystem permits "/" in a filename); %5C springs a Windows separator
      "file:///tmp/corpus%2Fprivate",
      "file:///data/a%5Cb",
      // url2pathname maps ":" → "|" and reads the letter before the first one as a
      // DRIVE, so both spellings re-root the path onto another volume outside the
      // drive position ("file:///a|/corpus" → A:\corpus, "/data/foo:bar" → O:bar).
      // WHATWG rewrites "a|" to "a:" in url.pathname, so only the raw-derived path
      // (which this gate validates) ever sees them.
      "file:///a|/corpus",
      "file:///data/a%7Cb",
      "file:///data/foo:bar",
      "file:///data:x/y",
      "file:///C:/data/foo:bar",
      // ...and the drive colon must be LITERAL: url2pathname detects the drive from
      // the still-encoded path, so "%3A" passes a decoded segment check but reads
      // "\C:\corpus" (no drive at all)
      "file:///C%3A/corpus",
      // a bare drive is DRIVE-RELATIVE ("C:" = the worker's cwd on that drive, not the
      // root) — the Windows spelling of the cwd hazard; "file:///C:/" is the root
      "file:///C:",
      // a malformed escape throws in decodeURIComponent but is LITERAL to Python's
      // unquote — the SoR refuses it too, so both gates accept exactly the same set
      "file:///data/100%",
      "file:///data/%zz",
    ]) {
      fireEvent.change(uri, { target: { value: bad } });
      expect(screen.getByText(/canonical/i)).toBeInTheDocument();
      expect(add()).toBeDisabled();
    }
    // the canonical triple-slash form is accepted — including the Windows drive form,
    // which the colon rule above must not over-block (it IS what Path.as_uri() emits
    // on a Windows worker, and url2pathname resolves it to exactly what it displays)
    // ...the drive ROOT (only the drive-relative "file:///C:" is refused), and a
    // directory legitimately named "100%" in its canonical "%25" spelling
    for (const good of [
      "file:///data/corpus/",
      "file:///C:/corpus",
      "file:///C:/",
      "file:///data/100%25",
    ]) {
      fireEvent.change(uri, { target: { value: good } });
      expect(screen.queryByText(/canonical/i)).not.toBeInTheDocument();
    }
    expect(add()).toBeEnabled();
    expect(post).not.toHaveBeenCalled();
  });

  it("renders a registered source as inert text, never a live link", async () => {
    stubSources([source({ uri: "https://evil.example/x", kind: "url" })]);
    renderImport(projectRoute("acme", "import"));

    // class-14: a source uri is arbitrary operator input — it must render as
    // text/<code>, never as an href/src that could become a live navigation
    expect(await screen.findByText("https://evil.example/x")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("triggers a build with an EMPTY body and watches the returned job", async () => {
    // the watcher fetches /jobs/{id}; the source list answers everything else
    vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      Promise.resolve(
        path === "/jobs/{job_id}"
          ? {
              data: { data: job({ status: "running", kind: "build" }), meta: META },
              error: undefined,
            }
          : { data: { data: [], meta: META }, error: undefined },
      )) as never);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([])));
    const post = stubPost({ job_id: JOB_ID, status: "queued" });
    renderImport(projectRoute("acme", "import"));

    // the run buttons fail closed until the config/source gates load
    const build = await screen.findByRole("button", { name: "開始建置" });
    await waitFor(() => expect(build).toBeEnabled());
    fireEvent.click(build);

    expect(await screen.findByText(/建置已排入佇列/)).toBeInTheDocument();
    // the accepted job feeds the shared live watcher (its status badge appears)
    expect(await screen.findByRole("status")).toHaveTextContent("running");
    // BA2e-1: the trigger body must be EMPTY — IngestRequest.source_ids and
    // BuildRequest.reason are 400-rejected by presence, so no field may ride along
    const [path, init] = post.mock.calls[0] as [string, { body?: unknown }];
    expect(path).toBe("/projects/{project}/build");
    expect(init.body).toBeUndefined();
  });

  it("surfaces a 409 when a job is already running instead of silently dropping it", async () => {
    stubSources([]);
    stubPostError("JOB_CONFLICT", "a job is already running for this project");
    renderImport(projectRoute("acme", "import"));

    const build = await screen.findByRole("button", { name: "開始建置" });
    await waitFor(() => expect(build).toBeEnabled());
    fireEvent.click(build);

    // create_job_exclusive serializes one job per project; the 409 must surface
    // (§22) rather than the trigger appearing to succeed
    expect(await screen.findByText(/建置啟動失敗:a job is already running/)).toBeInTheDocument();
  });

  it("fails closed: run buttons stay disabled until the config/source gates load", async () => {
    // unresolved query data must not read as "safe" — on a cold load the gates
    // (project config + source kinds) are unknown, and enabling the buttons for
    // that window lets an operator enqueue the very build the gate exists to block
    vi.spyOn(api, "GET").mockImplementation((() => new Promise(() => {})) as never);
    renderImport(projectRoute("acme", "import"));

    expect(screen.getByRole("button", { name: "開始建置" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "開始建置" })).toBeDisabled();
  });

  it("keeps the run gate closed through the post-add refetch window", async () => {
    // TOCTOU: after adding a text source, invalidation refetches the list but
    // react-query keeps the previous [] in data during the flight — a gate that
    // only checks presence would decide on stale data in exactly the window where
    // the just-added text source dooms the (ontology-less) build
    const sourceCalls: Array<(v: unknown) => void> = [];
    vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      path === "/projects"
        ? Promise.resolve({ data: { data: [project("acme")], meta: META }, error: undefined })
        : new Promise((res) => {
            sourceCalls.push(res);
          })) as never);
    stubPost(source({ kind: "text", uri: "file:///data/corpus/" }));
    renderImport(projectRoute("acme", "import"));

    // initial source list: empty → no text source → runnable
    await waitFor(() => expect(sourceCalls).toHaveLength(1));
    sourceCalls[0]({ data: { data: [], meta: META }, error: undefined });
    const build = await screen.findByRole("button", { name: "開始建置" });
    await waitFor(() => expect(build).toBeEnabled());

    // add a text source → the refetch is in flight → the gate must fail closed
    fireEvent.change(screen.getByLabelText("uri"), { target: { value: "file:///data/corpus/" } });
    fireEvent.click(screen.getByRole("button", { name: "登記來源" }));
    await waitFor(() => expect(sourceCalls).toHaveLength(2));
    expect(build).toBeDisabled();

    // the refetch lands with the text source → the ontology block engages
    sourceCalls[1]({
      data: { data: [source({ kind: "text", uri: "file:///data/corpus/" })], meta: META },
      error: undefined,
    });
    expect(await screen.findByText(/no valid ontology configured/i)).toBeInTheDocument();
    expect(build).toBeDisabled();
  });

  it("blocks a text build when the project has no ontology", async () => {
    stubImportGets(project("acme"), [source({ kind: "text", uri: "file:///data/corpus/" })]);
    const post = stubPost({ job_id: JOB_ID, status: "queued" });
    renderImport(projectRoute("acme", "import"));

    // create→text-source→build over a UI-created (config-less) project would fail at
    // the graph stage with OntologyRequiredError — the run must be blocked, not
    // accepted as a job guaranteed to fail after spending work
    expect(await screen.findByText(/no valid ontology configured/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "開始建置" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "開始建置" })).toBeDisabled();
    expect(post).not.toHaveBeenCalled();
  });

  it("does not block a text build once an ontology is configured", async () => {
    stubImportGets(
      {
        ...project("acme"),
        config: { ontology: { entity_types: ["Person"], relation_types: ["WORKS_AT"] } },
      },
      [source({ kind: "text", uri: "file:///data/corpus/" })],
    );
    renderImport(projectRoute("acme", "import"));

    await screen.findByText("file:///data/corpus/"); // the source list resolved
    expect(screen.queryByText(/no valid ontology configured/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "開始建置" })).toBeEnabled();
  });

  it("blocks a text build when the ontology is present but malformed", async () => {
    // presence is not validity: _load_ontology/TextOntology reject a block with
    // missing/empty relation_types (BuildConfigError before the pipeline runs), so
    // a config patched via API/CLI with a half-formed ontology must block — with
    // the present-but-invalid warning, which names the actual failure
    stubImportGets({ ...project("acme"), config: { ontology: { entity_types: ["Person"] } } }, [
      source({ kind: "text", uri: "file:///data/corpus/" }),
    ]);
    renderImport(projectRoute("acme", "import"));

    expect(await screen.findByText(/present but invalid/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "開始建置" })).toBeDisabled();
  });

  it("blocks ALL runs when the ontology is present but invalid — even structured-only", async () => {
    // "ontology" being present in config is enough to enter _load_ontology's
    // validation branch (even `ontology: null`), which raises in the worker
    // preflight regardless of source kinds — unlike an ABSENT ontology, which is
    // fine for structured-only builds
    stubImportGets({ ...project("acme"), config: { ontology: null } }, [
      source({
        kind: "structured",
        uri: "file:///data/rows.csv",
        metadata: { table: "documents", pk_column: "id" },
      }),
    ]);
    renderImport(projectRoute("acme", "import"));

    expect(await screen.findByText(/present but invalid/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "開始建置" })).toBeDisabled();
  });

  it("does not block a structured-only build without an ontology", async () => {
    // structured builds have no text docs, so the graph stage never needs an ontology
    stubImportGets(project("acme"), [
      source({
        kind: "structured",
        uri: "file:///data/rows.csv",
        metadata: { table: "documents", pk_column: "id" },
      }),
    ]);
    renderImport(projectRoute("acme", "import"));

    await screen.findByText("file:///data/rows.csv");
    expect(screen.queryByText(/no valid ontology configured/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "開始建置" })).toBeEnabled();
  });

  it("blocks runs when an existing source is one the pipeline can't resolve", async () => {
    // sources registered outside this form (CLI/API) can carry an unwired kind, a
    // non-file scheme, or missing structured metadata — resolve_source raises on
    // any of them, so ONE such source fails every build at ingest, regardless of
    // ontology; the run gate must check the whole list
    stubImportGets(
      { ...project("acme"), config: { ontology: { entity_types: ["P"], relation_types: ["R"] } } },
      [source({ kind: "url", uri: "https://example.com/feed" })],
    );
    renderImport(projectRoute("acme", "import"));

    expect(await screen.findByText(/can't be resolved by the pipeline/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "開始建置" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "開始建置" })).toBeDisabled();
  });

  it("does not block runs on a DISABLED unresolvable source (SRC2 recovery)", async () => {
    // SRC2: soft-disable exists to recover from exactly this — a broken source
    // (unwired kind / bad uri) registered via CLI/API. The build skips it
    // (_load_sources enabled_only=True), so once disabled it must stop arming
    // the unresolvable gate; otherwise disabling it can never unblock the build.
    stubImportGets(
      { ...project("acme"), config: { ontology: { entity_types: ["P"], relation_types: ["R"] } } },
      [source({ kind: "url", uri: "https://example.com/feed", enabled: false })],
    );
    renderImport(projectRoute("acme", "import"));

    await screen.findByText("https://example.com/feed"); // the row still lists (re-enable path)
    expect(screen.queryByText(/can't be resolved by the pipeline/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "開始建置" })).toBeEnabled();
  });

  it("does not arm the ontology block for a DISABLED text source (SRC2)", async () => {
    // a disabled text/xlsx source is not ingested, so it must not require an
    // ontology — the gate mirrors the build's enabled_only view, not the raw list
    stubImportGets(project("acme"), [
      source({ kind: "text", uri: "file:///data/corpus/", enabled: false }),
    ]);
    renderImport(projectRoute("acme", "import"));

    await screen.findByText("file:///data/corpus/");
    expect(screen.queryByText(/no valid ontology configured/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "開始建置" })).toBeEnabled();
  });

  it("blocks runs when an existing file uri would be silently reinterpreted", async () => {
    // a host-bearing (or query/hash-bearing) file uri doesn't raise — _local_path
    // reads only urlparse(uri).path, so the build would silently ingest /corpus
    // instead of the registered NAS target: wrong data, worse than a loud failure
    stubImportGets(
      { ...project("acme"), config: { ontology: { entity_types: ["P"], relation_types: ["R"] } } },
      [source({ kind: "text", uri: "file://nas/corpus" })],
    );
    renderImport(projectRoute("acme", "import"));

    expect(await screen.findByText(/can't be resolved by the pipeline/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "開始建置" })).toBeDisabled();
  });

  it("blocks runs when a stored uri carries edge whitespace", async () => {
    // the worker reads the stored uri verbatim — Python's urlparse KEEPS a
    // trailing space in the path (verified live), while new URL()/trim() strip
    // it, so a trimmed check would pass a source whose build reads a different
    // path than displayed; stored uris must be validated exactly as stored
    stubImportGets(
      { ...project("acme"), config: { ontology: { entity_types: ["P"], relation_types: ["R"] } } },
      [source({ kind: "text", uri: "file:///data/corpus " })],
    );
    renderImport(projectRoute("acme", "import"));

    expect(await screen.findByText(/can't be resolved by the pipeline/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "開始建置" })).toBeDisabled();
  });

  it("retries a trigger with the SAME idempotency key, then mints a new one after success", async () => {
    // create_job_exclusive only dedups while the first job is non-terminal, so a
    // retry after a lost 202 must replay the stored response (same key → original
    // job id) instead of double-running the full pipeline; a trigger that
    // SUCCEEDED clears the key so the next click is a deliberate new run
    stubSources([]);
    const post = vi
      .spyOn(api, "POST")
      .mockResolvedValueOnce({
        data: undefined,
        error: { error: { code: "STORE_UNAVAILABLE", message: "down", details: null } },
      } as never)
      .mockResolvedValue({
        data: { data: { job_id: JOB_ID, status: "queued" }, meta: META },
        error: undefined,
      } as never);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([])));
    renderImport(projectRoute("acme", "import"));

    const build = await screen.findByRole("button", { name: "開始建置" });
    await waitFor(() => expect(build).toBeEnabled());
    fireEvent.click(build);
    await screen.findByText(/建置啟動失敗/);
    fireEvent.click(build);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    fireEvent.click(build); // after success: a deliberate new run
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));

    type Call = [string, { params: { header: { "Idempotency-Key": string } } }];
    const keys = post.mock.calls.map((c) => (c as Call)[1].params.header["Idempotency-Key"]);
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]); // retry replays the same attempt
    expect(keys[2]).not.toBe(keys[0]); // post-success click is a new attempt
  });

  it("keeps the run gate closed after a failed sources refetch", async () => {
    // if the post-add invalidation refetch ERRORS, react-query keeps the previous
    // list in data and isFetching drops back to false — but the POST already
    // committed server-side, so reopening the gate on the stale snapshot enables
    // exactly the doomed build the gate exists to block; an errored source list
    // is not a decidable gate
    const errorEnvelope = {
      data: undefined,
      error: { error: { code: "STORE_UNAVAILABLE", message: "down", details: null } },
    };
    let sourcesCalls = 0;
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path === "/projects")
        return Promise.resolve({ data: { data: [project("acme")], meta: META }, error: undefined });
      sourcesCalls += 1;
      return sourcesCalls === 1
        ? Promise.resolve({ data: { data: [], meta: META }, error: undefined })
        : Promise.resolve(errorEnvelope);
    }) as never);
    stubPost(source({ kind: "text", uri: "file:///data/corpus/" }));
    renderImport(projectRoute("acme", "import"));

    const build = await screen.findByRole("button", { name: "開始建置" });
    await waitFor(() => expect(build).toBeEnabled());

    fireEvent.change(screen.getByLabelText("uri"), { target: { value: "file:///data/corpus/" } });
    fireEvent.click(screen.getByRole("button", { name: "登記來源" }));

    // the refetch failed loudly (the sources section shows it) — the gate stays shut
    expect(await screen.findByText(/could not load sources/i)).toBeInTheDocument();
    expect(build).toBeDisabled();
  });

  it("keeps the run gate closed after a failed project-config refetch", async () => {
    // same class on the config side: a refocus refetch that errors retains the
    // stale config with isFetching false — the gate must not decide on it
    const errorEnvelope = {
      data: undefined,
      error: { error: { code: "STORE_UNAVAILABLE", message: "down", details: null } },
    };
    let projectsCalls = 0;
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path === "/projects") {
        projectsCalls += 1;
        return projectsCalls === 1
          ? Promise.resolve({ data: { data: [project("acme")], meta: META }, error: undefined })
          : Promise.resolve(errorEnvelope);
      }
      return Promise.resolve({ data: { data: [], meta: META }, error: undefined });
    }) as never);
    renderImport(projectRoute("acme", "import"));
    const build = await screen.findByRole("button", { name: "開始建置" });
    await waitFor(() => expect(build).toBeEnabled());

    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    await waitFor(() => expect(projectsCalls).toBeGreaterThan(1));
    await waitFor(() => expect(build).toBeDisabled());
  });

  it("keeps the run gate closed while the project config refetches", async () => {
    // a CLI-side config.ontology change refetches the projects list on refocus;
    // react-query keeps the previous config in data during the flight, so a gate
    // keyed on data-presence would decide on the stale snapshot — it must close
    // until the refetch settles (same TOCTOU as the sources gate)
    let projectsCalls = 0;
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path === "/projects") {
        projectsCalls += 1;
        return projectsCalls === 1
          ? Promise.resolve({ data: { data: [project("acme")], meta: META }, error: undefined })
          : new Promise(() => {});
      }
      return Promise.resolve({ data: { data: [], meta: META }, error: undefined });
    }) as never);
    renderImport(projectRoute("acme", "import"));
    const build = await screen.findByRole("button", { name: "開始建置" });
    await waitFor(() => expect(build).toBeEnabled());

    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    await waitFor(() => expect(projectsCalls).toBeGreaterThan(1));
    expect(build).toBeDisabled();
  });

  it("blocks runs when a structured source lacks its table/pk_column metadata", async () => {
    // _required_meta raises for a structured source without non-empty table +
    // pk_column, so it is unresolvable even though its kind and scheme are wired
    stubImportGets(
      { ...project("acme"), config: { ontology: { entity_types: ["P"], relation_types: ["R"] } } },
      [source({ kind: "structured", uri: "file:///data/rows.csv", metadata: {} })],
    );
    renderImport(projectRoute("acme", "import"));

    expect(await screen.findByText(/can't be resolved by the pipeline/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "開始建置" })).toBeDisabled();
  });

  it("reports an un-addressable project without firing a project-scoped call", async () => {
    const get = vi
      .spyOn(api, "GET")
      .mockResolvedValue({ data: { data: [], meta: META }, error: undefined } as never);
    renderImport(projectRoute("a/b", "import"));

    // a "/"-bearing key can't ride the single {project} path segment, so the page
    // must refuse rather than fire a project-scoped call that would 404 on the wrong
    // endpoint. The non-scoped projects list may still load (it carries no key).
    expect(await screen.findByText(/isn't addressable over the api/i)).toBeInTheDocument();
    expect(get).not.toHaveBeenCalledWith("/projects/{project}/sources", expect.anything());
  });
});

// ---- UXC2b 上傳檔案 -----------------------------------------------------------

function acceptedRow(name: string) {
  return {
    filename: "deadbeefcafe0000.txt",
    original_filename: name,
    status: "accepted",
    document_uri: "file:///C:/data/uploads/acme/deadbeefcafe0000.txt",
    metadata: {
      schema_version: "1.2",
      system: { connector: "upload", original_filename: name },
      context: {},
      governance: {},
    },
  };
}

function rejectedRow(name: string, reason: string) {
  return { original_filename: name, status: "rejected", reason };
}

function stubUpload(files: unknown[]) {
  return vi.spyOn(api, "POST").mockResolvedValue({
    data: {
      data: { source_id: "50000000-0000-0000-0000-000000000000", files },
      meta: META,
    },
    error: undefined,
  } as never);
}

function pickFiles(files: File[]) {
  const input = screen.getByLabelText("選擇檔案") as HTMLInputElement;
  fireEvent.change(input, { target: { files } });
}

// the submit gate fails closed until the projects read settles (configLoaded)
// — clicking a disabled button is a silent no-op, so wait for it to open
async function clickUpload() {
  const btn = screen.getByRole("button", { name: "上傳" });
  await waitFor(() => expect(btn).toBeEnabled());
  fireEvent.click(btn);
}

describe("Import 上傳 (UXC2b)", () => {
  it("uploads the batch as real FormData with a per-attempt Idempotency-Key reused on retry", async () => {
    // the wire: files ride a FormData under repeated "files" parts (the
    // compiled type says string[] — the runtime shape is the contract's);
    // the key is minted per SELECTION and reused across retries of the same
    // batch, so a lost 201 replays the stored manifest instead of writing the
    // corpus twice
    stubSources([]);
    const post = vi
      .spyOn(api, "POST")
      .mockResolvedValueOnce({
        data: undefined,
        error: { error: { code: "STORE_UNAVAILABLE", message: "down", details: null } },
      } as never)
      .mockResolvedValue({
        data: {
          data: { source_id: null, files: [acceptedRow("a.txt"), acceptedRow("b.md")] },
          meta: META,
        },
        error: undefined,
      } as never);
    renderImport(projectRoute("acme", "import"));
    await screen.findByText("上傳檔案");

    pickFiles([
      new File(["hello"], "a.txt", { type: "text/plain" }),
      new File(["world"], "b.md", { type: "text/markdown" }),
    ]);
    await clickUpload();
    await screen.findByText(/上傳失敗:down/);
    await clickUpload();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));

    type Call = [string, { params: { header: { "Idempotency-Key": string } }; body: unknown }];
    const [path, first] = post.mock.calls[0] as Call;
    const [, second] = post.mock.calls[1] as Call;
    expect(path).toBe("/projects/{project}/uploads");
    expect(first.body).toBeInstanceOf(FormData);
    expect((first.body as FormData).getAll("files").map((f) => (f as File).name)).toEqual([
      "a.txt",
      "b.md",
    ]);
    expect(first.params.header["Idempotency-Key"]).toMatch(/[0-9a-f-]{36}/);
    expect(second.params.header["Idempotency-Key"]).toBe(first.params.header["Idempotency-Key"]);
    // no declared metadata schema → no metadata part rides along
    expect((first.body as FormData).get("metadata")).toBeNull();
  });

  it("collects the project's REQUIRED context attributes per file and sends them typed", async () => {
    // a project whose metadata_schema declares required attributes would
    // otherwise reject EVERY upload (the endpoint validates even an absent
    // context against the schema) with no UI path to supply the values — the
    // per-file inputs close that dead end (Codex #83). Only required fields
    // are offered; optional ones stay API/CLI territory.
    const proj = {
      ...project("acme"),
      config: {
        metadata_schema: {
          attributes: {
            location: { type: "string", required: true },
            floor: { type: "number", required: true },
            featured: { type: "boolean", required: true },
            note: { type: "string", required: false },
          },
        },
      },
    };
    vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      Promise.resolve(
        path === "/projects"
          ? { data: { data: [proj], meta: META }, error: undefined }
          : { data: { data: [], meta: META }, error: undefined },
      )) as never);
    const post = stubUpload([acceptedRow("a.txt")]);
    renderImport(projectRoute("acme", "import"));
    await screen.findByText("上傳檔案");

    pickFiles([new File(["x"], "a.txt")]);
    // required fields render per file; the optional one is not offered
    expect(await screen.findByText(/這個專案要求每個檔案填寫下列欄位/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/note/)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/location/), { target: { value: "深海探索廳" } });
    fireEvent.change(screen.getByLabelText(/floor/), { target: { value: "3" } });
    fireEvent.click(screen.getByLabelText("featured"));
    await clickUpload();

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    const body = (post.mock.calls[0][1] as { body: FormData }).body;
    expect(JSON.parse(body.get("metadata") as string)).toEqual({
      "a.txt": {
        context: { attributes: { location: "深海探索廳", floor: 3, featured: true } },
      },
    });
  });

  it("rotates the idempotency key when metadata is edited between attempts", async () => {
    // the server folds the metadata content into the idempotency fingerprint:
    // a lost-response retry of an EDITED batch under the old key would 409
    // IDEMPOTENCY_CONFLICT instead of submitting the correction — an edit
    // mints a fresh key (the source form's discipline), while an unchanged
    // retry keeps it (pinned by the FormData test above)
    const proj = {
      ...project("acme"),
      config: {
        metadata_schema: {
          attributes: { location: { type: "string", required: true } },
        },
      },
    };
    vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      Promise.resolve(
        path === "/projects"
          ? { data: { data: [proj], meta: META }, error: undefined }
          : { data: { data: [], meta: META }, error: undefined },
      )) as never);
    const post = vi
      .spyOn(api, "POST")
      .mockResolvedValueOnce({
        data: undefined,
        error: { error: { code: "STORE_UNAVAILABLE", message: "down", details: null } },
      } as never)
      .mockResolvedValue({
        data: { data: { source_id: null, files: [acceptedRow("a.txt")] }, meta: META },
        error: undefined,
      } as never);
    renderImport(projectRoute("acme", "import"));
    await screen.findByText("上傳檔案");

    pickFiles([new File(["x"], "a.txt")]);
    expect(await screen.findByText(/這個專案要求每個檔案填寫下列欄位/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/location/), { target: { value: "打錯的展廳" } });
    await clickUpload();
    await screen.findByText(/上傳失敗:down/);

    // correct the value, retry — the request content changed, so must the key
    fireEvent.change(screen.getByLabelText(/location/), { target: { value: "深海探索廳" } });
    await clickUpload();

    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    type Call = [string, { params: { header: { "Idempotency-Key": string } } }];
    const [, first] = post.mock.calls[0] as Call;
    const [, second] = post.mock.calls[1] as Call;
    expect(second.params.header["Idempotency-Key"]).not.toBe(
      first.params.header["Idempotency-Key"],
    );
  });

  it("omits a BLANK required field so the server's own refusal stays the verdict", async () => {
    // an empty string is server-legal for a required string (presence is the
    // rule), so a client non-blank gate would over-block; omitting the blank
    // key instead lets the server refuse that file with its own reason —
    // honest, and no checker fork
    const proj = {
      ...project("acme"),
      config: {
        metadata_schema: {
          attributes: {
            location: { type: "string", required: true },
            floor: { type: "number", required: true },
          },
        },
      },
    };
    vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      Promise.resolve(
        path === "/projects"
          ? { data: { data: [proj], meta: META }, error: undefined }
          : { data: { data: [], meta: META }, error: undefined },
      )) as never);
    const post = stubUpload([
      rejectedRow("a.txt", "required attribute 'location' is missing from context.attributes"),
    ]);
    renderImport(projectRoute("acme", "import"));
    await screen.findByText("上傳檔案");

    pickFiles([new File(["x"], "a.txt")]);
    expect(await screen.findByText(/這個專案要求每個檔案填寫下列欄位/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/floor/), { target: { value: "2" } });
    await clickUpload();

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    const body = (post.mock.calls[0][1] as { body: FormData }).body;
    expect(JSON.parse(body.get("metadata") as string)).toEqual({
      "a.txt": { context: { attributes: { floor: 2 } } },
    });
    // and the server's per-file refusal renders verbatim
    expect(await screen.findByText(/required attribute 'location' is missing/)).toBeInTheDocument();
  });

  it("renders the per-file manifest honestly: verdict words, verbatim reason, no bare stored ids", async () => {
    // a refused extension is a STATED refusal row beside the accepted ones —
    // never a silent drop; the stored corpus name/uri are identifiers and live
    // on hover only (chrome shows the operator's own filename)
    stubSources([]);
    stubUpload([
      acceptedRow("guide.txt"),
      rejectedRow("virus.exe", "extension '.exe' is not allowlisted (txt, md)"),
    ]);
    renderImport(projectRoute("acme", "import"));
    await screen.findByText("上傳檔案");

    pickFiles([new File(["x"], "guide.txt"), new File(["y"], "virus.exe")]);
    await clickUpload();

    expect(await screen.findByText(/接受 1 檔 · 退回 1 檔/)).toBeInTheDocument();
    expect(screen.getByText("已接受")).toBeInTheDocument();
    expect(screen.getByText("已退回")).toBeInTheDocument();
    expect(screen.getByText("guide.txt")).toBeInTheDocument();
    expect(screen.getByText("virus.exe")).toBeInTheDocument();
    expect(screen.getByText(/extension '\.exe' is not allowlisted/)).toBeInTheDocument();
    // stored identifiers never appear as text — hover title only
    expect(screen.queryByText(/deadbeefcafe0000/)).not.toBeInTheDocument();
    expect(screen.getByText("guide.txt")).toHaveAttribute(
      "title",
      expect.stringContaining("file:///"),
    );
  });

  it("refreshes the sources list from the WORLD the upload actually changed", async () => {
    // the accepted files register the project's managed corpus source — it
    // must appear in the list above without a manual refresh. The stub models
    // WORLD STATE (the source exists only after the POST landed), never a
    // call count — a premature refetch must not be handed the "after" world
    // (class 26).
    let uploaded = false;
    vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      Promise.resolve(
        path === "/projects"
          ? { data: { data: [project("acme")], meta: META }, error: undefined }
          : {
              data: {
                data: uploaded
                  ? [source({ uri: "file:///C:/data/uploads/acme", kind: "text" })]
                  : [],
                meta: META,
              },
              error: undefined,
            },
      )) as never);
    vi.spyOn(api, "POST").mockImplementation((() => {
      uploaded = true;
      return Promise.resolve({
        data: {
          data: {
            source_id: "50000000-0000-0000-0000-000000000000",
            files: [acceptedRow("a.txt")],
          },
          meta: META,
        },
        error: undefined,
      });
    }) as never);
    renderImport(projectRoute("acme", "import"));
    expect(await screen.findByText("No sources registered yet.")).toBeInTheDocument();

    pickFiles([new File(["x"], "a.txt")]);
    await clickUpload();

    expect(await screen.findByText("file:///C:/data/uploads/acme")).toBeInTheDocument();
  });

  it("gates the upload on a selection and accepts a drag-drop batch", async () => {
    stubSources([]);
    stubUpload([acceptedRow("dropped.txt")]);
    const { container } = renderImport(projectRoute("acme", "import"));
    await screen.findByText("上傳檔案");

    const button = screen.getByRole("button", { name: "上傳" });
    expect(button).toBeDisabled();

    const dropzone = container.querySelector(".import__dropzone") as HTMLElement;
    fireEvent.drop(dropzone, {
      dataTransfer: { files: [new File(["x"], "dropped.txt")] },
    });
    expect(screen.getByText("dropped.txt")).toBeInTheDocument();
    // opens once BOTH gates satisfy: files picked AND the config read settled
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("a new selection clears the previous batch's verdicts", async () => {
    // verdicts belong to the batch that produced them: a stale manifest
    // sitting beside a NEW selection would read as that selection's result
    stubSources([]);
    stubUpload([acceptedRow("first.txt")]);
    renderImport(projectRoute("acme", "import"));
    await screen.findByText("上傳檔案");

    pickFiles([new File(["x"], "first.txt")]);
    await clickUpload();
    expect(await screen.findByText(/接受 1 檔/)).toBeInTheDocument();

    pickFiles([new File(["y"], "second.txt")]);
    expect(screen.queryByText(/接受 1 檔/)).not.toBeInTheDocument();
    expect(screen.queryByText("已接受")).not.toBeInTheDocument();
  });

  it("stays locked while the project config is UNKNOWN — loading or failed (fail closed)", async () => {
    // requiredAttrs=[] during a pending/failed projects read means "unknown",
    // not "none": submitting then would skip the metadata form and recreate
    // the configured-project dead end for the window (Codex #83 triage 2) —
    // the same fail-closed predicate RunPipeline gates on, stated honestly
    // rather than a silent disabled button
    const post = stubUpload([acceptedRow("a.txt")]);
    // the projects read NEVER settles; sources settle normally
    vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      path === "/projects"
        ? new Promise(() => {})
        : Promise.resolve({ data: { data: [], meta: META }, error: undefined })) as never);
    renderImport(projectRoute("acme", "import"));
    await screen.findByText("上傳檔案");

    pickFiles([new File(["x"], "a.txt")]);
    expect(screen.getByRole("button", { name: "上傳" })).toBeDisabled();
    expect(screen.getByText(/正在確認專案設定/)).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("stays locked when the projects read FAILED (unknown ≠ none)", async () => {
    stubUpload([acceptedRow("a.txt")]);
    vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      path === "/projects"
        ? Promise.resolve({
            data: undefined,
            error: {
              error: {
                code: "STORE_UNAVAILABLE",
                message: "down",
                details: null,
                request_id: META.request_id,
              },
            },
          })
        : Promise.resolve({ data: { data: [], meta: META }, error: undefined })) as never);
    renderImport(projectRoute("acme", "import"));
    await screen.findByText("上傳檔案");

    pickFiles([new File(["x"], "a.txt")]);
    await waitFor(() => expect(screen.getByText(/正在確認專案設定/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "上傳" })).toBeDisabled();
  });

  it("surfaces a whole-request refusal verbatim (413/415/400 family)", async () => {
    stubSources([]);
    stubPostError("VALIDATION_ERROR", "upload exceeds the total size limit (50000000 bytes)");
    renderImport(projectRoute("acme", "import"));
    await screen.findByText("上傳檔案");

    pickFiles([new File(["x"], "huge.txt")]);
    await clickUpload();

    expect(
      await screen.findByText(/上傳失敗:upload exceeds the total size limit/),
    ).toBeInTheDocument();
  });
});

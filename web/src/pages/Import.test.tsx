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
    fireEvent.click(screen.getByRole("button", { name: /add source/i }));
    await screen.findByText(/add failed/i);
    fireEvent.click(screen.getByRole("button", { name: /add source/i }));
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
    expect(screen.getByRole("button", { name: /add source/i })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("table"), { target: { value: "documents" } });
    fireEvent.change(screen.getByLabelText("pk_column"), { target: { value: "id" } });
    fireEvent.click(screen.getByRole("button", { name: /add source/i }));

    await waitFor(() => expect(post).toHaveBeenCalled());
    const [, init] = post.mock.calls[0] as [string, { body: unknown }];
    expect(init.body).toEqual({
      uri: "file:///data/rows.csv",
      kind: "structured",
      metadata: { table: "documents", pk_column: "id" },
    });
  });

  it("blocks a uri the backend would misread, before POSTing", async () => {
    stubSources([]);
    const post = stubPost(source());
    renderImport(projectRoute("acme", "import"));

    const uri = screen.getByLabelText("uri");
    const add = () => screen.getByRole("button", { name: /add source/i });
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
    ]) {
      fireEvent.change(uri, { target: { value: bad } });
      expect(screen.getByText(/canonical/i)).toBeInTheDocument();
      expect(add()).toBeDisabled();
    }
    // the canonical triple-slash form is accepted
    fireEvent.change(uri, { target: { value: "file:///data/corpus/" } });
    expect(screen.queryByText(/canonical/i)).not.toBeInTheDocument();
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
    const build = await screen.findByRole("button", { name: /^build$/i });
    await waitFor(() => expect(build).toBeEnabled());
    fireEvent.click(build);

    expect(await screen.findByText(/accepted job/i)).toBeInTheDocument();
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

    const build = await screen.findByRole("button", { name: /^build$/i });
    await waitFor(() => expect(build).toBeEnabled());
    fireEvent.click(build);

    // create_job_exclusive serializes one job per project; the 409 must surface
    // (§22) rather than the trigger appearing to succeed
    expect(
      await screen.findByText(/trigger failed: a job is already running/i),
    ).toBeInTheDocument();
  });

  it("fails closed: run buttons stay disabled until the config/source gates load", async () => {
    // unresolved query data must not read as "safe" — on a cold load the gates
    // (project config + source kinds) are unknown, and enabling the buttons for
    // that window lets an operator enqueue the very build the gate exists to block
    vi.spyOn(api, "GET").mockImplementation((() => new Promise(() => {})) as never);
    renderImport(projectRoute("acme", "import"));

    expect(screen.getByRole("button", { name: /^build$/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^ingest$/i })).toBeDisabled();
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
    const build = await screen.findByRole("button", { name: /^build$/i });
    await waitFor(() => expect(build).toBeEnabled());

    // add a text source → the refetch is in flight → the gate must fail closed
    fireEvent.change(screen.getByLabelText("uri"), { target: { value: "file:///data/corpus/" } });
    fireEvent.click(screen.getByRole("button", { name: /add source/i }));
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
    expect(screen.getByRole("button", { name: /^build$/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^ingest$/i })).toBeDisabled();
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
    expect(screen.getByRole("button", { name: /^build$/i })).toBeEnabled();
  });

  it("blocks a text build when the ontology is present but malformed", async () => {
    // presence is not validity: _load_ontology/TextOntology reject a block with
    // missing/empty relation_types (BuildConfigError before the pipeline runs), so
    // a config patched via API/CLI with a half-formed ontology must gate exactly
    // like a missing one
    stubImportGets({ ...project("acme"), config: { ontology: { entity_types: ["Person"] } } }, [
      source({ kind: "text", uri: "file:///data/corpus/" }),
    ]);
    renderImport(projectRoute("acme", "import"));

    expect(await screen.findByText(/no valid ontology configured/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^build$/i })).toBeDisabled();
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
    expect(screen.getByRole("button", { name: /^build$/i })).toBeEnabled();
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
    expect(screen.getByRole("button", { name: /^build$/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^ingest$/i })).toBeDisabled();
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
    expect(screen.getByRole("button", { name: /^build$/i })).toBeDisabled();
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
    expect(screen.getByRole("button", { name: /^build$/i })).toBeDisabled();
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

    const build = await screen.findByRole("button", { name: /^build$/i });
    await waitFor(() => expect(build).toBeEnabled());
    fireEvent.click(build);
    await screen.findByText(/trigger failed/i);
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

    const build = await screen.findByRole("button", { name: /^build$/i });
    await waitFor(() => expect(build).toBeEnabled());

    fireEvent.change(screen.getByLabelText("uri"), { target: { value: "file:///data/corpus/" } });
    fireEvent.click(screen.getByRole("button", { name: /add source/i }));

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
    const build = await screen.findByRole("button", { name: /^build$/i });
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
    const build = await screen.findByRole("button", { name: /^build$/i });
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
    expect(screen.getByRole("button", { name: /^build$/i })).toBeDisabled();
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

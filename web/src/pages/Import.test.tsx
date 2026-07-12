import { fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Import } from "./Import";
import { api } from "../api/client";
import {
  job,
  projectRoute,
  renderWithProviders,
  source,
  sseResponse,
  stubPost,
  stubPostError,
  stubSources,
} from "../test-utils";

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

describe("Import", () => {
  it("registers a text source with NO idempotency key and clears the form on success", async () => {
    stubSources([]);
    const post = stubPost(source({ uri: "file:///data/corpus.txt", kind: "text" }));
    renderImport(projectRoute("acme", "import"));

    const uri = screen.getByLabelText("uri");
    fireEvent.change(uri, { target: { value: "file:///data/corpus.txt" } });
    fireEvent.click(screen.getByRole("button", { name: /add source/i }));

    await waitFor(() => expect(post).toHaveBeenCalled());
    const [path, init] = post.mock.calls[0] as [
      string,
      { params: { header?: unknown }; body: unknown },
    ];
    // uri is not unique server-side (each add mints a fresh id, duplicate uris are
    // permitted), so there is no natural key — a uri-derived one would wrongly
    // suppress an intentional re-registration, so the add sends no Idempotency-Key.
    // kind defaults to text (the wired kind), sent so the build doesn't fail on a
    // missing/unsupported kind.
    expect(path).toBe("/projects/{project}/sources");
    expect(init.body).toEqual({ uri: "file:///data/corpus.txt", kind: "text" });
    expect(init.params.header).toBeUndefined();
    // the form clears so the next source starts fresh
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

    fireEvent.click(await screen.findByRole("button", { name: /^build$/i }));

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

    fireEvent.click(await screen.findByRole("button", { name: /^build$/i }));

    // create_job_exclusive serializes one job per project; the 409 must surface
    // (§22) rather than the trigger appearing to succeed
    expect(
      await screen.findByText(/trigger failed: a job is already running/i),
    ).toBeInTheDocument();
  });

  it("reports an un-addressable project instead of calling the API", async () => {
    const get = vi.spyOn(api, "GET");
    renderImport(projectRoute("a/b", "import"));

    // a "/"-bearing key can't ride the single {project} path segment, so the page
    // must refuse rather than fire a call that would 404 on the wrong endpoint
    expect(await screen.findByText(/isn't addressable over the api/i)).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });
});

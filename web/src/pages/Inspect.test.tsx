import { focusManager } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Inspect } from "./Inspect";
import { api } from "../api/client";
import { projectRoute, renderWithProviders } from "../test-utils";

type Meta = {
  request_id: string;
  build_id: string | null;
  elapsed_ms: number;
  schema_version: string;
  next_cursor?: string | null;
};

const META: Meta = {
  request_id: "00000000-0000-0000-0000-000000000000",
  build_id: "b1",
  elapsed_ms: 1,
  schema_version: "0.5",
};

function ok(rows: unknown[], meta: Partial<Meta> = {}) {
  return { data: { data: rows, meta: { ...META, ...meta } }, error: undefined };
}

function fail(status: number, code: string, message: string) {
  return { data: undefined, error: { error: { code, message } }, response: { status } };
}

// The GET spy is typed `never` (the stubs bypass openapi-fetch's overloads), so read the
// recorded request params through this shape rather than the mock's own types.
function requestQuery(call: unknown): Record<string, unknown> {
  const opts = (call as [unknown, { params?: { query?: Record<string, unknown> } }])[1];
  return opts?.params?.query ?? {};
}

function doc(overrides: Record<string, unknown> = {}) {
  return {
    id: "d1",
    build_id: "b1",
    source_uri: "file:///data/corpus/a.txt",
    mime: "text/plain",
    status: "ingested",
    ingested_at: "2026-07-13T04:00:00Z",
    metadata: {},
    ...overrides,
  };
}

function chunk(overrides: Record<string, unknown> = {}) {
  return {
    id: "c1",
    document_id: "d1",
    build_id: "b1",
    ordinal: 0,
    text: "Ada Lovelace worked with Charles Babbage.",
    token_count: 9,
    ...overrides,
  };
}

function renderInspect() {
  return renderWithProviders(
    <Routes>
      <Route path="/p/:project/inspect" element={<Inspect />} />
    </Routes>,
    { route: projectRoute("acme", "inspect") },
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Inspect", () => {
  it("never sends sort or filter — on EVERY list request, from every tab", async () => {
    // WHY at the request level, and WHY across all tabs: reject_unsupported_query 400s any
    // filter[...] and any non-default sort — and for CHUNKS it passes sort_field=None, which
    // rejects EVERY explicit sort (the default order is the compound document_id, ordinal).
    // Verified live against the API: GET /chunks?sort=id:desc → HTTP 400.
    //
    // The invariant lives in each fetcher's own query object, not in one shared place, so
    // asserting it for documents alone would stay green if someone added a sort to the chunks
    // fetcher — the exact false-green this project keeps paying for. Assert over every call.
    const get = vi
      .spyOn(api, "GET")
      .mockImplementation(((path: string) =>
        Promise.resolve(path.endsWith("/chunks") ? ok([chunk()]) : ok([doc()]))) as never);
    renderInspect();

    expect(await screen.findByText("file:///data/corpus/a.txt")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Chunks" }));
    expect(await screen.findByText(/Ada Lovelace worked with Charles Babbage/)).toBeInTheDocument();

    const listCalls = get.mock.calls.filter(
      (call) => !String((call as unknown[])[0]).includes("_id}"),
    );
    expect(listCalls.length).toBeGreaterThanOrEqual(2); // both tabs actually fetched
    for (const call of listCalls) {
      const query = requestQuery(call);
      expect(query).not.toHaveProperty("sort");
      expect(Object.keys(query).some((k) => k.startsWith("filter"))).toBe(false);
    }
  });

  it("fails loud when the active build changes between pages, rather than splicing them", async () => {
    // Each request re-resolves the active build, so page 2 can be served by a DIFFERENT build
    // than page 1. Appending them would show one table stitched from two corpora — wrong data,
    // which this platform treats as strictly worse than a loud failure.
    const get = vi.spyOn(api, "GET");
    get.mockResolvedValueOnce(ok([doc({ id: "d1" })], { next_cursor: "c2" }) as never);
    get.mockResolvedValueOnce(ok([doc({ id: "d2" })], { build_id: "b2" }) as never);
    renderInspect();

    fireEvent.click(await screen.findByRole("button", { name: /load more documents/i }));

    expect(await screen.findByText(/active build changed/i)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument(); // no spliced rows survive
  });

  it("appends the next page when the build is unchanged", async () => {
    const get = vi.spyOn(api, "GET");
    get.mockResolvedValueOnce(
      ok([doc({ id: "d1", source_uri: "file:///a.txt" })], { next_cursor: "c2" }) as never,
    );
    get.mockResolvedValueOnce(ok([doc({ id: "d2", source_uri: "file:///b.txt" })]) as never);
    renderInspect();

    fireEvent.click(await screen.findByRole("button", { name: /load more documents/i }));

    expect(await screen.findByText("file:///b.txt")).toBeInTheDocument();
    expect(screen.getByText("file:///a.txt")).toBeInTheDocument();
    // page 2 asks with the OPAQUE cursor page 1's meta handed back — never an offset the
    // client invents (the cursor is a keyset token bound to the server's own order)
    expect(requestQuery(get.mock.calls[1]).cursor).toBe("c2");
  });

  it("keeps the loaded rows when only the NEXT page fails", async () => {
    // A failed "load more" sets isError while data still holds the pages that DID load.
    // Discarding a good single-build table over one bad page would be a worse failure than
    // the one being reported — say it, keep the rows.
    const get = vi.spyOn(api, "GET");
    get.mockResolvedValueOnce(ok([doc()], { next_cursor: "c2" }) as never);
    get.mockRejectedValueOnce(new Error("network down"));
    renderInspect();

    fireEvent.click(await screen.findByRole("button", { name: /load more documents/i }));

    expect(await screen.findByText(/could not load more documents/i)).toBeInTheDocument();
    expect(screen.getByText("file:///data/corpus/a.txt")).toBeInTheDocument();
  });

  it("hides the stale table when a REFETCH fails, instead of calling it a load-more failure", async () => {
    // The two cached-data errors are NOT alike. A failed refetch (focus/reconnect, or the
    // active build being removed → 409) also raises isError while react-query keeps the
    // previous pages — but those rows describe a build the server will no longer serve.
    // Rendering them is showing a corpus that no longer exists: the stale-data-during-
    // refetch trap the FE1 run gates were hardened against. Only a load-more failure may
    // keep its rows; everything else fails closed.
    const get = vi.spyOn(api, "GET");
    get.mockResolvedValueOnce(ok([doc()]) as never);
    renderInspect();
    expect(await screen.findByText("file:///data/corpus/a.txt")).toBeInTheDocument();

    // the refocus refetch finds the build gone
    get.mockResolvedValue(fail(409, "NO_ACTIVE_BUILD", "no active build for project") as never);
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });

    expect(await screen.findByText(/no active build for project/i)).toBeInTheDocument();
    // the stale rows are GONE, and this was never labelled a pagination problem
    await waitFor(() =>
      expect(screen.queryByText("file:///data/corpus/a.txt")).not.toBeInTheDocument(),
    );
    expect(screen.queryByText(/could not load more/i)).not.toBeInTheDocument();
  });

  it("fetches the detail-only field a row click exists for", async () => {
    // Document.raw comes back on the detail GET only — the list omits the key entirely
    // (verified against a real build). If the panel rendered from the list row, raw would
    // always be blank.
    const get = vi.spyOn(api, "GET");
    get.mockResolvedValueOnce(ok([doc()]) as never);
    get.mockResolvedValueOnce({
      data: { data: doc({ raw: "the full document text" }), meta: META },
      error: undefined,
    } as never);
    renderInspect();

    fireEvent.click(await screen.findByRole("button", { name: "file:///data/corpus/a.txt" }));

    expect(await screen.findByText("the full document text")).toBeInTheDocument();
  });

  it("drops the stale detail fields when the detail refetch fails, rather than showing a vanished build's document under the error", async () => {
    // The same cached-data-beside-isError trap as the list, one component over — and here
    // the list CANNOT catch it: on a build swap the list refetch succeeds (one build in the
    // pages, so the splice guard passes and the new table renders) while the detail refetch
    // 404s. react-query keeps the previous document as `data`, so rendering the fields
    // beside the error would print the OLD build's id/source_uri/raw underneath a line
    // saying the row is gone from the active build. Error and fields are exclusive.
    let swapped = false;
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path.includes("_id}"))
        return Promise.resolve(
          swapped
            ? fail(404, "VALIDATION_ERROR", "not found")
            : {
                data: { data: doc({ raw: "the full document text" }), meta: META },
                error: undefined,
              },
        );
      return Promise.resolve(
        swapped
          ? ok([doc({ id: "d2", source_uri: "file:///b.txt" })], { build_id: "b2" })
          : ok([doc()]),
      );
    }) as never);
    renderInspect();

    fireEvent.click(await screen.findByRole("button", { name: "file:///data/corpus/a.txt" }));
    expect(await screen.findByText("the full document text")).toBeInTheDocument();

    swapped = true; // a build is activated that does not contain d1
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });

    expect(await screen.findByText(/not found in the active build/i)).toBeInTheDocument();
    expect(await screen.findByText("file:///b.txt")).toBeInTheDocument(); // list is fine
    // …but the panel must not still be describing the build that no longer exists
    await waitFor(() =>
      expect(screen.queryByText("the full document text")).not.toBeInTheDocument(),
    );
  });

  it("branches on the HTTP status, not the error code, to explain a missing row", async () => {
    // code_for_status maps EVERY 4xx to VALIDATION_ERROR, so a missing row (404) and a bad
    // request (400) are code-IDENTICAL. These two cases are what make the test discriminate:
    // a gate keyed on error.code would rewrite BOTH into the "gone from the build" line.
    const get = vi.spyOn(api, "GET");
    get.mockResolvedValueOnce(ok([doc()]) as never);
    get.mockResolvedValueOnce(fail(404, "VALIDATION_ERROR", "not found") as never);
    const view = renderInspect();

    fireEvent.click(await screen.findByRole("button", { name: "file:///data/corpus/a.txt" }));
    expect(await screen.findByText(/not found in the active build/i)).toBeInTheDocument();
    view.unmount();

    // same code, different status: the server's own message must survive untouched
    get.mockReset();
    get.mockResolvedValueOnce(ok([doc()]) as never);
    get.mockResolvedValueOnce(fail(400, "VALIDATION_ERROR", "limit must be <= 500") as never);
    renderInspect();

    fireEvent.click(await screen.findByRole("button", { name: "file:///data/corpus/a.txt" }));
    expect(await screen.findByText(/limit must be <= 500/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText(/not found in the active build/i)).not.toBeInTheDocument(),
    );
  });

  it("fails loud when a list cannot be loaded", async () => {
    // An empty table would read as "this build produced nothing" — the exact wrong conclusion
    // to draw from a store outage.
    vi.spyOn(api, "GET").mockResolvedValue(fail(503, "INTERNAL", "neo4j is unreachable") as never);
    renderInspect();

    expect(await screen.findByText(/neo4j is unreachable/i)).toBeInTheDocument();
  });

  it("says the build is empty rather than showing a bare table", async () => {
    // Safe to state plainly: no active build answers 409 NO_ACTIVE_BUILD (verified live), not
    // a 200 with no rows — so an empty list really is an empty build.
    vi.spyOn(api, "GET").mockResolvedValue(ok([]) as never);
    renderInspect();

    expect(await screen.findByText(/no documents in the active build/i)).toBeInTheDocument();
  });
});

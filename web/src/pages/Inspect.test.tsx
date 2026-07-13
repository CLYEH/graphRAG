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

  it.each([
    ["the active build was deactivated", 409, "NO_ACTIVE_BUILD", "no active build for project"],
    ["the project was deleted", 404, "PROJECT_NOT_FOUND", "project 'acme' not found"],
  ])(
    "drops the rows when LOAD MORE reports the scope is gone (%s), even though it is a next-page failure",
    async (_why, status, code, msg) => {
      // These are the WHOLE reject surface of the endpoint's scope resolution: inspect.py::_bind
      // fails in exactly two ways, and BOTH prove the rows on screen came from a build that no
      // longer exists. (PROJECT_NOT_FOUND implies it too: delete_project refuses while any build
      // exists, so the project being gone means its builds are gone.) Each arrives as a
      // next-page failure, so keying on isFetchNextPageError alone leaves a vanished corpus on
      // display — the same defect in two spellings, which is why the guard now allowlists the
      // failures that CANNOT invalidate the binding rather than blocklisting the ones that can.
      const get = vi.spyOn(api, "GET");
      get.mockResolvedValueOnce(ok([doc()], { next_cursor: "c2" }) as never);
      get.mockResolvedValueOnce(fail(status as number, code as string, msg as string) as never);
      renderInspect();

      fireEvent.click(await screen.findByRole("button", { name: /load more documents/i }));

      expect(await screen.findByText(new RegExp(msg as string, "i"))).toBeInTheDocument();
      await waitFor(() =>
        expect(screen.queryByText("file:///data/corpus/a.txt")).not.toBeInTheDocument(),
      );
      // and it is NOT reported as a mere pagination hiccup
      expect(screen.queryByText(/could not load more/i)).not.toBeInTheDocument();
    },
  );

  it("keeps the rows when LOAD MORE hits a scope-NEUTRAL failure the server named", async () => {
    // The acceptance side, and it must be pinned through the ApiError path — not just the
    // transport path above — or the fail-closed default could quietly widen until a store blip
    // blanks a perfectly valid table. STORE_UNAVAILABLE says the store was down, which says
    // nothing about which build is active: the rows survive, the page failure is reported.
    const get = vi.spyOn(api, "GET");
    get.mockResolvedValueOnce(ok([doc()], { next_cursor: "c2" }) as never);
    get.mockResolvedValueOnce(fail(503, "STORE_UNAVAILABLE", "qdrant is unreachable") as never);
    renderInspect();

    fireEvent.click(await screen.findByRole("button", { name: /load more documents/i }));

    expect(await screen.findByText(/could not load more documents/i)).toBeInTheDocument();
    expect(screen.getByText("file:///data/corpus/a.txt")).toBeInTheDocument();
  });

  it("drops the rows when a load-more failure has no readable code, and says so in words", async () => {
    // A body that is NOT our envelope — a proxy's HTML 502, which never reached the app. There
    // is no code to read, so nothing has told us the build survived, and the allowlist's rule is
    // that only a code we RECOGNISE earns the rows back. Two failures are pinned here at once:
    //   * the rows must go (an unreadable answer is not a scope-neutral one), and
    //   * reading the envelope unguarded would throw a TypeError — which is NOT an ApiError, so
    //     it would land in the TRANSPORT branch and silently KEEP the rows, with every other
    //     test still green. That false-green is exactly why this branch needs its own pin.
    const get = vi.spyOn(api, "GET");
    get.mockResolvedValueOnce(ok([doc()], { next_cursor: "c2" }) as never);
    get.mockResolvedValueOnce({
      data: undefined,
      error: "<html>502 Bad Gateway</html>",
      response: { status: 502 },
    } as never);
    renderInspect();

    fireEvent.click(await screen.findByRole("button", { name: /load more documents/i }));

    expect(await screen.findByText(/the request failed/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText("file:///data/corpus/a.txt")).not.toBeInTheDocument(),
    );
    // the user is told what happened — not handed a JS crash escaping our own error path
    expect(screen.queryByText(/cannot read properties/i)).not.toBeInTheDocument();
  });

  it("hides the rows WHILE a refetch is re-verifying the active build, not only after it settles", async () => {
    // The in-flight state, which the settled-state guards cannot cover: a refocus refetch
    // exists to re-ask which build is active, so until it answers, the cached rows are exactly
    // the thing being verified. react-query serves them anyway (stale-while-revalidate) with
    // isError still false — build A's rows, still clickable, after build B was activated in
    // another tab. A HUNG request makes that window unbounded, which is why this test never
    // resolves the refetch at all: the rows must leave the screen without any settle.
    const get = vi.spyOn(api, "GET");
    get.mockResolvedValueOnce(ok([doc()]) as never);
    renderInspect();
    expect(await screen.findByText("file:///data/corpus/a.txt")).toBeInTheDocument();

    get.mockImplementation((() => new Promise(() => {})) as never); // the refetch hangs
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });

    await waitFor(() =>
      expect(screen.queryByText("file:///data/corpus/a.txt")).not.toBeInTheDocument(),
    );
    expect(screen.getByText(/loading documents/i)).toBeInTheDocument();
  });

  it("keeps the verified rows on screen while the NEXT page is on the wire", async () => {
    // The over-block dual of the test above. A next-page fetch is the one fetch that does NOT
    // re-open the question for the rows already on screen — it extends the pinned build, and
    // the splice/scope guards judge its answer when it lands. If the in-flight gate were keyed
    // on isFetching alone, every "load more" would blank a table whose rows are verified.
    const get = vi.spyOn(api, "GET");
    get.mockResolvedValueOnce(ok([doc()], { next_cursor: "c2" }) as never);
    get.mockImplementationOnce((() => new Promise(() => {})) as never); // page 2 hangs
    renderInspect();

    fireEvent.click(await screen.findByRole("button", { name: /load more documents/i }));

    expect(await screen.findByRole("button", { name: /loading/i })).toBeDisabled();
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

  it("hides the stale detail fields WHILE their refetch is on the wire, not only after the 404 lands", async () => {
    // The panel's own in-flight window, and the list's gate cannot cover it: the LIST refetch
    // here settles (same build, so the table re-renders) while only the DETAIL refetch hangs —
    // if the fields rendered from the cached document during that window, the old build's raw
    // text would sit on screen until the 404 that disowns it arrives, or forever on a hung
    // request. Fields render only from a settled, successful answer.
    let hang = false;
    vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path.includes("_id}"))
        return hang
          ? new Promise(() => {})
          : Promise.resolve({
              data: { data: doc({ raw: "the full document text" }), meta: META },
              error: undefined,
            });
      return Promise.resolve(ok([doc()]));
    }) as never);
    renderInspect();

    fireEvent.click(await screen.findByRole("button", { name: "file:///data/corpus/a.txt" }));
    expect(await screen.findByText("the full document text")).toBeInTheDocument();

    hang = true; // a build swap elsewhere; the panel's refetch never comes back
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });

    await waitFor(() =>
      expect(screen.queryByText("the full document text")).not.toBeInTheDocument(),
    );
    // the LIST is untouched by the hung detail — its rows settled and render
    expect(screen.getByText("file:///data/corpus/a.txt")).toBeInTheDocument();
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

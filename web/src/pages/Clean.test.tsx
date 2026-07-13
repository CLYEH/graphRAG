import { fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Clean } from "./Clean";
import { api } from "../api/client";
import { projectRoute, renderWithProviders } from "../test-utils";

const META = {
  request_id: "00000000-0000-0000-0000-000000000000",
  build_id: null as string | null,
  elapsed_ms: 1,
};

function projectBody(config: Record<string, unknown> = {}) {
  return {
    data: {
      data: { name: "acme", display_name: null, description: null, config, created_at: "x" },
      meta: META,
    },
    error: undefined,
  };
}

function previewBody(chunks: unknown[], buildId: string | null = null) {
  return { data: { data: { chunks }, meta: { ...META, build_id: buildId } }, error: undefined };
}

const CHUNK = { ordinal: 0, text: "alpha beta", start_offset: 0, end_offset: 10, token_count: 2 };

function renderClean() {
  return renderWithProviders(
    <Routes>
      <Route path="/p/:project/clean" element={<Clean />} />
    </Routes>,
    { route: projectRoute("acme", "clean") },
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Clean", () => {
  it("saves chunking by SPREADING the current config — never wiping sibling blocks", async () => {
    // THE load-bearing behavior of the save path: PATCH /projects/{project} REPLACES
    // the whole config column server-side (no deep merge — read from
    // core/registry/store.py). A save that sent {config: {chunking}} alone would
    // silently destroy the ontology every build needs and the wreck would surface
    // only at the next build. The ontology block in the PATCH body is the pin.
    vi.spyOn(api, "GET").mockResolvedValue(
      projectBody({
        ontology: { entity_types: ["PERSON"] },
        chunking: { max_chars: 500, overlap: 50 },
      }) as never,
    );
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderClean();

    fireEvent.click(await screen.findByRole("button", { name: /save 500\/50 to config/i }));

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const body = (patch.mock.calls[0] as unknown as [string, { body: unknown }])[1].body as {
      config: Record<string, unknown>;
    };
    expect(body.config["ontology"]).toEqual({ entity_types: ["PERSON"] }); // survived
    expect(body.config["chunking"]).toEqual({ max_chars: 500, overlap: 50 });
  });

  it("fails closed while the config is loading or failed — a form without the real config saves a wipe", async () => {
    // The save spread needs the LOADED config; rendering the form from nothing and
    // saving would PATCH {} + chunking over a project that has ontology configured.
    vi.spyOn(api, "GET").mockResolvedValue({
      data: undefined,
      error: { error: { code: "STORE_UNAVAILABLE", message: "pg down" } },
    } as never);
    renderClean();

    expect(await screen.findByText(/could not load the project/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /save/i })).not.toBeInTheDocument();
  });

  it("previews pasted text and renders chunks with offsets; nothing needs a build", async () => {
    vi.spyOn(api, "GET").mockResolvedValue(projectBody() as never);
    const post = vi.spyOn(api, "POST").mockResolvedValue(previewBody([CHUNK]) as never);
    renderClean();

    fireEvent.change(await screen.findByLabelText(/text/i, { selector: "textarea" }), {
      target: { value: "alpha beta gamma" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^preview$/i }));

    expect(await screen.findByText("alpha beta")).toBeInTheDocument();
    expect(screen.getByText(/\[0, 10\)/)).toBeInTheDocument();
    const body = (post.mock.calls[0] as unknown as [string, { body: unknown }])[1].body as Record<
      string,
      unknown
    >;
    expect(body).toEqual({ text: "alpha beta gamma" }); // knobs omitted = server-side fallback
  });

  it("sends ONLY the typed knobs — empty inputs are omissions, not zeros or nulls", async () => {
    // The v1.1 contract rejects explicit null and the server's fallback chain only
    // runs for ABSENT keys. An empty input serialized as 0 would 400 (max_chars>=1)
    // or silently change overlap; serialized as null it would 400. Omission is the
    // only correct encoding.
    vi.spyOn(api, "GET").mockResolvedValue(projectBody() as never);
    const post = vi.spyOn(api, "POST").mockResolvedValue(previewBody([CHUNK]) as never);
    renderClean();

    fireEvent.change(await screen.findByLabelText(/text/i, { selector: "textarea" }), {
      target: { value: "abc def" },
    });
    fireEvent.change(screen.getByLabelText(/max_chars/i), { target: { value: "300" } });
    fireEvent.click(screen.getByRole("button", { name: /^preview$/i }));

    await waitFor(() => expect(post).toHaveBeenCalled());
    const body = (post.mock.calls[0] as unknown as [string, { body: unknown }])[1].body as Record<
      string,
      unknown
    >;
    expect(body).toEqual({ text: "abc def", max_chars: 300 });
    expect("overlap" in body).toBe(false);
  });

  it("mirrors the pair rule with the EFFECTIVE values, not just the typed ones", async () => {
    // A bad pair saved to config fails LATE — at the next build's config load — and
    // preview alone can't catch what an operator saves without previewing (class-15
    // gate criterion). The mirror must compose typed knobs with the CONFIGURED
    // fallbacks the server would use: here only overlap is typed (60) but the
    // project's configured max_chars (50) makes the pair illegal.
    vi.spyOn(api, "GET").mockResolvedValue(
      projectBody({ chunking: { max_chars: 50, overlap: 10 } }) as never,
    );
    renderClean();

    fireEvent.change(await screen.findByLabelText(/overlap/i), { target: { value: "60" } });

    expect(await screen.findByText(/overlap must satisfy/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^preview/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /save/i })).toBeDisabled();
  });

  it("marks a preview stale the moment any input changes — chunks must not impersonate new parameters", async () => {
    // The preview is a mutation (no cache), but its RESULT still answers the inputs
    // it was made with. After an edit, showing the old table unlabelled would let an
    // operator read chunk shapes as if they described the new values — the same
    // wrong-data-over-loud-failure tradeoff every page here makes.
    vi.spyOn(api, "GET").mockResolvedValue(projectBody() as never);
    vi.spyOn(api, "POST").mockResolvedValue(previewBody([CHUNK]) as never);
    renderClean();

    fireEvent.change(await screen.findByLabelText(/text/i, { selector: "textarea" }), {
      target: { value: "abc def" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^preview$/i }));
    expect(await screen.findByText("alpha beta")).toBeInTheDocument();
    expect(screen.queryByText(/changed since this preview/i)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/max_chars/i), { target: { value: "500" } }); // 500 > default overlap 200 — the pair mirror must NOT be what blocks the rerun

    expect(await screen.findByText(/changed since this preview/i)).toBeInTheDocument();
    // rerunning the preview clears the flag
    fireEvent.click(screen.getByRole("button", { name: /^preview$/i }));
    await waitFor(() =>
      expect(screen.queryByText(/changed since this preview/i)).not.toBeInTheDocument(),
    );
  });

  it("keeps the stale flag when an input was edited WHILE the preview was in flight", async () => {
    // The in-flight window: the mutate captured its body at click time, so an edit
    // during the round-trip means the settled chunks answer OLD inputs. An
    // unconditional clear-on-success would wipe the flag exactly then — the result
    // must land already labelled stale.
    vi.spyOn(api, "GET").mockResolvedValue(projectBody() as never);
    let resolvePreview: (v: unknown) => void = () => {};
    const post = vi
      .spyOn(api, "POST")
      .mockImplementation((() => new Promise((res) => (resolvePreview = res))) as never);
    renderClean();

    fireEvent.change(await screen.findByLabelText(/text/i, { selector: "textarea" }), {
      target: { value: "abc def" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^preview/i }));
    await waitFor(() => expect(post).toHaveBeenCalled()); // the request is ON the wire
    // ...and the edit lands while it is still in flight
    fireEvent.change(screen.getByLabelText(/max_chars/i), { target: { value: "500" } });
    resolvePreview(previewBody([CHUNK]));

    expect(await screen.findByText("alpha beta")).toBeInTheDocument();
    expect(await screen.findByText(/changed since this preview/i)).toBeInTheDocument();
  });

  it("surfaces a preview rejection loud, with the server's own message", async () => {
    // The server owns the real validation (strict ints, pair rule with config
    // fallbacks it alone resolves); its message names the offending values and must
    // reach the operator verbatim.
    vi.spyOn(api, "GET").mockResolvedValue(projectBody() as never);
    vi.spyOn(api, "POST").mockResolvedValue({
      data: undefined,
      error: { error: { code: "VALIDATION_ERROR", message: "overlap must satisfy 0 <= overlap" } },
    } as never);
    renderClean();

    fireEvent.change(await screen.findByLabelText(/text/i, { selector: "textarea" }), {
      target: { value: "abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^preview$/i }));

    expect(await screen.findByText(/preview failed: overlap must satisfy/i)).toBeInTheDocument();
  });

  it("names the active build a document preview was served from", async () => {
    // meta.build_id is §15's "which build served this" — a document-source preview
    // is only valid against that build, and the page must say which one.
    const doc = {
      id: "d1",
      build_id: "b1",
      source_uri: "file:///a.txt",
      mime: null,
      status: null,
      ingested_at: null,
      metadata: {},
    };
    vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      Promise.resolve(
        path.endsWith("/documents")
          ? {
              data: { data: [doc], meta: { ...META, build_id: "b1", next_cursor: null } },
              error: undefined,
            }
          : projectBody(),
      )) as never);
    vi.spyOn(api, "POST").mockResolvedValue(previewBody([CHUNK], "b1") as never);
    renderClean();

    fireEvent.click(await screen.findByLabelText(/ingested document/i));
    fireEvent.change(await screen.findByLabelText(/^document/i, { selector: "select" }), {
      target: { value: "d1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^preview$/i }));

    expect(await screen.findByText(/from active build b1/i)).toBeInTheDocument();
  });
});

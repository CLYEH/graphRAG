import { focusManager } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
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
  it("saves chunking by SPREADING a FRESH config read — never wiping or resurrecting sibling blocks", async () => {
    // TWO load-bearing behaviors in one pin. (1) PATCH /projects/{project} REPLACES
    // the whole config column server-side (no deep merge — read from
    // core/registry/store.py): {config:{chunking}} alone would silently destroy the
    // ontology every build needs. (2) The spread must come from a FRESH read inside
    // the mutation, not the page's cached copy — a config changed elsewhere since
    // page load would otherwise be resurrected wholesale (Codex, #74). The mock
    // serves DIFFERENT configs per GET: the page loaded with PERSON-ontology, but
    // by save time the server holds ORG-ontology plus a query_policy block — the
    // PATCH body must carry the LATER config, proving the mutation re-read it.
    let gets = 0;
    vi.spyOn(api, "GET").mockImplementation((() =>
      Promise.resolve(
        ++gets === 1
          ? projectBody({
              ontology: { entity_types: ["PERSON"] },
              chunking: { max_chars: 500, overlap: 50 },
            })
          : projectBody({
              ontology: { entity_types: ["ORG"] },
              query_policy: { deny: ["drop"] },
              chunking: { max_chars: 500, overlap: 50 },
            }),
      )) as never);
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderClean();

    fireEvent.click(await screen.findByRole("button", { name: /儲存 500\/50 到專案設定/ }));

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const body = (patch.mock.calls[0] as unknown as [string, { body: unknown }])[1].body as {
      config: Record<string, unknown>;
    };
    expect(body.config["ontology"]).toEqual({ entity_types: ["ORG"] }); // the FRESH read
    expect(body.config["query_policy"]).toEqual({ deny: ["drop"] }); // survived, not wiped
    expect(body.config["chunking"]).toEqual({ max_chars: 500, overlap: 50 });
  });

  it("resolves EMPTY knobs against the FRESH config, not the page's cached fallbacks", async () => {
    // Codex's second config-spread finding: the fresh re-read preserved sibling
    // BLOCKS but the page had already baked its cached fallbacks into concrete
    // numbers. Page loads {500,50}; another operator sets overlap=80; the user
    // types ONLY max_chars=1000 — the save must write {1000, 80}, not {1000, 50}
    // (a silent revert of the other operator's change).
    let gets = 0;
    vi.spyOn(api, "GET").mockImplementation((() =>
      Promise.resolve(
        ++gets === 1
          ? projectBody({ chunking: { max_chars: 500, overlap: 50 } })
          : projectBody({
              chunking: { max_chars: 500, overlap: 80 },
              ontology: { entity_types: ["ORG"] },
            }),
      )) as never);
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderClean();

    fireEvent.change(await screen.findByLabelText(/max_chars/i), { target: { value: "1000" } });
    fireEvent.click(screen.getByRole("button", { name: /儲存 1000\/50 到專案設定/ }));

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const body = (patch.mock.calls[0] as unknown as [string, { body: unknown }])[1].body as {
      config: Record<string, unknown>;
    };
    expect(body.config["chunking"]).toEqual({ max_chars: 1000, overlap: 80 }); // fresh fallback
    expect(body.config["ontology"]).toEqual({ entity_types: ["ORG"] }); // blocks still spread
  });

  it("fails the save loud when fresh fallbacks make the typed knob an illegal pair", async () => {
    // The composed pair is only validated client-side against CACHED fallbacks; a
    // fresh overlap can make a typed max_chars illegal, PATCH does not validate
    // chunking, and the wreck would surface at the next build's config load.
    // The mutation re-validates after resolving and aborts — PATCH never fires.
    let gets = 0;
    vi.spyOn(api, "GET").mockImplementation((() =>
      Promise.resolve(
        ++gets === 1
          ? projectBody({ chunking: { max_chars: 500, overlap: 50 } })
          : projectBody({ chunking: { max_chars: 500, overlap: 200 } }),
      )) as never);
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderClean();

    fireEvent.change(await screen.findByLabelText(/max_chars/i), { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: /儲存 100\/50 到專案設定/ }));

    expect(await screen.findByText(/儲存失敗:overlap must satisfy/)).toBeInTheDocument();
    expect(patch).not.toHaveBeenCalled();
  });

  it("aborts the save loud when the fresh re-read fails — never PATCHes a fallback", async () => {
    // The fresh-GET error branch is the fix's load-bearing invariant: "cannot read
    // fresh" must abort the save, not fall back to a cached/empty spread — a
    // fallback would quietly reintroduce the resurrection/wipe the re-read exists
    // to prevent. Both halves pinned: the failure surfaces, and PATCH never fires.
    let gets = 0;
    vi.spyOn(api, "GET").mockImplementation((() =>
      Promise.resolve(
        ++gets === 1
          ? projectBody({ ontology: { entity_types: ["PERSON"] } })
          : {
              data: undefined,
              error: { error: { code: "STORE_UNAVAILABLE", message: "pg gone" } },
            },
      )) as never);
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderClean();

    fireEvent.click(await screen.findByRole("button", { name: /儲存 .* 到專案設定/ }));

    expect(await screen.findByText(/儲存失敗:pg gone/)).toBeInTheDocument();
    expect(patch).not.toHaveBeenCalled();
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
    expect(screen.queryByRole("button", { name: /儲存/ })).not.toBeInTheDocument();
  });

  it("previews pasted text and renders chunks with offsets; nothing needs a build", async () => {
    vi.spyOn(api, "GET").mockResolvedValue(projectBody() as never);
    const post = vi.spyOn(api, "POST").mockResolvedValue(previewBody([CHUNK]) as never);
    renderClean();

    fireEvent.change(await screen.findByLabelText("文字內容", { selector: "textarea" }), {
      target: { value: "alpha beta gamma" },
    });
    fireEvent.click(screen.getByRole("button", { name: "預覽" }));

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

    fireEvent.change(await screen.findByLabelText("文字內容", { selector: "textarea" }), {
      target: { value: "abc def" },
    });
    fireEvent.change(screen.getByLabelText(/max_chars/i), { target: { value: "300" } });
    fireEvent.click(screen.getByRole("button", { name: "預覽" }));

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
    expect(screen.getByRole("button", { name: "預覽" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /儲存/ })).toBeDisabled();
  });

  it("marks a preview stale the moment any input changes — chunks must not impersonate new parameters", async () => {
    // The preview is a mutation (no cache), but its RESULT still answers the inputs
    // it was made with. After an edit, showing the old table unlabelled would let an
    // operator read chunk shapes as if they described the new values — the same
    // wrong-data-over-loud-failure tradeoff every page here makes.
    vi.spyOn(api, "GET").mockResolvedValue(projectBody() as never);
    vi.spyOn(api, "POST").mockResolvedValue(previewBody([CHUNK]) as never);
    renderClean();

    fireEvent.change(await screen.findByLabelText("文字內容", { selector: "textarea" }), {
      target: { value: "abc def" },
    });
    fireEvent.click(screen.getByRole("button", { name: "預覽" }));
    expect(await screen.findByText("alpha beta")).toBeInTheDocument();
    expect(screen.queryByText(/預覽後改過了/)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/max_chars/i), { target: { value: "500" } }); // 500 > default overlap 200 — the pair mirror must NOT be what blocks the rerun

    expect(await screen.findByText(/預覽後改過了/)).toBeInTheDocument();
    // rerunning the preview clears the flag
    fireEvent.click(screen.getByRole("button", { name: "預覽" }));
    await waitFor(() => expect(screen.queryByText(/預覽後改過了/)).not.toBeInTheDocument());
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

    fireEvent.change(await screen.findByLabelText("文字內容", { selector: "textarea" }), {
      target: { value: "abc def" },
    });
    fireEvent.click(screen.getByRole("button", { name: "預覽" }));
    await waitFor(() => expect(post).toHaveBeenCalled()); // the request is ON the wire
    // ...and the edit lands while it is still in flight
    fireEvent.change(screen.getByLabelText(/max_chars/i), { target: { value: "500" } });
    resolvePreview(previewBody([CHUNK]));

    expect(await screen.findByText("alpha beta")).toBeInTheDocument();
    expect(await screen.findByText(/預覽後改過了/)).toBeInTheDocument();
  });

  it("marks the preview stale when a CONFIG refetch moves the fallback pair — no input event at all", async () => {
    // The event-trail hole Codex named (#74): with empty knobs the effective pair
    // comes from project config, and a focus refetch can change that config with no
    // input handler running. Staleness is a comparison against the snapshot the
    // preview captured, so the moved fallback must flag the table by itself.
    let gets = 0;
    vi.spyOn(api, "GET").mockImplementation((() =>
      Promise.resolve(
        ++gets === 1
          ? projectBody({ chunking: { max_chars: 100, overlap: 10 } })
          : projectBody({ chunking: { max_chars: 300, overlap: 30 } }),
      )) as never);
    vi.spyOn(api, "POST").mockResolvedValue(previewBody([CHUNK]) as never);
    renderClean();

    fireEvent.change(await screen.findByLabelText("文字內容", { selector: "textarea" }), {
      target: { value: "abc def" },
    });
    fireEvent.click(screen.getByRole("button", { name: "預覽" }));
    expect(await screen.findByText("alpha beta")).toBeInTheDocument();
    expect(screen.queryByText(/預覽後改過了/)).not.toBeInTheDocument();

    // another tab / CLI PATCHed the config; the window-focus refetch picks it up
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });

    expect(await screen.findByText(/預覽後改過了/)).toBeInTheDocument();
  });

  it("withdraws the save confirmation when the effective pair moves past what was saved", async () => {
    // "Saved — the next build will chunk with these values" beside freshly edited,
    // UNSAVED values is a false receipt (Codex, #74): the banner names the pair it
    // saved and stands only while the effective pair still matches it.
    vi.spyOn(api, "GET").mockResolvedValue(
      projectBody({ chunking: { max_chars: 100, overlap: 10 } }) as never,
    );
    vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderClean();

    fireEvent.click(await screen.findByRole("button", { name: /儲存 100\/10 到專案設定/ }));
    expect(await screen.findByText(/已儲存 100\/10/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/max_chars/i), { target: { value: "800" } });

    await waitFor(() => expect(screen.queryByText(/已儲存 100\/10/)).not.toBeInTheDocument());
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

    fireEvent.change(await screen.findByLabelText("文字內容", { selector: "textarea" }), {
      target: { value: "abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "預覽" }));

    expect(await screen.findByText(/預覽失敗:overlap must satisfy/)).toBeInTheDocument();
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

    fireEvent.click(await screen.findByLabelText(/已匯入的文件/));
    fireEvent.change(await screen.findByLabelText(/^document/i, { selector: "select" }), {
      target: { value: "d1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "預覽" }));

    expect(await screen.findByText(/來自目前上線中的知識庫/)).toBeInTheDocument();
  });
});

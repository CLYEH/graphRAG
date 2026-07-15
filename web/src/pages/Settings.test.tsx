import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Settings } from "./Settings";
import { DEFAULT_QUERY_POLICY } from "../api/queries";
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

// A config with EVERY block family: the load-bearing assertion in most tests
// below is that blocks a section did NOT touch survive its PATCH verbatim —
// PATCH replaces the whole config column server-side (no deep merge), so one
// dropped sibling here is a wiped ontology or guardrail in production.
const FULL_CONFIG = {
  ontology: {
    entity_types: ["EVENT"],
    relation_types: ["PRACTICED_BY"],
    proposal_policy: "review",
  },
  chunking: { max_chars: 500, overlap: 50 },
  query_policy: {
    ...DEFAULT_QUERY_POLICY,
    default_mode: "semantic",
    max_top_k: 5,
    max_graph_hops: 3,
  },
  structured_mappings: { companies: { entities: {}, relations: [] } },
  resolution: { auto_merge_threshold: 0.92 },
  future_unknown_block: { keep: "me" },
};

function patchedConfig(patch: ReturnType<typeof vi.spyOn>): Record<string, unknown> {
  const body = (patch.mock.calls[0] as unknown as [string, { body: unknown }])[1].body as {
    config: Record<string, unknown>;
  };
  return body.config;
}

function renderSettings() {
  return renderWithProviders(
    <Routes>
      <Route path="/p/:project/settings" element={<Settings />} />
    </Routes>,
    { route: projectRoute("acme", "settings") },
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Settings — ontology", () => {
  it("saves the vocabulary by SPREADING a FRESH config read — siblings and unknown blocks survive", async () => {
    // The FE2 discipline, re-pinned for the new writer: the page loaded one
    // config, the server has since moved on (another tab, a CLI PATCH) — the
    // PATCH body must carry the SERVER's later siblings, not resurrect the
    // page's stale copy, and must not drop the blocks this form never renders.
    let gets = 0;
    vi.spyOn(api, "GET").mockImplementation((() =>
      Promise.resolve(
        ++gets === 1
          ? projectBody(FULL_CONFIG)
          : projectBody({ ...FULL_CONFIG, chunking: { max_chars: 999, overlap: 9 } }),
      )) as never);
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderSettings();

    fireEvent.change(await screen.findByLabelText("新增實體類型"), {
      target: { value: "PLACE" },
    });
    fireEvent.click(screen.getByRole("button", { name: "加入實體類型" }));
    fireEvent.click(screen.getByRole("button", { name: "儲存知識類型" }));

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const config = patchedConfig(patch);
    expect(config["ontology"]).toEqual({
      entity_types: ["EVENT", "PLACE"],
      relation_types: ["PRACTICED_BY"],
      proposal_policy: "review",
    });
    expect(config["chunking"]).toEqual({ max_chars: 999, overlap: 9 }); // the FRESH read
    expect(config["structured_mappings"]).toEqual(FULL_CONFIG.structured_mappings);
    expect(config["resolution"]).toEqual(FULL_CONFIG.resolution);
    expect(config["future_unknown_block"]).toEqual(FULL_CONFIG.future_unknown_block);
  });

  it("an emptied vocabulary DELETES the ontology key — {} and null are both malformed to the build loader", async () => {
    vi.spyOn(api, "GET").mockResolvedValue(
      projectBody({
        ontology: { entity_types: ["EVENT"], relation_types: ["PRACTICED_BY"] },
        chunking: { max_chars: 500, overlap: 50 },
      }) as never,
    );
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderSettings();

    fireEvent.click(await screen.findByRole("button", { name: "移除 EVENT" }));
    fireEvent.click(screen.getByRole("button", { name: "移除 PRACTICED_BY" }));
    fireEvent.click(screen.getByRole("button", { name: "儲存(移除整份詞彙表)" }));

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const config = patchedConfig(patch);
    expect("ontology" in config).toBe(false); // absent, not {} / null
    expect(config["chunking"]).toEqual({ max_chars: 500, overlap: 50 });
  });

  it("refuses a ONE-SIDED vocabulary at save time — the build would refuse it later with a stack trace", async () => {
    // core/builds/config.py: a present ontology block requires BOTH lists
    // (TextOntology). Removing only the relation types must not PATCH.
    vi.spyOn(api, "GET").mockResolvedValue(
      projectBody({
        ontology: { entity_types: ["EVENT"], relation_types: ["PRACTICED_BY"] },
      }) as never,
    );
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderSettings();

    fireEvent.click(await screen.findByRole("button", { name: "移除 PRACTICED_BY" }));

    await screen.findByText(/必須同時提供,或同時清空/);
    expect(screen.getByRole("button", { name: "儲存知識類型" })).toBeDisabled();
    expect(patch).not.toHaveBeenCalled();
  });

  it("proposal_policy 自動採用 writes 'auto' — the only other legal value", async () => {
    vi.spyOn(api, "GET").mockResolvedValue(projectBody(FULL_CONFIG) as never);
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderSettings();

    fireEvent.click(await screen.findByLabelText("自動採用"));
    fireEvent.click(screen.getByRole("button", { name: "儲存知識類型" }));

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const ontology = patchedConfig(patch)["ontology"] as Record<string, unknown>;
    expect(ontology["proposal_policy"]).toBe("auto");
  });

  it("REPAIRS a malformed ontology block WITHOUT an edit — the salvaged form is itself unsaved", async () => {
    // Codex #79 R4 (the ontology sibling of the policy R1/R2): a hand-written
    // block with a typo'd proposal_policy renders as a clean `review` form
    // with draft === null, so the save stayed disabled and the corrected value
    // was never written — builds stay blocked while the page looks clean. The
    // malformed block is itself unsaved state: the notice shows, the save
    // enables with no edit, and it writes the salvaged-clean block. Predicate
    // parity with the real loader is pinned separately (ontologyValidityParity).
    vi.spyOn(api, "GET").mockResolvedValue(
      projectBody({
        ontology: {
          entity_types: ["EVENT"],
          relation_types: ["PRACTICED_BY"],
          proposal_policy: "typo",
        },
      }) as never,
    );
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderSettings();

    await screen.findByText(/知識類型設定不符規範/);
    const saveBtn = screen.getByRole("button", { name: "儲存知識類型" });
    expect(saveBtn).toBeEnabled(); // no edit needed — the repair IS the save
    fireEvent.click(saveBtn);

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(patchedConfig(patch)["ontology"]).toEqual({
      entity_types: ["EVENT"],
      relation_types: ["PRACTICED_BY"],
      proposal_policy: "review", // the junk "typo" fell back to the safe default
    });
  });

  it("repairs an empty malformed ontology ({}) by DELETING the key", async () => {
    // {} is malformed to the build (TextOntology needs both lists), but its
    // salvage is an empty vocabulary — the repair is deletion, not a rewrite,
    // and the button must offer it without a prior edit.
    vi.spyOn(api, "GET").mockResolvedValue(
      projectBody({ ontology: {}, chunking: { max_chars: 500, overlap: 50 } }) as never,
    );
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderSettings();

    await screen.findByText(/知識類型設定不符規範/);
    const del = screen.getByRole("button", { name: "儲存(移除整份詞彙表)" });
    expect(del).toBeEnabled();
    fireEvent.click(del);

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const config = patchedConfig(patch);
    expect("ontology" in config).toBe(false); // deleted, not {} / null
    expect(config["chunking"]).toEqual({ max_chars: 500, overlap: 50 }); // sibling survives
  });
});

describe("Settings — query policy", () => {
  it("creates the default policy WITHOUT requiring an edit first — the missing state is itself unsaved", async () => {
    // Codex #79 R1: gating the create button on draft !== null forced an
    // arbitrary field change before the operator could accept the safe
    // defaults — leaving query/graph unusable until they invented an edit.
    vi.spyOn(api, "GET").mockResolvedValue(
      projectBody({ chunking: { max_chars: 500, overlap: 50 } }) as never,
    );
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderSettings();

    const create = await screen.findByRole("button", { name: "以預設範本建立並儲存" });
    expect(create).toBeEnabled(); // no edit needed
    fireEvent.click(create);

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(patchedConfig(patch)["query_policy"]).toEqual(DEFAULT_QUERY_POLICY);
  });

  it("REBUILDS a partial curl-era policy on the template — never spreads an incomplete base", async () => {
    // Codex #79 R2: a block like {default_mode, max_top_k, max_graph_hops}
    // (hand-written before this page existed) passed the old object check and
    // became the spread base — the PATCH "succeeded" while every query kept
    // 400ing on the missing required fields (PATCH validates nothing). The
    // save must rebuild on the template, salvaging only the operator fields;
    // the page must say so up front and enable the save without a prior edit
    // (the R1 rule extended: what this save writes lives nowhere on the server).
    vi.spyOn(api, "GET").mockResolvedValue(
      projectBody({
        query_policy: { default_mode: "semantic", max_top_k: 5, max_graph_hops: 4 },
        chunking: { max_chars: 500, overlap: 50 },
      }) as never,
    );
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderSettings();

    await screen.findByText(/問答安全政策不符規範/);
    const rebuild = screen.getByRole("button", { name: "以預設範本重建並儲存" });
    expect(rebuild).toBeEnabled(); // no edit needed — R1's rule extended
    fireEvent.click(rebuild);

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(patchedConfig(patch)["query_policy"]).toEqual({
      ...DEFAULT_QUERY_POLICY,
      default_mode: "semantic", // the salvaged operator fields
      max_top_k: 5,
      max_graph_hops: 4,
    });
  });

  it("rebuilds a KEY-COMPLETE block that violates value constraints — validity is the server's verdict, mirrored", async () => {
    // Codex #79 R3: R2's key-set check let a complete-looking block with a
    // bad value (schema_version "2.0" here; enabled-sql-empty-tables and
    // shrunken frozen lists are corpus siblings) through as the spread base —
    // same silent brick, one level deeper. The full predicate is pinned to
    // the server validator by the parity corpus (policyValidityParity.test);
    // this test pins the SETTINGS wiring: invalid ⇒ rebuild flow.
    vi.spyOn(api, "GET").mockResolvedValue(
      projectBody({
        query_policy: { ...FULL_CONFIG.query_policy, schema_version: "2.0" },
      }) as never,
    );
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderSettings();

    await screen.findByText(/問答安全政策不符規範/);
    fireEvent.click(screen.getByRole("button", { name: "以預設範本重建並儲存" }));

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const policy = patchedConfig(patch)["query_policy"] as Record<string, unknown>;
    expect(policy["schema_version"]).toBe("1.0"); // rebuilt, not spread
    expect(policy["default_mode"]).toBe("semantic"); // operator fields still salvage
  });

  it("does not salvage an OUT-OF-RANGE operator field — a max_top_k of 0 must not lock the rebuild button", async () => {
    // Codex #79 R5 (the R3 value-domain class, in the salvage): the block is
    // malformed via max_top_k: 0, which isValidPolicyBlock correctly flags —
    // but seeding that 0 into the form trips the form's own fieldError, which
    // disabled the very "rebuild with template" button meant to repair it, so
    // accepting the safe defaults was impossible without first editing the
    // bad field. The salvage must fall back to the template default (≥ 1).
    vi.spyOn(api, "GET").mockResolvedValue(
      projectBody({ query_policy: { ...FULL_CONFIG.query_policy, max_top_k: 0 } }) as never,
    );
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderSettings();

    await screen.findByText(/問答安全政策不符規範/);
    const rebuild = screen.getByRole("button", { name: "以預設範本重建並儲存" });
    expect(rebuild).toBeEnabled(); // no edit needed despite the 0 in the saved block
    fireEvent.click(rebuild);

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const policy = patchedConfig(patch)["query_policy"] as Record<string, unknown>;
    expect(policy["max_top_k"]).toBe(DEFAULT_QUERY_POLICY.max_top_k); // fell back, not 0
    expect(policy["max_graph_hops"]).toBe(3); // the VALID salvaged field still lands
  });

  it("does not salvage a junk default_mode of sql — the rebuild target disables sql", async () => {
    // Seeding the form to "sql" from a malformed block re-creates R1's dead
    // end one click later: the rebuilt policy has text_to_sql disabled, so
    // the save the form invites would refuse its own seeded value.
    vi.spyOn(api, "GET").mockResolvedValue(
      projectBody({ query_policy: { default_mode: "sql", max_top_k: 9 } }) as never,
    );
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderSettings();

    fireEvent.click(await screen.findByRole("button", { name: "以預設範本重建並儲存" }));

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const policy = patchedConfig(patch)["query_policy"] as Record<string, unknown>;
    expect(policy["default_mode"]).toBe("hybrid"); // template's mode, not the junk "sql"
    expect(policy["max_top_k"]).toBe(9); // other operator fields still salvage
  });

  it("rebuilds on the template even when the block DEGRADES between page load and save", async () => {
    // The fresh-read discipline applied to completeness: the page loaded a
    // COMPLETE policy, but by save time another writer had replaced it with a
    // partial one — spreading the page's belief would ship the same silent
    // brick R2 names. The completeness check must run against the FRESH block.
    let gets = 0;
    vi.spyOn(api, "GET").mockImplementation((() =>
      Promise.resolve(
        ++gets === 1
          ? projectBody(FULL_CONFIG)
          : projectBody({
              ...FULL_CONFIG,
              query_policy: { default_mode: "global" },
            }),
      )) as never);
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderSettings();

    fireEvent.change(await screen.findByLabelText(/單次檢索筆數上限/), {
      target: { value: "7" },
    });
    fireEvent.click(screen.getByRole("button", { name: "儲存問答安全設定" }));

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const policy = patchedConfig(patch)["query_policy"] as Record<string, unknown>;
    expect(policy["schema_version"]).toBe("1.0"); // template-complete, not the partial junk
    expect(policy["text_to_sql"]).toEqual(DEFAULT_QUERY_POLICY.text_to_sql);
    expect(policy["max_top_k"]).toBe(7); // the operator's edit still lands
  });

  it("creates a COMPLETE schema-valid policy from the template when the project has none", async () => {
    // The frozen contract requires every top-level field — writing only the
    // three operator knobs would brick every query with 400 "invalid". The
    // missing-policy save must therefore emit template + overrides, whole.
    vi.spyOn(api, "GET").mockResolvedValue(
      projectBody({ chunking: { max_chars: 500, overlap: 50 } }) as never,
    );
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderSettings();

    await screen.findByText(/尚未設定問答安全政策/);
    fireEvent.change(screen.getByLabelText(/單次檢索筆數上限/), { target: { value: "20" } });
    fireEvent.click(screen.getByRole("button", { name: "以預設範本建立並儲存" }));

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const config = patchedConfig(patch);
    expect(config["query_policy"]).toEqual({
      ...DEFAULT_QUERY_POLICY,
      default_mode: "hybrid",
      max_top_k: 20,
      max_graph_hops: 2,
    });
    expect(config["chunking"]).toEqual({ max_chars: 500, overlap: 50 }); // sibling survives
  });

  it("overrides ONLY the operator fields on an existing policy — the guardrail blocks ride along untouched", async () => {
    const custom = {
      ...FULL_CONFIG.query_policy,
      text_to_sql: {
        ...DEFAULT_QUERY_POLICY.text_to_sql,
        enabled: true,
        allowed_tables: ["exhibits"],
      },
    };
    vi.spyOn(api, "GET").mockResolvedValue(
      projectBody({ ...FULL_CONFIG, query_policy: custom }) as never,
    );
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderSettings();

    fireEvent.change(await screen.findByLabelText(/圖譜跳數上限/), { target: { value: "4" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存問答安全設定" }));

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const policy = patchedConfig(patch)["query_policy"] as Record<string, unknown>;
    expect(policy["max_graph_hops"]).toBe(4);
    expect(policy["max_top_k"]).toBe(5); // untouched operator field keeps its value
    expect(policy["text_to_sql"]).toEqual(custom.text_to_sql); // guardrail block verbatim
  });

  it("preserves a CONCURRENT change to an untouched operator field — only the edited knob is written (R6)", async () => {
    // Codex #79 R6 (the chunking fresh-resolution rule, #74): the page loaded
    // default_mode "semantic" + max_graph_hops 3; by save time a concurrent
    // tab changed them to "graph"/7 (still valid). The operator edits ONLY
    // max_top_k. The save must keep the FRESH default_mode/hops — sending all
    // three from the page's snapshot would silently revert the concurrent edit.
    let gets = 0;
    vi.spyOn(api, "GET").mockImplementation((() =>
      Promise.resolve(
        gets++ === 0
          ? projectBody(FULL_CONFIG)
          : projectBody({
              ...FULL_CONFIG,
              query_policy: {
                ...FULL_CONFIG.query_policy,
                default_mode: "graph",
                max_graph_hops: 7,
              },
            }),
      )) as never);
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderSettings();

    fireEvent.change(await screen.findByLabelText(/單次檢索筆數上限/), { target: { value: "8" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存問答安全設定" }));

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const policy = patchedConfig(patch)["query_policy"] as Record<string, unknown>;
    expect(policy["default_mode"]).toBe("graph"); // fresh concurrent value, NOT the page's "semantic"
    expect(policy["max_graph_hops"]).toBe(7); // untouched → preserved from fresh
    expect(policy["max_top_k"]).toBe(8); // the operator's actual edit
  });

  it("blocks default_mode=sql when the FRESH read has text_to_sql disabled — the schema allOf, mirrored at the last moment", async () => {
    // Class-10 shaped: the page loaded a policy with sql ENABLED (so the
    // option is selectable), but by save time another writer disabled it.
    // PATCH validates nothing — this mirror is the only guard before the
    // next query 400s — so it must check the FRESH block, not the page's.
    const sqlOn = {
      ...FULL_CONFIG.query_policy,
      text_to_sql: {
        ...DEFAULT_QUERY_POLICY.text_to_sql,
        enabled: true,
        allowed_tables: ["exhibits"],
      },
    };
    let gets = 0;
    vi.spyOn(api, "GET").mockImplementation((() =>
      Promise.resolve(
        ++gets === 1
          ? projectBody({ ...FULL_CONFIG, query_policy: sqlOn })
          : projectBody(FULL_CONFIG), // sql back to disabled
      )) as never);
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(projectBody() as never);
    renderSettings();

    fireEvent.change(await screen.findByLabelText(/預設問答模式/), {
      target: { value: "sql" },
    });
    fireEvent.click(screen.getByRole("button", { name: "儲存問答安全設定" }));

    await screen.findByText(/未啟用 SQL 查詢/);
    expect(patch).not.toHaveBeenCalled();
  });

  it("the sql option is disabled in the picker while the policy has text_to_sql off", async () => {
    vi.spyOn(api, "GET").mockResolvedValue(projectBody(FULL_CONFIG) as never);
    renderSettings();

    const option = (await screen.findByRole("option", {
      name: /SQL 查詢/,
    })) as HTMLOptionElement;
    expect(option.disabled).toBe(true);
  });

  it("surfaces a PATCH failure verbatim", async () => {
    vi.spyOn(api, "GET").mockResolvedValue(projectBody(FULL_CONFIG) as never);
    vi.spyOn(api, "PATCH").mockResolvedValue({
      data: undefined,
      error: { error: { code: "VALIDATION_ERROR", message: "config rejected by server" } },
    } as never);
    renderSettings();

    fireEvent.change(await screen.findByLabelText(/單次檢索筆數上限/), {
      target: { value: "7" },
    });
    fireEvent.click(screen.getByRole("button", { name: "儲存問答安全設定" }));

    await screen.findByText(/儲存失敗:config rejected by server/);
  });
});

describe("Settings — drafts survive sibling saves", () => {
  it("a dirty ontology draft outlives another section's save-and-refetch", async () => {
    // The deliberate divergence from Clean.tsx: this page must NOT unmount
    // its forms while the project query revalidates. If it did (an early
    // return on isFetching), every section's local draft would die each time
    // a SIBLING saved — chunking's save below invalidates the project query,
    // and the unsaved PLACE chip would silently vanish (the class-20 family:
    // state must live where renders can't kill it). Reverting Settings to
    // Clean's early-return style makes this test fail at the last assertion.
    // Stateful stub, like a real server: the post-save refetch sees what was
    // PATCHed. The refetch (the 3rd GET: load, mutation's fresh read, refetch)
    // HANGS until released — instantly-resolving mocks collapse the isFetching
    // window into a single act() flush and the revalidation state never
    // renders, which is exactly how the first version of this test stayed
    // green against the early-return revert it claims to catch (empirically
    // probed; the class-20(b) lesson).
    let serverConfig: Record<string, unknown> = FULL_CONFIG;
    let release: ((v: unknown) => void) | null = null;
    let gets = 0;
    vi.spyOn(api, "GET").mockImplementation(((): Promise<unknown> => {
      if (++gets >= 3) return new Promise((r) => (release = r));
      return Promise.resolve(projectBody(serverConfig));
    }) as never);
    const patch = vi.spyOn(api, "PATCH").mockImplementation(((
      _path: string,
      opts: { body: { config: Record<string, unknown> } },
    ) => {
      serverConfig = opts.body.config;
      return Promise.resolve(projectBody(serverConfig));
    }) as never);
    renderSettings();

    fireEvent.change(await screen.findByLabelText("新增實體類型"), {
      target: { value: "PLACE" },
    });
    fireEvent.click(screen.getByRole("button", { name: "加入實體類型" }));
    await screen.findByRole("button", { name: "移除 PLACE" }); // draft is dirty

    fireEvent.change(screen.getByLabelText(/每塊字元上限/), { target: { value: "800" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存 800/50 到專案設定" }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(release).not.toBeNull()); // the refetch is in flight

    // mid-revalidation: the forms are STILL MOUNTED (the draft lives) but the
    // save affordances fail closed
    expect(screen.getByRole("button", { name: "移除 PLACE" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "儲存知識類型" })).toBeDisabled();

    await act(async () => release!(projectBody(serverConfig)));
    await screen.findByText("已儲存。"); // chunking's confirmation stands on the new baseline

    expect(screen.getByRole("button", { name: "移除 PLACE" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "儲存知識類型" })).toBeEnabled();
  });
});

describe("Settings — fail closed", () => {
  it("shows no forms before the config read settles — a form without the real config saves a wipe", async () => {
    vi.spyOn(api, "GET").mockImplementation((() => new Promise(() => {})) as never);
    renderSettings();

    await screen.findByText("載入專案設定中…");
    expect(screen.queryByRole("button", { name: /儲存/ })).toBeNull();
  });

  it("shows the error, not forms, when the initial config read fails", async () => {
    vi.spyOn(api, "GET").mockResolvedValue({
      data: undefined,
      error: { error: { code: "INTERNAL", message: "boom" } },
    } as never);
    renderSettings();

    await screen.findByText(/無法載入專案設定:boom/);
    expect(screen.queryByRole("button", { name: /儲存/ })).toBeNull();
  });
});

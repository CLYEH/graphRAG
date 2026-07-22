import { fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ReviewQueue } from "./ReviewQueue";
import { api } from "../api/client";
import {
  entity,
  mergeCandidate,
  projectRoute,
  relation,
  renderWithProviders,
  stubMergeCandidates,
  stubReviewWorld,
} from "../test-utils";

const META = { next_cursor: null, build_id: "b1", request_id: "r", elapsed_ms: 1 };

function renderAt(key: string) {
  return renderWithProviders(
    <Routes>
      <Route path="/p/:project/review" element={<ReviewQueue />} />
    </Routes>,
    { route: projectRoute(key, "review") },
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ReviewQueue", () => {
  it("renders the review flow for an addressable project", async () => {
    stubReviewWorld({
      candidates: [
        mergeCandidate({ status: "pending", left_snapshot: { name: "海祭", type: "EVENT" } }),
      ],
    });
    renderAt("acme");

    // the governance surface (治理) defaults to the 合併 (merge) tab
    expect(await screen.findByRole("heading", { name: "治理" })).toBeInTheDocument();
    expect(await screen.findByText("海祭")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "是,合併" })).toBeEnabled();
  });

  it("lists ontology proposals in the 本體提案 tab and accepts one via the accept path (GOV3)", async () => {
    const proposal = {
      id: "p1111111-1111-4111-8111-000000000001",
      project: "acme",
      kind: "entity",
      type_name: "Spaceship",
      proposal_key: "fpv2:spaceship",
      status: "proposed",
      example: "Rocinante",
      chunk_ref: "chunk:hash-x:0",
    };
    // route-aware GET: proposals for the pool, empty for the default 合併 queue —
    // a single mock would feed merge-shaped data to the pool (false green: assert
    // the pool renders from THE ontology-proposals endpoint specifically)
    vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      path === "/projects/{project}/ontology-proposals"
        ? Promise.resolve({ data: { data: [proposal], meta: META }, error: undefined })
        : Promise.resolve({ data: { data: [], meta: META }, error: undefined })) as never);
    const post = vi.spyOn(api, "POST").mockResolvedValue({
      data: { data: { ...proposal, status: "accepted" }, meta: META },
      error: undefined,
    } as never);
    renderAt("acme");

    // switch to the proposals tab, then the pool lists the proposed type
    fireEvent.click(await screen.findByRole("tab", { name: "本體提案" }));
    expect(await screen.findByText("Spaceship")).toBeInTheDocument();
    expect(screen.getByText(/Rocinante/)).toBeInTheDocument();

    // 採納 arms the §17-terminal confirm; only 確定採納 posts the ACCEPT path (verb
    // rides the URL) with the deterministic Idempotency-Key — a body-verb or the
    // reject path would fail this
    fireEvent.click(screen.getByRole("button", { name: /加入本體/ }));
    fireEvent.click(await screen.findByRole("button", { name: "確定採納" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/projects/{project}/ontology-proposals/{proposal_id}/accept",
        expect.objectContaining({
          params: expect.objectContaining({
            header: { "Idempotency-Key": `${proposal.id}:accept` },
          }),
        }),
      ),
    );
  });

  it("lists needs_review entities on the 知識點 tab and associates tab↔panel for a11y (GOV2-fe)", async () => {
    // route-aware GET: entities for the review queue, empty elsewhere — a blanket
    // mock would feed merge-shaped data to the entity tab (false green)
    vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      path === "/projects/{project}/entities"
        ? Promise.resolve({
            data: { data: [entity({ canonical_name: "海祭" })], meta: META },
            error: undefined,
          })
        : Promise.resolve({ data: { data: [], meta: META }, error: undefined })) as never);
    renderAt("acme");

    const tab = await screen.findByRole("tab", { name: "知識點" });
    fireEvent.click(tab);
    expect(await screen.findByText("海祭")).toBeInTheDocument();

    // a11y: the tab points at a real tabpanel that is labelled back by the tab
    const controlled = tab.getAttribute("aria-controls");
    expect(controlled).toBeTruthy();
    const panel = document.getElementById(controlled as string);
    expect(panel).toHaveAttribute("role", "tabpanel");
    expect(panel).toHaveAttribute("aria-labelledby", tab.id);
  });

  it("lists needs_review relations on the 關聯 tab from /relations (GOV2-fe-2)", async () => {
    vi.spyOn(api, "GET").mockImplementation(((path: string) =>
      path === "/projects/{project}/relations"
        ? Promise.resolve({
            data: { data: [relation({ type: "PRACTICED_BY" })], meta: META },
            error: undefined,
          })
        : Promise.resolve({ data: { data: [], meta: META }, error: undefined })) as never);
    renderAt("acme");

    fireEvent.click(await screen.findByRole("tab", { name: "關聯" }));
    // the row shows src→type→dst; evidence loads lazily behind 查看原文證據
    expect(await screen.findByText(/PRACTICED_BY/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看原文證據" })).toBeInTheDocument();
  });

  it.each([
    ["低信心", { status: "active", confidence: "low" }],
    ["缺證據", { status: "active", evidence: "missing" }],
  ] as const)(
    "the %s tab fetches ACTIVE relations with its facet — BOTH filter halves (GOV2-fe-5)",
    async (label, expectedFilter) => {
      // the facet is ORTHOGONAL to lifecycle: gauge parity comes from the
      // COMBINATION (#109) — a fetch missing filter[status]=active (or the
      // facet) lists a DIFFERENT population than the Health gauge that
      // deep-links here, the exact false-affordance this pin forbids
      const seen: unknown[] = [];
      vi.spyOn(api, "GET").mockImplementation(((path: string, opts: unknown) => {
        if (path === "/projects/{project}/relations") {
          seen.push((opts as { params: { query: { filter: unknown } } }).params.query.filter);
          return Promise.resolve({
            data: { data: [relation({ type: "PRACTICED_BY" })], meta: META },
            error: undefined,
          });
        }
        return Promise.resolve({ data: { data: [], meta: META }, error: undefined });
      }) as never);
      renderAt("acme");

      fireEvent.click(await screen.findByRole("tab", { name: label }));
      expect(await screen.findByText(/PRACTICED_BY/)).toBeInTheDocument();
      expect(seen).toContainEqual(expectedFilter);
    },
  );

  it("reports an un-addressable key instead of firing a doomed request", () => {
    // "a/b" opens in the route (base64url) but can't ride the {project} path
    // segment; the page must report that and the list query must stay disabled
    const get = stubMergeCandidates([]);
    renderAt("a/b");

    expect(screen.getByText(/isn't addressable over the api/i)).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });
});

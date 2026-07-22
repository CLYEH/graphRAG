import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { GovernanceBacklog } from "./GovernanceBacklog";

function renderBacklog(counts: Record<string, number | undefined>) {
  return render(
    <MemoryRouter>
      <GovernanceBacklog counts={counts} />
    </MemoryRouter>,
  );
}

describe("GovernanceBacklog", () => {
  it("deep-links each non-zero review backlog to its governance tab and stays display-only", () => {
    renderBacklog({
      needs_review_entities: 3,
      needs_review_relations: 2,
      pending_ontology_proposals: 1,
    });

    // display-only + honest scope: the build-scoped review counts sit under the
    // 上線中知識庫 group (they describe the ACTIVE build — Codex #107 P2, not a
    // candidate) while the project-wide proposal pool sits under 全專案 (Codex
    // #107 R2); the title says it does not affect activation (never a fake gate)
    expect(screen.getByText("上線中知識庫")).toBeInTheDocument();
    expect(screen.getByText("全專案")).toBeInTheDocument();
    expect(screen.getByText(/不影響上線/)).toBeInTheDocument();
    // pair each label to ITS row's link so a label↔tab swap fails (a plain
    // some()-over-all-hrefs check would pass an entity↔relation swap)
    const linkFor = (label: string) => {
      const row = screen.getByText(new RegExp(label)).closest("li") as HTMLElement;
      return within(row).getByRole("link", { name: "前往處理" }).getAttribute("href");
    };
    expect(linkFor("待審知識點")).toContain("tab=entity");
    expect(linkFor("待審關聯")).toContain("tab=relation");
    expect(linkFor("待審本體提案")).toContain("tab=proposals");
  });

  it("shows proposals under 全專案 only — no live-KB claim when no active build exists (Codex #107 R2)", () => {
    // the no-active-build corner: build-scoped counts are all zero (health.py
    // returns zeros without an active build) but the project-wide proposal pool
    // is non-empty — the panel must NOT claim a live knowledge base exists
    renderBacklog({ pending_ontology_proposals: 4 });

    expect(screen.getByText(/待審本體提案/)).toBeInTheDocument();
    expect(screen.getByText("全專案")).toBeInTheDocument();
    expect(screen.queryByText("上線中知識庫")).not.toBeInTheDocument();
  });

  it("deep-links the relation-quality counts into their gap-list tabs (GOV2-fe-5)", () => {
    // WHY: the #107-era info-only state existed because no list could render
    // these; the #109 facets + GOV2-fe-5 tabs retired it — an operator
    // entering through the Overview must reach the SAME actionable lists the
    // Health page links to (entry-point consistency, class 2)
    renderBacklog({ low_confidence_relations: 149, missing_evidence_relations: 20 });

    expect(screen.getByText(/低信心關聯/)).toBeInTheDocument();
    expect(screen.getByText(/缺證據關聯/)).toBeInTheDocument();
    const links = screen.getAllByRole("link", { name: "前往處理" });
    const hrefs = links.map((l) => l.getAttribute("href") ?? "");
    expect(hrefs.some((h) => h.includes("tab=low-confidence"))).toBe(true);
    expect(hrefs.some((h) => h.includes("tab=missing-evidence"))).toBe(true);
  });

  it("renders nothing when there are no quality signals (never a fake/empty panel)", () => {
    const { container } = renderBacklog({ needs_review_entities: 0, documents: 400 });
    expect(container).toBeEmptyDOMElement();
  });

  it("excludes pending_merge_candidates — merge has its own Overview action card", () => {
    // only the merge backlog is non-zero → the panel surfaces nothing (no dup)
    const { container } = renderBacklog({ pending_merge_candidates: 5 });
    expect(container).toBeEmptyDOMElement();
  });
});

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

    // display-only + honest scope: the copy names the LIVE knowledge base (the
    // counts are §19 active-build scoped — Codex #107 P2, they do NOT describe a
    // candidate build) and says it does not affect activation (never a fake gate)
    expect(screen.getByText(/上線中知識庫/)).toBeInTheDocument();
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

  it("shows the relation-quality counts as info WITHOUT a link (no facet endpoint yet)", () => {
    renderBacklog({ low_confidence_relations: 149, missing_evidence_relations: 20 });

    expect(screen.getByText(/低信心關聯/)).toBeInTheDocument();
    expect(screen.getByText(/缺證據關聯/)).toBeInTheDocument();
    // WHY: these have no list endpoint yet, so a deep-link would land on a page
    // that can't render them — show the count as info, not a false affordance
    expect(screen.queryByRole("link", { name: "前往處理" })).not.toBeInTheDocument();
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

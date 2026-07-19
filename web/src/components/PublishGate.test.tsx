import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { PublishGate } from "./PublishGate";

function renderGate(counts: Record<string, number | undefined>) {
  return render(
    <MemoryRouter>
      <PublishGate counts={counts} />
    </MemoryRouter>,
  );
}

describe("PublishGate", () => {
  it("deep-links each non-zero review backlog to its governance tab and stays display-only", () => {
    renderGate({
      needs_review_entities: 3,
      needs_review_relations: 2,
      pending_ontology_proposals: 1,
    });

    // display-only: the copy says it does not affect activation (never a fake gate)
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
    renderGate({ low_confidence_relations: 149, missing_evidence_relations: 20 });

    expect(screen.getByText(/低信心關聯/)).toBeInTheDocument();
    expect(screen.getByText(/缺證據關聯/)).toBeInTheDocument();
    // WHY: these have no list endpoint yet, so a deep-link would land on a page
    // that can't render them — show the count as info, not a false affordance
    expect(screen.queryByRole("link", { name: "前往處理" })).not.toBeInTheDocument();
  });

  it("renders nothing when there are no quality signals (never a fake/empty gate)", () => {
    const { container } = renderGate({ needs_review_entities: 0, documents: 400 });
    expect(container).toBeEmptyDOMElement();
  });

  it("excludes pending_merge_candidates — merge has its own Overview action card", () => {
    // only the merge backlog is non-zero → the panel surfaces nothing (no dup)
    const { container } = renderGate({ pending_merge_candidates: 5 });
    expect(container).toBeEmptyDOMElement();
  });
});

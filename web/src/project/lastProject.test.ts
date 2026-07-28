import { afterEach, describe, expect, it, vi } from "vitest";

import { readLastProject, resolveLandingProject, writeLastProject } from "./lastProject";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("landing project precedence", () => {
  it("prefers the project the operator last opened over the newest one", () => {
    // WHY (QA9/D17): the API lists projects created_at DESC, so `projects[0]`
    // is the NEWEST — typically the empty shell someone just created. Landing
    // there meant every visit started by switching away from it. The remembered
    // project is what the operator was actually working in.
    writeLastProject("working-corpus");
    const listedNewestFirst = [{ name: "brand-new-shell" }, { name: "working-corpus" }];

    expect(resolveLandingProject(listedNewestFirst)).toBe("working-corpus");
  });

  it("falls back to the list when the remembered project is gone", () => {
    // The stored key is a HINT, not an authority. A project can be deleted
    // (QA7 shipped DELETE /projects) or the storage can be stale from another
    // machine — honouring it blindly would route the operator into a 404 shell,
    // which is worse than the newest-project behaviour this replaces.
    writeLastProject("deleted-since");

    expect(resolveLandingProject([{ name: "acme" }, { name: "beta" }])).toBe("acme");
  });

  it("has no answer when there are no projects", () => {
    // the caller must render the create form, not navigate somewhere
    writeLastProject("acme");

    expect(resolveLandingProject([])).toBeUndefined();
    expect(resolveLandingProject(undefined)).toBeUndefined();
  });

  it("degrades to no preference when storage throws", () => {
    // WHY: localStorage ACCESS throws under Safari private mode and some
    // enterprise policies — not just get/set, the property itself. A Console
    // that fails to render because a convenience feature could not read
    // storage would be a worse bug than the one this fixes.
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });

    expect(readLastProject()).toBeUndefined();
    expect(resolveLandingProject([{ name: "acme" }])).toBe("acme");
  });

  it("does not throw when writing is refused", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });

    expect(() => writeLastProject("acme")).not.toThrow();
  });
});

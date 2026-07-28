import { describe, expect, it } from "vitest";

import { FILTER_THRESHOLD, switcherView } from "./switcherView";

describe("switcher view", () => {
  const many = (n: number) =>
    Array.from({ length: n }, (_, i) => ({ name: `proj-${i}`, display_name: `Corpus ${i}` }));

  it("stops filtering when the list shrinks below the threshold", () => {
    // WHY: the threshold used to gate only the INPUT's rendering, so a typed
    // filter stayed APPLIED while its control was unmounted. Projects shrink
    // out-of-band (DELETE /projects, the CLI — the SPA has no delete entry) and
    // refetchOnWindowFocus re-renders on alt-tab, so the operator came back to
    // a silently narrowed list, possibly showing only the active project plus
    // "沒有符合的專案", with no control to clear it short of a full reload.
    const filter = "Corpus 7";

    const atThreshold = switcherView(many(FILTER_THRESHOLD), filter, "proj-0");
    expect(atThreshold.filtering).toBe(true);
    expect(atThreshold.options.map((p) => p.name)).toEqual(["proj-7", "proj-0"]);

    // one fewer project: the filter must stop applying WITH its control
    const belowThreshold = switcherView(many(FILTER_THRESHOLD - 1), filter, "proj-0");
    expect(belowThreshold.filtering).toBe(false);
    expect(belowThreshold.options).toHaveLength(FILTER_THRESHOLD - 1);
    expect(belowThreshold.noMatch).toBe(false);
  });

  it("keeps the active project listed even when the filter excludes it", () => {
    // a select whose value is absent from its options renders BLANK, which
    // reads as "no project open" rather than "your filter excluded it"
    const view = switcherView(many(10), "Corpus 7", "proj-0");

    expect(view.options.some((p) => p.name === "proj-0")).toBe(true);
    expect(view.options.some((p) => p.name === "proj-3")).toBe(false);
  });

  it("matches the key as well as the display name, anywhere in the string", () => {
    // the label is what the operator sees; the key is what they may know from
    // the API or a URL. A native select's type-ahead matches neither as a
    // substring — only a prefix of the label — which is the gap this closes.
    expect(switcherView(many(10), "proj-4", undefined).options.map((p) => p.name)).toEqual([
      "proj-4",
    ]);
    expect(switcherView(many(10), "us 4", undefined).options.map((p) => p.name)).toEqual([
      "proj-4",
    ]);
  });

  it("reports no match only while it is actually filtering", () => {
    expect(switcherView(many(10), "zzz", "proj-0").noMatch).toBe(true);
    expect(switcherView(many(3), "zzz", "proj-0").noMatch).toBe(false);
    expect(switcherView(many(10), "   ", "proj-0").noMatch).toBe(false);
    expect(switcherView(undefined, "zzz", undefined).noMatch).toBe(false);
  });
});

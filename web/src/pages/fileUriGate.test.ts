/**
 * Why: the Console gate and the source-of-truth gate (core/builds/sources.py
 * _local_path) must accept EXACTLY the same set of source uris. When they drift, a
 * source registered via API/CLI is buildable from one side and permanently unrunnable
 * from the other — the Console marks it unresolvable and blocks EVERY build for the
 * project. Two of the six Codex findings on PR #71 were precisely that drift, and both
 * were invisible to per-side tests: each gate was self-consistent and they disagreed.
 *
 * So parity is enforced mechanically, from one corpus both suites read
 * (tests/fixtures/canonical_file_uri.json — the pytest half is in
 * tests/test_builds_sources.py). A pure differential is blind to a defect the two gates
 * SHARE, so each side keeps its own display==read oracle as well; this is the parity half.
 */
import { describe, expect, it } from "vitest";

// The corpus lives with the backend suite (which also reads it) rather than being
// duplicated here — a second copy could drift, and a drifting parity corpus asserts
// nothing.
import fixture from "../../../tests/fixtures/canonical_file_uri.json";
import { isCanonicalFileUri } from "./Import";

describe("canonical file uri gate (shared corpus with the SoR)", () => {
  it.each(fixture.reject)("rejects $uri — $why", ({ uri }) => {
    expect(isCanonicalFileUri(uri)).toBe(false);
  });

  // Every accept case, INCLUDING the ones the fixture marks "worker": "posix". A browser
  // cannot know the worker's OS, so the Console accepts driveless paths everywhere while
  // the SoR — which resolves the path and sees it is drive-relative — refuses them on a
  // Windows worker. That is the one legitimate asymmetry between the two gates, and it
  // fails in the safe direction (a loud build error, never a silent read of the wrong
  // tree). Asserting the full accept set here is what keeps the Console from silently
  // narrowing to some OS it guessed at.
  it.each(fixture.accept)("accepts $uri — $why", ({ uri }) => {
    expect(isCanonicalFileUri(uri)).toBe(true);
  });
});

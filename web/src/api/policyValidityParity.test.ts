/**
 * Why: isValidPolicyBlock decides whether a project's existing query_policy
 * is a usable spread base for the settings save, but the AUTHORITY on
 * validity is the server's query_policy_from_mapping (frozen jsonschema +
 * the §21 typed re-checks) — the validator behind every query/subgraph 400.
 * When the two drift, a policy the server rejects becomes a spread base the
 * settings PATCH "succeeds" with, and every query keeps 400ing after the UI
 * reported success (Codex #79 R2/R3).
 *
 * So parity is enforced mechanically, from one corpus both suites read
 * (tests/fixtures/query_policy_validity.json — the pytest half,
 * tests/test_query_policy_validity_parity.py, runs the REAL validator; the
 * canonical-file-uri corpus set the pattern). The corpus base must BE the
 * template this page writes — asserted below — so the pytest half doubles as
 * proof that DEFAULT_QUERY_POLICY passes the server it will be handed to.
 */
import { describe, expect, it } from "vitest";

import fixture from "../../../tests/fixtures/query_policy_validity.json";
import { DEFAULT_QUERY_POLICY, isValidPolicyBlock } from "./queries";

type Case = {
  name: string;
  valid: boolean;
  set?: Record<string, unknown>;
  unset?: string[];
  replace?: unknown;
};

function apply(base: Record<string, unknown>, c: Case): unknown {
  if ("replace" in c) return c.replace;
  const doc = structuredClone(base);
  for (const [path, value] of Object.entries(c.set ?? {})) {
    const keys = path.split(".");
    let cur: Record<string, unknown> = doc;
    for (const k of keys.slice(0, -1)) cur = cur[k] as Record<string, unknown>;
    cur[keys[keys.length - 1]] = value;
  }
  for (const path of c.unset ?? []) {
    const keys = path.split(".");
    let cur: Record<string, unknown> = doc;
    for (const k of keys.slice(0, -1)) cur = cur[k] as Record<string, unknown>;
    delete cur[keys[keys.length - 1]];
  }
  return doc;
}

describe("isValidPolicyBlock parity with the server validator", () => {
  it("the corpus base IS the template this page writes", () => {
    // a corpus validating some OTHER document proves nothing about the
    // template the missing/malformed save path actually PATCHes
    expect(fixture.base).toEqual(DEFAULT_QUERY_POLICY);
  });

  for (const c of fixture.cases as Case[]) {
    it(`${c.name} → ${c.valid ? "usable" : "rebuild"}`, () => {
      expect(isValidPolicyBlock(apply(fixture.base as Record<string, unknown>, c))).toBe(c.valid);
    });
  }
});

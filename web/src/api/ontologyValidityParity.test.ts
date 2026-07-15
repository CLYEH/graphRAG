/**
 * Why: isValidOntologyBlock decides whether a project's existing ontology
 * block is repairable-clean or MALFORMED (build-blocking, its corrected form
 * must be saved), but the AUTHORITY on that verdict is the server's
 * load_build_config (core/builds/config.py _load_ontology + TextOntology).
 * When the two drift, a block the server rejects looks clean in the settings
 * form, its repair value never gets written, and every build stays blocked
 * (Codex #79 R4).
 *
 * So parity is enforced mechanically, from one corpus both suites read
 * (tests/fixtures/ontology_block_validity.json — the pytest half,
 * tests/test_ontology_block_validity_parity.py, runs the REAL loader; the
 * query_policy corpus set the pattern). Each case is an ontology BLOCK value
 * (the parity harnesses wrap it under the ontology key); the absent-key legal
 * no-vocabulary state is handled by the caller, not this predicate.
 */
import { describe, expect, it } from "vitest";

import fixture from "../../../tests/fixtures/ontology_block_validity.json";
import { isValidOntologyBlock } from "./queries";

type Case = {
  name: string;
  valid: boolean;
  set?: Record<string, unknown>;
  unset?: string[];
  replace?: unknown;
};

function apply(base: Record<string, unknown>, c: Case): unknown {
  if ("replace" in c) return c.replace;
  const block = structuredClone(base);
  for (const [k, v] of Object.entries(c.set ?? {})) block[k] = v;
  for (const k of c.unset ?? []) delete block[k];
  return block;
}

describe("isValidOntologyBlock parity with the server loader", () => {
  for (const c of fixture.cases as Case[]) {
    it(`${c.name} → ${c.valid ? "valid" : "malformed"}`, () => {
      expect(isValidOntologyBlock(apply(fixture.base as Record<string, unknown>, c))).toBe(c.valid);
    });
  }
});

/**
 * Why: isValidChunkingBlock decides whether a project's existing chunking
 * block is repairable-clean or MALFORMED (build-blocking, its salvaged
 * {max_chars, overlap} must be saved with no dummy knob edit), but the
 * AUTHORITY on that verdict is the server's load_build_config
 * (core/builds/config.py _load_chunking). When the two drift, a block the
 * server rejects looks clean in the settings form, its repair value never gets
 * written, and every build stays blocked (Codex #79 R8).
 *
 * So parity is enforced mechanically, from one corpus both suites read
 * (tests/fixtures/chunking_block_validity.json — the pytest half,
 * tests/test_chunking_block_validity_parity.py, runs the REAL loader; the
 * query_policy / ontology corpora set the pattern). Each case is a chunking
 * BLOCK value (the parity harnesses wrap it under the chunking key); the
 * absent-key legal engine-default state is handled by the caller, not this
 * predicate. The one whole-number-float representation gap is intentionally
 * uncovered — see the fixture description and isValidChunkingBlock.
 */
import { describe, expect, it } from "vitest";

import fixture from "../../../tests/fixtures/chunking_block_validity.json";
import { isValidChunkingBlock } from "./queries";

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

describe("isValidChunkingBlock parity with the server loader", () => {
  for (const c of fixture.cases as Case[]) {
    it(`${c.name} → ${c.valid ? "valid" : "malformed"}`, () => {
      expect(isValidChunkingBlock(apply(fixture.base as Record<string, unknown>, c))).toBe(c.valid);
    });
  }
});

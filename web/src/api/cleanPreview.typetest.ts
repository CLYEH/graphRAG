/**
 * Compile-time pins for the generated CleanPreviewRequest union (contract
 * v1.1, DR-009). These assertions are the CLIENT half of guarantees the API
 * enforces with 400s — they only hold under `strict` (strictNullChecks), so
 * this file is also the tripwire that keeps `strict` on: if it is ever
 * removed from tsconfig, the `@ts-expect-error` lines stop erroring, become
 * "unused" errors themselves, and `tsc -b` fails loud (Codex, #73: without
 * strictNullChecks `{ text: null }` typechecks while the server rejects it).
 *
 * Never imported at runtime; `tsc -b` covers it via the src include, and the
 * vitest glob (*.{test,spec}.ts) does not match *.typetest.ts.
 */
import type { components } from "./schema";

type CleanPreviewRequest = components["schemas"]["CleanPreviewRequest"];

// ---- the accepted shapes must stay assignable (the over-block direction) ----

export const byText: CleanPreviewRequest = { text: "abc" };
export const byTextWithKnobs: CleanPreviewRequest = { text: "abc", max_chars: 100, overlap: 10 };
export const byDocument: CleanPreviewRequest = {
  document_id: "0d9250bd-5b18-45c2-a655-33fdd8b989ea",
};

// ---- the rejected shapes must stay UNassignable ------------------------------

// @ts-expect-error explicit null is not omission — the schema's text is non-null
export const nullText: CleanPreviewRequest = { text: null };

// @ts-expect-error explicit null source beside the real one is rejected too
export const nullBesideReal: CleanPreviewRequest = { text: "abc", document_id: null };

// @ts-expect-error naming both sources fails the exact union (text?: never)
export const bothSources: CleanPreviewRequest = {
  document_id: "0d9250bd-5b18-45c2-a655-33fdd8b989ea",
  text: "abc",
};

// The exact-union guarantee for NON-literals — the case excess-property
// checking cannot catch, which is why the variants carry `?: never` fields.
declare const builtElsewhere: { document_id: string; text: string };
// @ts-expect-error string is not assignable to never on either variant
export const bothViaVariable: CleanPreviewRequest = builtElsewhere;

// @ts-expect-error a sourceless body satisfies neither variant
export const knobsOnly: CleanPreviewRequest = { max_chars: 100 };

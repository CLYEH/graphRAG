import { describe, expect, it } from "vitest";

import { queryBody } from "./queries";

import type { QueryForm } from "./queries";

// queryBody is the crux of the contract⇄runtime divergence: codegen types all
// five modes with one permissive QueryRequest, but each runtime model rejects a
// forbidden field by its PRESENCE — so a key must be OMITTED, never sent as null.
const form = (over: Partial<QueryForm> = {}): QueryForm => ({
  mode: "semantic",
  query: "hi",
  topK: null,
  options: null,
  ...over,
});

describe("queryBody", () => {
  it("semantic/sql/global send query (+ top_k), never options", () => {
    for (const mode of ["semantic", "sql", "global"] as const) {
      expect(queryBody(form({ mode }))).toEqual({ query: "hi" });
      expect(queryBody(form({ mode, topK: 5 }))).toEqual({ query: "hi", top_k: 5 });
    }
  });

  it("graph sends query + options and drops top_k entirely", () => {
    const options = { template: "neighbors", entity: "Ada" } as const;
    // top_k is set in the form but must not reach the graph body (its model forbids it)
    const body = queryBody(form({ mode: "graph", topK: 5, options }));
    expect(body).toEqual({ query: "hi", options });
    expect("top_k" in body).toBe(false);
  });

  it("hybrid carries top_k and options only when provided, and never a null options", () => {
    expect(queryBody(form({ mode: "hybrid" }))).toEqual({ query: "hi" });
    const options = { template: "path", entity: "A", other_entity: "B" } as const;
    expect(queryBody(form({ mode: "hybrid", topK: 3, options }))).toEqual({
      query: "hi",
      top_k: 3,
      options,
    });
    // no graph options → the options key is absent (an explicit null would 400)
    expect("options" in queryBody(form({ mode: "hybrid", topK: 3 }))).toBe(false);
  });

  it("never leaks a forbidden field even if the form carries a stale value", () => {
    // a semantic form that somehow holds graph options must still omit them
    expect(
      "options" in
        queryBody(form({ mode: "semantic", options: { template: "neighbors", entity: "x" } })),
    ).toBe(false);
  });
});

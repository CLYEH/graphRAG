import { describe, expect, it } from "vitest";

// UXB1: the settings page CREATES a policy from DEFAULT_QUERY_POLICY when a
// project has none — and PATCH validates nothing, so a template that drifts
// from contracts/query_policy.schema.json bricks every query with 400
// "invalid" at the worst possible moment (after the operator believes they
// just configured it). This pin reads the FROZEN schema itself (required,
// const, contains, enum — the load-bearing clauses) so the template cannot
// drift without failing here. No ajv: the handful of clause kinds the schema
// uses are checked by hand, against values READ from the schema, not copied.
// The schema is imported from contracts/ itself (the fileUriGate precedent) —
// a duplicated copy could drift, and a drifting pin asserts nothing.
import rawSchema from "../../../contracts/query_policy.schema.json";
import { DEFAULT_QUERY_POLICY } from "./queries";

const schema = rawSchema as unknown as {
  required: string[];
  properties: Record<string, { const?: unknown }>;
  $defs: Record<
    string,
    {
      required: string[];
      properties: Record<string, { const?: unknown; enum?: string[]; minItems?: number }>;
      allOf?: { contains: { const: string } }[];
    }
  >;
};

const template = DEFAULT_QUERY_POLICY as Record<string, unknown>;

describe("DEFAULT_QUERY_POLICY vs the frozen schema", () => {
  it("carries EXACTLY the required top-level fields (additionalProperties is false)", () => {
    expect(Object.keys(template).sort()).toEqual([...schema.required].sort());
  });

  it("matches every top-level const the schema pins", () => {
    for (const [field, def] of Object.entries(schema.properties)) {
      if ("const" in def) expect(template[field]).toBe(def.const);
    }
  });

  it("keeps integer knobs at or above the schema minimum of 1", () => {
    for (const field of ["max_top_k", "max_graph_hops", "max_sql_rows", "max_latency_ms"]) {
      const v = template[field];
      expect(Number.isInteger(v) && (v as number) >= 1, field).toBe(true);
    }
  });

  it("does not default to a mode the same policy disables (the schema allOf)", () => {
    const sql = template["text_to_sql"] as Record<string, unknown>;
    expect(template["default_mode"] !== "sql" || sql["enabled"] === true).toBe(true);
    // and the empty allowed_tables is legal only BECAUSE sql is disabled
    if ((sql["allowed_tables"] as string[]).length === 0) expect(sql["enabled"]).toBe(false);
  });

  for (const [key, blockedField] of [
    ["TextToSql", "blocked_keywords"],
    ["TextToCypher", "blocked"],
  ] as const) {
    const field = key === "TextToSql" ? "text_to_sql" : "text_to_cypher";
    it(`${field}: exact required keys, consts, and the frozen ${blockedField} minimum`, () => {
      const def = schema.$defs[key];
      const block = template[field] as Record<string, unknown>;
      expect(Object.keys(block).sort()).toEqual([...def.required].sort());
      for (const [sub, subDef] of Object.entries(def.properties)) {
        if ("const" in subDef) expect(block[sub], `${field}.${sub}`).toBe(subDef.const);
      }
      // the frozen six-item minimum, read FROM the schema's contains clauses
      // (they live on the blocked-list PROPERTY, not the block definition)
      const blockedDef = def.properties[blockedField] as unknown as {
        allOf?: { contains: { const: string } }[];
      };
      const frozen = (blockedDef.allOf ?? []).map((c) => c.contains.const);
      expect(frozen.length).toBeGreaterThan(0); // the schema still pins a core
      for (const kw of frozen) expect(block[blockedField], kw).toContain(kw);
    });
  }

  it("text_to_cypher: allowed_clauses stays inside the frozen four-clause universe, non-empty", () => {
    const def = schema.$defs["TextToCypher"].properties["allowed_clauses"] as {
      minItems?: number;
      items?: { enum?: string[] };
    };
    const clauses = (template["text_to_cypher"] as Record<string, unknown>)[
      "allowed_clauses"
    ] as string[];
    expect(clauses.length).toBeGreaterThanOrEqual(def.minItems ?? 1);
    for (const c of clauses) expect(def.items?.enum ?? []).toContain(c);
  });
});

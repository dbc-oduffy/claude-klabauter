/**
 * test-initiative-shape.mjs — validator test for initiative.schema.json C1 (target_date shape).
 *
 * Validates that initiative.schema.json accepts both burn-down (target_date: null) and
 * completion (target_date: "YYYY-MM-DD") shapes, and rejects records that violate the schema.
 * Owned by chunk C1 of the initiative-govern-sweep-prioritize plan; do NOT merge into a
 * shared schema-test harness — a peer chunk edits other schemas in parallel.
 *
 * Run with: node coordinator/bin/tests/test-initiative-shape.mjs
 * Exits 0 on all-pass, 1 on any failure.
 *
 * Spec backlink: docs/plans/2026-07-04-initiative-govern-sweep-prioritize-doe-d.md § C1 (AC1, AC2)
 *
 * Negative-spec: schema.js is a CommonJS module; import via createRequire (not static ESM import).
 */

import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import assert from "node:assert/strict";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

// ---------------------------------------------------------------------------
// Load schema.js and the initiative JSON Schema
// ---------------------------------------------------------------------------
const SCHEMA_JS_PATH = path.resolve(__dirname, "../lib/schema.js");
const SCHEMA_PATH = path.resolve(__dirname, "../../schemas/initiative.schema.json");

const { validateRecord } = require(SCHEMA_JS_PATH);
const initiativeSchema = JSON.parse(readFileSync(SCHEMA_PATH, "utf8"));

// ---------------------------------------------------------------------------
// Test harness
// ---------------------------------------------------------------------------
let passed = 0;
let failed = 0;

function pass(label) {
  process.stdout.write(`  PASS  ${label}\n`);
  passed++;
}

function fail(label, detail) {
  process.stderr.write(`  FAIL  ${label}: ${detail}\n`);
  failed++;
}

async function test(label, fn) {
  try {
    await fn();
    pass(label);
  } catch (err) {
    fail(label, err.message ?? String(err));
  }
}

// ---------------------------------------------------------------------------
// Sample records
// ---------------------------------------------------------------------------

/** Valid burn-down shape — target_date: null (ongoing, no fixed end). */
const VALID_BURN_DOWN = {
  id: "coordinator-schema-discipline",
  label: "Coordinator Schema Discipline",
  owner: "coordinator-claude-em",
  status: "active",
  target_date: null
};

/** Valid completion shape — target_date: ISO date string. */
const VALID_COMPLETION = {
  id: "example-fleet-pro-launch",
  label: "Example-Fleet Pro Launch",
  owner: "coordinator-claude-em",
  status: "active",
  target_date: "2026-09-30"
};

/** Valid minimal record — only required fields, no target_date. */
const VALID_MINIMAL = {
  id: "some-initiative",
  label: "Some Initiative",
  status: "active"
};

// ---------------------------------------------------------------------------
// Tests — GREEN PATH
// ---------------------------------------------------------------------------

process.stdout.write("test-initiative-shape.mjs\n");

await test("burn-down shape (target_date: null) returns ok: true", () => {
  const result = validateRecord(VALID_BURN_DOWN, initiativeSchema);
  assert.equal(result.ok, true,
    `Expected ok=true; errors=${JSON.stringify(result.errors ?? null)}`);
});

await test("completion shape (target_date: ISO date string) returns ok: true", () => {
  const result = validateRecord(VALID_COMPLETION, initiativeSchema);
  assert.equal(result.ok, true,
    `Expected ok=true; errors=${JSON.stringify(result.errors ?? null)}`);
});

await test("minimal record (no target_date) returns ok: true — field is optional", () => {
  const result = validateRecord(VALID_MINIMAL, initiativeSchema);
  assert.equal(result.ok, true,
    `Expected ok=true; errors=${JSON.stringify(result.errors ?? null)}`);
});

await test("all four status enum values are accepted", () => {
  for (const status of ["active", "paused", "shipped", "abandoned"]) {
    const rec = { ...VALID_MINIMAL, status };
    const result = validateRecord(rec, initiativeSchema);
    assert.equal(result.ok, true,
      `status="${status}" should be valid; errors=${JSON.stringify(result.errors ?? null)}`);
  }
});

await test("owner: null is valid (nullable — may be unassigned at inception)", () => {
  const rec = { ...VALID_MINIMAL, owner: null };
  const result = validateRecord(rec, initiativeSchema);
  assert.equal(result.ok, true,
    `Expected ok=true; errors=${JSON.stringify(result.errors ?? null)}`);
});

await test("target_date absent is valid (optional field)", () => {
  const { target_date: _dropped, ...rec } = VALID_BURN_DOWN;
  const result = validateRecord(rec, initiativeSchema);
  assert.equal(result.ok, true,
    `Expected ok=true when target_date absent; errors=${JSON.stringify(result.errors ?? null)}`);
});

// ---------------------------------------------------------------------------
// Tests — RED PATH
// ---------------------------------------------------------------------------

await test("missing required id returns ok: false", () => {
  const { id: _dropped, ...rec } = VALID_MINIMAL;
  const result = validateRecord(rec, initiativeSchema);
  assert.equal(result.ok, false, "Expected ok=false when id is missing");
  assert.ok(Array.isArray(result.errors) && result.errors.length > 0,
    "Expected non-empty errors array");
});

await test("missing required label returns ok: false", () => {
  const { label: _dropped, ...rec } = VALID_MINIMAL;
  const result = validateRecord(rec, initiativeSchema);
  assert.equal(result.ok, false, "Expected ok=false when label is missing");
  assert.ok(Array.isArray(result.errors) && result.errors.length > 0,
    "Expected non-empty errors array");
});

await test("missing required status returns ok: false", () => {
  const { status: _dropped, ...rec } = VALID_MINIMAL;
  const result = validateRecord(rec, initiativeSchema);
  assert.equal(result.ok, false, "Expected ok=false when status is missing");
  assert.ok(Array.isArray(result.errors) && result.errors.length > 0,
    "Expected non-empty errors array");
});

await test("invalid status enum value returns ok: false", () => {
  const rec = { ...VALID_MINIMAL, status: "complete" };
  const result = validateRecord(rec, initiativeSchema);
  assert.equal(result.ok, false,
    "Expected ok=false: 'complete' is not in canonical enum (complete≡shipped per DR-207 amendment)");
  assert.ok(Array.isArray(result.errors) && result.errors.length > 0,
    "Expected non-empty errors array");
});

await test("invalid status 'archived' returns ok: false (claude-klabauter Zod has this; canonical does not)", () => {
  const rec = { ...VALID_MINIMAL, status: "archived" };
  const result = validateRecord(rec, initiativeSchema);
  assert.equal(result.ok, false,
    "Expected ok=false: 'archived' is not in canonical enum (claude-klabauter Zod widening is pending — C7 memo)");
  assert.ok(Array.isArray(result.errors) && result.errors.length > 0,
    "Expected non-empty errors array");
});

await test("wrong type for id (number) returns ok: false", () => {
  const rec = { ...VALID_MINIMAL, id: 42 };
  const result = validateRecord(rec, initiativeSchema);
  assert.equal(result.ok, false, "Expected ok=false when id is not a string");
  assert.ok(Array.isArray(result.errors) && result.errors.length > 0,
    "Expected non-empty errors array");
});

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

process.stdout.write(`\n${passed + failed} tests: ${passed} passed, ${failed} failed\n`);

if (failed > 0) {
  process.exit(1);
}

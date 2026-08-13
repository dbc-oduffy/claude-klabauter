/**
 * test-initiative-fk-backlog.mjs — chunk-owned validator test for the nullable `initiative`
 * FK field declared on bug-backlog.yaml, debt-backlog.yaml, improvement-queue.yaml, and
 * roadmap.schema.json (C0 of the initiative govern/sweep/prioritize plan).
 *
 * Validates that:
 *   (a) a backlog entry carrying initiative: <id> (string) passes schema validation
 *   (b) a backlog entry carrying initiative: null passes schema validation
 *   (c) a backlog entry with no initiative field passes schema validation (absent≡null)
 *   (d) a roadmap entry carrying initiative: <id> passes JSON Schema validation
 *   (e) a roadmap entry carrying initiative: null passes JSON Schema validation
 *
 * Run with: node coordinator/bin/tests/test-initiative-fk-backlog.mjs
 * Exits 0 on all-pass, 1 on any failure.
 *
 * Spec backlink: docs/plans/2026-07-04-initiative-govern-sweep-prioritize-doe-d.md § C0 (AC0)
 *
 * Negative-spec: this is a chunk-owned test file. Do NOT add these tests to the shared
 * schema.test.js harness — a peer chunk (C1) edits initiative.schema.json in parallel;
 * a shared harness edit would collide. This file is standalone and uses the same
 * validateFrontmatter / validateRecord entry-points as the shared harness.
 *
 * Negative-spec: schema.js is CommonJS; import via createRequire, not static ESM import.
 */

import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import assert from "node:assert/strict";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

// ---------------------------------------------------------------------------
// Load schema.js and the schemas directory
// ---------------------------------------------------------------------------
const SCHEMA_JS_PATH = path.resolve(__dirname, "../lib/schema.js");
const SCHEMAS_DIR = path.resolve(__dirname, "../../schemas");

const { loadSchemas, validateFrontmatter, validateRecord } = require(SCHEMA_JS_PATH);

// Load all schemas once — same pattern as schema.test.js
const SCHEMAS = loadSchemas(SCHEMAS_DIR);

// ---------------------------------------------------------------------------
// Test harness (same shape as test-session-hierarchy-schema.mjs)
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
// Helpers — minimal valid records for each schema
// ---------------------------------------------------------------------------

/** Minimal valid bug-backlog frontmatter (all required fields). */
function bugBacklogBase(overrides = {}) {
  return Object.assign({
    created: "2026-07-04",
    title: "Test bug",
    body: "Test body",
    status: "open",
    surface: "coordinator/bin/query-records.js",
    severity: "P2",
  }, overrides);
}

/** Minimal valid debt-backlog frontmatter (all required fields). */
function debtBacklogBase(overrides = {}) {
  return Object.assign({
    created: "2026-07-04",
    title: "Test debt",
    body: "Test body",
    status: "open",
    source: "schema-validation",
    risk: "low",
    proposed_action: "Fix it",
  }, overrides);
}

/** Minimal valid improvement-queue frontmatter (all required fields). */
function improvementQueueBase(overrides = {}) {
  return Object.assign({
    created: "2026-07-04",
    title: "Test improvement",
    body: "Test body",
    status: "open",
    surface: "coordinator/bin/query-records.js",
    proposed_action: "Improve it",
    from_repo: "coordinator-claude",
    change_kind: "script-edit",
  }, overrides);
}

/** Minimal valid roadmap frontmatter (all required fields). */
function roadmapBase(overrides = {}) {
  return Object.assign({
    title: "Test Roadmap",
    created: "2026-07-04",
    status: "active",
  }, overrides);
}

// ---------------------------------------------------------------------------
// Tests — bug-backlog schema (YAML dialect)
// ---------------------------------------------------------------------------

process.stdout.write("test-initiative-fk-backlog.mjs\n");
process.stdout.write("\nbug-backlog — YAML dialect\n");

const bugSchema = SCHEMAS["bug-backlog"];
assert.ok(bugSchema, "bug-backlog schema must load");

await test("bug-backlog: initiative field is declared on the schema", () => {
  assert.ok(
    bugSchema.optional && Object.prototype.hasOwnProperty.call(bugSchema.optional, "initiative"),
    `Expected bug-backlog schema to have initiative in optional; got optional keys: ${Object.keys(bugSchema.optional || {}).join(", ")}`
  );
});

await test("bug-backlog: record with initiative: <id> (string) passes", () => {
  const fm = bugBacklogBase({ initiative: "example-fleet-pro-launch" });
  const result = validateFrontmatter(fm, bugSchema);
  assert.equal(result.ok, true,
    `Expected ok=true; errors=${JSON.stringify(result.errors ?? null)}`);
});

await test("bug-backlog: record with initiative: null passes", () => {
  const fm = bugBacklogBase({ initiative: null });
  const result = validateFrontmatter(fm, bugSchema);
  assert.equal(result.ok, true,
    `Expected ok=true for initiative:null; errors=${JSON.stringify(result.errors ?? null)}`);
});

await test("bug-backlog: record without initiative field passes (absent≡null)", () => {
  const fm = bugBacklogBase(); // no initiative field
  const result = validateFrontmatter(fm, bugSchema);
  assert.equal(result.ok, true,
    `Expected ok=true for absent initiative; errors=${JSON.stringify(result.errors ?? null)}`);
});

// ---------------------------------------------------------------------------
// Tests — debt-backlog schema (YAML dialect)
// ---------------------------------------------------------------------------

process.stdout.write("\ndebt-backlog — YAML dialect\n");

const debtSchema = SCHEMAS["debt-backlog"];
assert.ok(debtSchema, "debt-backlog schema must load");

await test("debt-backlog: initiative field is declared on the schema", () => {
  assert.ok(
    debtSchema.optional && Object.prototype.hasOwnProperty.call(debtSchema.optional, "initiative"),
    `Expected debt-backlog schema to have initiative in optional; got optional keys: ${Object.keys(debtSchema.optional || {}).join(", ")}`
  );
});

await test("debt-backlog: record with initiative: <id> (string) passes", () => {
  const fm = debtBacklogBase({ initiative: "coordinator-schema-discipline" });
  const result = validateFrontmatter(fm, debtSchema);
  assert.equal(result.ok, true,
    `Expected ok=true; errors=${JSON.stringify(result.errors ?? null)}`);
});

await test("debt-backlog: record with initiative: null passes", () => {
  const fm = debtBacklogBase({ initiative: null });
  const result = validateFrontmatter(fm, debtSchema);
  assert.equal(result.ok, true,
    `Expected ok=true for initiative:null; errors=${JSON.stringify(result.errors ?? null)}`);
});

await test("debt-backlog: record without initiative field passes (absent≡null)", () => {
  const fm = debtBacklogBase();
  const result = validateFrontmatter(fm, debtSchema);
  assert.equal(result.ok, true,
    `Expected ok=true for absent initiative; errors=${JSON.stringify(result.errors ?? null)}`);
});

// ---------------------------------------------------------------------------
// Tests — improvement-queue schema (YAML dialect)
// ---------------------------------------------------------------------------

process.stdout.write("\nimprovement-queue — YAML dialect\n");

const improvSchema = SCHEMAS["improvement-queue"];
assert.ok(improvSchema, "improvement-queue schema must load");

await test("improvement-queue: initiative field is declared on the schema", () => {
  assert.ok(
    improvSchema.optional && Object.prototype.hasOwnProperty.call(improvSchema.optional, "initiative"),
    `Expected improvement-queue schema to have initiative in optional; got optional keys: ${Object.keys(improvSchema.optional || {}).join(", ")}`
  );
});

await test("improvement-queue: record with initiative: <id> (string) passes", () => {
  const fm = improvementQueueBase({ initiative: "coordinator-schema-discipline" });
  const result = validateFrontmatter(fm, improvSchema);
  assert.equal(result.ok, true,
    `Expected ok=true; errors=${JSON.stringify(result.errors ?? null)}`);
});

await test("improvement-queue: record with initiative: null passes", () => {
  const fm = improvementQueueBase({ initiative: null });
  const result = validateFrontmatter(fm, improvSchema);
  assert.equal(result.ok, true,
    `Expected ok=true for initiative:null; errors=${JSON.stringify(result.errors ?? null)}`);
});

await test("improvement-queue: record without initiative field passes (absent≡null)", () => {
  const fm = improvementQueueBase();
  const result = validateFrontmatter(fm, improvSchema);
  assert.equal(result.ok, true,
    `Expected ok=true for absent initiative; errors=${JSON.stringify(result.errors ?? null)}`);
});

// ---------------------------------------------------------------------------
// Tests — roadmap.schema.json (JSON Schema, validateRecord path)
// ---------------------------------------------------------------------------

process.stdout.write("\nroadmap.schema.json — JSON Schema\n");

const roadmapSchema = SCHEMAS["roadmap"];
assert.ok(roadmapSchema, "roadmap schema must load");

await test("roadmap: initiative property is declared on the schema", () => {
  assert.ok(
    roadmapSchema.properties && Object.prototype.hasOwnProperty.call(roadmapSchema.properties, "initiative"),
    `Expected roadmap schema to have initiative in properties; got: ${Object.keys(roadmapSchema.properties || {}).join(", ")}`
  );
});

await test("roadmap: additionalProperties is explicitly stated as true", () => {
  assert.equal(
    roadmapSchema.additionalProperties, true,
    `Expected roadmap schema additionalProperties=true (explicitly open); got: ${roadmapSchema.additionalProperties}`
  );
});

await test("roadmap: record with initiative: <id> (string) passes", () => {
  const fm = roadmapBase({ initiative: "example-fleet-pro-launch" });
  const result = validateRecord(fm, roadmapSchema);
  assert.equal(result.ok, true,
    `Expected ok=true; errors=${JSON.stringify(result.errors ?? null)}`);
});

await test("roadmap: record with initiative: null passes", () => {
  const fm = roadmapBase({ initiative: null });
  const result = validateRecord(fm, roadmapSchema);
  assert.equal(result.ok, true,
    `Expected ok=true for initiative:null; errors=${JSON.stringify(result.errors ?? null)}`);
});

await test("roadmap: record without initiative field passes (absent≡null)", () => {
  const fm = roadmapBase();
  const result = validateRecord(fm, roadmapSchema);
  assert.equal(result.ok, true,
    `Expected ok=true for absent initiative; errors=${JSON.stringify(result.errors ?? null)}`);
});

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

process.stdout.write(`\n${passed + failed} tests: ${passed} passed, ${failed} failed\n`);

if (failed > 0) {
  process.exit(1);
}

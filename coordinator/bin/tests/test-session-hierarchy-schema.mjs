/**
 * test-session-hierarchy-schema.mjs — validator test for session-hierarchy.schema.json (ccos-5 C1).
 *
 * Validates that the session-hierarchy JSON Schema is accepted by the ccos-1 dual-context
 * validateRecord() from bin/lib/schema.js; asserts green on a valid sample record and
 * red on malformed records.
 *
 * Run with: node plugins/coordinator-claude/coordinator/bin/tests/test-session-hierarchy-schema.mjs
 * Exits 0 on all-pass, 1 on any failure.
 *
 * Spec backlink: docs/plans/2026-06-27-ccos-5-session-workstream-hierarchy-record.md § C1 (AC5)
 *
 * Negative-spec: schema.js is a CommonJS module; import via createRequire (not static ESM import).
 * The validate-cockpit-record.test.mjs pattern uses a separate ESM wrapper; here we call
 * schema.js directly for the shortest path consistent with AC5's gate-bound test requirement.
 */

import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import assert from "node:assert/strict";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

// ---------------------------------------------------------------------------
// Load schema.js and the session-hierarchy JSON Schema
// ---------------------------------------------------------------------------
const SCHEMA_JS_PATH = path.resolve(__dirname, "../lib/schema.js");
const SCHEMA_PATH = path.resolve(__dirname, "../../schemas/session-hierarchy.schema.json");

const { validateRecord } = require(SCHEMA_JS_PATH);
const sessionHierarchySchema = JSON.parse(readFileSync(SCHEMA_PATH, "utf8"));

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

/** Valid complete record — all required fields + optional fields present. */
const VALID_RECORD = {
  session_id: "d964cd5a-f3db-4d10-b906-9b63701ec4a4",
  session_type: "session",
  workstream: "ccos",
  branch: "work/machine-b/2026-06-23to25",
  parent_session_id: "abc00001-0000-0000-0000-000000000001",
  linked_handoffs: ["state/handoffs/2026-06-27_095005_roadmap-ccos-5.md"],
  system: {
    created_by_session: "d964cd5a-f3db-4d10-b906-9b63701ec4a4",
    provenance_completeness: "complete",
    capture_source: "handoff_consumed_by",
    completeness: "complete"
  }
};

/** Valid minimal record — only required fields. */
const VALID_MINIMAL = {
  session_id: "abc00001-0000-0000-0000-000000000001",
  session_type: "workstream",
  workstream: "ccos",
  parent_session_id: null
};

/** Valid partial record — unconsumed handoff, partial completeness. */
const VALID_PARTIAL = {
  session_id: "ffffffff-ffff-ffff-ffff-ffffffffffff",
  session_type: "blitz",
  workstream: "bug-blitz-2026-06",
  linked_handoffs: [],
  system: {
    provenance_completeness: "unknown",
    capture_source: "authored",
    completeness: "partial"
  }
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

process.stdout.write("test-session-hierarchy-schema.mjs\n");

// --- GREEN PATH ---

await test("valid complete record returns ok: true", () => {
  const result = validateRecord(VALID_RECORD, sessionHierarchySchema);
  assert.equal(result.ok, true,
    `Expected ok=true; errors=${JSON.stringify(result.errors ?? null)}`);
});

await test("valid minimal record (required fields only) returns ok: true", () => {
  const result = validateRecord(VALID_MINIMAL, sessionHierarchySchema);
  assert.equal(result.ok, true,
    `Expected ok=true; errors=${JSON.stringify(result.errors ?? null)}`);
});

await test("valid partial-completeness record returns ok: true", () => {
  const result = validateRecord(VALID_PARTIAL, sessionHierarchySchema);
  assert.equal(result.ok, true,
    `Expected ok=true; errors=${JSON.stringify(result.errors ?? null)}`);
});

await test("parent_session_id: null is valid (workstream-root / role-exclusivity limb)", () => {
  const rec = { ...VALID_RECORD, session_type: "workstream", parent_session_id: null };
  const result = validateRecord(rec, sessionHierarchySchema);
  assert.equal(result.ok, true,
    `Expected ok=true; errors=${JSON.stringify(result.errors ?? null)}`);
});

await test("all three session_type enum values are accepted", () => {
  for (const sessionType of ["session", "workstream", "blitz"]) {
    const rec = { ...VALID_MINIMAL, session_type: sessionType };
    const result = validateRecord(rec, sessionHierarchySchema);
    assert.equal(result.ok, true,
      `session_type="${sessionType}" should be valid; errors=${JSON.stringify(result.errors ?? null)}`);
  }
});

await test("both provenance_completeness enum values are accepted", () => {
  for (const val of ["complete", "unknown"]) {
    const rec = {
      ...VALID_RECORD,
      system: { ...VALID_RECORD.system, provenance_completeness: val }
    };
    const result = validateRecord(rec, sessionHierarchySchema);
    assert.equal(result.ok, true,
      `provenance_completeness="${val}" should be valid; errors=${JSON.stringify(result.errors ?? null)}`);
  }
});

await test("all three completeness enum values are accepted", () => {
  for (const val of ["complete", "partial", "unknown"]) {
    const rec = {
      ...VALID_RECORD,
      system: { ...VALID_RECORD.system, completeness: val }
    };
    const result = validateRecord(rec, sessionHierarchySchema);
    assert.equal(result.ok, true,
      `completeness="${val}" should be valid; errors=${JSON.stringify(result.errors ?? null)}`);
  }
});

// --- RED PATH ---

await test("missing required session_id returns ok: false", () => {
  const { session_id: _dropped, ...rec } = VALID_RECORD;
  const result = validateRecord(rec, sessionHierarchySchema);
  assert.equal(result.ok, false, "Expected ok=false when session_id is missing");
  assert.ok(Array.isArray(result.errors) && result.errors.length > 0,
    "Expected non-empty errors array");
});

await test("missing required session_type returns ok: false", () => {
  const { session_type: _dropped, ...rec } = VALID_RECORD;
  const result = validateRecord(rec, sessionHierarchySchema);
  assert.equal(result.ok, false, "Expected ok=false when session_type is missing");
  assert.ok(Array.isArray(result.errors) && result.errors.length > 0,
    "Expected non-empty errors array");
});

await test("missing required workstream returns ok: false", () => {
  const { workstream: _dropped, ...rec } = VALID_RECORD;
  const result = validateRecord(rec, sessionHierarchySchema);
  assert.equal(result.ok, false, "Expected ok=false when workstream is missing");
  assert.ok(Array.isArray(result.errors) && result.errors.length > 0,
    "Expected non-empty errors array");
});

await test("invalid session_type enum value returns ok: false", () => {
  const rec = { ...VALID_RECORD, session_type: "voice" };
  const result = validateRecord(rec, sessionHierarchySchema);
  assert.equal(result.ok, false,
    "Expected ok=false for session_type='voice' (example-voice-system type dropped — no coordinator analog)");
  assert.ok(Array.isArray(result.errors) && result.errors.length > 0,
    "Expected non-empty errors array");
});

await test("invalid provenance_completeness enum returns ok: false", () => {
  const rec = {
    ...VALID_RECORD,
    system: { ...VALID_RECORD.system, provenance_completeness: "partial" }
  };
  const result = validateRecord(rec, sessionHierarchySchema);
  assert.equal(result.ok, false,
    "Expected ok=false: provenance_completeness only allows 'complete' | 'unknown' (not 'partial')");
  assert.ok(Array.isArray(result.errors) && result.errors.length > 0,
    "Expected non-empty errors array");
});

await test("invalid completeness enum returns ok: false", () => {
  const rec = {
    ...VALID_RECORD,
    system: { ...VALID_RECORD.system, completeness: "yes" }
  };
  const result = validateRecord(rec, sessionHierarchySchema);
  assert.equal(result.ok, false,
    "Expected ok=false: completeness='yes' is not in allowed enum");
  assert.ok(Array.isArray(result.errors) && result.errors.length > 0,
    "Expected non-empty errors array");
});

await test("wrong type for session_id (number) returns ok: false", () => {
  const rec = { ...VALID_RECORD, session_id: 12345 };
  const result = validateRecord(rec, sessionHierarchySchema);
  assert.equal(result.ok, false, "Expected ok=false when session_id is not a string");
  assert.ok(Array.isArray(result.errors) && result.errors.length > 0,
    "Expected non-empty errors array");
});

await test("linked_handoffs containing non-string item returns ok: false", () => {
  const rec = { ...VALID_RECORD, linked_handoffs: ["valid/path.md", 42] };
  const result = validateRecord(rec, sessionHierarchySchema);
  assert.equal(result.ok, false,
    "Expected ok=false: linked_handoffs must be array of strings");
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

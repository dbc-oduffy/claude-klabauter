'use strict';
/**
 * test-schema-completion-entry.js — integration smoke test for completion-entry schema.
 *
 * Verifies that bin/lib/schema.js loadSchemas() picks up the new completion-entry schema
 * and that valid/invalid sample entries behave correctly under the validator.
 *
 * Run with: node --test bin/tests/test-schema-completion-entry.js
 *
 * Spec backlink: docs/plans/2026-05-19-completion-log-phase1-foundational-loop.md § Chunk 1
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');

const { loadSchemas, matchSchemaForPath, validateFrontmatter } = require('../lib/schema.js');

const SCHEMAS_DIR = path.resolve(__dirname, '../../schemas');
const SCHEMAS = loadSchemas(SCHEMAS_DIR);

// ---------------------------------------------------------------------------
// Schema discovery
// ---------------------------------------------------------------------------

describe('completion-entry schema — discovery', () => {
  it('loadSchemas picks up completion-entry schema', () => {
    const names = Object.keys(SCHEMAS).filter(k => k !== '_byGlob');
    assert.ok(
      names.includes('completion-entry'),
      `completion-entry schema not found; schemas present: ${names.join(', ')}`
    );
  });

  // completion-entry uses .schema.json (JSON Schema draft-2020-12): required is an array,
  // not a YAML-dialect dict. Assert using Array.includes — the previous `in` operator check
  // was written for the YAML-dialect shape and was stale after the schema migrated to JSON Schema.
  // Bug-backlog closure: state/bug-backlog/2026-07-06-stale-required-optional-field-assertions.yaml
  it('schema has required fields: title, created, nature', () => {
    const schema = SCHEMAS['completion-entry'];
    assert.ok(Array.isArray(schema.required), 'schema.required must be an array (JSON Schema shape)');
    assert.ok(schema.required.includes('title'), 'title not in required');
    assert.ok(schema.required.includes('created'), 'created not in required');
    assert.ok(schema.required.includes('nature'), 'nature not in required');
  });

  // In JSON Schema, optional fields live in `properties` and are absent from `required`.
  // The YAML-dialect `optional:` block no longer exists — the previous assertion against
  // schema.optional was stale after the migration to .schema.json.
  // Bug-backlog closure: state/bug-backlog/2026-07-06-stale-required-optional-field-assertions.yaml
  it('schema has optional fields including nature_inferred', () => {
    const schema = SCHEMAS['completion-entry'];
    assert.ok(schema.properties, 'schema.properties block missing');
    assert.ok('nature_inferred' in schema.properties, 'nature_inferred not in properties');
    // Review: code-reviewer — Finding 4: verify the property node is a well-formed object,
    // not a bare null. A schema author who writes `nature_inferred: null` instead of
    // `nature_inferred: { type: 'boolean' }` would pass a key-presence check while producing
    // a semantically broken schema; this assertion catches it.
    assert.ok(
      schema.properties['nature_inferred'] !== null && typeof schema.properties['nature_inferred'] === 'object',
      'nature_inferred property descriptor must be a non-null object (e.g. {type: "boolean"}), not a bare null'
    );
    assert.ok('chain' in schema.properties, 'chain not in properties');
    assert.ok(
      schema.properties['chain'] !== null && typeof schema.properties['chain'] === 'object',
      'chain property descriptor must be a non-null object, not a bare null'
    );
    assert.ok('status' in schema.properties, 'status not in properties');
    assert.ok(
      schema.properties['status'] !== null && typeof schema.properties['status'] === 'object',
      'status property descriptor must be a non-null object, not a bare null'
    );
    assert.ok('commits' in schema.properties, 'commits not in properties');
    assert.ok(
      schema.properties['commits'] !== null && typeof schema.properties['commits'] === 'object',
      'commits property descriptor must be a non-null object, not a bare null'
    );
    // Confirm these are genuinely optional (not in the required array).
    assert.ok(!schema.required.includes('nature_inferred'), 'nature_inferred should be optional');
    assert.ok(!schema.required.includes('chain'), 'chain should be optional');
    assert.ok(!schema.required.includes('status'), 'status should be optional');
    assert.ok(!schema.required.includes('commits'), 'commits should be optional');
  });

  it('applies_to glob matches archive/completed/YYYY-MM/*.md paths', () => {
    const match = matchSchemaForPath('archive/completed/2026-05/2026-05-19-installer-redesign-abc123.md', SCHEMAS);
    assert.ok(match !== null, 'expected a schema match for completion-entry path');
    assert.equal(match.schemaName, 'completion-entry');
  });

  it('applies_to glob does NOT match flat-root archive/completed/2026-05.md (legacy monolith shape)', () => {
    // Legacy monoliths live at archive/completed/YYYY-MM.md (no subdir) — the glob
    // archive/completed/*/*.md requires TWO path components after the prefix, so flat
    // files at archive/completed/YYYY-MM.md correctly produce no match.
    // Note: archive/completed/legacy/*.md DOES match (legacy/ is the first * segment);
    // exclusion of legacy entries from query results is a bin/query-completions concern,
    // not a schema-glob concern.
    const noMatch = matchSchemaForPath('archive/completed/2026-05.md', SCHEMAS);
    assert.equal(noMatch, null, 'flat legacy monolith path should not match completion-entry glob');
  });
});

// ---------------------------------------------------------------------------
// Valid entry — nature: bugfix, chain: null, status: pending-release
// ---------------------------------------------------------------------------

describe('completion-entry schema — valid entries', () => {
  const schema = SCHEMAS['completion-entry'];

  it('minimal valid entry (bugfix, no chain, pending-release) passes', () => {
    const fm = {
      title: 'Fix session-end crash on empty touched.txt',
      created: '2026-05-19',
      nature: 'bugfix',
    };
    const result = validateFrontmatter(fm, schema);
    assert.ok(result.ok, `Expected ok, got errors: ${JSON.stringify(result.errors)}`);
  });

  it('valid entry with chain: null passes (null permitted on optional chain field)', () => {
    const fm = {
      title: 'Rewrote session-end Step 2.6 for per-entry completion log',
      created: '2026-05-19',
      nature: 'roadmap',
      chain: null,
      status: 'pending-release',
    };
    const result = validateFrontmatter(fm, schema);
    assert.ok(result.ok, `Expected ok, got errors: ${JSON.stringify(result.errors)}`);
  });

  it('valid entry with chain set to a plan path passes', () => {
    const fm = {
      title: 'Implemented completion-entry schema and smoke tests',
      created: '2026-05-19',
      nature: 'infra',
      chain: 'docs/plans/2026-05-19-completion-log-phase1-foundational-loop.md',
      commits: ['abc123def456', '789abcdef012'],
      status: 'pending-release',
      chain_terminal: true,
      nature_inferred: false,
    };
    const result = validateFrontmatter(fm, schema);
    assert.ok(result.ok, `Expected ok, got errors: ${JSON.stringify(result.errors)}`);
  });

  it('all four nature enum values are individually valid', () => {
    const schema = SCHEMAS['completion-entry'];
    for (const nature of ['roadmap', 'bugfix', 'tech-debt', 'infra']) {
      const fm = { title: `Test entry`, created: '2026-05-19', nature };
      const result = validateFrontmatter(fm, schema);
      assert.ok(result.ok, `nature: ${nature} should be valid, got: ${JSON.stringify(result.errors)}`);
    }
  });

  it('valid entry with status: released (post-merge state) passes', () => {
    const fm = {
      title: 'Ship Phase 1 completion log',
      created: '2026-05-19',
      nature: 'roadmap',
      status: 'released',
      released_in: 'v2.1.0',
      released_at: '2026-05-20',
      released_sha: 'deadbeef12345678',
    };
    const result = validateFrontmatter(fm, schema);
    assert.ok(result.ok, `Expected ok, got errors: ${JSON.stringify(result.errors)}`);
  });
});

// ---------------------------------------------------------------------------
// Invalid entries — enum violations, missing required fields
// ---------------------------------------------------------------------------

describe('completion-entry schema — invalid entries', () => {
  const schema = SCHEMAS['completion-entry'];

  it('nature: other (not in enum) fails validation', () => {
    const fm = {
      title: 'Some work',
      created: '2026-05-19',
      nature: 'other',   // invalid — not in [roadmap, bugfix, tech-debt, infra]
    };
    const result = validateFrontmatter(fm, schema);
    assert.equal(result.ok, false, 'Expected validation failure for nature: other');
    const natureErr = result.errors.find(e => e.field === 'nature');
    assert.ok(natureErr, `Expected nature error, got: ${JSON.stringify(result.errors)}`);
    assert.match(natureErr.error, /invalid enum value/);
    assert.match(natureErr.hint, /roadmap/);
  });

  it('missing title fails with required-field error', () => {
    const fm = {
      created: '2026-05-19',
      nature: 'bugfix',
      // title omitted
    };
    const result = validateFrontmatter(fm, schema);
    assert.equal(result.ok, false);
    const err = result.errors.find(e => e.field === 'title');
    assert.ok(err, `Expected title error, got: ${JSON.stringify(result.errors)}`);
    assert.match(err.error, /missing/);
  });

  it('missing created fails with required-field error', () => {
    const fm = {
      title: 'Some work',
      nature: 'bugfix',
      // created omitted
    };
    const result = validateFrontmatter(fm, schema);
    assert.equal(result.ok, false);
    const err = result.errors.find(e => e.field === 'created');
    assert.ok(err, `Expected created error, got: ${JSON.stringify(result.errors)}`);
  });

  it('missing nature fails with required-field error', () => {
    const fm = {
      title: 'Some work',
      created: '2026-05-19',
      // nature omitted
    };
    const result = validateFrontmatter(fm, schema);
    assert.equal(result.ok, false);
    const err = result.errors.find(e => e.field === 'nature');
    assert.ok(err, `Expected nature error, got: ${JSON.stringify(result.errors)}`);
  });

  it('invalid status enum value fails', () => {
    const fm = {
      title: 'Some work',
      created: '2026-05-19',
      nature: 'bugfix',
      status: 'done',    // invalid — not in [pending-release, released]
    };
    const result = validateFrontmatter(fm, schema);
    assert.equal(result.ok, false);
    const err = result.errors.find(e => e.field === 'status');
    assert.ok(err, `Expected status error, got: ${JSON.stringify(result.errors)}`);
    assert.match(err.hint, /pending-release/);
  });

  it('invalid created date format fails', () => {
    const fm = {
      title: 'Some work',
      created: '19-05-2026',  // wrong format
      nature: 'bugfix',
    };
    const result = validateFrontmatter(fm, schema);
    assert.equal(result.ok, false);
    const err = result.errors.find(e => e.field === 'created');
    assert.ok(err, `Expected created date error, got: ${JSON.stringify(result.errors)}`);
    assert.match(err.hint, /YYYY-MM-DD/);
  });
});

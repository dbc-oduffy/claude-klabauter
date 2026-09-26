'use strict';
/**
 * test-provenance-parity.js — cross-package parity guard for ProvenanceEnvelope.
 *
 * PURPOSE
 * Guards against the two independently-authored ProvenanceEnvelope definitions
 * diverging again (the root cause the slice-provenance-envelope plan fixes).
 * Fails with a normalized diff when the shapes disagree semantically.
 *
 * Run with: node bin/tests/test-provenance-parity.js
 *
 * SOURCE A: cockpit-contract/schema/provenance-envelope.schema.json
 *   (root IS the ProvenanceEnvelope shape)
 * SOURCE B: artifact-shape-contract/artifact-shape-contract.schema.json
 *   → $defs.ProvenanceEnvelope
 *
 * NORMALIZATION: strips annotation-only fields (title, description, $schema,
 * $id, $comment, default, format, pattern) so legitimate emitter differences
 * in labelling and datetime format-strings don't false-fail.  Remaining
 * semantic fields (type, enum, anyOf, required, additionalProperties, not, if,
 * then, properties, allOf, …) are compared after recursive key-sort and allOf
 * clause-sort.
 *
 * Spec backlink: docs/plans/2026-07-02-slice-provenance-envelope.md § D4 [DEAD-CITATION: plan file never committed to this repo]
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs     = require('fs');
const path   = require('path');


const COORDINATOR        = path.resolve(__dirname, '../../');
const COCKPIT_SCHEMA     = path.join(COORDINATOR, 'cockpit-contract', 'schema', 'provenance-envelope.schema.json');
const ARTIFACT_SCHEMA    = path.join(COORDINATOR, 'artifact-shape-contract', 'artifact-shape-contract.schema.json');


const STRIP_FIELDS = new Set([
  'title', 'description', '$schema', '$id', '$comment', 'default',
  'format', 'pattern',
  'version',    // schema-document CONTRACT_VERSION stamp — annotation, not envelope shape
]);

/**
 * Recursively strip annotation fields, sort object keys, and sort allOf
 * clauses into a canonical order so two schemas that are semantically
 * identical but were independently authored compare equal.
 *
 * @param {unknown} schema
 * @returns {unknown}
 */
function normalizeSchema(schema) {
  if (schema === null || typeof schema !== 'object' || Array.isArray(schema)) {
    return schema;
  }

  /** @type {Record<string, unknown>} */
  const result = {};

  for (const [key, value] of Object.entries(schema)) {
    if (STRIP_FIELDS.has(key)) continue;

    
    if (key === 'type' && Object.prototype.hasOwnProperty.call(schema, 'enum')) continue;

    if (key === 'allOf' && Array.isArray(value)) {
      
      const normalized = value.map(normalizeSchema);
      result[key] = [...normalized].sort((a, b) =>
        JSON.stringify(a).localeCompare(JSON.stringify(b))
      );
    } else if (key === 'required' && Array.isArray(value)) {
      
      result[key] = [...value].sort();
    } else if (
      key === 'properties' &&
      value !== null &&
      typeof value === 'object' &&
      !Array.isArray(value)
    ) {
      result[key] = Object.fromEntries(
        Object.entries(value).map(([k, v]) => [k, normalizeSchema(v)])
      );
    } else if (key === 'anyOf' && Array.isArray(value)) {
      result[key] = value.map(normalizeSchema);
    } else if (typeof value === 'object' && value !== null) {
      result[key] = normalizeSchema(value);
    } else {
      result[key] = value;
    }
  }

  
  return Object.fromEntries(
    Object.entries(result).sort(([a], [b]) => a.localeCompare(b))
  );
}


describe('ProvenanceEnvelope cross-package parity (D4 drift guard)', () => {
  it('cockpit-contract and artifact-shape-contract ProvenanceEnvelope shapes are semantically equal after normalization', () => {
    
    const cockpitRaw   = JSON.parse(fs.readFileSync(COCKPIT_SCHEMA,  'utf8'));
    const artifactRaw  = JSON.parse(fs.readFileSync(ARTIFACT_SCHEMA, 'utf8'));

    
    const cockpitEnvelope  = cockpitRaw;                              
    const artifactEnvelope = artifactRaw['$defs']?.['ProvenanceEnvelope'];

    assert.ok(
      artifactEnvelope !== undefined,
      `$defs.ProvenanceEnvelope not found in: ${ARTIFACT_SCHEMA}`
    );

    // Normalize both
    const normalizedCockpit  = normalizeSchema(cockpitEnvelope);
    const normalizedArtifact = normalizeSchema(artifactEnvelope);

    const cockpitStr  = JSON.stringify(normalizedCockpit,  null, 2);
    const artifactStr = JSON.stringify(normalizedArtifact, null, 2);

    if (cockpitStr !== artifactStr) {
      console.error('\n=== PARITY FAILURE: ProvenanceEnvelope definitions diverged ===');
      console.error('\n--- cockpit-contract (normalized) ---');
      console.error(cockpitStr);
      console.error('\n--- artifact-shape-contract (normalized) ---');
      console.error(artifactStr);
      console.error('\n=== end parity diff ===\n');

      assert.fail(
        'ProvenanceEnvelope semantic shapes differ between cockpit-contract and artifact-shape-contract.\n' +
        'See normalized diff printed above.  D1/D2 emitters have diverged — fix the source schema, do not weaken this test.'
      );
    }
  });
});

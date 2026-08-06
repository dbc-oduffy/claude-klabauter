'use strict';
/**
 * test-emit-artifact-shape-contract.js — regression guard for the emitter's output shape.
 *
 * PURPOSE
 * Locks the current CONTRACT_VERSION shape of artifact-shape-contract.schema.json so that any
 * future in-place change (version bump omitted, $defs widened, queue keys dropped)
 * is caught immediately.  The shape was silently changed during the example-initiative reshape
 * (queue/$def additions, source_memo union, nullable chain fields) without a version
 * bump — this test prevents a recurrence.
 *
 * Run with: node --test bin/tests/test-emit-artifact-shape-contract.js
 *
 * Spec backlink: archive/specs/2026-06/2026-06-25-example-initiative-tc-4-fleet-machinery-contract-emit.md § B1
 */

const { describe, it, before, after } = require('node:test');
const assert = require('node:assert/strict');
const fs     = require('fs');
const os     = require('os');
const path   = require('path');
const { execFileSync } = require('child_process');

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

const COORDINATOR = path.resolve(__dirname, '../../');
const EMITTER     = path.join(COORDINATOR, 'bin', 'emit-artifact-shape-contract.js');

// ---------------------------------------------------------------------------
// Shared state: temp dir + parsed bundle
// ---------------------------------------------------------------------------

let tmpDir;
let bundle;

before(() => {
  // Create a temp dir and run the emitter into it.
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'test-artifact-contract-'));
  execFileSync(process.execPath, [EMITTER], {
    env: { ...process.env, ARTIFACT_CONTRACT_OUT_DIR: tmpDir },
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  const outFile = path.join(tmpDir, 'artifact-shape-contract.schema.json');
  bundle = JSON.parse(fs.readFileSync(outFile, 'utf8'));
});

after(() => {
  // Clean up — idempotent; ignore errors if already removed.
  if (tmpDir) {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// Assertion 1 — version pin
// ---------------------------------------------------------------------------

describe('artifact-shape-contract emitter — version pin', () => {
  it('top-level version field is "1.19.0"', () => {
    assert.equal(
      bundle.version,
      '1.19.0',
      `Expected version "1.19.0", got "${bundle.version}". ` +
      'A shape change happened without a version bump — update the CONTRACT_VERSION ' +
      'constant in emit-artifact-shape-contract.js and re-pin this assertion.'
    );
  });
});

// ---------------------------------------------------------------------------
// Assertion 2 — source_memo union shape
// ---------------------------------------------------------------------------

describe('artifact-shape-contract emitter — source_memo union', () => {
  it('$defs.plan.properties.source_memo is an anyOf [string | array-of-string]', () => {
    const planDef = bundle['$defs'] && bundle['$defs']['plan'];
    assert.ok(planDef, '$defs.plan is absent from the contract');
    const sourceMemo = planDef.properties && planDef.properties['source_memo'];
    assert.ok(sourceMemo, '$defs.plan.properties.source_memo is absent');

    // source_memo is in plan.schema.json which is a full JSON Schema — the description
    // field is carried through verbatim. Check the structural shape (anyOf union) rather
    // than exact equality so the description text doesn't make this brittle.
    assert.ok(
      Array.isArray(sourceMemo['anyOf']) && sourceMemo['anyOf'].length === 2,
      'source_memo must be an anyOf union with 2 members. ' +
      `Got: ${JSON.stringify(sourceMemo)}`
    );
    assert.deepStrictEqual(
      sourceMemo['anyOf'],
      [
        { type: 'string' },
        { type: 'array', items: { type: 'string' } },
      ],
      'source_memo anyOf members do not match the expected string|array-of-string union. ' +
      `Got: ${JSON.stringify(sourceMemo['anyOf'])}`
    );
  });
});

// ---------------------------------------------------------------------------
// Assertion 3 — queue/outbox $defs present with x-coordinator-applies_to
// ---------------------------------------------------------------------------

describe('artifact-shape-contract emitter — queue/outbox $defs', () => {
  const REQUIRED_QUEUE_DEFS = [
    'improvement-queue',
    'bug-backlog',
    'debt-backlog',
    'lessons-outbox',
  ];

  for (const defName of REQUIRED_QUEUE_DEFS) {
    it(`$defs["${defName}"] is present and carries x-coordinator-applies_to`, () => {
      const def = bundle['$defs'] && bundle['$defs'][defName];
      assert.ok(
        def,
        `$defs["${defName}"] is absent — was a schema file removed or renamed?`
      );
      assert.ok(
        Object.prototype.hasOwnProperty.call(def, 'x-coordinator-applies_to'),
        `$defs["${defName}"] lacks x-coordinator-applies_to — the emitter stopped ` +
        'carrying the applies_to extension for this schema'
      );
    });
  }
});

// ---------------------------------------------------------------------------
// Assertion 4 — no [emit-note] markers (unrecognised-field sentinel)
// ---------------------------------------------------------------------------

describe('artifact-shape-contract emitter — emitter faithfulness', () => {
  it('serialized JSON contains zero occurrences of the "[emit-note]" sentinel', () => {
    const json = JSON.stringify(bundle);
    const occurrences = (json.match(/\[emit-note\]/g) || []).length;
    assert.equal(
      occurrences,
      0,
      `Found ${occurrences} occurrence(s) of "[emit-note]" in the emitted contract. ` +
      'This means one or more schema fields used an unrecognised descriptor type — ' +
      'check emit-artifact-shape-contract.js fieldToJsonSchema() and the relevant schema YAML.'
    );
  });
});

// ---------------------------------------------------------------------------
// Assertion 5 — routing_strategy field pin
// ---------------------------------------------------------------------------

describe('artifact-shape-contract emitter — routing_strategy pin', () => {
  // Lock the load-bearing routing_strategy field so a future accidental removal
  // or value change is caught immediately. Mirrors the version-pin assertion style
  // in Assertion 1.
  it('top-level routing_strategy field is "most-specific-glob-wins"', () => {
    assert.equal(
      bundle.routing_strategy,
      'most-specific-glob-wins',
      `Expected routing_strategy "most-specific-glob-wins", got "${bundle.routing_strategy}". ` +
      'This field documents the glob dispatch strategy for consumers — update ' +
      'emit-artifact-shape-contract.js and re-pin this assertion if the strategy changes.'
    );
  });
});

// ---------------------------------------------------------------------------
// Assertion 6 — review-integration-record permissive container
// ---------------------------------------------------------------------------

describe('artifact-shape-contract emitter — review-integration-record permissive container', () => {
  // Description reflects both permissive axes (no required key, additionalProperties not false).
  it('$defs["review-integration-record"] exists, has correct applies_to, and is permissive (no required key, additionalProperties not false)', () => {
    const def = bundle['$defs'] && bundle['$defs']['review-integration-record'];
    assert.ok(
      def,
      '$defs["review-integration-record"] is absent — was the schema file added to schemas/?'
    );
    assert.equal(
      def['x-coordinator-applies_to'],
      'state/review-trail/*-integration.json',
      `x-coordinator-applies_to mismatch: got "${def['x-coordinator-applies_to']}"`
    );
    assert.ok(
      !Object.prototype.hasOwnProperty.call(def, 'required'),
      'review-integration-record must be a PERMISSIVE container (no required key). ' +
      'Remove the required: block from schemas/review-integration-record.yaml.'
    );
    // Assert additionalProperties is NOT false so a future strict-mode addition cannot
    // silently make this schema non-permissive while passing the "no required key" check above.
    assert.notStrictEqual(
      def['additionalProperties'],
      false,
      'review-integration-record must not set additionalProperties:false — must stay permissive for arbitrary EM-hand-authored blobs.'
    );
  });
});

// ---------------------------------------------------------------------------
// Assertion 7 — review-integration-record over-typed-properties relaxation lock
// (2026-06-26 fix: 14/25 real records false-failed due to typed optional properties)
// ---------------------------------------------------------------------------

describe('artifact-shape-contract emitter — review-integration-record relaxation lock', () => {
  it('rationale property has no "type" key (permissive-by-design, was string → any)', () => {
    const def = bundle['$defs'] && bundle['$defs']['review-integration-record'];
    assert.ok(def, '$defs["review-integration-record"] is absent');
    const rationale = def.properties && def.properties['rationale'];
    assert.ok(rationale, '$defs["review-integration-record"].properties.rationale is absent');
    assert.ok(
      !('type' in rationale),
      'rationale must have NO "type" key — it is permissive-by-design (any) per the ' +
      '2026-06-26 over-typed-properties fix. Real records write rationale as a per-finding object. ' +
      'If this fails, revert rationale back to `any` in schemas/review-integration-record.yaml.'
    );
  });

  it('findings_received property has no "type" key (permissive-by-design, was number → any)', () => {
    const def = bundle['$defs'] && bundle['$defs']['review-integration-record'];
    assert.ok(def, '$defs["review-integration-record"] is absent');
    const findingsReceived = def.properties && def.properties['findings_received'];
    assert.ok(findingsReceived, '$defs["review-integration-record"].properties.findings_received is absent');
    assert.ok(
      !('type' in findingsReceived),
      'findings_received must have NO "type" key — it is permissive-by-design (any) per the ' +
      '2026-06-26 over-typed-properties fix. Real records write this field as an id/path array. ' +
      'If this fails, revert findings_received back to `any` in schemas/review-integration-record.yaml.'
    );
  });

  it('artifact property is an anyOf union (string | array-of-string)', () => {
    const def = bundle['$defs'] && bundle['$defs']['review-integration-record'];
    assert.ok(def, '$defs["review-integration-record"] is absent');
    const artifact = def.properties && def.properties['artifact'];
    assert.ok(artifact, '$defs["review-integration-record"].properties.artifact is absent');
    assert.ok(
      Array.isArray(artifact['anyOf']),
      'artifact must be an anyOf union (string | array-of-string) per the ' +
      '2026-06-26 over-typed-properties fix. Real records write artifact as a list of paths. ' +
      `Got: ${JSON.stringify(artifact)}. ` +
      'If this fails, revert artifact back to `string-or-list-of-string` in schemas/review-integration-record.yaml.'
    );
  });
});

// ---------------------------------------------------------------------------
// Assertion 9 — deliverable-spine identity fields on handoff, plan, completion-entry
// and initiative $def present (1.11.0 C2 deliverable-spine identity chunk)
// ---------------------------------------------------------------------------

describe('artifact-shape-contract emitter — deliverable-spine identity fields (1.11.0 C2)', () => {
  // handoff: deliverable_id, initiative, caption, status_reason, owner
  for (const field of ['deliverable_id', 'initiative', 'caption', 'status_reason', 'owner']) {
    it(`$defs.handoff.properties.${field} is present and nullable (anyOf string/null)`, () => {
      const def = bundle['$defs'] && bundle['$defs']['handoff'];
      assert.ok(def, '$defs.handoff is absent');
      const prop = def.properties && def.properties[field];
      assert.ok(prop, `$defs.handoff.properties.${field} is absent — deliverable-spine field not added to handoff.schema.json`);
      assert.ok(
        Array.isArray(prop['anyOf']) && prop['anyOf'].length === 2,
        `$defs.handoff.properties.${field} must be anyOf [string, null]. Got: ${JSON.stringify(prop)}`
      );
    });
  }

  // plan: deliverable_id, plan_id, initiative, caption, status_reason, owner
  for (const field of ['deliverable_id', 'plan_id', 'initiative', 'caption', 'status_reason', 'owner']) {
    it(`$defs.plan.properties.${field} is present and nullable (anyOf string/null)`, () => {
      const def = bundle['$defs'] && bundle['$defs']['plan'];
      assert.ok(def, '$defs.plan is absent');
      const prop = def.properties && def.properties[field];
      assert.ok(prop, `$defs.plan.properties.${field} is absent — deliverable-spine field not added to plan.schema.json`);
      assert.ok(
        Array.isArray(prop['anyOf']) && prop['anyOf'].length === 2,
        `$defs.plan.properties.${field} must be anyOf [string, null]. Got: ${JSON.stringify(prop)}`
      );
    });
  }

  // completion-entry: deliverable_id, initiative
  for (const field of ['deliverable_id', 'initiative']) {
    it(`$defs.completion-entry.properties.${field} is present and nullable (anyOf string/null)`, () => {
      const def = bundle['$defs'] && bundle['$defs']['completion-entry'];
      assert.ok(def, '$defs.completion-entry is absent');
      const prop = def.properties && def.properties[field];
      assert.ok(prop, `$defs.completion-entry.properties.${field} is absent — deliverable-spine field not added to completion-entry.schema.json`);
      assert.ok(
        Array.isArray(prop['anyOf']) && prop['anyOf'].length === 2,
        `$defs.completion-entry.properties.${field} must be anyOf [string, null]. Got: ${JSON.stringify(prop)}`
      );
    });
  }

  // initiative $def: present, with required fields, and carries x-coordinator-applies_to
  it('$defs.initiative is present with required [id, label, status] and x-coordinator-applies_to', () => {
    const def = bundle['$defs'] && bundle['$defs']['initiative'];
    assert.ok(def, '$defs.initiative is absent — initiative.schema.json not added to schemas/');
    assert.ok(
      Object.prototype.hasOwnProperty.call(def, 'x-coordinator-applies_to'),
      '$defs.initiative must carry x-coordinator-applies_to (state/initiatives/*.yaml)'
    );
    assert.equal(
      def['x-coordinator-applies_to'],
      'state/initiatives/*.yaml',
      `$defs.initiative.x-coordinator-applies_to must be "state/initiatives/*.yaml". Got: "${def['x-coordinator-applies_to']}"`
    );
    assert.ok(Array.isArray(def.required), '$defs.initiative.required must be an array');
    assert.deepStrictEqual(
      [...def.required].sort(),
      ['id', 'label', 'status'],
      `$defs.initiative.required must be [id, label, status]. Got: ${JSON.stringify(def.required)}`
    );
  });

  it('$defs.initiative.properties.owner is nullable (anyOf string/null)', () => {
    const def = bundle['$defs'] && bundle['$defs']['initiative'];
    assert.ok(def, '$defs.initiative is absent');
    const owner = def.properties && def.properties['owner'];
    assert.ok(owner, '$defs.initiative.properties.owner is absent');
    assert.ok(
      Array.isArray(owner['anyOf']) && owner['anyOf'].length === 2,
      `$defs.initiative.properties.owner must be anyOf [string, null]. Got: ${JSON.stringify(owner)}`
    );
  });
});

// ---------------------------------------------------------------------------
// Assertion 8 — ProvenanceEnvelope sub-shape present, correctly shaped, uncounted
// ---------------------------------------------------------------------------

describe('artifact-shape-contract emitter — ProvenanceEnvelope sub-shape', () => {
  it('$defs.ProvenanceEnvelope exists with all 7 required fields (ref + entity_anchor present-as-null, D9 + 2026-07-14 identity-space freeze) and no applies_to', () => {
    const def = bundle['$defs'] && bundle['$defs']['ProvenanceEnvelope'];
    assert.ok(def, '$defs.ProvenanceEnvelope is absent — the injected sub-shape was dropped from the emitter.');
    assert.deepStrictEqual(
      [...def.required].sort(),
      ['derivation', 'entity_anchor', 'observed_at', 'path', 'ref', 'repo', 'source_kind'],
      'ProvenanceEnvelope.required must include ref and entity_anchor (both present-as-null): source_kind,repo,ref,path,observed_at,derivation,entity_anchor. ' +
      `Got: ${JSON.stringify(def.required)}`
    );
    assert.ok(
      !Object.prototype.hasOwnProperty.call(def, 'x-coordinator-applies_to'),
      'ProvenanceEnvelope is a sub-shape, not an artifact type — it must NOT carry x-coordinator-applies_to.'
    );
    assert.equal(def.additionalProperties, false, 'ProvenanceEnvelope must be a closed shape (additionalProperties:false).');
  });

  // Lock the present-as-null + nullable-object ref semantic (D9): ref is always present, either
  // a {branch,sha} object (github_*) or null (local_fs/coordinator_artifact).
  it('$defs.ProvenanceEnvelope.properties.ref is an anyOf [closed-object | null] (D9 present-as-null)', () => {
    const def = bundle['$defs'] && bundle['$defs']['ProvenanceEnvelope'];
    assert.ok(def, '$defs.ProvenanceEnvelope is absent');
    const ref = def.properties && def.properties['ref'];
    assert.ok(ref, '$defs.ProvenanceEnvelope.properties.ref is absent — the ref sub-shape was dropped from SUB_SHAPES.');
    assert.ok(
      Array.isArray(ref.anyOf) && ref.anyOf.length === 2,
      'ProvenanceEnvelope.properties.ref must be an anyOf union with 2 members (object | null). ' +
      `Got: ${JSON.stringify(ref)}`
    );
    // First member: closed object with required branch+sha
    const objMember = ref.anyOf[0];
    assert.equal(objMember.type, 'object', 'ref.anyOf[0].type must be "object"');
    assert.equal(objMember.additionalProperties, false, 'ref.anyOf[0] must be a closed object (additionalProperties:false)');
    assert.deepStrictEqual(
      [...objMember.required].sort(),
      ['branch', 'sha'],
      `ref.anyOf[0].required must be exactly [branch, sha]. Got: ${JSON.stringify(objMember.required)}`
    );
    // Second member: null
    assert.equal(ref.anyOf[1].type, 'null', 'ref.anyOf[1].type must be "null" (present-as-null arm)');
  });

  // Lock the entity_anchor present-as-null shape (2026-07-14 identity-space freeze, model A):
  // entity_anchor is always present, either a {kind,value} object (entity-anchored fact) or
  // null (repo-anchored fact) — mirrors the ref present-as-null semantic above.
  it('$defs.ProvenanceEnvelope.properties.entity_anchor is an anyOf [closed-object | null] (present-as-null)', () => {
    const def = bundle['$defs'] && bundle['$defs']['ProvenanceEnvelope'];
    assert.ok(def, '$defs.ProvenanceEnvelope is absent');
    const entityAnchor = def.properties && def.properties['entity_anchor'];
    assert.ok(entityAnchor, '$defs.ProvenanceEnvelope.properties.entity_anchor is absent — the entity_anchor sub-shape was dropped from SUB_SHAPES.');
    assert.ok(
      Array.isArray(entityAnchor.anyOf) && entityAnchor.anyOf.length === 2,
      'ProvenanceEnvelope.properties.entity_anchor must be an anyOf union with 2 members (object | null). ' +
      `Got: ${JSON.stringify(entityAnchor)}`
    );
    // First member: closed object with required kind+value
    const objMember = entityAnchor.anyOf[0];
    assert.equal(objMember.type, 'object', 'entity_anchor.anyOf[0].type must be "object"');
    assert.equal(objMember.additionalProperties, false, 'entity_anchor.anyOf[0] must be a closed object (additionalProperties:false)');
    assert.deepStrictEqual(
      [...objMember.required].sort(),
      ['kind', 'value'],
      `entity_anchor.anyOf[0].required must be exactly [kind, value]. Got: ${JSON.stringify(objMember.required)}`
    );
    // Second member: null
    assert.equal(entityAnchor.anyOf[1].type, 'null', 'entity_anchor.anyOf[1].type must be "null" (present-as-null arm)');
  });

  // Lock the D9 bidirectional conditional: github_* → ref non-null; local_fs/coordinator_artifact → ref null.
  it('$defs.ProvenanceEnvelope has allOf with 4 if/then conditionals (D9 bidirectional ref + 2026-07-14 anchorless + well-formedness guards)', () => {
    const def = bundle['$defs'] && bundle['$defs']['ProvenanceEnvelope'];
    assert.ok(def, '$defs.ProvenanceEnvelope is absent');
    assert.ok(
      Array.isArray(def.allOf) && def.allOf.length === 4,
      'ProvenanceEnvelope must have an allOf array with exactly 4 if/then entries (D9 bidirectional conditional + anchorless guard + well-formedness guard). ' +
      `Got allOf: ${JSON.stringify(def.allOf)}`
    );
    // (i) github_* → ref must not be null
    const clause0 = def.allOf[0];
    assert.deepStrictEqual(
      clause0.if.properties.source_kind.enum.sort(),
      ['git_commit', 'github_graphql', 'github_rest'],
      `allOf[0].if must guard github_graphql+github_rest+git_commit. Got: ${JSON.stringify(clause0.if)}`
    );
    assert.deepStrictEqual(
      clause0.then.properties.ref,
      { not: { type: 'null' } },
      `allOf[0].then must require ref to be non-null. Got: ${JSON.stringify(clause0.then)}`
    );
    // (ii) local_fs/coordinator_artifact/transcript_summary/sec_edgar/code_comparison → ref must be null
    const clause1 = def.allOf[1];
    assert.deepStrictEqual(
      clause1.if.properties.source_kind.enum.sort(),
      ['code_comparison', 'coordinator_artifact', 'local_fs', 'sec_edgar', 'transcript_summary'],
      `allOf[1].if must guard local_fs+coordinator_artifact+transcript_summary+sec_edgar+code_comparison. Got: ${JSON.stringify(clause1.if)}`
    );
    assert.deepStrictEqual(
      clause1.then.properties.ref,
      { type: 'null' },
      `allOf[1].then must require ref to be null. Got: ${JSON.stringify(clause1.then)}`
    );
    // (iii) identity-space-freeze anchorless guard (2026-07-14, model A): repo === "" →
    // entity_anchor must be a non-null object with a non-empty value.
    const clause2 = def.allOf[2];
    assert.deepStrictEqual(
      clause2.if,
      { properties: { repo: { const: '' } }, required: ['repo'] },
      `allOf[2].if must guard repo === "". Got: ${JSON.stringify(clause2.if)}`
    );
    assert.deepStrictEqual(
      clause2.then.required,
      ['entity_anchor'],
      `allOf[2].then must require entity_anchor. Got: ${JSON.stringify(clause2.then)}`
    );
    assert.deepStrictEqual(
      clause2.then.properties.entity_anchor.type,
      'object',
      `allOf[2].then.properties.entity_anchor must require type object (non-null). Got: ${JSON.stringify(clause2.then.properties.entity_anchor)}`
    );
    assert.deepStrictEqual(
      clause2.then.properties.entity_anchor.properties.value,
      { not: { const: '' } },
      `allOf[2].then.properties.entity_anchor.properties.value must forbid empty string. Got: ${JSON.stringify(clause2.then.properties.entity_anchor.properties.value)}`
    );
    // (iv) well-formedness guard, UNCONDITIONAL on repo (2026-07-14 hardening): entity_anchor,
    // when present as a non-null object, must have both kind and value non-empty — regardless of
    // whether repo also anchors the fact (closes the malformed-composite hole clause (iii) alone
    // leaves open: real repo + entity_anchor:{kind:"",value:""} previously passed vacuously).
    const clause3 = def.allOf[3];
    assert.deepStrictEqual(
      clause3.if,
      { properties: { entity_anchor: { type: 'object' } }, required: ['entity_anchor'] },
      `allOf[3].if must guard entity_anchor being a non-null object. Got: ${JSON.stringify(clause3.if)}`
    );
    assert.deepStrictEqual(
      clause3.then.properties.entity_anchor.properties.kind,
      { not: { const: '' } },
      `allOf[3].then.properties.entity_anchor.properties.kind must forbid empty string. Got: ${JSON.stringify(clause3.then.properties.entity_anchor.properties.kind)}`
    );
    assert.deepStrictEqual(
      clause3.then.properties.entity_anchor.properties.value,
      { not: { const: '' } },
      `allOf[3].then.properties.entity_anchor.properties.value must forbid empty string. Got: ${JSON.stringify(clause3.then.properties.entity_anchor.properties.value)}`
    );
  });

  it('ProvenanceEnvelope is NOT counted in schema_count (54 artifact types — percolate-store + platform-outcome added post-52)', () => {
    // NOTE: initiative.schema.json was added in 1.11.0 (deliverable-spine identity chunk C2),
    // bringing the artifact-type count from 43 to 44. goal.schema.json was added afterward
    // (goal-setting-okr phase1 C1), bringing it to 45. workstream.schema.json and
    // workstream-event.schema.json were added afterward (project-tracker-render-from-queue C2,
    // commit e4659d63), bringing it to 47. plan-tasks.schema.json was added afterward
    // (plan-full-coverage-and-deferred-harvest C1) — it registers as its own top-level
    // schema (the dual-format loader in bin/lib/schema.js counts every schemas/*.schema.json
    // file regardless of applies_to presence; plan-tasks.schema.json deliberately carries no
    // applies_to since it is a $ref'd sub-schema of plan.schema.json's `tasks` items, not a
    // directly-authored artifact glob), bringing the count to 48. cross-repo-commitment.yaml
    // (commit 631d3108) and strategic-self-description.schema.json (commit 017da24b) were
    // both added to schemas/ afterward but the bundle was never re-emitted to pick them up
    // until the 1.15.0 sec_edgar widen's regeneration incidentally re-synced this pre-existing
    // source/output drift (same class of incidental re-sync the 1.14.0 transcript_summary
    // widen's commit message called out) — bringing the count to 50. capability-manifest.schema.json
    // and fleet-capability-index.schema.json (commit e22d582f, "C1-capability-lens") were added
    // afterward — each carries its own distinct `applies_to` glob (state/capabilities/manifest.json
    // and state/capabilities/fleet-index.json respectively), confirming two new artifact types,
    // not a double-registration — but again the committed bundle went un-regenerated until this
    // fix, bringing the count to 52. percolate-store.schema.json (commit cc2780d6) and
    // platform-outcome.schema.json (commit 3261e47b) were added afterward — neither declares
    // applies_to (permitted; mirrors the plan-tasks.schema.json no-applies_to precedent above),
    // but each is still a distinct top-level schemas/ entry, not a double-registration — and again
    // the committed bundle went un-regenerated until this fix, bringing the count to 54. The
    // invariant this test guards is that ProvenanceEnvelope (the injected sub-shape) is NOT
    // counted on top of that — schema_count reflects only schemas/*.yaml|.schema.json artifact
    // types (54), not 55.
    assert.equal(
      bundle.schema_count,
      54,
      `schema_count must be 54 (artifact-type count) — the injected sub-shape must not increment it. Got ${bundle.schema_count}.`
    );
  });
});

// ---------------------------------------------------------------------------
// Assertion 10 — committed-vs-fresh drift guard
// ---------------------------------------------------------------------------
// PURPOSE: Catches the class of failure where a merge or manual edit leaves the
// committed artifact-shape-contract.schema.json out of sync with its source
// schemas/*.yaml — a scenario where all other assertions stay green (they test
// the FRESH emit, not the committed file) but the committed artifact is stale.
//
// Real incident: committed carried 45 $defs while a fresh emit produced 43;
// invisible to prior tests.
//
// COMPARISON STRATEGY: byte-match (strongest).
//   Determinism verified 2026-07-06: two independent ARTIFACT_CONTRACT_OUT_DIR
//   runs produce byte-identical output. No timestamps or non-deterministic
//   fields in the emitted JSON (emitted_at was deliberately dropped per the
//   "emitted_at dropped" comment in emit-artifact-shape-contract.js). A full
//   byte comparison is therefore safe and catches every possible drift, including
//   field reordering, whitespace changes, and value mutations.
//
//   If byte-match were ever rejected (e.g. the emitter gained a timestamp field),
//   fall back to: compare version + schema_count + sorted Object.keys($defs) +
//   each $defs entry via JSON.stringify. Document the reason byte-match was
//   rejected in a comment here.
//
// Spec backlink: cross-repo/inbox/2026-07-05-committed-contract-emit-drift-guard.md
// ---------------------------------------------------------------------------

const COMMITTED_ARTIFACT = path.join(COORDINATOR, 'artifact-shape-contract', 'artifact-shape-contract.schema.json');

describe('artifact-shape-contract emitter — committed-vs-fresh drift guard', () => {
  it('committed artifact-shape-contract.schema.json matches a fresh emit byte-for-byte', () => {
    // Emit fresh to a dedicated temp dir for this guard (separate from the shared
    // tmpDir used by the shape assertions above, to avoid cross-test interference).
    const driftTmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'test-artifact-drift-'));
    try {
      execFileSync(process.execPath, [EMITTER], {
        env: { ...process.env, ARTIFACT_CONTRACT_OUT_DIR: driftTmpDir },
        stdio: ['ignore', 'pipe', 'pipe'],
      });

      const freshBytes     = fs.readFileSync(path.join(driftTmpDir, 'artifact-shape-contract.schema.json'));
      const committedBytes = fs.readFileSync(COMMITTED_ARTIFACT);

      assert.ok(
        freshBytes.equals(committedBytes),
        "committed artifact-shape-contract.schema.json is out of sync with source — " +
        "run 'node bin/emit-artifact-shape-contract.js' and commit the result."
      );
    } finally {
      fs.rmSync(driftTmpDir, { recursive: true, force: true });
    }
  });
});

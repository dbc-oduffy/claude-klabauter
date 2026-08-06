'use strict';
/**
 * test-in-repo-capture-allowlist.js — regression test for the in_repo_capture
 * cross-field rule's home-path allowlist (CROSS_FIELD_RULES['cross-repo-memo']
 * in bin/lib/schema.js).
 *
 * Covers the coordinator/docs/... source-repo variant widening: the coordinator
 * plugin SOURCE repo (this repo) nests its canonical doctrine homes one level
 * deeper than a consumer repo (coordinator/docs/wiki/ vs docs/wiki/, etc.),
 * because coordinator/ is the plugin root resolved live via --plugin-dir. A
 * legitimate ratification capture pointing at the real doctrine wiki in this
 * repo previously failed validation because the allowlist was anchored to the
 * bare consumer-repo prefixes only.
 *
 * Also re-asserts the isClaudeMemoryPointer guard (finding #12) is intact after
 * the allowlist widening — a ~/.claude path must still fail even though it
 * might now superficially resemble a home path.
 *
 * Run with: node --test bin/tests/test-in-repo-capture-allowlist.js
 *
 * Spec backlink: docs/plans/2026-06-21-memo-pickup-claim-lock-and-routed-plan-reconcile.md § C2
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');

const { applyCrossFieldRulesFor } = require('../lib/schema.js');

function inRepoCaptureError(errors) {
  return errors.find((e) => e.field === 'in_repo_capture');
}

describe('cross-repo-memo — in_repo_capture allowlist (coordinator/docs/... source-repo variants)', () => {
  it('accepts coordinator/docs/wiki/foo.md', () => {
    const errors = applyCrossFieldRulesFor('cross-repo-memo', {
      distill_fate: 'ratification',
      in_repo_capture: 'coordinator/docs/wiki/foo.md',
    });
    const err = inRepoCaptureError(errors);
    assert.equal(err, undefined, `Expected no in_repo_capture error, got: ${JSON.stringify(err)}`);
  });

  it('accepts coordinator/docs/decisions/DR-x.md', () => {
    const errors = applyCrossFieldRulesFor('cross-repo-memo', {
      distill_fate: 'ratification',
      in_repo_capture: 'coordinator/docs/decisions/DR-x.md',
    });
    const err = inRepoCaptureError(errors);
    assert.equal(err, undefined, `Expected no in_repo_capture error, got: ${JSON.stringify(err)}`);
  });

  it('still accepts the bare docs/wiki/foo.md form (no regression on consumer-repo layout)', () => {
    const errors = applyCrossFieldRulesFor('cross-repo-memo', {
      distill_fate: 'ratification',
      in_repo_capture: 'docs/wiki/foo.md',
    });
    const err = inRepoCaptureError(errors);
    assert.equal(err, undefined, `Expected no in_repo_capture error, got: ${JSON.stringify(err)}`);
  });

  it('still rejects a ~/.claude memory-pointer path (finding #12 guard intact)', () => {
    const errors = applyCrossFieldRulesFor('cross-repo-memo', {
      distill_fate: 'ratification',
      in_repo_capture: '~/.claude/state/foo.md',
    });
    const err = inRepoCaptureError(errors);
    assert.ok(err, 'Expected an in_repo_capture error for a ~/.claude path, got none');
    assert.match(err.error, /malformed in_repo_capture/);
  });

  it('rejects coordinator/docs/plans/foo.txt (wrong suffix — .md requirement still applies to plans)', () => {
    const errors = applyCrossFieldRulesFor('cross-repo-memo', {
      distill_fate: 'ratification',
      in_repo_capture: 'coordinator/docs/plans/foo.txt',
    });
    const err = inRepoCaptureError(errors);
    assert.ok(err, 'Expected an in_repo_capture error for a non-.md plans path, got none');
    assert.match(err.error, /malformed in_repo_capture/);
  });
});

'use strict';
/**
 * test-lint-frontmatter-sidecar-exemption.js — regression test for SIDECAR_RE
 * suffix/bare-form classifier coverage.
 *
 * Verifies that reviewer sidecars in both prefix form (.review-<name>.md) and
 * suffix form (.<name>-review.md, .review.md, .code-review-*.md) are exempted
 * from the parent plan/handoff schema, while genuine plan files whose names
 * merely contain "-review"/"code-review" as a hyphen-joined word (not a
 * dot-separated sidecar suffix) are NOT exempted.
 *
 * Run with: node --test bin/tests/test-lint-frontmatter-sidecar-exemption.js
 *
 * Spec backlink: coordinator/bin/lint-frontmatter.js:154 (SIDECAR_RE)
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');

const { isSidecarFile } = require('../lint-frontmatter.js');

// ---------------------------------------------------------------------------
// Exempted — sidecar shapes must be recognized (prefix, suffix, and bare forms)
// ---------------------------------------------------------------------------

describe('isSidecarFile — exempted sidecar shapes', () => {
  const exemptedPaths = [
    'docs/plans/x.md.patrik-review.md',
    'docs/plans/x.zoli-review.md',
    'docs/plans/x.pali-review.md',
    'docs/plans/x.md.patrik-delta-review.md',
    'docs/plans/x.review.md',
    'docs/plans/x.code-review-C-dashboard.md',
    'docs/plans/x.code-review-sliceA.md',
    'docs/plans/x.review-patrik.md',
    'docs/plans/x.prior-art-check.md',
  ];

  for (const p of exemptedPaths) {
    it(`exempts ${p}`, () => {
      assert.strictEqual(isSidecarFile(p), true, `expected ${p} to be exempted (sidecar), but isSidecarFile returned false`);
    });
  }
});

// ---------------------------------------------------------------------------
// NOT exempted — genuine plan files must still be linted (over-exemption guard)
// ---------------------------------------------------------------------------

describe('isSidecarFile — genuine plan files are NOT exempted', () => {
  const genuinePlanPaths = [
    'docs/plans/2026-07-08-topic.md',
    'docs/plans/2026-07-08-security-review-doctrine.md',
    'docs/plans/2026-07-08-code-review-gate.md',
  ];

  for (const p of genuinePlanPaths) {
    it(`does not exempt ${p}`, () => {
      assert.strictEqual(isSidecarFile(p), false, `expected ${p} to NOT be exempted (genuine plan), but isSidecarFile returned true`);
    });
  }
});

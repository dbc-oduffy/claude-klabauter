'use strict';
/**
 * test-schema-scalar-coercion.js — regression test for parseScalar numeric-coercion correctness.
 *
 * Verifies that string-shaped SHAs like `717e385` are NOT coerced to Infinity by Number(),
 * that legitimate numerics still coerce to number types, and that non-round-tripping
 * numeric-shaped strings (leading zeros, hex) remain strings.
 *
 * Run with: node --test bin/tests/test-schema-scalar-coercion.js
 *
 * Spec backlink: cross-repo/inbox/2026-07-06-schema-scalar-coerces-sci-notation-sha.md
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');

const { _parseScalar, parseFrontmatter } = require('../lib/schema.js');

// ---------------------------------------------------------------------------
// Bug case: scientific-notation overflow stays string
// ---------------------------------------------------------------------------

describe('parseScalar — scientific-notation overflow stays string', () => {
  it('717e385 returns the string "717e385", not Infinity', () => {
    const result = _parseScalar('717e385');
    assert.strictEqual(result, '717e385');
    assert.strictEqual(typeof result, 'string');
  });

  it('1e400 returns the string "1e400", not Infinity', () => {
    const result = _parseScalar('1e400');
    assert.strictEqual(result, '1e400');
    assert.strictEqual(typeof result, 'string');
  });

  it('9e999 returns the string "9e999", not Infinity', () => {
    const result = _parseScalar('9e999');
    assert.strictEqual(result, '9e999');
    assert.strictEqual(typeof result, 'string');
  });
});

// ---------------------------------------------------------------------------
// Legitimate numerics still coerce to number (regression guard)
// ---------------------------------------------------------------------------

describe('parseScalar — legitimate numerics coerce to number', () => {
  it('"8" coerces to the number 8', () => {
    const result = _parseScalar('8');
    assert.strictEqual(result, 8);
    assert.strictEqual(typeof result, 'number');
  });

  it('"0" coerces to the number 0', () => {
    const result = _parseScalar('0');
    assert.strictEqual(result, 0);
    assert.strictEqual(typeof result, 'number');
  });

  it('"-2" coerces to the number -2', () => {
    const result = _parseScalar('-2');
    assert.strictEqual(result, -2);
    assert.strictEqual(typeof result, 'number');
  });

  it('"4.5" coerces to the number 4.5', () => {
    const result = _parseScalar('4.5');
    assert.strictEqual(result, 4.5);
    assert.strictEqual(typeof result, 'number');
  });

  it('"1000" coerces to the number 1000', () => {
    const result = _parseScalar('1000');
    assert.strictEqual(result, 1000);
    assert.strictEqual(typeof result, 'number');
  });
});

// ---------------------------------------------------------------------------
// Non-round-tripping numeric-shaped strings stay strings
// ---------------------------------------------------------------------------

describe('parseScalar — non-round-tripping numeric-shaped strings stay strings', () => {
  it('"007" stays the string "007" (leading zero)', () => {
    const result = _parseScalar('007');
    assert.strictEqual(result, '007');
    assert.strictEqual(typeof result, 'string');
  });

  it('"0x1f" stays the string "0x1f" (hex literal)', () => {
    const result = _parseScalar('0x1f');
    assert.strictEqual(result, '0x1f');
    assert.strictEqual(typeof result, 'string');
  });
});

// ---------------------------------------------------------------------------
// End-to-end: parseFrontmatter with realized_by: 717e385
// ---------------------------------------------------------------------------

describe('parseFrontmatter — realized_by hex-SHA is preserved as string', () => {
  it('realized_by: 717e385 survives round-trip through parseFrontmatter', () => {
    const doc = '---\nkind: handoff\nrealized_by: 717e385\n---\nbody text\n';
    const { frontmatter } = parseFrontmatter(doc);
    assert.strictEqual(frontmatter.realized_by, '717e385');
    assert.strictEqual(typeof frontmatter.realized_by, 'string');
  });
});

// ---------------------------------------------------------------------------
// null / bool / quoted-string behavior preserved
// ---------------------------------------------------------------------------

describe('parseScalar — null, bool, and quoted-string behavior preserved', () => {
  it('"null" returns null', () => {
    assert.strictEqual(_parseScalar('null'), null);
  });

  it('"true" returns true', () => {
    assert.strictEqual(_parseScalar('true'), true);
  });

  it('double-quoted string strips quotes', () => {
    assert.strictEqual(_parseScalar('"quoted"'), 'quoted');
  });
});

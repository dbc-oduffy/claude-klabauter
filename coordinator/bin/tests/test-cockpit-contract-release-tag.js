'use strict';
/**
 * test-cockpit-contract-release-tag.js — anti-drift regression guard for the
 * `cockpit-contract-release` tag-advance seam.
 *
 * PURPOSE
 * AC8 (docs/plans/2026-07-04-doe-emission-conformance-fixture.md) commits DoE
 * to advancing the local `cockpit-contract-release` tag on every intentional
 * cockpit-contract schema change. That obligation used to be implemented in
 * `coordinator/bin/gen-emission-conformance.sh`; the script was deleted
 * 2026-07-08 (commit 454bc0ab, "retire-js-emitter C4 ... D2=REMOVE") when
 * conformance ownership moved to claude-klabauter, and the tag-advance rode
 * along as collateral — never re-homed. Result: the tag silently drifted ten
 * contract versions behind (2.10.0 vs the emitted 2.20.0) with nothing
 * catching it.
 *
 * The real seam today is `coordinator/bin/regen-cockpit-schema.py` — the ONLY
 * entrypoint that regenerates `coordinator/cockpit-contract/schema/*.json`.
 * This test statically greps that file for the tag-advance wiring so a future
 * deletion or refactor that drops it again fails the fast tier immediately,
 * instead of drifting silently for months.
 *
 * SCOPE — hermetic and static only. Does NOT execute the script (it spawns a
 * sibling repo's venv and would regenerate real artifacts), does NOT read or
 * mutate any git tag, and does NOT touch network. Source-level checks only.
 *
 * Run with: node bin/tests/test-cockpit-contract-release-tag.js
 *
 * Spec backlink: DoE-claude:pln-doe-hosted-emission-conformanc-67bca4 § AC8
 * Spec backlink: coordinator/docs/wiki/emission-conformance-contract.md § Dedicated-Ref Freshness Protocol
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs     = require('fs');
const path   = require('path');

// ---------------------------------------------------------------------------
// Paths — resolved from __dirname (coordinator/bin/tests/)
// ---------------------------------------------------------------------------

const COORDINATOR      = path.resolve(__dirname, '../../');
const REGEN_SCRIPT      = path.join(COORDINATOR, 'bin', 'regen-cockpit-schema.py');
const RELEASE_TAG_NAME  = 'cockpit-contract-release';

describe('cockpit-contract-release tag-advance seam (AC8 anti-drift guard)', () => {
  it('regen-cockpit-schema.py exists — the seam has not been deleted out from under this test', () => {
    assert.ok(
      fs.existsSync(REGEN_SCRIPT),
      `Expected entrypoint not found: ${REGEN_SCRIPT}\n` +
      'This is the ONLY entrypoint that regenerates coordinator/cockpit-contract/schema/*.json. ' +
      'If it has moved, update REGEN_SCRIPT above AND re-verify the tag-advance wiring moved with it — ' +
      'this is exactly the drift class that orphaned the AC8 obligation on 2026-07-08 (commit 454bc0ab).'
    );
  });

  it('wires the --advance-ref opt-in flag and the cockpit-contract-release tag name', () => {
    const source = fs.readFileSync(REGEN_SCRIPT, 'utf8');

    assert.ok(
      source.includes('--advance-ref'),
      `${REGEN_SCRIPT} does not mention '--advance-ref' — the AC8 tag-advance opt-in flag ` +
      'appears to be missing. This is the drift class that orphaned the obligation when ' +
      'gen-emission-conformance.sh was deleted (454bc0ab) — do not let it happen again silently.'
    );

    assert.ok(
      source.includes(RELEASE_TAG_NAME),
      `${REGEN_SCRIPT} does not mention the '${RELEASE_TAG_NAME}' tag — the AC8 dedicated-ref ` +
      'freshness signal (coordinator/docs/wiki/emission-conformance-contract.md § Dedicated-Ref ' +
      'Freshness Protocol) appears disconnected from its enforcement point.'
    );
  });

  it('never invokes a `git push` argv — the tag advance is local-only; publishing the ref is undecided', () => {
    const source = fs.readFileSync(REGEN_SCRIPT, 'utf8');

    const gitPushInvocation = source.match(/\[\s*"git"\s*,\s*"push"\s*,[\s\S]*?\]/);

    assert.ok(
      !gitPushInvocation,
      `${REGEN_SCRIPT} contains a \`["git", "push", ...]\` invocation — the tag-advance seam ` +
      'must be LOCAL-ONLY. Pushing the release ref to origin is not this script\'s surface and ' +
      'must never be added here. The publishing mechanism for this ref is under active ' +
      'discussion between claude-klabauter and DoE-claude as of 2026-07-25 and is not yet decided ' +
      '— do not add a push call here to pre-empt that decision.'
    );
  });

  it('refuses --advance-ref on a dirty schema dir (tag must point at the schema-containing commit)', () => {
    const source = fs.readFileSync(REGEN_SCRIPT, 'utf8');

    assert.ok(
      source.includes('committed and clean'),
      `${REGEN_SCRIPT} does not mention 'committed and clean' — the refuse-if-dirty guard on ` +
      '--advance-ref appears to be missing. Without it, the tag can advance to HEAD while the ' +
      'regenerated schema sits uncommitted, landing the tag on the commit BEFORE the schema and ' +
      'shipping stale bytes to any downstream consumer (e.g. Claude-klabauter) that re-vendors from it.'
    );
  });

  it('advances the release tag as ANNOTATED (`-a`/`-m`), never lightweight — a bare `git tag -f` drops or staleifies the tag message', () => {
    const source = fs.readFileSync(REGEN_SCRIPT, 'utf8');

    const gitTagInvocation = source.match(/\[\s*"git"\s*,\s*"tag"\s*,\s*"-f"\s*,[\s\S]*?\]/);

    assert.ok(
      gitTagInvocation,
      `${REGEN_SCRIPT} does not contain a \`["git", "tag", "-f", ...]\` invocation — the tag-advance ` +
      'wiring appears to have moved or been deleted. This is exactly the drift class that orphaned ' +
      'the AC8 obligation on 2026-07-08 (commit 454bc0ab) — do not let it happen again silently.'
    );

    const invocationText = gitTagInvocation[0];

    assert.ok(
      invocationText.includes('"-a"') && invocationText.includes('"-m"'),
      `${REGEN_SCRIPT}'s \`git tag -f\` invocation is missing '-a' and/or '-m': ${invocationText}\n` +
      'A bare `git tag -f <name>` writes a LIGHTWEIGHT tag, which drops or staleifies the ' +
      'annotation — this is precisely the regression that once let the tag point at 2.21.0 ' +
      "content while its message still read \"CONTRACT_VERSION 2.10.0\". The invocation MUST " +
      'annotate (`-a`) with a message (`-m`), never force-move a lightweight ref.'
    );
  });
});

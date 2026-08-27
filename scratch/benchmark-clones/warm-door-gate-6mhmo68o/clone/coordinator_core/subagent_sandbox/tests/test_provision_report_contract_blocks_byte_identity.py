"""
coordinator_core.subagent_sandbox.tests.test_provision_report_contract_blocks_byte_identity
-- W0.2b red test: proves the `contract_blocks` assembler's `header_style`-aware
extraction reproduces, byte for byte, the exact text an existing paste-model
`verify-snippet-sync` consumer already carries for the same block.

This is the only artifact that proves the engine-side collapse (one extraction
call, per canonical spec `state/subagent-share/conductor/seam-adjudication.md`
§ 2.3) delivers the SAME content the N-copies paste model delivered -- not an
approximation, not a re-wrapped summary.

Extensible by construction (canonical spec requirement): G1 and G2 append
their own `(block_name, consumer_relpath)` rows to CASES when they wire new
`contract_blocks` consumers. Neither plan re-authors this test body.

Requires the sibling DoE-claude checkout (where `coordinator/snippets/` and
the agent `.md` consumers actually live) -- skipped, not failed, when it
can't be resolved on the running machine.

Module under test: coordinator_core/subagent_sandbox/provision_report.py
Spec backlink: state/subagent-share/conductor/seam-adjudication.md § 2.2(3), § 2.3
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.frontmatter.sentinel_blocks import extract_block
from coordinator_core.snippet_sync.registry import get_snippet_entry, load_registry
from coordinator_core.subagent_sandbox.provision_report import _extract_contract_block_body
from coordinator_core.testing.doe_root import doe_root_and_present

DOE_ROOT, DOE_ROOT_PRESENT = doe_root_and_present()

pytestmark = pytest.mark.skipif(
    not DOE_ROOT_PRESENT,
    reason="sibling DoE-claude checkout not resolvable on this machine "
    "(see coordinator_core.testing.doe_root.resolve_doe_root)",
)

#: (block_name, consumer .md path repo-relative to the DoE-claude root) --
#: one representative pairing per `header_style` dialect that has a REAL
#: block on disk today (canonical spec § 2.2(3) / § 2.3), each an existing
#: `verify-snippet-sync`-paste-governed consumer whose pasted copy is
#: currently in sync with its canonical `snippets/<name>.md` source (see
#: `test_all_cases_are_currently_paste_synced` below -- a case pointing at a
#: drifted consumer would make this test permanently red for a reason that
#: has nothing to do with the assembler under test).
#:
#: `reviewer-calibration` and `plan-coverage-check-consumption` (the original
#: rows here) were retired 2026-07-25: DoE-claude completed a paste-to-inject
#: migration for all four of reviewer-calibration / docs-checker-consumption /
#: prior-art-check-consumption / plan-coverage-check-consumption -- every
#: named reviewer/eng-director consumer now receives these via dispatch-time
#: `contract_blocks` injection (subagent-sandbox-policy.yaml), not a pasted
#: sentinel block. As of this writing NO live, non-example-game-repo consumer carries
#: any of the three `sentinel-embedded` blocks in that family pasted anymore
#: -- confirmed by a repo-wide grep for their BEGIN/END sentinels turning up
#: only the snippet source files themselves. `persona-dispatch-contract`
#: supplies this test's `sentinel-embedded` case instead: diagnosing the two
#: original failing rows also surfaced that persona-dispatch-contract's
#: registry `header_style` was itself wrong (declared `comment-block`; fixed
#: to `sentinel-embedded` in the same DoE-claude commit) -- see
#: `docs-checker-consumption`'s known trailing-blank-line drift note below,
#: which is a distinct, still-open issue and NOT why the original rows failed.
#: `do-not-commit` supplies the `comment-block` case instead of
#: `reviewer-calibration`, which (like the sentinel-embedded family above) no
#: longer has any live paste consumer -- registry `consumers = []`.
#:
#: `docs-checker-consumption` (also `sentinel-embedded`) remains unused for
#: that dialect for a separate, still-open reason: as of this writing its
#: canonical source carries a trailing blank line before its own END sentinel
#: that none of its real consumers carried even before the paste-to-inject
#: migration (a pre-existing DoE-claude content drift, out of this
#: module's/repo's scope to fix).
#:
#: Reconciled 2026-08-02 (stale-test cleanup, triage-F): DoE commit c76ba3e36
#: ("registry: flip three stale paste rows to inject before --fix re-pastes
#: 37 blocks") flipped `persona-dispatch-contract` and
#: `quota-self-detect-preamble` themselves from delivery="paste" to
#: delivery="inject" -- both had gone stale the same way the retired
#: `reviewer-calibration`/`plan-coverage-check-consumption` rows above did
#: (their in-repo consumers were already resident-stripped; the registry
#: declaration just hadn't caught up). `eng-director.md` and
#: `code-reviewer.md` therefore no longer carry these sentinel blocks pasted
#: at all -- confirmed by the same repo-wide grep approach used for the
#: retired rows above. Both CASES rows are dropped rather than repointed:
#: `quota-self-detect-preamble`'s remaining genuine paste surface is the 14
#: example-game-repo `conditional_consumer` entries (a separate live-install tree,
#: not resolvable from a DoE-claude-relative consumer_relpath the way this
#: module's CASES shape requires), and `persona-dispatch-contract` has no
#: live paste consumer left anywhere -- see `_KNOWN_HEADER_STYLES` below for
#: the resulting dialect-coverage consequence.
CASES = [
    ("do-not-commit", "coordinator/agents/staff-eng.md"),
    ("guard-encounter-preamble", "coordinator/agents/code-reviewer.md"),
]

# Every `header_style` dialect `_extract_contract_block_body` knows how to
# handle -- kept complete as documentation even though, per the 2026-08-02
# reconciliation above, only a subset currently has a live paste consumer
# this test can prove byte-identity against.
_KNOWN_HEADER_STYLES = {
    "sentinel-embedded",
    "fixed-2-line",
    "fixed-2-line-strip-end-sentinel",
    "comment-block",
}

# Reconciled 2026-08-02 (stale-test cleanup, triage-F, DoE commit c76ba3e36):
# `sentinel-embedded` and `fixed-2-line-strip-end-sentinel` no longer have
# ANY live paste consumer in DoE-claude -- every snippet declaring either
# dialect (docs-checker-consumption, plan-coverage-check-consumption,
# prior-art-check-consumption, persona-dispatch-contract,
# persona-persisting-findings, quota-self-detect-preamble) is now
# delivery="inject". `test_all_known_header_style_dialects_covered_by_a_case`
# can therefore only assert coverage over the dialects CASES is still able to
# exercise -- a case pointing at a delivery="inject" row's former consumer
# would be permanently red for a reason that has nothing to do with the
# assembler under test (canonical spec § 2.3's own stated failure mode).
_PASTE_DELIVERED_HEADER_STYLES = {
    "fixed-2-line",
    "comment-block",
}


def _snippets_dir() -> Path:
    return Path(DOE_ROOT) / "coordinator" / "snippets"


def _registry_data():
    return load_registry(_snippets_dir() / "registry.toml")


@pytest.mark.parametrize("block_name,consumer_relpath", CASES)
def test_extracted_block_byte_identical_to_pasted_consumer_copy(
    block_name: str, consumer_relpath: str
) -> None:
    registry_data = _registry_data()
    entry = get_snippet_entry(registry_data, block_name)
    header_style = entry.get("header_style", "sentinel-embedded")

    snippet_path = _snippets_dir() / f"{block_name}.md"
    snippet_text = snippet_path.read_text(encoding="utf-8")
    extracted = _extract_contract_block_body(
        snippet_text, header_style, entry["sentinel_begin"], entry["sentinel_end"]
    )
    assert extracted is not None, (
        f"assembler failed to extract {block_name!r} (header_style={header_style!r}) "
        f"from its own canonical source at {snippet_path}"
    )

    consumer_path = Path(DOE_ROOT) / consumer_relpath
    consumer_text = consumer_path.read_text(encoding="utf-8")
    pasted = extract_block(consumer_text, entry["sentinel_begin"], entry["sentinel_end"])
    assert pasted is not None, (
        f"{consumer_relpath} carries no {block_name!r} sentinel block to compare against "
        "-- pick a real verify-snippet-sync consumer for this case"
    )

    assert extracted == pasted.block, (
        f"{block_name!r} (header_style={header_style!r}): assembler extraction diverges "
        f"byte-for-byte from its pasted copy in {consumer_relpath} -- the engine-side "
        "collapse must reproduce exactly what the paste model delivered"
    )


def test_all_known_header_style_dialects_covered_by_a_case() -> None:
    """CASES must exercise every `header_style` branch `_extract_contract_block_body`
    knows how to handle that still has a live paste consumer to prove it against
    (canonical spec § 2.2(3) / § 5.4: "the branch is still mandatory in W0").

    Reconciled 2026-08-02 (stale-test cleanup, triage-F, DoE commit c76ba3e36):
    the original assertion pinned coverage against `_KNOWN_HEADER_STYLES`
    (every dialect the assembler supports). Two of those dialects
    (`sentinel-embedded`, `fixed-2-line-strip-end-sentinel`) lost their last
    live paste consumer when their remaining registry rows flipped to
    delivery="inject" -- see the CASES module comment above. Asserting
    against `_PASTE_DELIVERED_HEADER_STYLES` keeps this test honest about
    what it can currently prove; a future paste-model dialect reintroduction
    should widen that set (not `_KNOWN_HEADER_STYLES`, which stays the full
    documentation set).
    """
    registry_data = _registry_data()
    covered_styles = {
        get_snippet_entry(registry_data, name).get("header_style", "sentinel-embedded")
        for name, _ in CASES
    }
    assert covered_styles == _PASTE_DELIVERED_HEADER_STYLES

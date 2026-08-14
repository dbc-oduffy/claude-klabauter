"""Structural pin for the write-guard family's hard-deny/advisory split (M1,
2026-08-06 -- B5 write-guard classification pass).

Mirrors `bash_guards/tests/test_guard_band_membership.py`'s mechanism, NOT
its two-attribute (band + fail_closed) shape -- this package's `CLASS`
attribute already IS a real, consumed classification (`engine.py`'s
hard-deny phase runs first and short-circuits, advisory phase runs only if
no hard-deny fired; see `engine.evaluate`), unlike the bash family's own
`CLASS`, which `dispatch.py` does not read at all (the chain order is
hand-built literal registration, and `CLASS` there governs nothing -- see
that module's own docstring, "WHY A SUBCLASS" section, and the vestigial-ness
this test's sibling exists to avoid reproducing). What was missing on this
side was not a REAL classification (the engine already enforces one) but a
GATED, visible artifact recording what each guard's classification IS and
WHY -- so a new guard landing with the wrong `CLASS`, or an existing one
drifting, fails a test instead of requiring a 38-module reading pass to
notice.

Reads the live registration via the engine's own private `_discover_guards()`
(same precedent as the bash sibling reading `dispatch._build_guard_chain`
directly rather than through a public re-export) -- `check()` closures are
never invoked here, so this stays a STRUCTURAL registration check.

Spec backlink: pln-apply-the-guard-class-census-u-4cae4a, under
DR-277 (docs/decisions/DR-277-guards-are-advisory-by-default-two-named.md).
DR-277 REPLACES the B5-era irreversible-harm-only hard-deny bar this
docstring previously argued for: the ruling is guards are advisory BY
DEFAULT, with hard-deny reserved for three named carve-outs, quoted from
DR-277's own headings rather than paraphrased (Review: coordinator:code-
reviewer, 36bfdde30 follow-up -- this enumeration previously invented a
DIFFERENT and equally wrong three-item list, "(1) a genuinely
silent/total/irreversible loss..., (2) a fleet-wide resource-exhaustion
risk..., or (3) a handful of PM-ratified exceptions recorded in the census
itself" -- none of which are DR-277's actual carve-out names, and there is
no "PM-ratified exceptions" carve-out at all):

- Carve-out 1 -- broken control plane: "the harm is a broken control
  plane -- the tool surface itself, doctrine loaded before review is
  possible, or the boundary deciding what other guards enforce -- not
  merely a bad file" (e.g. a settings.json poison that locks every tool
  fleet-wide with no in-session repair path, or the stash guards, where a
  dropped stash leaves no reflog).
- Carve-out 2 -- the Windows-cost boundary: "a guard's deny leg is a
  Windows-cost boundary" (e.g. `block_reviewer_bash_outside_allowlist`,
  `guard_multiprobe_banner`, `guard_plumbing_and_loops`).
- Carve-out 3 -- the machine itself: "resource exhaustion that takes the
  session or the box down" (the `check_test_suite_invocation` carve-out).

Everything that does not clear one of those three carve-outs is advisory,
including guards this file previously pinned hard-deny under the
superseded B5 bar (the nine C2-C12 flips: `check_claude_md_size`,
`block_cutover_phase_hand_edit`, `block_tracker_edit`,
`block_priority_ledger_edit`,
`block_em_hand_edit_pending_review_integration`,
`nudge_prose_queue_creation`, `nudge_improvement_queue_write`,
`guard_memory_store_cap`, `block_dev_repo_sentinel_write` -- disk-truth
wires and dev-side mirrors whose harm is reversible and
individually-correctable, not the broken-control-plane, Windows-cost, or
machine-exhaustion shape DR-277's three carve-outs reserve (Review:
coordinator:code-reviewer, 36bfdde30 follow-up -- "silent-and-total" was
this same wrong paraphrase's own language, not DR-277's; see the corrected
enumeration above). The guards remaining hard-deny below are the
carve-out set DR-277 names, not a residual of the old bar.

`bump_out_of_repo_tool_write` is hard-deny (moved from advisory by the bug
fix `2026-08-10-cross-repo-write-boundary-denies-on-bash-b6fd16ed9ab9`) for
none of the "silent" or "total" reasons a first read of the guard's own
harm suggests -- a cross-repo write is neither silent (it shows up in the
peer session's own `git status`) nor total (it is revertible with a plain
`git checkout`/`git revert`).

Review: coordinator:code-reviewer (2074e4dd) -- an earlier version of this
paragraph claimed this cleared DR-277's bar under "the third carve-out:
PM-ratified parity with this guard's Bash-surface siblings." No such
carve-out exists. DR-277's actual three carve-outs are (1) a broken
control plane, (2) the Windows-cost boundary, and (3) fleet-wide resource
exhaustion that takes the machine down -- none is cross-surface parity,
and `bump_out_of_repo_tool_write` is not in the
`state/audits/2026-08-06-guard-class-census/` census this file's own
carve-out-3 paraphrase (above) would also require. The parity argument
below is real and worth keeping on its own terms; it is not, as written,
covered by DR-277.

RULED 2026-08-11 (DoE, PM-delegated). This guard stays hard-deny by a
NAMED ONE-OFF RULING OUTSIDE THE THREE CARVE-OUTS -- not pending, not
implied, and not a fourth carve-out. DR-277 records the ruling in its own
§ "Not a carve-out: cross-surface parity", verbatim: "A sibling guard's
hardness on another tool surface is not an argument for hardness here." The
same section states the disposition of THIS guard: "That guard stays hard by
named one-off ruling, not by carve-out, and is not a precedent." No
carve-out was added, because a parity carve-out propagates hardness from one
leg of a boundary to every surface the same subject can be reached from --
the opposite of DR-277's default -- and because exactly one guard qualifies,
which is not a class. DR-277 also fixes the standing answer for the next
guard proposed on this argument: "If a second guard is ever proposed for
hardness on a parity argument, the answer this record points at is to soften
the Bash legs into genuine advisories, not to admit a second member."

Two facts DR-277 now carries alongside the ruling, repeated here because
this file is where a class change would be attempted: the
`state/audits/2026-08-06-guard-class-census/` census contains NO entry for
any of the three write-confinement bump guards, in either direction (so
"this guard is missing from the census" is not evidence against its class,
it is evidence the census never reached this boundary); and the two Bash
legs deny from a band literally named `ADVISORY_REWRITE` with
`fail_closed=False`, which is a registration-hygiene defect the ruling
neither fixes nor blesses. Do not resolve either by editing this prose --
the first needs a census entry, the second a registration change.

The parity argument itself: leaving this module advisory while its
Bash-surface siblings, `bump_foreign_repo_write` [C4] and
`bump_outside_repo_write` [C5], already compose a real
`permissionDecision: "deny"` envelope for the identical payload shape
(verified live, same session, same target) is a tool-surface loophole
around an already-hard-denied boundary: an agent blocked on
`git checkout` into a foreign repo could simply reach for `Write`/`Edit`
instead and land the identical unannounced write. On a machine where a
dozen-plus sessions share worktrees, that write lands as unattributed
drift in a peer session's diff, with no attribution and no announcement
at the point of harm.

`block_subagent_grant_record_write` is hard-deny under carve-out 1
(broken control plane): "the harm is a broken control plane -- the tool
surface itself, doctrine loaded before review is possible, or the
boundary deciding what other guards enforce -- not merely a bad file"
(DR-277). Its own module docstring states the target directly: the
CLAUDE.md write-grant record is "the record `coordinator_core.session.
claude_md_grant` writes and `check_claude_md_write_grant` reads to gate
`block_unauthorized_claude_md_write`" -- i.e. the artifact this guard
protects IS the boundary that decides whether a separate guard
(`block_unauthorized_claude_md_write`, already hard-deny in this same
list) allows or denies a write. A subagent able to hand-author a
live-looking grant record walks straight through that other guard's
allow leg, so this guard's own harm is not "a bad file" but the control
plane one hard-deny guard's enforcement rests on.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from coordinator_core.write_guards import engine

# Every hard-deny guard, alphabetical. A hard-deny here means: the harm this
# guard prevents is silent-and-total (no error at write time AND no error at
# the moment of loss), fleet-wide-and-compounding in a way after-the-fact
# review cannot hold down, or a disk-truth wire whose corruption an
# automated downstream consumer trusts before any human reviews it.
HARD_DENY_NAMES = [
    "block_consumed_handoff_edit",
    "block_derived_global_doctrine_write",
    "block_disarm_marker_sentinel_write",
    "block_goals_log_hand_write",
    "block_home_dir_memo_delivery",
    "block_illegal_filename",
    "block_memo_status_hand_edit",
    "block_oss_mirror_memo_delivery",
    "block_subagent_archive_write",
    "block_subagent_grant_record_write",
    "block_subagent_plan_body_write",
    "block_unauthorized_claude_md_write",
    "block_worktree_sentinel_write",
    "bump_out_of_repo_tool_write",
    "guard_doctrine_surface_edits",
    "guard_settings_json_write",
    "validate_frontmatter_schema_deny",
]

# Every advisory guard, alphabetical. Includes the two 2026-08-06 B5
# conversions from hard-deny (`block_completion_monolith_write`,
# `block_dev_side_mirror_wiki`) -- the harm each flags is a plainly
# reversible, individually-correctable mistake, not the irreversible-harm
# shape the hard-deny band is reserved for; see their own module docstrings.
ADVISORY_NAMES = [
    "block_completion_monolith_write",
    "block_cutover_phase_hand_edit",
    "block_dev_repo_sentinel_write",
    "block_dev_side_mirror_wiki",
    "block_em_hand_edit_pending_review_integration",
    "block_priority_ledger_edit",
    "block_tracker_edit",
    "check_claude_md_size",
    "guard_concrete_path_citations",
    "guard_memory_store_cap",
    "nudge_baton_body_bar",
    "nudge_em_code_dispatch",
    "nudge_improvement_queue_write",
    "nudge_new_sh_file_naked_python",
    "nudge_outbox_draft_frontmatter_shape",
    "nudge_plan_sidecar_family_split",
    "nudge_private_git_fact_resolver",
    "nudge_prose_queue_append",
    "nudge_prose_queue_creation",
    "nudge_sentinel_retained_review_sidecar",
    "nudge_shell_shaped_spawn",
    "nudge_tasks_state_folder_split",
    "nudge_terminal_artifact_edit",
    "nudge_windows_subprocess_popup",
    "validate_frontmatter_schema_advisory",
]

_EXPECTED_CLASS_BY_NAME: Dict[str, str] = {
    **{name: "hard-deny" for name in HARD_DENY_NAMES},
    **{name: "advisory" for name in ADVISORY_NAMES},
}


def _guards() -> "List":
    guards, import_failed = engine._discover_guards()
    assert import_failed == [], (
        f"guard module(s) failed to import: {import_failed} -- this test cannot "
        "trust the discovered set while an import is silently broken"
    )
    return guards


def test_chain_is_readable_and_non_trivial():
    """Guard the guard: if `_discover_guards` stops returning entries, every
    assertion below would pass vacuously on an empty list."""
    guards = _guards()
    assert len(guards) > 10, guards
    names = {g.name for g in guards}
    assert "block_illegal_filename" in names
    assert "nudge_baton_body_bar" in names


def test_all_named_guards_are_actually_registered():
    """A typo in either list above would silently weaken every other
    assertion in this file to a no-op against a name that was never
    checked."""
    registered = {g.name for g in _guards()}
    all_named = set(HARD_DENY_NAMES) | set(ADVISORY_NAMES)
    missing = all_named - registered
    assert not missing, (
        f"named in this test but not registered/discoverable: {sorted(missing)} -- "
        "either the guard was renamed/removed, or this test has gone stale."
    )


def test_no_registered_guard_is_unclassified_by_this_test():
    """The mirror image of the check above: every discovered guard must be
    named in exactly one of the two lists -- a new guard module landing
    without updating this file is exactly the "unclassified guard, visible
    only by reading 38 modules" failure mode B5 exists to close."""
    registered = {g.name for g in _guards()}
    all_named = set(HARD_DENY_NAMES) | set(ADVISORY_NAMES)
    unclassified = registered - all_named
    assert not unclassified, (
        f"discovered but not named in either classification list here: "
        f"{sorted(unclassified)} -- a new guard must be classified explicitly, "
        "not left to default."
    )


def test_each_guard_carries_its_expected_class():
    wrong = []
    for g in _guards():
        expected = _EXPECTED_CLASS_BY_NAME.get(g.name)
        if expected is not None and g.cls != expected:
            wrong.append((g.name, g.cls, expected))
    assert not wrong, (
        "guards whose runtime CLASS diverged from this pinned classification "
        "(name, actual, expected): %r" % wrong
    )


def test_no_name_appears_in_both_lists():
    overlap = set(HARD_DENY_NAMES) & set(ADVISORY_NAMES)
    assert not overlap, f"guard(s) named in both classification lists: {sorted(overlap)}"


# Three sources independently assert the `bump_out_of_repo_tool_write`
# named-one-off-ruling facts and declare their own mutual agreement
# load-bearing (see this module's own docstring, "RULED 2026-08-11" section,
# and `bump_out_of_repo_tool_write.py`'s "THE HARD-DENY CLASS IS A NAMED
# ONE-OFF RULING" section) -- nothing mechanical enforced that agreement
# before this test (Review: coordinatorcode-reviewer-ad7b843b P3). Asserted
# on a small number of exact quoted sentences, not whole-paragraph equality,
# per that finding's own guidance: paragraph structure is free to diverge,
# the load-bearing FACTS are not.
_DR_277_PATH = Path(
    "docs/decisions/DR-277-guards-are-advisory-by-default-two-named.md"
)
_BUMP_GUARD_MODULE_PATH = Path(
    "coordinator_core/write_guards/bump_out_of_repo_tool_write.py"
)
_THIS_FILE_PATH = Path(__file__)

# The DR-277 quote, verbatim, is line-wrapped differently in each of the
# three sources -- normalized whitespace (collapse runs of whitespace,
# including the newline+indentation a wrapped docstring/markdown paragraph
# introduces) before substring comparison so re-wrapping prose does not
# break this test.
_PARITY_NOT_ARGUMENT_SENTENCE = (
    "A sibling guard's hardness on another tool surface is not an argument "
    "for hardness here."
)
# The second load-bearing fact -- this guard is a named one-off, not a
# precedent -- is phrased slightly differently at each of the three sites
# (paragraph structure, not fact, is what is free to diverge here), so this
# checks the shared substring rather than one exact sentence.
_NOT_A_PRECEDENT_SUBSTRING = "is not a precedent"


def _normalized_text(path: Path) -> str:
    repo_root = Path(__file__).resolve().parents[3]
    return re.sub(r"\s+", " ", (repo_root / path).read_text(encoding="utf-8"))


def test_bump_guard_parity_ruling_sentences_agree_across_the_three_sources():
    """DR-277 § "Not a carve-out: cross-surface parity",
    `bump_out_of_repo_tool_write.py`'s own docstring, and this file's own
    module docstring each independently assert the census gap and the
    named-one-off-ruling disposition for `bump_out_of_repo_tool_write`, and
    the guard's own docstring calls keeping the three in agreement
    load-bearing. This test is the mechanical enforcement that was missing:
    it fails if any of the three sources drifts off the shared quoted text."""
    sources = {
        "DR-277": _normalized_text(_DR_277_PATH),
        "bump_out_of_repo_tool_write.py": _normalized_text(_BUMP_GUARD_MODULE_PATH),
        "test_guard_classification.py (this file)": _normalized_text(_THIS_FILE_PATH),
    }

    missing_parity_sentence = [
        name for name, text in sources.items() if _PARITY_NOT_ARGUMENT_SENTENCE not in text
    ]
    assert not missing_parity_sentence, (
        "source(s) missing the exact quoted DR-277 parity sentence "
        f"{_PARITY_NOT_ARGUMENT_SENTENCE!r}: {missing_parity_sentence} -- "
        "the three-way agreement this guard's own docstring calls "
        "load-bearing has drifted."
    )

    missing_precedent_fact = [
        name for name, text in sources.items() if _NOT_A_PRECEDENT_SUBSTRING not in text
    ]
    assert not missing_precedent_fact, (
        f"source(s) missing the shared {_NOT_A_PRECEDENT_SUBSTRING!r} fact: "
        f"{missing_precedent_fact} -- the three-way agreement this guard's "
        "own docstring calls load-bearing has drifted."
    )

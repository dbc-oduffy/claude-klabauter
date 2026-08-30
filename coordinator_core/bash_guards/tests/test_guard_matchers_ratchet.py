"""Two-directional ratchet over `GuardEntry.matchers` -- the field
`test_ac8_regeneration_is_byte_identical` (DoE-claude side) structurally
cannot watch, because a narrowing from `COMMAND_TOOL_NAMES` back to
`("Bash",)` restores agreement between their generated `hooks.json` and
Claude-klabauter's roster rather than breaking it. Losing PowerShell coverage and
never having had it are indistinguishable to that test.

Spec backlink: pln-a-narrowed-guard-registration-80dbad, chunk C1.
Reference: `docs/reference/guard-tool-name-membership.md` § 3 (the held-
cohort ruling this ratchet pins) -- this module is the enforcement that
section's own text names as its guard against silent drift (§ 3's own
"treat the counts in this paragraph as stale-by-default and re-run the
grep" is exactly the discipline AC1 below replaces with a live read).

AC1: matchers come from `coordinator_core.bash_guards.roster.guard_roster()`
-- the live registration -- never from a source-text/AST read of a
`MATCHERS` literal. `_scoped_module_stems()` below uses `hasattr(module,
"MATCHERS")` only to describe which modules declare a module-level
`MATCHERS` constant (a documentation fact pinned by `test_discovery_found_
the_expected_scope`); it plays NO role in deciding which `guard_roster()`
entries this ratchet ENFORCES -- `_actual_matchers()` below enforces every
live registration, full stop. The compared VALUE for every guard always
comes from `guard_roster()` (AC1).

CORRECTED 2026-08-26 (C6, pln-the-destructive-core-learns-the-shell-it-
guards, staff-eng Finding #0): this ratchet previously scoped its
enforcement to `hasattr(module, "MATCHERS")`-declaring modules only --
23 of the 48 live registrations. The 21 Bash-only entries registered
INLINE in `dispatch.py` (14 backed by `dispatch_checks.py`, which has no
module-level `MATCHERS` at all, plus a handful of module-backed entries
whose module never declares the constant even though the registration
sets `matchers=` directly) were invisible to it -- `_scoped_module_stems`
selects modules by `hasattr`, so an inline `GuardEntry` with no backing
module, or a module that never declares `MATCHERS`, could never appear in
the scoped set regardless of what its live `matchers=` said. This is the
gap `docs/reference/guard-tool-name-membership.md` § 3z names as owed
follow-up work ("extend the ratchet ... to cover inline registrations").
Closed here by enforcing over ALL of `guard_roster()`'s output, not a
module-scoped subset of it.

Population note (verified against disk this session, not taken from the
plan's own "19 full-universe, 5 Bash-only" estimate): `grep -n
"^MATCHERS"`-style scoping finds 24 modules (19 full + 5 held), but
`block_dev_repo_sentinel_removal.py` is one of the 19 only by a literal
that its own live registration never uses -- `dispatch.py`'s own import
comment (search for "block_dev_repo_sentinel_removal.py DOES declare")
documents that the module's `check()` leg pairing with that constant was
RETIRED, and the surviving `block-dev-repo-sentinel-removal-advisory` entry
passes `matchers=("Bash",)` directly. Comparing a live Bash-only
registration against a constant it never references would make this
ratchet permanently red for a guard nobody is narrowing or widening --
excluded below, not silently (see `_EXCLUDED_INAPPLICABLE_DECLARATION`,
which now governs only `test_discovery_found_the_expected_scope`'s module
count, not enforcement).

Re-censused 2026-08-19: the `held-pending-tokenizer-fix` cohort
(`block_stash_destruction`, `block_subagent_destructive_action`,
`block_subagent_stash_creation`, `block_worktree_creation`) now declares
`MATCHERS = COMMAND_TOOL_NAMES` on disk and registers full-universe in
`guard_roster()` -- the tokenizer fallback blocking their widening was
fixed upstream of this chunk. The `held-pending-tokenizer-fix` kind is
now empty; `bash-only-by-construction` (`guard_powershell_via_bash`,
correct by construction, not a conversion candidate) is retained and the
two kinds are kept distinct rather than collapsed, since the ratchet
records kinds, not a single allow-list.

Declaring the full universe is not the same as reading it: of the 48 live
registrations, several full-universe entries have zero `_dialect`
references in their own module source (`_scoped_actual_matchers` renamed
`_actual_matchers`; see `DUAL_DECLARING_BASH_DETECTING` below) --
`MATCHERS = COMMAND_TOOL_NAMES` only governs whether `fn()` is called at
all (§ 6 of the reference doc) -- it says nothing about whether that
`fn()` branches on which dialect it was called with. A reader who checks
`MATCHERS` alone will read these as PowerShell-aware; they are
chain-eligible for a PowerShell payload but detect with whatever
Bash-shaped logic they already had. `state/audits/2026-08-26-guard-
detection-language-dependence-recensus.md` Findings 2+3 name the nine
confirmed members of this cohort (six module-backed, three inline) --
the plan body's own AC8 row cites a stale pre-census "seven"; the nine
here is the measured, current population, not the plan-text estimate.

THREE-VALUED PARTITION (AC8): every entry in `EXPECTED` is in exactly one
of:

  1. Dual-declaring AND dialect-reading -- `kind=None`. The default; no
     exemption record needed.
  2. Bash-only WITH a written reason -- `kind` is one of
     `BASH_ONLY_BY_CONSTRUCTION` (permanently correct, no PowerShell
     equivalent exists -- doc § 8's Bucket C table) or `NOT_YET_CONVERTED`
     (a real, temporary gap: pending Bucket A conversion (C2/C3), a
     Bucket-D built-but-not-wired defect (C9), or simply not yet audited
     for PowerShell applicability). Both sub-kinds carry a written reason;
     `BASH_ONLY_BY_CONSTRUCTION` additionally asserts the reason never
     reads as a remediation ask (see
     `test_powershell_via_bash_kind_is_pinned_and_carries_no_remediation`).
     Bucket A entries land in bucket (1) at the end of C3, never in (3).
  3. Dual-declaring BUT Bash-detecting -- `kind=DUAL_DECLARING_BASH_
     DETECTING`, an explicitly enumerated, named exemption list a new
     entry cannot join without a written record.

A newly-added Bash-only or dual-declaring entry with no roster row fails
the ratchet's `_compare` until it is classified into one of the three
buckets above -- see `test_every_entry_is_in_exactly_one_partition_
bucket`, AC8's own precondition.
"""
from __future__ import annotations

import importlib
import pathlib
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple

from coordinator_core.bash_guards.roster import guard_roster

_PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent
_REFERENCE_DOC = "docs/reference/guard-tool-name-membership.md § 3"

#: Same non-guard-module exclusion `test_tool_name_membership.py` already
#: uses -- dispatch.py/dispatch_checks.py are the dispatcher itself,
#: commit_tripwires.py is a library `dispatch_checks.py` calls internally,
#: none of the three register their own `MATCHERS` contract. Governs only
#: `_scoped_module_stems()` / `test_discovery_found_the_expected_scope`'s
#: module-declaration count -- NOT `_actual_matchers()`'s enforcement
#: scope, which now covers every `guard_roster()` id including the inline
#: entries `dispatch.py`/`dispatch_checks.py` back.
_NON_GUARD_MODULES = {"dispatch.py", "dispatch_checks.py", "commit_tripwires.py"}

#: See module docstring's "Population note". Kept to one name, not a
#: general-purpose skip list -- a second entry here would need the same
#: level of on-disk justification as this one carries.
_EXCLUDED_INAPPLICABLE_DECLARATION = {"block_dev_repo_sentinel_removal"}

HELD_PENDING_TOKENIZER_FIX = "held-pending-tokenizer-fix"
BASH_ONLY_BY_CONSTRUCTION = "bash-only-by-construction"
NOT_YET_CONVERTED = "not-yet-converted"
DUAL_DECLARING_BASH_DETECTING = "dual-declaring-bash-detecting"

#: kinds that mark a Bash-only declaration (bucket 2 of AC8's partition).
_BASH_ONLY_KINDS = frozenset({BASH_ONLY_BY_CONSTRUCTION, NOT_YET_CONVERTED})


@dataclass(frozen=True)
class _Expected:
    matchers: Tuple[str, ...]
    kind: Optional[str] = None
    reason: Optional[str] = None


#: The full tracked population (AC1, AC5, AC8) -- all 52 live `guard_
#: roster()` registrations, inline and module-backed alike. Full-universe,
#: dialect-reading entries carry no `kind`; every Bash-only entry and every
#: dual-declaring-but-Bash-detecting entry carries a machine-
#: distinguishable `kind` plus prose (AC5, AC7, AC8).
EXPECTED: Dict[str, _Expected] = {
    # -- full-universe, dialect-reading (26) --
    "block-approval-sentinel-creation": _Expected(("Bash", "PowerShell")),
    "block-disarm-marker-sentinel-creation": _Expected(("Bash", "PowerShell")),
    "block-illegal-filename": _Expected(("Bash", "PowerShell")),
    "block-reviewer-bash-outside-allowlist": _Expected(("Bash", "PowerShell")),
    "block-stash-destruction": _Expected(("Bash", "PowerShell")),
    "block-subagent-destructive-action": _Expected(("Bash", "PowerShell")),
    "block-subagent-plan-body-bash-write": _Expected(("Bash", "PowerShell")),
    "block-subagent-stash-creation": _Expected(("Bash", "PowerShell")),
    "block-worktree-creation": _Expected(("Bash", "PowerShell")),
    "block-worktree-sentinel-creation": _Expected(("Bash", "PowerShell")),
    "check-raw-pid-liveness": _Expected(("Bash", "PowerShell")),
    "check-test-suite-invocation": _Expected(("Bash", "PowerShell")),
    "grep-via-bash-guard": _Expected(("Bash", "PowerShell")),
    "inprocess-search": _Expected(("Bash", "PowerShell")),
    "multiprobe-banner": _Expected(("Bash", "PowerShell")),
    "plumbing-and-loops": _Expected(("Bash", "PowerShell")),
    "bump-foreign-repo-write": _Expected(("Bash", "PowerShell")),
    "bump-outside-repo-write": _Expected(("Bash", "PowerShell")),
    # Bucket A, already landed dialect-aware by a concurrent chunk (C2) as
    # of this ratchet's authoring session -- verified live via
    # guard_roster(), not carried over from an earlier read of dispatch.py.
    "no-verify": _Expected(("Bash", "PowerShell")),
    "destructive-rm": _Expected(("Bash", "PowerShell")),
    "destructive-git-orphan": _Expected(("Bash", "PowerShell")),
    "destructive-git-clean": _Expected(("Bash", "PowerShell")),
    "blanket-git-add": _Expected(("Bash", "PowerShell")),
    "guard-doctrine-surface-bash-write": _Expected(("Bash", "PowerShell")),
    "guard-repo-setup-claude-home-refusal": _Expected(("Bash", "PowerShell")),
    "guard-host-subagent-bash-spawn-shapes": _Expected(("Bash", "PowerShell")),
    # -- Bash-only by construction, never a conversion candidate (11) --
    # docs/reference/guard-tool-name-membership.md § 8's Bucket C table.
    "guard-host-subagent-bash-ban": _Expected(
        ("Bash",),
        BASH_ONLY_BY_CONSTRUCTION,
        "Bash-only by PM ruling recorded in the guard's own docstring. Its deny message names the PowerShell tool as the remedy for a confined agent, so registering it on PowerShell would deny the escape hatch its own message points at. The asymmetry with guard-repo-setup-claude-home-refusal is deliberate, not an inconsistency to tidy.",
    ),
    "powershell-via-bash-guard": _Expected(
        ("Bash",),
        BASH_ONLY_BY_CONSTRUCTION,
        "Guards PowerShell invoked VIA the Bash tool; the PowerShell tool "
        "is not a surface it can meaningfully watch.",
    ),
    "find-exec-rewrite": _Expected(
        ("Bash",),
        BASH_ONLY_BY_CONSTRUCTION,
        "Rewrites `find ... -exec <bin> {} ;` / `for f in $(find ...)` -- "
        "POSIX `find`, no PowerShell equivalent idiom exists.",
    ),
    "grep-via-bash-rewrite": _Expected(
        ("Bash",),
        BASH_ONLY_BY_CONSTRUCTION,
        "The rewrite's OUTPUT is a Bash-argv replacement (a python3 -c "
        "os.walk/re one-liner spliced into a Bash argv slot) -- widening "
        "would declare a dialect the rewrite cannot safely act on; the "
        "dual-declaring sibling `grep-via-bash-guard` already covers "
        "PowerShell at advisory band. See doc § 8a.",
    ),
    "sed-range-read-advise": _Expected(
        ("Bash",),
        BASH_ONLY_BY_CONSTRUCTION,
        "Detects `sed -n 'A,Bp' FILE` -- no `sed` on PowerShell.",
    ),
    "cat-heredoc-write-advise": _Expected(
        ("Bash",),
        BASH_ONLY_BY_CONSTRUCTION,
        "Detects `cat > FILE <<'EOF' ... EOF` -- POSIX heredoc grammar; "
        "PowerShell's here-string (@'...'@) is a different grammar this "
        "detector does not parse.",
    ),
    "heredoc-repo-write-advise": _Expected(
        ("Bash",),
        BASH_ONLY_BY_CONSTRUCTION,
        "Same heredoc family as cat-heredoc-write-advise, scriptable-"
        "interpreter sibling (python3 - <<'PY' ... PY) -- POSIX-only "
        "grammar.",
    ),
    "multiprobe-banner-rewrite": _Expected(
        ("Bash",),
        BASH_ONLY_BY_CONSTRUCTION,
        "The rewrite splices a single-process replacement into a Bash "
        "`;`-chain -- a Bash-argv-specific output, same reasoning as "
        "grep-via-bash-rewrite. See doc § 8a.",
    ),
    "offer-git-c": _Expected(
        ("Bash",),
        BASH_ONLY_BY_CONSTRUCTION,
        "Splits `cd <dir> && git <sub>` via a quote-aware segmenter that "
        "tracks POSIX shell escaping rules (backslash-in-double-quote, "
        "none in single-quote) -- not PowerShell's quoting/backtick "
        "grammar.",
    ),
    "offer-invoke-params-stdin": _Expected(
        ("Bash",),
        BASH_ONLY_BY_CONSTRUCTION,
        "Rewrites an inline shell-quoted argv token into a POSIX heredoc "
        "(--params-file -) form -- both the failure mode and the rewrite "
        "target are POSIX-shell-quoting-specific.",
    ),
    "runaway-find": _Expected(
        ("Bash",),
        BASH_ONLY_BY_CONSTRUCTION,
        "C3 (pln-the-destructive-core-learns-the-she): detects POSIX "
        "`find`'s argv shape (-mtime/-exec/anchor-path walk); no "
        "PowerShell cmdlet or binary shares that argv, so there is no "
        "vocabulary to widen onto -- reclassified from a temporary gap "
        "to permanently Bash-only.",
    ),
    # -- Bash-only, NOT permanently correct: a real, temporary gap (6) --
    # Bucket D, WIRED by C9 (2026-08-26). Both entries already read the
    # dialect at their registered leg and simply never declared it; C9
    # changed the declaration only, with no detection work. They move here
    # to bucket (1) -- dual-declaring AND dialect-reading -- because that
    # is now literally true of both. Leaving them at NOT_YET_CONVERTED
    # after the widening is what turned this ratchet red: the gate is
    # comparing the live registration against this table, which is exactly
    # the regrowth it exists to catch, fired against its own plan.
    "block-dev-repo-sentinel-removal-advisory": _Expected(
        ("Bash", "PowerShell"),
    ),
    # Full-universe from birth: the false-green it catches is dialect-neutral,
    # and the guard reuses block_stash_destruction's existing PowerShell leg
    # rather than declaring a Bash-only hold it would later have to widen off.
    "stash-apply-verification-advisory": _Expected(
        ("Bash", "PowerShell"),
    ),
    "head-tail-plumbing-rewrite": _Expected(
        ("Bash", "PowerShell"),
    ),
    "reap-stale-git-lock": _Expected(
        ("Bash", "PowerShell"),
    ),
    "git-no-optional-locks": _Expected(
        ("Bash", "PowerShell"),
    ),
    # Bucket B, WIRED by C4 (2026-08-26): the four git-shaped advisories.
    # git's argv is byte-identical across dialects, so these fire on the
    # same argv under both -- the PowerShell-applicability audit these rows
    # were waiting on IS the C4 conversion plus its own per-entry test
    # (test_git_shaped_advisories_fire_under_both.py). Moved to bucket (1).
    # NOTE `probe-spray` is slated for deletion by
    # state/handoffs/2026-08-21-2026-08-21_191819_guards-under-the-
    # brightline.md and was PM-cut from this plan's Bucket B; C4 widened it
    # anyway. Harmless (advisory band, and it fires on identical argv), but
    # the row is recorded here rather than silently inheriting the cohort's
    # rationale, so whoever deletes the entry does not read this as an
    # endorsement of keeping it.
    "validate-commit": _Expected(
        ("Bash", "PowerShell"),
    ),
    "probe-spray": _Expected(
        ("Bash", "PowerShell"),
    ),
    # -- formerly dual-declaring-but-Bash-detecting (9), now CONVERTED
    # (C8's second pass, Finding 7 of the recensus record) -- moved to
    # bucket (1). state/audits/2026-08-26-guard-detection-language-
    # dependence-recensus.md Findings 2 (six module-backed) and 3 (three
    # inline) found these nine declaring `COMMAND_TOOL_NAMES` with zero
    # `_dialect` references. The first C8 pass measured only base-argv
    # identity and wrongly read eight of the nine as correct-as-drafted;
    # re-measured against the PowerShell `Start-Process` anti-bypass
    # surface specifically (the same surface that gapped
    # `destructive-git-revert`, whose own base argv also matched
    # identically), seven were REAL detection gaps and are now converted
    # (a dialect-gated `_dialect.tokenize_command` +
    # `expand_start_process_invocations` pass, narrowly scoped to
    # `Start-Process`, ahead of each entry's existing Bash-shaped
    # pipeline). The ninth, `destructive-git-revert-advisory`, is a thin
    # wrapper over the SAME `_check_destructive_git_revert_full` function
    # `destructive-git-revert`'s hard-deny leg calls, so the first C8
    # pass's fix already covered it too -- a genuine no-change verdict,
    # confirmed empirically, not re-derived. All nine now demonstrably
    # branch on dialect at detection time, which is bucket (1)'s test.
    # Moving them here (rather than leaving DUAL_DECLARING_BASH_DETECTING
    # with a corrected reason) is what bucket (1)'s own definition
    # requires once detection genuinely branches on dialect -- see
    # `test_dual_declaring_bash_detecting_kind_is_pinned` below, now
    # asserting the empty set for the same reason Bucket D's two entries
    # moved here under C9 above.
    "block-noncanonical-branch-creation": _Expected(
        ("Bash", "PowerShell"),
    ),
    "block-subagent-commit": _Expected(
        ("Bash", "PowerShell"),
    ),
    "block-subagent-grant-acquisition": _Expected(
        ("Bash", "PowerShell"),
    ),
    "block-subagent-guard-grant": _Expected(
        ("Bash", "PowerShell"),
    ),
    "branch-set-precedence": _Expected(
        ("Bash", "PowerShell"),
    ),
    "longlived-branch-naming": _Expected(
        ("Bash", "PowerShell"),
    ),
    "destructive-git-revert": _Expected(
        ("Bash", "PowerShell"),
    ),
    "destructive-git-revert-advisory": _Expected(
        ("Bash", "PowerShell"),
    ),
    "git-commit-safe-commit-advise": _Expected(
        ("Bash", "PowerShell"),
    ),
}


def _scoped_module_stems() -> FrozenSet[str]:
    """Which guard MODULES declare a module-level `MATCHERS` constant --
    a documentation fact pinned by `test_discovery_found_the_expected_
    scope`, and (per AC1) NOT the set `_actual_matchers()` enforces below.
    `hasattr(module, "MATCHERS")` is a presence check, not a read of the
    tuple's contents, same role `test_tool_name_membership.py::
    _guard_modules_with_matchers` already plays for its own, differently-
    shaped sweep.
    """
    stems = set()
    for path in sorted(_PACKAGE_DIR.glob("*.py")):
        if path.name.startswith("_") or path.name in _NON_GUARD_MODULES:
            continue
        if path.stem in _EXCLUDED_INAPPLICABLE_DECLARATION:
            continue
        module = importlib.import_module("coordinator_core.bash_guards." + path.stem)
        if hasattr(module, "MATCHERS"):
            stems.add(path.stem)
    return frozenset(stems)


def _actual_matchers() -> Dict[str, Tuple[str, ...]]:
    """The live registration for EVERY `guard_roster()` entry -- read
    entirely through `guard_roster()` (AC1), with no module-presence
    scoping filter. This is the fix for the gap `docs/reference/guard-
    tool-name-membership.md` § 3z names: an inline `GuardEntry` (no
    backing module, or a module that never declares `MATCHERS`) is
    exactly as watched here as a module-scoped one. `roster.py`'s own
    structural-read discipline (never calling `GuardEntry.fn`) is
    inherited unchanged since this function never touches `.fn`.
    """
    return {entry.id: tuple(entry.matchers) for entry in guard_roster()}


def _compare(
    actual: Dict[str, Tuple[str, ...]], expected: Dict[str, _Expected]
) -> List[str]:
    """The two-directional comparison (AC2, AC3, AC4). One shared function
    so a narrowing, a held-guard widening, and an unclassified new guard
    all fail through the same, single code path -- there is no separate
    "widen" branch to accidentally leave un-symmetric with the "narrow"
    one.
    """
    failures: List[str] = []
    actual_ids = set(actual)
    expected_ids = set(expected)

    for missing in sorted(expected_ids - actual_ids):
        failures.append(
            "guard %r is in the expected mapping but not in the live "
            "guard_roster() registration -- removed or renamed? See %s."
            % (missing, _REFERENCE_DOC)
        )

    for extra in sorted(actual_ids - expected_ids):
        failures.append(
            "guard %r is registered in guard_roster() but absent from the "
            "expected mapping -- classify it (full-universe, or Bash-only "
            "with a kind) before it is admitted. See %s."
            % (extra, _REFERENCE_DOC)
        )

    for guard_id in sorted(actual_ids & expected_ids):
        exp = expected[guard_id].matchers
        found = actual[guard_id]
        if tuple(sorted(found)) != tuple(sorted(exp)):
            failures.append(
                "guard %r: expected matchers %r, found %r. See %s."
                % (guard_id, exp, found, _REFERENCE_DOC)
            )

    return failures


def test_discovery_found_the_expected_scope():
    """Guards the guard: pins the module-level-`MATCHERS`-declaring
    population this module's docstring derives -- 27 modules (25 full +
    2 Bash-only) -- not the plan's own unverified 19/5 estimate. This is a
    documentation fact about module declarations, distinct from (and
    smaller than) the 52-entry population `_actual_matchers()` enforces
    (see `test_every_registered_guard_is_classified`)."""
    stems = _scoped_module_stems()
    assert len(stems) == 27, sorted(stems)
    assert "block_stash_destruction" in stems
    assert "guard_powershell_via_bash" in stems
    assert "block_dev_repo_sentinel_removal" not in stems


def test_every_registered_guard_is_classified():
    """AC8's own precondition: if `_actual_matchers` or `EXPECTED` drift
    out of step with the live 52-entry chain, this fails loudly instead of
    every other assertion below passing vacuously by comparing an empty or
    partial set."""
    actual = _actual_matchers()
    assert len(actual) == 53, sorted(actual)
    assert set(actual) == set(EXPECTED)


def test_guard_matchers_ratchet():
    """AC8: green at HEAD, no guard module edited."""
    actual = _actual_matchers()
    failures = _compare(actual, EXPECTED)
    assert not failures, "\n".join(failures)


def test_every_entry_is_in_exactly_one_partition_bucket():
    """AC8: the three-valued partition itself. Every `EXPECTED` entry is
    exactly one of (1) dual-declaring with `kind=None`, (2) Bash-only with
    a kind in `_BASH_ONLY_KINDS`, or (3) dual-declaring with
    `kind=DUAL_DECLARING_BASH_DETECTING`. No entry may be Bash-only with
    `kind=None` (an unclassified Bash-only declaration), and no
    full-universe entry may carry a Bash-only kind."""
    bucket1 = bucket2 = bucket3 = 0
    for guard_id, exp in EXPECTED.items():
        is_bash_only = exp.matchers == ("Bash",)
        if exp.kind is None:
            assert not is_bash_only, (
                "%r is Bash-only with no kind -- every Bash-only entry "
                "must carry a written reason" % guard_id
            )
            bucket1 += 1
        elif exp.kind in _BASH_ONLY_KINDS:
            assert is_bash_only, (
                "%r carries a Bash-only kind but declares %r" % (guard_id, exp.matchers)
            )
            assert exp.reason, "%r's kind carries no reason" % guard_id
            bucket2 += 1
        elif exp.kind == DUAL_DECLARING_BASH_DETECTING:
            assert not is_bash_only, (
                "%r is DUAL_DECLARING_BASH_DETECTING but declares only "
                "Bash" % guard_id
            )
            assert exp.reason, "%r's kind carries no reason" % guard_id
            bucket3 += 1
        else:
            raise AssertionError("%r has an unrecognised kind %r" % (guard_id, exp.kind))
    assert bucket1 + bucket2 + bucket3 == len(EXPECTED) == 53
    assert bucket3 == 0, (
        "expected 0 dual-declaring-but-Bash-detecting entries -- C8's "
        "second pass converted all 9 (Finding 7 of the recensus record), "
        "found %d" % bucket3
    )


def test_powershell_via_bash_kind_is_pinned_and_carries_no_remediation(
) -> None:
    """AC6: `guard_powershell_via_bash`'s entry can never be silently
    "fixed" -- its kind is asserted directly (not merely implied by which
    bucket it happens to land in), and its reason text is checked for the
    words a remediation suggestion would use."""
    entry = EXPECTED["powershell-via-bash-guard"]
    assert entry.matchers == ("Bash",)
    assert entry.kind == BASH_ONLY_BY_CONSTRUCTION
    assert entry.reason is not None
    forbidden = ("widen", "todo", "fixme", "should be converted", "not yet converted")
    lowered = entry.reason.lower()
    assert not any(word in lowered for word in forbidden), entry.reason


def test_held_cohort_kinds_are_uniform_and_distinct_from_by_construction():
    """AC5, AC11: the kinds are machine-distinguishable and do not
    collapse into one "allowed Bash-only" set -- even now that the
    `held-pending-tokenizer-fix` cohort has been discharged and that kind
    is empty. The kind constant and its distinctness from
    `bash-only-by-construction` and `not-yet-converted` are still pinned;
    a future held guard lands in a non-empty set again without
    re-deriving the discipline."""
    held = [
        gid
        for gid, exp in EXPECTED.items()
        if exp.kind == HELD_PENDING_TOKENIZER_FIX
    ]
    assert held == []
    by_construction = {
        gid for gid, exp in EXPECTED.items() if exp.kind == BASH_ONLY_BY_CONSTRUCTION
    }
    not_yet_converted = {
        gid for gid, exp in EXPECTED.items() if exp.kind == NOT_YET_CONVERTED
    }
    assert len(by_construction) == 11
    # EMPTY as of 2026-08-26, and that is this plan's terminal state, not a
    # dropped assertion: pln-the-destructive-core-learns-th-d5ade0 converted
    # every entry that was carrying `not-yet-converted` -- Bucket B's four
    # git-shaped advisories (C4), Bucket D's two built-but-not-wired entries
    # (C9), and Bucket A's five (C2/C3, of which `runaway-find` moved to
    # `bash-only-by-construction` instead). The pin stays at an exact count
    # rather than being deleted, so a NEW entry parked here is visible as a
    # change to this line, with the same "write the reason down" pressure
    # the non-empty cohort carried.
    assert len(not_yet_converted) == 0
    assert by_construction.isdisjoint(not_yet_converted)
    assert set(held).isdisjoint(by_construction)
    assert HELD_PENDING_TOKENIZER_FIX != BASH_ONLY_BY_CONSTRUCTION != NOT_YET_CONVERTED


def test_dual_declaring_bash_detecting_kind_is_pinned():
    """AC8's bucket (3): EMPTY as of C8's second pass (Finding 7 of the
    recensus record). The 2026-08-26 recensus found nine members
    (Findings 2+3); the first C8 pass converted one
    (`destructive-git-revert`) and left the other eight here, believing
    them correct-as-drafted under the foreign-binary-argv carve-out. That
    read measured only base-argv identity -- re-measured against the
    PowerShell `Start-Process` anti-bypass surface, seven were real
    detection gaps (now converted) and the ninth
    (`destructive-git-revert-advisory`) was already covered by the first
    pass's shared-function fix. The pin stays at an exact (empty) set
    rather than being deleted, so a NEW bucket-3 member is visible as a
    change to this assertion, matching `test_held_cohort_kinds_are_
    uniform_and_distinct_from_by_construction`'s own convention for its
    `not_yet_converted` cohort."""
    members = {
        gid
        for gid, exp in EXPECTED.items()
        if exp.kind == DUAL_DECLARING_BASH_DETECTING
    }
    assert members == set()
    for gid in (
        "block-noncanonical-branch-creation",
        "block-subagent-commit",
        "block-subagent-grant-acquisition",
        "block-subagent-guard-grant",
        "branch-set-precedence",
        "longlived-branch-naming",
        "destructive-git-revert",
        "destructive-git-revert-advisory",
        "git-commit-safe-commit-advise",
    ):
        assert EXPECTED[gid].matchers == ("Bash", "PowerShell")
        assert EXPECTED[gid].kind is None


def test_narrowing_a_full_universe_guard_is_detected():
    """AC2, proven able to fail: a positive control built from a real
    snapshot of the live registration, narrowed in a LOCAL copy only --
    never mutates a real guard module."""
    actual = dict(_actual_matchers())
    victim = "block-approval-sentinel-creation"
    assert actual[victim] == ("Bash", "PowerShell")
    actual[victim] = ("Bash",)
    failures = _compare(actual, EXPECTED)
    assert any(victim in f for f in failures), failures


def test_widening_a_held_guard_is_detected():
    """AC3, proven able to fail: widening a Bash-only entry must fail too
    -- the ratchet is two-directional, not a floor. Uses
    `powershell-via-bash-guard`, a Bash-only-by-construction entry."""
    actual = dict(_actual_matchers())
    victim = "powershell-via-bash-guard"
    assert actual[victim] == ("Bash",)
    actual[victim] = ("Bash", "PowerShell")
    failures = _compare(actual, EXPECTED)
    assert any(victim in f for f in failures), failures


def test_unclassified_new_guard_is_detected():
    """AC4, proven able to fail: a guard present in the (simulated) live
    registration but absent from EXPECTED must fail rather than pass by
    omission."""
    actual = dict(_actual_matchers())
    actual["a-brand-new-guard"] = ("Bash", "PowerShell")
    failures = _compare(actual, EXPECTED)
    assert any("a-brand-new-guard" in f for f in failures), failures


def test_a_guard_removed_from_the_live_registration_is_detected():
    """The other half of AC4's "both directions": an EXPECTED entry with
    no matching live registration (e.g. a guard deleted or renamed without
    updating this mapping) must also fail, not silently pass."""
    actual = dict(_actual_matchers())
    del actual["powershell-via-bash-guard"]
    failures = _compare(actual, EXPECTED)
    assert any("powershell-via-bash-guard" in f for f in failures), failures


def test_narrowing_a_dual_declaring_bash_detecting_guard_is_also_detected():
    """AC8 bucket (3) is not a silent allow-list: narrowing one of the
    nine dual-declaring-but-Bash-detecting members must fail the ratchet
    identically to narrowing a bucket-(1) entry -- the exemption covers
    the DETECTION gap, not the DECLARATION."""
    actual = dict(_actual_matchers())
    victim = "block-subagent-commit"
    assert actual[victim] == ("Bash", "PowerShell")
    actual[victim] = ("Bash",)
    failures = _compare(actual, EXPECTED)
    assert any(victim in f for f in failures), failures

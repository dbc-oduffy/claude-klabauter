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

#: none of the three register their own `MATCHERS` contract. Governs only
_NON_GUARD_MODULES = {"dispatch.py", "dispatch_checks.py", "commit_tripwires.py"}

_EXCLUDED_INAPPLICABLE_DECLARATION = {"block_dev_repo_sentinel_removal"}

HELD_PENDING_TOKENIZER_FIX = "held-pending-tokenizer-fix"
BASH_ONLY_BY_CONSTRUCTION = "bash-only-by-construction"
NOT_YET_CONVERTED = "not-yet-converted"
DUAL_DECLARING_BASH_DETECTING = "dual-declaring-bash-detecting"

_BASH_ONLY_KINDS = frozenset({BASH_ONLY_BY_CONSTRUCTION, NOT_YET_CONVERTED})


@dataclass(frozen=True)
class _Expected:
    matchers: Tuple[str, ...]
    kind: Optional[str] = None
    reason: Optional[str] = None


EXPECTED: Dict[str, _Expected] = {
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
    "no-verify": _Expected(("Bash", "PowerShell")),
    "destructive-rm": _Expected(("Bash", "PowerShell")),
    "destructive-git-orphan": _Expected(("Bash", "PowerShell")),
    "destructive-git-clean": _Expected(("Bash", "PowerShell")),
    "blanket-git-add": _Expected(("Bash", "PowerShell")),
    "guard-doctrine-surface-bash-write": _Expected(("Bash", "PowerShell")),
    "guard-repo-setup-claude-home-refusal": _Expected(("Bash", "PowerShell")),
    "guard-host-subagent-bash-spawn-shapes": _Expected(("Bash", "PowerShell")),
    # p4-verb-fence: full-universe (`MATCHERS = COMMAND_TOOL_NAMES`) and
    "p4-verb-fence": _Expected(("Bash", "PowerShell")),
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
    # is now literally true of both. Leaving them at NOT_YET_CONVERTED
    "block-dev-repo-sentinel-removal-advisory": _Expected(
        ("Bash", "PowerShell"),
    ),
    "stash-apply-verification-advisory": _Expected(
        ("Bash", "PowerShell"),
    ),
    "block-fleet-delegation-creation": _Expected(
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
    "validate-commit": _Expected(
        ("Bash", "PowerShell"),
    ),
    # -- formerly dual-declaring-but-Bash-detecting (9), now CONVERTED
    # inline) found these nine declaring `COMMAND_TOOL_NAMES` with zero
    # Moving them here (rather than leaving DUAL_DECLARING_BASH_DETECTING
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
    "destructive-git-revert": _Expected(
        ("Bash", "PowerShell"),
    ),
    "destructive-git-revert-advisory": _Expected(
        ("Bash", "PowerShell"),
    ),
    "git-commit-safe-commit-advise": _Expected(
        ("Bash", "PowerShell"),
    ),
    # matchers=COMMAND_TOOL_NAMES (full-universe) but unclassified here
    "stale-write": _Expected(
        ("Bash", "PowerShell"),
        DUAL_DECLARING_BASH_DETECTING,
        "Registers full-universe (matchers=COMMAND_TOOL_NAMES) but its "
        "candidate resolver tokenizes via the generic POSIX-shaped "
        "`_command_tokenizer` and matches only a bare `>` redirect or "
        "bare `tee` invocation -- no `_dialect`/`resolve_segments_for_"
        "dialect` call anywhere in the check, so a PowerShell-dialect "
        "redirect is chain-eligible but not actually detected.",
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
    population this module's docstring derives -- not the plan's own
    unverified 19/5 estimate. This is a documentation fact about module
    declarations, distinct from (and smaller than) the 51-entry population
    `_actual_matchers()` enforces (see
    `test_every_registered_guard_is_classified`).

    Both counts rose by one on 2026-08-30 with
    `block_fleet_delegation_creation`, which had been live in
    `guard_roster()` and in this scan while classified in neither pin --
    the drift this pin exists to make loud, working as intended.

    Rose by one again with `p4_verb_fence` (full-universe, dialect-reading;
    docs/reference/guard-tool-name-membership.md § 3), discovered in the
    roster with no corpus/ratchet classification -- 28 -> 29.

    Narrowed 29 -> 27 on 2026-09-19 (docs/plans/2026-08-21-the-advisory-
    band-gets-smaller-cheaper-and-honest.md, C6): `guard_branch_set_
    precedence.py` and `guard_longlived_branch_naming.py` -- both
    module-level `MATCHERS`-declaring -- were deleted. Re-measured live
    against `_scoped_module_stems()` rather than re-derived by arithmetic,
    per this pin's own charter."""
    stems = _scoped_module_stems()
    assert len(stems) == 27, sorted(stems)
    assert "block_stash_destruction" in stems
    assert "guard_powershell_via_bash" in stems
    assert "block_dev_repo_sentinel_removal" not in stems


def test_every_registered_guard_is_classified():
    """AC8's own precondition: if `_actual_matchers` or `EXPECTED` drift
    out of step with the live 53-entry chain, this fails loudly instead of
    every other assertion below passing vacuously by comparing an empty or
    partial set. Narrowed 54 -> 51 (docs/plans/2026-08-21-the-advisory-
    band-gets-smaller-cheaper-and-honest.md, C6): `branch-set-precedence`
    and `longlived-branch-naming` deleted. Widened 51 -> 52 by the same
    process that had already made this docstring's own count disagree with
    the assertion below it before this reconciliation (the 52nd entry
    never got its assertion bumped). Widened 52 -> 53 reconciling the
    2026-09-20 origin/main merge: `stale-write` (C2,
    docs/plans/2026-09-02-a-write-that-discards-what-you-never-saw.md)
    arrived live in `guard_roster()` on origin/main only -- absent from
    this branch's pre-merge tip -- and had no classification here."""
    actual = _actual_matchers()
    assert len(actual) == 53, sorted(actual)
    assert set(actual) == set(EXPECTED)


def test_guard_matchers_ratchet():
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
    assert bucket3 == 1, (
        "expected 1 dual-declaring-but-Bash-detecting entry (`stale-write`, "
        "merged in from origin/main 2026-09-20 -- see EXPECTED's own "
        "comment) -- C8's second pass converted all 9 recensus-era members "
        "(Finding 7), leaving the bucket empty until this arrival, "
        "found %d" % bucket3
    )


def test_powershell_via_bash_kind_is_pinned_and_carries_no_remediation(
) -> None:
    entry = EXPECTED["powershell-via-bash-guard"]
    assert entry.matchers == ("Bash",)
    assert entry.kind == BASH_ONLY_BY_CONSTRUCTION
    assert entry.reason is not None
    forbidden = ("widen", "todo", "fixme", "should be converted", "not yet converted")
    lowered = entry.reason.lower()
    assert not any(word in lowered for word in forbidden), entry.reason


def test_held_cohort_kinds_are_uniform_and_distinct_from_by_construction():
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
    `not_yet_converted` cohort. Narrowed to seven (from nine) 2026-09-19
    (docs/plans/2026-08-21-the-advisory-band-gets-smaller-cheaper-and-
    honest.md, C6): `branch-set-precedence` and `longlived-branch-naming`
    were deleted, not converted -- their rows leave this tuple rather than
    being replaced. Widened to one member 2026-09-20 reconciling the
    origin/main merge: `stale-write` arrived live with zero `_dialect`
    references in its own check, the same signature this bucket's other
    (now-converted) members carried -- see EXPECTED's own comment on that
    entry."""
    members = {
        gid
        for gid, exp in EXPECTED.items()
        if exp.kind == DUAL_DECLARING_BASH_DETECTING
    }
    assert members == {"stale-write"}
    assert EXPECTED["stale-write"].matchers == ("Bash", "PowerShell")
    assert EXPECTED["stale-write"].kind == DUAL_DECLARING_BASH_DETECTING
    for gid in (
        "block-noncanonical-branch-creation",
        "block-subagent-commit",
        "block-subagent-grant-acquisition",
        "block-subagent-guard-grant",
        "destructive-git-revert",
        "destructive-git-revert-advisory",
        "git-commit-safe-commit-advise",
    ):
        assert EXPECTED[gid].matchers == ("Bash", "PowerShell")
        assert EXPECTED[gid].kind is None


def test_narrowing_a_full_universe_guard_is_detected():
    actual = dict(_actual_matchers())
    victim = "block-approval-sentinel-creation"
    assert actual[victim] == ("Bash", "PowerShell")
    actual[victim] = ("Bash",)
    failures = _compare(actual, EXPECTED)
    assert any(victim in f for f in failures), failures


def test_widening_a_held_guard_is_detected():
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

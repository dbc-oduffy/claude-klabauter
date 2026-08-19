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
"MATCHERS")` only to decide which modules this ratchet WATCHES (the same
scoping test.tool_name_membership.py already runs); the compared VALUE for
every guard always comes from `guard_roster()`.

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
excluded below, not silently (see `_EXCLUDED_INAPPLICABLE_DECLARATION`).
The real tracked population is 18 full-universe + 5 Bash-only = 23.
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
#: none of the three register their own `MATCHERS` contract.
_NON_GUARD_MODULES = {"dispatch.py", "dispatch_checks.py", "commit_tripwires.py"}

#: See module docstring's "Population note". Kept to one name, not a
#: general-purpose skip list -- a second entry here would need the same
#: level of on-disk justification as this one carries.
_EXCLUDED_INAPPLICABLE_DECLARATION = {"block_dev_repo_sentinel_removal"}

HELD_PENDING_TOKENIZER_FIX = "held-pending-tokenizer-fix"
BASH_ONLY_BY_CONSTRUCTION = "bash-only-by-construction"


@dataclass(frozen=True)
class _Expected:
    matchers: Tuple[str, ...]
    kind: Optional[str] = None
    reason: Optional[str] = None


#: The full tracked population (AC1, AC5). Full-universe entries carry no
#: `kind` -- only a Bash-only entry needs an exemption record. Every
#: Bash-only entry below carries a machine-distinguishable `kind` plus
#: prose (AC5, AC7).
EXPECTED: Dict[str, _Expected] = {
    # -- full-universe (18) --
    "block-approval-sentinel-creation": _Expected(("Bash", "PowerShell")),
    "block-disarm-marker-sentinel-creation": _Expected(("Bash", "PowerShell")),
    "block-illegal-filename": _Expected(("Bash", "PowerShell")),
    "block-noncanonical-branch-creation": _Expected(("Bash", "PowerShell")),
    "block-reviewer-bash-outside-allowlist": _Expected(("Bash", "PowerShell")),
    "block-subagent-commit": _Expected(("Bash", "PowerShell")),
    "block-subagent-grant-acquisition": _Expected(("Bash", "PowerShell")),
    "block-subagent-guard-grant": _Expected(("Bash", "PowerShell")),
    "block-subagent-plan-body-bash-write": _Expected(("Bash", "PowerShell")),
    "block-worktree-sentinel-creation": _Expected(("Bash", "PowerShell")),
    "check-raw-pid-liveness": _Expected(("Bash", "PowerShell")),
    "check-test-suite-invocation": _Expected(("Bash", "PowerShell")),
    "branch-set-precedence": _Expected(("Bash", "PowerShell")),
    "grep-via-bash-guard": _Expected(("Bash", "PowerShell")),
    "inprocess-search": _Expected(("Bash", "PowerShell")),
    "longlived-branch-naming": _Expected(("Bash", "PowerShell")),
    "multiprobe-banner": _Expected(("Bash", "PowerShell")),
    "plumbing-and-loops": _Expected(("Bash", "PowerShell")),
    # -- held, pending the shlex-tokenizer fallback fix (4) --
    "block-stash-destruction": _Expected(
        ("Bash",),
        HELD_PENDING_TOKENIZER_FIX,
        "if tokens is None: return _evaluate_legacy(cmd) fails CLOSED on "
        "unparseable input; PowerShell here-strings/backtick escapes defeat "
        "the shlex tokenizer feeding it, so widening ships a live "
        "spurious-deny path.",
    ),
    "block-subagent-destructive-action": _Expected(
        ("Bash",),
        HELD_PENDING_TOKENIZER_FIX,
        "Same fail-closed shlex fallback as block-stash-destruction.",
    ),
    "block-subagent-stash-creation": _Expected(
        ("Bash",),
        HELD_PENDING_TOKENIZER_FIX,
        "Same fail-closed shlex fallback as block-stash-destruction.",
    ),
    "block-worktree-creation": _Expected(
        ("Bash",),
        HELD_PENDING_TOKENIZER_FIX,
        "Same fail-closed shlex fallback as block-stash-destruction.",
    ),
    # -- Bash-only by construction, never a conversion candidate (1) --
    "powershell-via-bash-guard": _Expected(
        ("Bash",),
        BASH_ONLY_BY_CONSTRUCTION,
        "Guards PowerShell invoked VIA the Bash tool; the PowerShell tool "
        "is not a surface it can meaningfully watch.",
    ),
}


def _scoped_module_stems() -> FrozenSet[str]:
    """Which guard modules this ratchet watches -- membership only,
    determined by `hasattr(module, "MATCHERS")` (a presence check, not a
    read of the tuple's contents). The compared VALUE always comes from
    `guard_roster()` (AC1); this function only answers "is this module in
    scope," same role `test_tool_name_membership.py::_guard_modules_with_
    matchers` already plays for its own, differently-shaped sweep.
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


def _scoped_actual_matchers() -> Dict[str, Tuple[str, ...]]:
    """The live registration, restricted to the scoped modules -- read
    entirely through `guard_roster()` (AC1). A roster entry is in scope
    when its `script` tail names one of the scoped modules; `roster.py`'s
    own structural-read discipline (never calling `GuardEntry.fn`) is
    inherited unchanged since this function never touches `.fn`.
    """
    scoped_scripts = {"bash_guards/%s.py" % stem for stem in _scoped_module_stems()}
    return {
        entry.id: tuple(entry.matchers)
        for entry in guard_roster()
        if entry.script in scoped_scripts
    }


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
    """Guards the guard (AC8's own precondition): if `_scoped_module_stems`
    breaks, every assertion below passes vacuously by finding nothing to
    compare. Pins the population size this module's docstring derives --
    18 full-universe + 5 Bash-only = 23 -- not the plan's own unverified
    19/5 estimate."""
    stems = _scoped_module_stems()
    assert len(stems) == 23, sorted(stems)
    assert "block_stash_destruction" in stems
    assert "guard_powershell_via_bash" in stems
    assert "block_dev_repo_sentinel_removal" not in stems


def test_guard_matchers_ratchet():
    """AC8: green at HEAD, no guard module edited."""
    actual = _scoped_actual_matchers()
    failures = _compare(actual, EXPECTED)
    assert not failures, "\n".join(failures)


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
    """AC5: the two kinds are machine-distinguishable and do not collapse
    into one "allowed Bash-only" set."""
    held = [
        gid
        for gid, exp in EXPECTED.items()
        if exp.kind == HELD_PENDING_TOKENIZER_FIX
    ]
    assert sorted(held) == [
        "block-stash-destruction",
        "block-subagent-destructive-action",
        "block-subagent-stash-creation",
        "block-worktree-creation",
    ]
    by_construction = [
        gid for gid, exp in EXPECTED.items() if exp.kind == BASH_ONLY_BY_CONSTRUCTION
    ]
    assert by_construction == ["powershell-via-bash-guard"]
    assert set(held).isdisjoint(by_construction)


def test_narrowing_a_full_universe_guard_is_detected():
    """AC2, proven able to fail: a positive control built from a real
    snapshot of the live registration, narrowed in a LOCAL copy only --
    never mutates a real guard module."""
    actual = dict(_scoped_actual_matchers())
    victim = "block-approval-sentinel-creation"
    assert actual[victim] == ("Bash", "PowerShell")
    actual[victim] = ("Bash",)
    failures = _compare(actual, EXPECTED)
    assert any(victim in f for f in failures), failures


def test_widening_a_held_guard_is_detected():
    """AC3, proven able to fail: widening a held guard must fail too --
    the ratchet is two-directional, not a floor."""
    actual = dict(_scoped_actual_matchers())
    victim = "block-stash-destruction"
    assert actual[victim] == ("Bash",)
    actual[victim] = ("Bash", "PowerShell")
    failures = _compare(actual, EXPECTED)
    assert any(victim in f for f in failures), failures


def test_unclassified_new_guard_is_detected():
    """AC4, proven able to fail: a guard present in the (simulated) live
    registration but absent from EXPECTED must fail rather than pass by
    omission."""
    actual = dict(_scoped_actual_matchers())
    actual["a-brand-new-guard"] = ("Bash", "PowerShell")
    failures = _compare(actual, EXPECTED)
    assert any("a-brand-new-guard" in f for f in failures), failures


def test_a_guard_removed_from_the_live_registration_is_detected():
    """The other half of AC4's "both directions": an EXPECTED entry with
    no matching live registration (e.g. a guard deleted or renamed without
    updating this mapping) must also fail, not silently pass."""
    actual = dict(_scoped_actual_matchers())
    del actual["powershell-via-bash-guard"]
    failures = _compare(actual, EXPECTED)
    assert any("powershell-via-bash-guard" in f for f in failures), failures

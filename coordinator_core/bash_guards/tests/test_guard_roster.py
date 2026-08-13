"""Tests for `coordinator_core.bash_guards.roster.guard_roster` -- the
public, payload-free enumeration of `dispatch.py::_build_guard_chain`'s
live registration.

Purpose: pin the roster's TRUTH relationship to the live registration as
guards keep changing under it, never a census of it. Reads the live
registration exactly as `test_guard_band_membership.py::_dummy_chain` does
-- `dispatch._build_guard_chain(...)` with an inert dummy command/payload,
never invoking a returned `GuardEntry.fn` closure.

Spec backlink: docs/plans/2026-08-13-guard-roster-export.md, chunk C4 (AC3).

Negative spec: no assertion here pins an exact guard COUNT or a hardcoded
name list -- the registration this file reads is actively changing under a
peer session's live edits to `dispatch.py`, and a count/list assertion
would fail on their landing rather than on a real defect. Every assertion
below is a RELATIONSHIP between the roster and the chain, not a snapshot of
either.
"""
from __future__ import annotations

import inspect
import json
import subprocess
import sys
from typing import List

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import roster as roster_module
from coordinator_core.bash_guards.dispatch import GuardEntry
from coordinator_core.bash_guards.roster import GuardRosterEntry, guard_roster
from coordinator_core.ops.session.guard_settings_integrity import _tail_key

# `test_lazy_reexport_resolves_and_stays_lazy` spawns a real
# `sys.executable -c` fresh interpreter because the property under test --
# that importing `coordinator_core.bash_guards` alone does not pull in
# `guard_settings_integrity` -- is only observable in a process that has
# never imported the heavy module, which no mock or same-process trick can
# fake. The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue
# and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _dummy_chain() -> List[GuardEntry]:
    """Same call shape as `test_guard_band_membership.py::_dummy_chain` --
    proven safe by that file's own executed evidence. `fn` closures are
    never invoked, only registration-time attributes are inspected."""
    return dispatch._build_guard_chain(
        cmd="echo guard-roster-test-probe",
        session_id="guard-roster-test-probe",
        cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "echo x"}},
        policy_file=None,
        host_is_windows=None,
    )


def test_guard_roster_takes_no_arguments():
    """`guard_roster()` is a payload-free public seam -- assert via the
    signature itself, never by calling it wrongly and catching TypeError."""
    sig = inspect.signature(guard_roster)
    assert list(sig.parameters) == [], (
        "guard_roster() must take no arguments -- found parameters: %r"
        % list(sig.parameters)
    )


def test_roster_is_bijective_with_the_live_registration():
    """AC3: every `GuardEntry` from a structural chain read has exactly one
    roster entry and vice versa -- same names, same order, no drops, no
    extras. This is the assertion that makes the roster trustworthy rather
    than merely present."""
    chain = _dummy_chain()
    roster = guard_roster()

    chain_names = [entry.name for entry in chain]
    roster_names = [entry.id for entry in roster]

    assert roster_names == chain_names, (
        "roster ids diverge from the live registration's names/order -- "
        "chain: %r, roster: %r" % (chain_names, roster_names)
    )
    assert len(set(roster_names)) == len(roster_names), (
        "roster carries a duplicate id: %r" % roster_names
    )


def test_per_guard_matchers_are_identical_to_registration_never_widened():
    """Each entry's `matchers` is identical to its registration's,
    INCLUDING every guard still at `("Bash",)`. A test that only checked
    the widened guards would pass on a roster that quietly reported the
    universe everywhere."""
    chain = _dummy_chain()
    roster = guard_roster()
    roster_by_id = {entry.id: entry for entry in roster}

    mismatches = []
    bash_only_seen = 0
    widened_seen = 0
    for entry in chain:
        roster_entry = roster_by_id.get(entry.name)
        assert roster_entry is not None, "no roster entry for %r" % entry.name
        expected = tuple(entry.matchers)
        if roster_entry.matchers != expected:
            mismatches.append((entry.name, expected, roster_entry.matchers))
        if expected == ("Bash",):
            bash_only_seen += 1
        else:
            widened_seen += 1

    assert not mismatches, (
        "roster matchers diverge from registration (name, expected, got): %r"
        % mismatches
    )
    # Derive both populations from the chain itself -- never hardcode counts.
    assert bash_only_seen > 0, "expected at least one ('Bash',)-only guard in the live chain"
    assert widened_seen > 0, "expected at least one widened-matcher guard in the live chain"


def test_no_entry_silently_falls_back_to_the_dispatcher_module():
    """`_resolve_referenced_module` returns `None` for a registration shape
    it cannot walk, and `_script_tail_for` then silently falls back to
    reporting the dispatcher's own module -- wrong, but not a crash. This
    assertion converts that silent mis-attribution into a caught failure."""
    roster = guard_roster()
    dispatcher_tail = _tail_key(dispatch.__name__.replace(".", "/") + ".py")

    offenders = [entry.id for entry in roster if entry.script == dispatcher_tail]
    assert not offenders, (
        "roster entries silently fell back to reporting dispatch.py's own "
        "module as their script (a resolution failure, not a real "
        "attribution): %r" % offenders
    )


def test_every_script_matches_tail_keys_normal_form():
    """Every `script` matches `_tail_key`'s own normal form -- verified
    against `_tail_key` itself rather than a regex reimplementation of it."""
    roster = guard_roster()
    for entry in roster:
        renormalized = _tail_key(entry.script)
        assert renormalized == entry.script, (
            "%r's script %r is not already in _tail_key's normal form "
            "(renormalizes to %r)" % (entry.id, entry.script, renormalized)
        )
        assert entry.script.count("/") == 1, (
            "%r's script %r is not exactly two path segments: %r"
            % (entry.id, entry.script, entry.script)
        )
        assert entry.script == entry.script.lower(), (
            "%r's script %r is not lowercased" % (entry.id, entry.script)
        )


def test_roster_is_plain_json_serialisable_data():
    """The return is JSON-serialisable after a trivial tuple->list
    coercion, carrying no closures and no live `GuardEntry` references."""
    roster = guard_roster()
    assert isinstance(roster, tuple)
    for entry in roster:
        assert isinstance(entry, GuardRosterEntry)
        assert not isinstance(entry, GuardEntry)

    plain = [
        {
            "id": entry.id,
            "matchers": list(entry.matchers),
            "band": entry.band,
            "fail_closed": entry.fail_closed,
            "script": entry.script,
        }
        for entry in roster
    ]
    # Raises if anything here is not plain data (e.g. a closure or a live
    # GuardEntry leaking through).
    json.dumps(plain)


def test_known_guard_module_pairs_pin_ground_truth():
    """Hand-verified anchors for a small set of guard->module pairs, checked
    directly against dispatch.py's own registration source rather than
    against `_tail_key`'s normal form alone -- the latter would pass even
    if `_resolve_referenced_module` silently misattributed a guard, since a
    wrong-but-well-formed tail still satisfies the normal-form shape.

    Deliberately NOT a census of all 46 entries (that would fail on the
    peer session's in-flight `dispatch.py` landings rather than on a real
    defect) -- just enough anchors to catch a future
    `co_names`-vs-`LOAD_GLOBAL` style misattribution regression.

      - "no-verify" -> `_dc.check_no_verify(...)`, a shape-1 direct global
        (`_dc` is `dispatch_checks` imported as `_dc`).
      - "block-worktree-creation" -> `_check_worktree_creation(payload)`, a
        shape-1 direct global imported from `block_worktree_creation.py`.
      - "destructive-git-revert" -> `lambda: _git_revert_full()[0]`, a
        shape-2 closure: `_git_revert_full` is `def`'d INSIDE
        `_build_guard_chain` (so its own `__module__` is `dispatch`), and
        its body calls `_dc._check_destructive_git_revert_full(...)` --
        the 2-hop recursion case, resolving to the same `dispatch_checks`
        module as "no-verify" above.
    """
    roster = guard_roster()
    roster_by_id = {entry.id: entry.script for entry in roster}

    expected = {
        "no-verify": "bash_guards/dispatch_checks.py",
        "block-worktree-creation": "bash_guards/block_worktree_creation.py",
        "destructive-git-revert": "bash_guards/dispatch_checks.py",
    }
    for guard_id, expected_script in expected.items():
        assert guard_id in roster_by_id, "expected guard %r missing from roster" % guard_id
        assert roster_by_id[guard_id] == expected_script, (
            "%r resolved to %r, expected %r"
            % (guard_id, roster_by_id[guard_id], expected_script)
        )


def test_lazy_reexport_resolves_and_stays_lazy():
    """`from coordinator_core.bash_guards import guard_roster` resolves,
    and importing the package alone does NOT import
    `coordinator_core.ops.session.guard_settings_integrity` -- the hot-path
    laziness `__init__.py`'s own negative spec calls out. Run in a fresh
    interpreter: this property is order-sensitive within one process (an
    earlier test in this same session may already have imported the heavy
    module), so a same-process assertion here would be flaky rather than
    meaningful."""
    probe = (
        "import sys\n"
        "import coordinator_core.bash_guards as bg\n"
        "assert 'coordinator_core.ops.session.guard_settings_integrity' not in sys.modules, "
        "'importing bash_guards alone must not import guard_settings_integrity'\n"
        "fn = bg.guard_roster\n"
        "assert callable(fn)\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        "fresh-interpreter lazy re-export probe failed:\nstdout=%s\nstderr=%s"
        % (result.stdout, result.stderr)
    )
    assert "OK" in result.stdout

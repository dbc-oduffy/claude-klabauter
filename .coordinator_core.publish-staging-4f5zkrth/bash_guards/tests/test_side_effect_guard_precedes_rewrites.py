"""Ordering pin for the side-effect-only guard invariant added alongside
`reap-stale-git-lock`'s move ahead of `git-no-optional-locks` (bug-backlog
2026-08-12-guard-chain-returns-on-first-non-none-so-ac0e3b775cb1).

`dispatch.py`'s guard loop returns on the first non-`None` envelope
(`_build_guard_chain`'s caller, `evaluate_payload_json`, ~L1315's `return
out`). A guard whose entire mechanism is an on-disk side effect (it always
returns `None`) is therefore unreachable for any call shape where an
earlier-registered guard fires a rewrite/deny -- the side effect never runs.
`reap-stale-git-lock` is exactly that guard: its whole job is reaping an
orphaned `.git/index.lock` before a lock-taking git invocation, and it must
be registered ahead of `git-no-optional-locks` (which returns a rewrite
envelope for `git status`/bare `git diff`) or it starves for those two
command shapes in every repo.

This test asserts the property structurally, over the live registration via
`dispatch._build_guard_chain(...)` (same introspection seam
`test_guard_band_membership.py` uses), rather than by regexing source text
or hardcoding line numbers -- it keeps holding as the ADVISORY_REWRITE band
grows or reorders, so long as the invariant itself (side-effect-only guard
precedes every guard that can return a rewrite envelope) is preserved.
"""
from __future__ import annotations

import json
import os
import time
from typing import List

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards.dispatch import GuardEntry

# Guards known, by registration comment / module docstring, to be
# side-effect-only: their `fn` closure always returns `None` regardless of
# command shape. `reap-stale-git-lock` is the sole member today; a future
# side-effect-only guard should be added here rather than invented a
# separate ordering test.
_SIDE_EFFECT_ONLY_GUARD_NAMES = frozenset({"reap-stale-git-lock"})

# Guards known to return a non-`None` (rewrite or deny) envelope for at
# least one command shape -- i.e. guards that CAN starve a side-effect-only
# guard registered after them in this first-wins chain. `git-no-optional-
# locks` is the guard the filed bug traced this against (rewrites bare
# `git status`/`git diff`); named explicitly here so a regression in this
# one pairing is caught even if other rewriting guards are later added
# without updating this set.
_KNOWN_REWRITING_GUARD_NAMES = frozenset({"git-no-optional-locks"})


def _dummy_chain() -> List[GuardEntry]:
    """Build the real registration with harmless dummy call-time arguments
    -- mirrors `test_guard_band_membership.py`'s `_dummy_chain`. None of the
    `fn` closures are invoked; only `name` (a registration-time fact) is
    inspected."""
    return dispatch._build_guard_chain(
        cmd="echo bash-guard-side-effect-ordering-probe",
        session_id="side-effect-ordering-probe",
        cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "echo x"}},
        policy_file=None,
        host_is_windows=None,
    )


def test_side_effect_only_guards_are_registered_in_the_chain():
    """Guard the guard: if the named side-effect-only guard falls out of
    the registration entirely, the ordering assertion below would pass
    vacuously."""
    names = [entry.name for entry in _dummy_chain()]
    for name in _SIDE_EFFECT_ONLY_GUARD_NAMES:
        assert name in names, (
            "%r is not registered in the guard chain -- update this test's "
            "expectations if it was intentionally removed" % name
        )


def test_known_rewriting_guards_are_registered_in_the_chain():
    """Same vacuous-pass guard for the rewriting-guard side of the pairing."""
    names = [entry.name for entry in _dummy_chain()]
    for name in _KNOWN_REWRITING_GUARD_NAMES:
        assert name in names, (
            "%r is not registered in the guard chain -- update this test's "
            "expectations if it was intentionally removed" % name
        )


def test_every_side_effect_only_guard_precedes_every_known_rewriting_guard():
    """The invariant itself: in registration order, every side-effect-only
    guard's index must be strictly less than every known rewriting guard's
    index. Registration order is what the first-wins loop actually walks,
    so index comparison over the live chain is the correct (not merely
    convenient) way to assert this -- a guard registered "later" in this
    list is unreachable behind an earlier one that fires."""
    chain = _dummy_chain()
    index_by_name = {entry.name: i for i, entry in enumerate(chain)}

    for side_effect_name in _SIDE_EFFECT_ONLY_GUARD_NAMES:
        side_effect_index = index_by_name[side_effect_name]
        for rewriting_name in _KNOWN_REWRITING_GUARD_NAMES:
            rewriting_index = index_by_name[rewriting_name]
            assert side_effect_index < rewriting_index, (
                "side-effect-only guard %r (index %d) is registered AFTER "
                "rewriting guard %r (index %d) -- a fired rewrite from "
                "%r would starve %r's side effect for the command shapes "
                "%r rewrites, exactly the bug this test pins against a "
                "regression"
                % (
                    side_effect_name,
                    side_effect_index,
                    rewriting_name,
                    rewriting_index,
                    rewriting_name,
                    side_effect_name,
                    rewriting_name,
                )
            )


class TestGitStatusReachesReaperEndToEnd:
    """Confirms the fix against the actual bug, not just the registration
    order: a `git status` invocation with a stale, orphaned
    `.git/index.lock` present must have the lock reaped even though
    `git-no-optional-locks` ALSO fires a rewrite envelope for the same
    command. Before the fix, `git-no-optional-locks`'s earlier registration
    returned first and the reaper never ran; the lock would still be
    present after the call."""

    def test_stale_lock_is_reaped_for_git_status_despite_the_rewrite(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_AGE_SEC", "10")
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        lock = git_dir / "index.lock"
        lock.write_text("")
        old = time.time() - 999
        os.utime(lock, (old, old))

        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "session_id": "side-effect-e2e-probe",
            "cwd": str(tmp_path),
        }
        out = dispatch.evaluate_payload_json(json.dumps(payload))

        assert not lock.exists(), (
            "reap-stale-git-lock must reap the orphaned lock even though "
            "git-no-optional-locks also fires a rewrite for `git status` -- "
            "if this fails, reap-stale-git-lock is registered behind a "
            "rewriting guard again"
        )
        assert out is not None, (
            "git-no-optional-locks' own rewrite envelope must still be "
            "returned for `git status` -- this test only pins the reaper's "
            "side effect, not the rewrite's own envelope shape"
        )

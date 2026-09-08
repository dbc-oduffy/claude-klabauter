"""
Per-site assertions that each declared safe direction actually holds, plus the
meta-test that refuses a declaring site with no assertion.

## Specimen census (the five real sites this primitive was generalised from)

MIGRATED (declaration + assertion below):
  1. `concurrency_probe.evaluate_escape_hatch` -- FALL_BACK to a refusal.
  2. `concurrency_probe.default_physical_cores` -- FALL_BACK to os.cpu_count().
  3. `concurrency_probe.default_usable_ram_gb` -- RAISE.

DECLARED AS-IS, deliberately not migrated:
  4. `concurrency_probe.compute_parallelism_cap` and `validate_levels` raise on
     INVALID ARGUMENTS, which is a different thing from an undeterminable
     precondition: the caller supplied the wrong value and can see that it did.
     Decorating them would stretch the vocabulary to cover ordinary argument
     validation and make the declaration mean nothing.
  5. `DoE-claude coordinator/hooks/scripts/_posture.py` is coordinator-claude's
     file and may not be edited from here (this baton's anti-scope). Its
     discipline is pinned behaviourally by `test_posture_fail_direction.py`.

`cloud-em-03` and `cloud-em-04` were expected to add two more specimens. This
baton generalised from the five that existed then rather than waiting, because
it gates `cloud-em-02`.

  6. ADDED by `cloud-em-04`: `install.derive_worker_cap.cap_command_for_this_box`
     -- FALL_BACK to the command as committed, asserted in its own test file
     (see `_ASSERTED_IN_SIBLING_TESTS`).
     It is the first declaring site outside `concurrency_probe`, which is why
     the roster below scans a tuple of modules rather than one.

## Bucket-1 cloud-guard-party-screen specimens (chunk C7)

Seven `bash_guards`/`write_guards` sites the 2026-09-06 party-screen audit
(`state/audits/2026-09-06-cloud-guard-party-screen.md`) recorded as
`verdict: premise-false` and `provenance: enforced-here-and-stated-here` --
this chunk's exact, epistemic-premise-gate-discharged site set (C7's brief,
"THE SITE SET"). Each below is a BEHAVIOUR-PRESERVING decoration: the
`safe_direction` values are taken verbatim from the audit row, never
re-derived here. `nudge_windows_subprocess_popup.check` is NOT among them --
its module docstring's own negative-spec ("Never raises: any unexpected
input shape returns None") flatly contradicts the audit's `RAISE` value for
that row, so per C7's own instruction ("if you believe a row's direction is
wrong, STOP and report it; do not silently decorate the other way") that one
site is left undecorated and reported as a discrepancy rather than guessed.
"""

from __future__ import annotations

import builtins
import contextlib
import importlib
import os

import pytest

from coordinator_core.benchmarks import concurrency_probe as cp
from coordinator_core.install import derive_worker_cap as dwc
from coordinator_core.bash_guards import guard_reap_stale_git_lock as grsg
from coordinator_core.bash_guards import guard_no_optional_locks as gnol
from coordinator_core.bash_guards import guard_offer_git_c as gogc
from coordinator_core.bash_guards import block_worktree_sentinel_creation as bwsc
from coordinator_core.bash_guards import block_noncanonical_branch_creation as bncbc
from coordinator_core.write_guards import block_worktree_sentinel_write as bwsw
from coordinator_core.write_guards import nudge_peer_notice_unread as npnu
from coordinator_core.conservatism import (
    SafeDirection,
    declares_safe_direction,
    iter_declarations,
)
from coordinator_core.conservatism.verify import assert_safe_direction_holds


@contextlib.contextmanager
def _no_psutil():
    """Make `import psutil` fail for the duration of the block -- the exact
    precondition both psutil-backed specimens cannot determine."""
    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "psutil" or name.startswith("psutil."):
            raise ImportError("psutil blocked by test")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _blocked
    try:
        yield
    finally:
        builtins.__import__ = real_import


def test_default_usable_ram_gb_raises_as_declared():
    pytest.importorskip("psutil", reason="the control run needs a real reading to be distinguishable")
    assert_safe_direction_holds(
        cp.default_usable_ram_gb,
        invoke=cp.default_usable_ram_gb,
        undeterminable=_no_psutil,
        # Without this, the RAISE branch accepts ANY exception raised inside
        # `_no_psutil()` as proof the site refused -- including one from a
        # broken fixture, which would read as compliance.
        expect_raises=ImportError,
    )


def test_default_physical_cores_falls_back_as_declared():
    assert_safe_direction_holds(
        cp.default_physical_cores,
        invoke=cp.default_physical_cores,
        undeterminable=_no_psutil,
        # control=False, with the reason the helper demands: on a box without SMT
        # psutil's physical count EQUALS os.cpu_count(), so the intact path is
        # indistinguishable from the anchor and the control run would fail for a
        # property of the hardware rather than a property of the code.
        control=False,
    )


def _readable_state() -> cp.MachineState:
    return cp.MachineState(readable=True, free_ram_gb=64.0, cpu_percent=10.0, process_count=10, error=None)


def _unreadable_state() -> cp.MachineState:
    return cp.MachineState(readable=False, free_ram_gb=None, cpu_percent=None, process_count=None, error="probe failed")


def test_evaluate_escape_hatch_refuses_on_unreadable_state_as_declared():
    state = {"value": _readable_state()}

    @contextlib.contextmanager
    def _unreadable():
        state["value"] = _unreadable_state()
        try:
            yield
        finally:
            state["value"] = _readable_state()

    assert_safe_direction_holds(
        cp.evaluate_escape_hatch,
        invoke=lambda: cp.evaluate_escape_hatch(
            state["value"], min_free_ram_gb=1.0, max_cpu_percent=95.0, max_process_count=10_000
        ),
        undeterminable=_unreadable,
    )


def test_reap_stale_git_lock_falls_back_as_declared(tmp_path, monkeypatch):
    """Precondition: this guard's cheap, no-subprocess walk can resolve the
    lock-holding git directory. Undeterminable: `cwd` sits outside any repo
    this guard's bounded upward walk can reach, so `_resolve_git_dir`
    returns `None` and the reap side effect is never attempted.

    The function itself always `return`s `None` (see its own module
    docstring) -- its OBSERVABLE direction is the reap SIDE EFFECT, not its
    return value, so `invoke` here exposes that side effect as a proxy
    result rather than the function's own return (which cannot distinguish
    the two runs by construction -- see this file's module docstring on why
    a trivially-constant return would defeat the control-run check)."""
    called = {"value": False}
    monkeypatch.setattr(
        grsg, "_reap_candidate_locks", lambda git_dir: called.__setitem__("value", True)
    )

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    state = {"cwd": str(repo)}

    def _invoke():
        called["value"] = False
        grsg.check_reap_stale_git_lock("git commit -m x", state["cwd"])
        return called["value"]

    @contextlib.contextmanager
    def _undeterminable():
        state["cwd"] = str(outside)
        try:
            yield
        finally:
            state["cwd"] = str(repo)

    assert_safe_direction_holds(
        grsg.check_reap_stale_git_lock,
        invoke=_invoke,
        undeterminable=_undeterminable,
    )


def test_no_optional_locks_falls_back_as_declared():
    """Precondition: the raw-text scanner's token spans agree with the
    shared tokenizer's own output, so the insertion offset is trustworthy.
    Undeterminable: the shared tokenizer itself returns `None` (simulated
    directly -- an unparseable command in real use), leaving no offset to
    insert at."""
    original = gnol._bt_tokenize_full_command

    def _invoke():
        return gnol.check_git_no_optional_locks("git status")

    @contextlib.contextmanager
    def _undeterminable():
        gnol._bt_tokenize_full_command = lambda cmd: None
        try:
            yield
        finally:
            gnol._bt_tokenize_full_command = original

    assert_safe_direction_holds(
        gnol.check_git_no_optional_locks,
        invoke=_invoke,
        undeterminable=_undeterminable,
    )


def test_offer_git_c_falls_back_as_declared():
    """Precondition: the `cd`/`git` segments' quoting is confirmed balanced
    (even quote counts). Undeterminable: an odd double-quote count in either
    segment -- a real, naturally-occurring ambiguous shape, not a monkeypatch
    -- which this guard's own odd-count guard already declines to evaluate
    (see module docstring's quoted-semicolon discussion)."""
    state = {"cmd": "cd /tmp && git status"}

    def _invoke():
        return gogc.check_offer_git_c(state["cmd"])

    @contextlib.contextmanager
    def _undeterminable():
        state["cmd"] = 'cd /tmp && git commit -m "unterminated'
        try:
            yield
        finally:
            state["cmd"] = "cd /tmp && git status"

    assert_safe_direction_holds(
        gogc.check_offer_git_c,
        invoke=_invoke,
        undeterminable=_undeterminable,
    )


def test_block_worktree_sentinel_creation_raises_as_declared():
    """Precondition: the shared `SentinelCreationDetector` can evaluate
    `cmd` without error. Undeterminable: the detector itself raises (its
    internal state is broken) -- this guard has deliberately no try/except
    (module docstring), so the failure propagates rather than being
    swallowed into a silent allow."""
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}

    def _invoke():
        return bwsc.check(payload)

    original_evaluate = bwsc._detector.evaluate

    @contextlib.contextmanager
    def _undeterminable():
        def _boom(cmd):
            raise RuntimeError("detector broken")

        bwsc._detector.evaluate = _boom
        try:
            yield
        finally:
            bwsc._detector.evaluate = original_evaluate

    assert_safe_direction_holds(
        bwsc.check,
        invoke=_invoke,
        undeterminable=_undeterminable,
        expect_raises=RuntimeError,
    )


def test_block_noncanonical_branch_creation_falls_back_as_declared(monkeypatch):
    """Precondition: the target branch-name literal is fully readable from
    the tokenized command. Undeterminable: a PARTIAL command substitution
    (`"work/machine-b/$(date +%F)"`) leaves a neutralization artifact
    (`_looks_unsafe`) this guard cannot evaluate confidently, so it stays
    silent rather than risk flagging a genuinely canonical branch."""
    monkeypatch.setattr(bncbc, "resolve_git_root", lambda cwd: "/repo")
    monkeypatch.setattr(bncbc, "_is_hazard_repo", lambda root: True)

    state = {"name": "fix/foo"}

    def _invoke():
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "git checkout -b %s" % state["name"]},
            "cwd": "/repo",
        }
        return bncbc.check(payload)

    @contextlib.contextmanager
    def _undeterminable():
        state["name"] = '"work/machine-b/$(date +%F)"'
        try:
            yield
        finally:
            state["name"] = "fix/foo"

    assert_safe_direction_holds(
        bncbc.check,
        invoke=_invoke,
        undeterminable=_undeterminable,
    )


def test_block_worktree_sentinel_write_raises_as_declared():
    """Precondition: the write-target path resolves cleanly. Undeterminable:
    path resolution itself raises -- this guard has no try/except (module
    docstring: "Fail-CLOSED on its own resolution failures"), so the
    failure propagates rather than being swallowed into a silent allow."""
    payload = {"tool_name": "Write", "tool_input": {"file_path": "foo.txt"}}

    def _invoke():
        return bwsw.check(payload)

    original = bwsw.extract_target_path

    @contextlib.contextmanager
    def _undeterminable():
        def _boom(tool_input):
            raise RuntimeError("path resolution broken")

        bwsw.extract_target_path = _boom
        try:
            yield
        finally:
            bwsw.extract_target_path = original

    assert_safe_direction_holds(
        bwsw.check,
        invoke=_invoke,
        undeterminable=_undeterminable,
        expect_raises=RuntimeError,
    )


# Review: code-reviewer -- the per-site tests above assert each site's
# CURRENT `declaration.direction`, whatever it is; they do not pin what that
# direction should be. A decorator edit flipping e.g. `default_usable_ram_gb`
# from RAISE to FALL_BACK would leave its per-site test green under a now-
# lying name. `test_both_directions_are_expressible` below is the only place
# that independently derives "both directions are still represented" from
# the live declarations, decoupled from any one site's test.
_ASSERTED_SITES = {
    "coordinator_core.benchmarks.concurrency_probe.default_usable_ram_gb",
    "coordinator_core.benchmarks.concurrency_probe.default_physical_cores",
    "coordinator_core.benchmarks.concurrency_probe.evaluate_escape_hatch",
    "coordinator_core.bash_guards.guard_reap_stale_git_lock.check_reap_stale_git_lock",
    "coordinator_core.bash_guards.guard_no_optional_locks.check_git_no_optional_locks",
    "coordinator_core.bash_guards.guard_offer_git_c.check_offer_git_c",
    "coordinator_core.bash_guards.block_worktree_sentinel_creation.check",
    "coordinator_core.bash_guards.block_noncanonical_branch_creation.check",
    "coordinator_core.write_guards.block_worktree_sentinel_write.check",
    "coordinator_core.write_guards.nudge_peer_notice_unread.check",
}

#: Sites whose assertion lives with the site rather than here, named so the
#: meta-test still refuses an unasserted declaration. A site belongs here only
#: when its `undeterminable` context needs fixtures this file has no business
#: owning -- keep the assertion beside the code, not the roster.
#:
#: Values are `(module, attrname)` pairs, not strings: resolved below at
#: import time, so a renamed-away sibling assertion fails the collection of
#: THIS file rather than satisfying the meta-test while asserting nothing
#: (`state/lessons/2026-09-01-killed-op-names-live-on-in-string-keyed-guards.md`).
_ASSERTED_IN_SIBLING_TESTS = {
    "coordinator_core.install.derive_worker_cap.cap_command_for_this_box": (
        "coordinator_core.install.test_derive_worker_cap",
        "test_an_unreadable_host_keeps_the_committed_ceiling",
    ),
}

for _site, (_sibling_module, _sibling_attr) in _ASSERTED_IN_SIBLING_TESTS.items():
    getattr(importlib.import_module(_sibling_module), _sibling_attr)
del _site, _sibling_module, _sibling_attr

#: Modules scanned for declarations. A declaring module absent from this tuple
#: is invisible to the meta-test below -- which is the failure this roster
#: exists to prevent, so add the module in the same commit as the declaration.
_DECLARING_MODULES = (cp, dwc, grsg, gnol, gogc, bwsc, bncbc, bwsw, npnu)


def test_nudge_peer_notice_unread_falls_back_as_declared():
    """Precondition: the notice channel is readable for this session.
    Undeterminable: reading it raises -- an advisory guard that only ever ADDS
    context must degrade to "surface nothing" (`None`), never propagate into
    the peer's Write/Edit. Found undecorated by the close-out criterion-only
    read after a case-sensitive scan of the audit's free-prose verdict column
    missed its `**Premise-false ...**` cell."""
    state = {"raise": False}

    def _list_unread_notices(repo_root, session_id):
        if state["raise"]:
            raise OSError("notice channel unreadable")
        # The intact path must return something DISTINGUISHABLE from the
        # anchor, or the fall-back leg proves nothing -- verify.py rejects a
        # control run that already sits on the declared anchor.
        return [
            {
                "from_session_id": "peer-1",
                "artifact_path": "state/handoffs/x.md",
                "message": "contending this artifact",
            }
        ]

    monkeyed = {"orig": npnu.list_unread_notices}
    npnu.list_unread_notices = _list_unread_notices

    def _invoke():
        return npnu.check(
            {
                "tool_name": "Write",
                "session_id": "abc123",
                "tool_input": {"file_path": "x.md", "content": "y"},
            }
        )

    @contextlib.contextmanager
    def _undeterminable():
        state["raise"] = True
        try:
            yield
        finally:
            state["raise"] = False

    try:
        assert_safe_direction_holds(
            npnu.check,
            invoke=_invoke,
            undeterminable=_undeterminable,
        )
    finally:
        npnu.list_unread_notices = monkeyed["orig"]


def test_every_declaring_site_has_an_assertion():
    """A declaration nobody asserts is the convention this module replaced,
    with extra steps. New declaring site -> new test above, then this set."""
    declared = {
        name for module in _DECLARING_MODULES for name, _ in iter_declarations(module)
    }
    asserted = _ASSERTED_SITES | set(_ASSERTED_IN_SIBLING_TESTS)
    assert declared - asserted == set(), (
        f"declaring sites with no assertion: {sorted(declared - asserted)}"
    )
    assert asserted - declared == set(), (
        f"asserted sites that no longer declare a direction: {sorted(asserted - declared)}"
    )


def test_both_directions_are_expressible():
    """Survives a second review that deleted it as "redundant with the
    per-site tests" (Review: code-reviewer). It is not redundant: the
    per-site tests each assert whatever direction its site CURRENTLY
    declares, so a regression that flips the corpus's last RAISE to
    FALL_BACK (or vice versa) leaves every per-site test green under a name
    that now lies about which branch it exercises. This test is the only
    place that independently derives, from the live declarations, that both
    directions are still represented at all -- do not delete it a third
    time."""
    directions = {declaration.direction for _, declaration in iter_declarations(cp)}
    assert directions == {SafeDirection.RAISE, SafeDirection.FALL_BACK}


def test_an_imported_declaration_is_not_attributed_to_the_importer():
    """`derive_worker_cap` imports two declaring helpers from the probe. They
    are the probe's sites, asserted once, above -- attributing them here would
    demand a second assertion for the same function in every consumer."""
    names = {name for name, _ in iter_declarations(dwc)}
    assert names == {"coordinator_core.install.derive_worker_cap.cap_command_for_this_box"}


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"direction": SafeDirection.FALL_BACK, "because": "x"}, "anchor"),
        ({"direction": SafeDirection.FALL_BACK, "because": "x", "anchor": 1}, "anchor"),
        ({"direction": SafeDirection.RAISE, "because": "x", "anchor": 1}, "anchor"),
        ({"direction": SafeDirection.RAISE, "because": "  "}, "because"),
    ],
)
def test_malformed_declarations_are_refused_at_import(kwargs, message):
    with pytest.raises(ValueError, match=message):
        declares_safe_direction(**kwargs)


def test_verify_catches_a_site_that_lies_about_its_direction():
    """The instrument must fail as well as pass -- a declaration that does not
    hold has to produce a red, or none of the tests above mean anything."""

    @declares_safe_direction(SafeDirection.RAISE, because="claims to refuse, does not")
    def liar():
        return 0

    broken = {"value": False}

    @contextlib.contextmanager
    def _break():
        broken["value"] = True
        try:
            yield
        finally:
            broken["value"] = False

    with pytest.raises(AssertionError, match="declares SafeDirection.RAISE"):
        assert_safe_direction_holds(liar, invoke=lambda: 0, undeterminable=_break)

"""Oracle for the two fixes in `state/bug-backlog/2026-08-21-bare-commit-
guard-likely-fails-open-unde-0d2276775068.yaml`.

Sub-fix (1) -- a fail-open index probe RECORDS why it failed open, and the
advisory the command falls through to says so. The reported defect is not a
matcher bug (the entry falsifies that hypothesis against the exact reported
shape); it is that a degraded probe and a clean index produced byte-identical
operator output, so a guard that had silently stopped guarding was
indistinguishable from one that had checked and found nothing.

Sub-fix (3) -- `git commit -h`/`--help` stages nothing and commits nothing,
and is no longer denied.

NEGATIVE SPEC: this module does NOT re-test the advisory-vs-deny verdict
table (`test_git_commit_safe_commit_deny_escalation.py` owns that), and does
NOT assert any change of POSTURE -- a failed probe must still fail OPEN, and
the rows below pin that it does. It spawns nothing: every git fact this
check reads comes through `_run_git`, which is stubbed, so the fail-open
paths are reachable without a real repo or a real timeout.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import pytest

from coordinator_core.bash_guards import dispatch_checks


def _install_git_stub(monkeypatch, diff_result: Tuple[int, str]):
    """Replace `_run_git` with a table: sequencer `rev-parse` probes resolve
    to a path that does not exist (no merge/cherry-pick/revert in flight),
    and every `git diff` -- the index probes this module is about -- returns
    `diff_result`."""

    def _stub(
        args: List[str],
        cwd: Optional[str] = None,
        timeout: float = 2.0,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, str]:
        if args and args[0] == "rev-parse":
            return 0, "no-such-sequencer-state\n"
        return diff_result

    monkeypatch.setattr(dispatch_checks, "_run_git", _stub)


def _verdict(cmd: str) -> str:
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-fail-open")
    if out is None:
        return "none"
    decision = out["hookSpecificOutput"]["permissionDecision"]
    assert decision in ("allow", "deny")
    return "deny" if decision == "deny" else "advisory"


def _advisory_text(cmd: str) -> str:
    out = dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-fail-open")
    assert out is not None
    hook = out["hookSpecificOutput"]
    assert hook["permissionDecision"] == "allow"
    return hook["additionalContext"]


@pytest.fixture(autouse=True)
def _clean_probe_state():
    dispatch_checks._clear_probe_fail_open_reasons()
    dispatch_checks._git_probe_last_failure.set(None)
    dispatch_checks._disarm_git_probe_deadline()
    yield
    dispatch_checks._clear_probe_fail_open_reasons()
    dispatch_checks._git_probe_last_failure.set(None)
    dispatch_checks._disarm_git_probe_deadline()


def test_solo_bare_commit_probe_timeout_is_named_in_the_advisory(monkeypatch):
    _install_git_stub(monkeypatch, (-1, ""))
    text = _advisory_text('git -C /repo commit -m "x"')
    assert "The index was NOT read" in text
    assert "timed out" in text


def test_probe_timeout_still_fails_open_never_denies(monkeypatch):
    """Posture is unchanged by the recording: a guard-process error must not
    manufacture a deny."""
    _install_git_stub(monkeypatch, (-1, ""))
    assert _verdict('git -C /repo commit -m "x"') == "advisory"


def test_probe_failure_is_announced_on_stderr(monkeypatch, capsys):
    _install_git_stub(monkeypatch, (-1, ""))
    _advisory_text('git -C /repo commit -m "x"')
    assert "index probe failed open" in capsys.readouterr().err


def test_spent_budget_names_the_budget_not_a_bare_exit_code(monkeypatch):
    """A budget-spent probe and an unresolvable `git` both return 127, which
    is why `_run_git` records the cause rather than leaving the recorder to
    guess from the return code."""
    dispatch_checks._arm_git_probe_deadline(-1.0)
    text = _advisory_text('git -C /repo commit -m "x"')
    assert "The index was NOT read" in text
    assert "budget" in text


def test_unreadable_index_advisory_says_it_did_not_check(monkeypatch):
    _install_git_stub(monkeypatch, (128, ""))
    text = _advisory_text('git -C /repo commit -m "x"')
    assert "git exited 128" in text
    assert "not because it checked and found nothing" in text


def test_clean_index_advisory_carries_no_fail_open_note(monkeypatch):
    """The other half of the distinction the fix restores: a probe that DID
    run and found an empty index must not claim it failed."""
    _install_git_stub(monkeypatch, (0, ""))
    text = _advisory_text('git -C /repo commit -m "x"')
    assert "The index was NOT read" not in text


def test_deny_path_is_unaffected_by_the_recording(monkeypatch):
    _install_git_stub(monkeypatch, (0, "peer.txt\n"))
    assert _verdict('git -C /repo commit -m "x"') == "deny"


def test_sweep_all_probe_failure_is_named_too(monkeypatch):
    _install_git_stub(monkeypatch, (-1, ""))
    monkeypatch.setattr(dispatch_checks, "_is_hazard_repo", lambda _p: True)
    text = _advisory_text('git -C /repo commit -am "x"')
    assert "The index was NOT read" in text
    assert "sweep-all" in text


def test_help_short_flag_is_not_guarded(monkeypatch):
    _install_git_stub(monkeypatch, (0, "peer.txt\n"))
    assert _verdict("git -C /repo commit -h") == "none"


def test_help_long_flag_is_not_guarded(monkeypatch):
    _install_git_stub(monkeypatch, (0, "peer.txt\n"))
    assert _verdict("git -C /repo commit --help") == "none"


def test_help_spends_no_probe(monkeypatch):
    """The carve-out sits ahead of every predicate, so a help invocation
    spawns nothing -- the point is noise removal, not a cheaper deny."""

    def _never(*args, **kwargs):
        raise AssertionError("help invocation must not probe git")

    monkeypatch.setattr(dispatch_checks, "_run_git", _never)
    assert _verdict("git -C /repo commit -h") == "none"


def test_a_message_operand_spelled_dash_h_is_not_help(monkeypatch):
    _install_git_stub(monkeypatch, (0, "peer.txt\n"))
    assert _verdict('git -C /repo commit -m "-h"') == "deny"


def test_a_pathspec_named_dash_h_is_not_help(monkeypatch):
    """Past the `--` separator the token is a FILE, and a commit naming a
    pathspec is the ratified scoped form -- neither help nor a deny."""
    _install_git_stub(monkeypatch, (0, "peer.txt\n"))
    assert dispatch_checks._bt_commit_is_help_invocation(
        ["git", "commit", "-m", "x", "--", "-h"]
    ) is False


def test_help_segment_does_not_excuse_a_later_bare_commit(monkeypatch):
    _install_git_stub(monkeypatch, (0, "peer.txt\n"))
    assert _verdict('git -C /repo commit -h && git -C /repo commit -m "x"') == "deny"


# --- The fail-open record must OUTLIVE the dispatch -------------------------
#
# Naming the cause in the advisory makes the degradation legible in flight and
# undecidable five minutes later: the reasons live in a ContextVar drained into
# one message. `state/bug-backlog/2026-08-21-bare-commit-guard-likely-fails-
# open-unde-0d2276775068.yaml` is exactly that question asked after the fact —
# a peer's staged blob committed past this guard, with nothing in the record
# able to say whether the probe had degraded. An advisory nobody kept cannot
# answer it.


def _fail_open_log(tmp_path):
    return tmp_path / "audit" / "guard-fail-open.log"


def _point_audit_dir_at(monkeypatch, tmp_path):
    audit = tmp_path / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        dispatch_checks,
        "_override_log_path",
        lambda _root, _sid: str(audit / "overrides.log"),
    )
    return audit


def test_fail_open_is_persisted_with_cause_session_and_command(monkeypatch, tmp_path):
    _point_audit_dir_at(monkeypatch, tmp_path)
    _install_git_stub(monkeypatch, (-1, ""))

    dispatch_checks.check_git_commit_safe_commit_advise(
        'git -C /repo commit -m "sweeping subject"',
        "sess-persisted",
        git_root=str(tmp_path),
    )

    line = _fail_open_log(tmp_path).read_text(encoding="utf-8").strip()
    assert "GIT-COMMIT-SCOPE-PROBE-FAIL-OPEN" in line
    assert "sess-persisted" in line, "the record must attribute a session"
    assert "sweeping subject" in line, "the record must identify the command"
    assert "timed out" in line, "the record must name WHY it failed open"


def test_a_clean_index_writes_no_record(monkeypatch, tmp_path):
    """The paired negative. A log that also fires when the probe SUCCEEDED
    cannot distinguish a degraded guard from a working one, which is the
    distinction the whole record exists to preserve."""
    _point_audit_dir_at(monkeypatch, tmp_path)
    _install_git_stub(monkeypatch, (0, ""))

    dispatch_checks.check_git_commit_safe_commit_advise(
        'git -C /repo commit -m "x"', "sess-clean", git_root=str(tmp_path)
    )

    assert not _fail_open_log(tmp_path).exists()


def test_persistence_failure_never_breaks_the_guard(monkeypatch, tmp_path):
    """A guard that crashes because it could not write its own audit line is
    worse than one that loses the line."""
    _install_git_stub(monkeypatch, (-1, ""))

    def _boom(_root, _sid):
        raise OSError("audit dir is unwritable")

    monkeypatch.setattr(dispatch_checks, "_override_log_path", _boom)

    out = dispatch_checks.check_git_commit_safe_commit_advise(
        'git -C /repo commit -m "x"', "sess-boom", git_root=str(tmp_path)
    )
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "was NOT read" in out["hookSpecificOutput"]["additionalContext"]


def test_no_git_root_is_a_silent_skip_not_a_crash(monkeypatch, tmp_path):
    """`git_root` is optional on this entry point; absent it there is no
    session audit directory to write into, and that must not raise."""
    _install_git_stub(monkeypatch, (-1, ""))
    out = dispatch_checks.check_git_commit_safe_commit_advise(
        'git -C /repo commit -m "x"', "sess-no-root"
    )
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

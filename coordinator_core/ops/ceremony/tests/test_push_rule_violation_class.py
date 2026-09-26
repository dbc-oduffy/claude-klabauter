
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.push as push_mod
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.ceremony.git_native import GitResult
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, **no_console_creationflags())


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["checkout", "-q", "-b", "work/x"], repo)
    (repo / "README.md").write_text("seed", encoding="utf-8")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", str((repo.parent / "origin.git"))], repo)
    return repo


_GH013_RULE_VIOLATION_STDERR = (
    "remote: error: GH013: Repository rule violations found for refs/heads/work/x.\n"
    "remote: - coverage-gate status check is required\n"
    "! [remote rejected] work/x -> work/x (push declined due to repository rule violations)\n"
    "error: failed to push some refs to 'origin'\n"
)

_GH013_SECRET_SCANNING_STDERR = (
    "remote: error: GH013: Repository rule violations found for refs/heads/work/x.\n"
    "remote: - Push cannot contain secrets\n"
    "remote: — Secret detected: Generic Credential\n"
    "! [remote rejected] work/x -> work/x (push declined due to repository rule violations)\n"
    "error: failed to push some refs to 'origin'\n"
)


def test_gh013_rule_violation_without_secret_phrase_is_rule_violation_not_secret_scanning():
    assert push_mod._is_rule_violation_reject(_GH013_RULE_VIOLATION_STDERR) is True
    assert push_mod._is_secret_scanning_reject(_GH013_RULE_VIOLATION_STDERR) is False


def test_gh013_secret_scanning_phrase_is_secret_scanning_not_rule_violation():
    assert push_mod._is_secret_scanning_reject(_GH013_SECRET_SCANNING_STDERR) is True
    assert push_mod._is_rule_violation_reject(_GH013_SECRET_SCANNING_STDERR) is False


def test_non_gh_push_protection_reason_is_neither_subclass():
    non_fast_forward = "! [rejected] work/x -> work/x (non-fast-forward)\n"
    assert push_mod._is_rule_violation_reject(non_fast_forward) is False
    assert push_mod._is_secret_scanning_reject(non_fast_forward) is False


def test_rule_violation_reject_skips_rebase_ladder_and_recovers_then_repushes(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)

    push_calls = []

    def _fake_push(*a, **kw):
        push_calls.append(1)
        if len(push_calls) == 1:
            return GitResult(returncode=1, stdout="", stderr=_GH013_RULE_VIOLATION_STDERR)
        return GitResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(git_native, "push", _fake_push)

    fetch_calls = []
    monkeypatch.setattr(
        git_native, "fetch",
        lambda *a, **kw: fetch_calls.append(1) or GitResult(returncode=0, stdout="", stderr=""),
    )
    rebase_calls = []
    monkeypatch.setattr(
        push_mod, "_rebase_onto_fetched_ref",
        lambda *a, **kw: rebase_calls.append(1) or (0, ""),
    )
    recovery_calls = []
    monkeypatch.setattr(
        push_mod, "_recover_rule_violation_reject",
        lambda *a, **kw: recovery_calls.append(1) or None,
    )

    outcome = push_mod.push_with_retry(repo)

    assert recovery_calls == [1]
    assert fetch_calls == []
    assert rebase_calls == []
    assert len(push_calls) == 2
    assert outcome.exit_code == 0
    assert outcome.failed == []
    assert outcome.unconfirmed == []
    assert "push" in outcome.acted


def test_rule_violation_recovery_failure_is_failed_never_unconfirmed(tmp_path, monkeypatch):
    """When `_recover_rule_violation_reject` cannot clear the way (status
    unpostable, or posted but still red), the outcome is a confirmed
    `failed` -- this was OBSERVED, not indeterminate, so it must never
    collapse into `unconfirmed`."""
    repo = _init_repo(tmp_path)

    monkeypatch.setattr(
        git_native, "push",
        lambda *a, **kw: GitResult(returncode=1, stdout="", stderr=_GH013_RULE_VIOLATION_STDERR),
    )
    fetch_calls = []
    monkeypatch.setattr(
        git_native, "fetch",
        lambda *a, **kw: fetch_calls.append(1) or GitResult(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        push_mod, "_recover_rule_violation_reject",
        lambda *a, **kw: "rule-violation recovery: coverage status unpostable (no token)",
    )

    outcome = push_mod.push_with_retry(repo)

    assert fetch_calls == []
    assert outcome.failed
    assert outcome.unconfirmed == []
    assert push_mod.derive_push_status(outcome) == push_mod.PUSH_STATUS_FAILED


def test_secret_scanning_reject_is_never_repushed_even_though_it_shares_gh_push_protection(
    tmp_path, monkeypatch,
):
    repo = _init_repo(tmp_path)

    push_calls = []
    monkeypatch.setattr(
        git_native, "push",
        lambda *a, **kw: push_calls.append(1) or GitResult(
            returncode=1, stdout="", stderr=_GH013_SECRET_SCANNING_STDERR
        ),
    )
    fetch_calls = []
    monkeypatch.setattr(
        git_native, "fetch",
        lambda *a, **kw: fetch_calls.append(1) or GitResult(returncode=0, stdout="", stderr=""),
    )
    recovery_calls = []
    monkeypatch.setattr(
        push_mod, "_recover_rule_violation_reject",
        lambda *a, **kw: recovery_calls.append(1) or None,
    )

    outcome = push_mod.push_with_retry(repo)

    assert len(push_calls) == 1
    assert fetch_calls == []
    assert recovery_calls == []
    assert outcome.failed
    assert outcome.unconfirmed == []
    assert push_mod.derive_push_status(outcome) == push_mod.PUSH_STATUS_FAILED


def test_push_subprocess_timeout_still_yields_unconfirmed_not_failed_in_c2_neighbourhood(
    tmp_path, monkeypatch,
):
    repo = _init_repo(tmp_path)

    timeout_stderr = "git push: timed out after 120s (Command '['git', 'push']' timed out after 120 seconds)"
    monkeypatch.setattr(
        git_native, "push",
        lambda *a, **kw: GitResult(returncode=-1, stdout="", stderr=timeout_stderr),
    )
    fetch_calls = []
    monkeypatch.setattr(
        git_native, "fetch",
        lambda *a, **kw: fetch_calls.append(1) or GitResult(returncode=0, stdout="", stderr=""),
    )
    recovery_calls = []
    monkeypatch.setattr(
        push_mod, "_recover_rule_violation_reject",
        lambda *a, **kw: recovery_calls.append(1) or None,
    )

    outcome = push_mod.push_with_retry(repo)

    assert fetch_calls == []
    assert recovery_calls == []
    assert outcome.unconfirmed
    assert outcome.failed == []
    assert push_mod.derive_push_status(outcome) == push_mod.PUSH_STATUS_UNCONFIRMED

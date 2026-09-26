"""C4 (docs/plans/2026-09-23-completion-evidence-contract.md) -- pins that
`push.outstanding` is declared `fire_and_forget` in the DR-442 registry, and
that an `unconfirmed` outcome the op hands back never reads as an instruction
to reconcile or adjudicate by hand.

Uses the same on-disk-repo fixture shape as `test_push_outstanding.py`
(`push_with_retry` monkeypatched at the delegation boundary; the git plumbing
itself is exercised for real).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.push_outstanding as push_outstanding_mod
from coordinator_core.authz.completion_evidence import (
    OP_COMPLETION_EVIDENCE,
    EvidenceClass,
    evidence_class,
)
from coordinator_core.ops.ceremony.push import PushOutcome

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> None:
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=no_window,
    )


def _init_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _make_repo_with_remote(tmp_path: Path, *, branch: str = "work/some-branch") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    bare = tmp_path / "bare.git"
    _git(["init", "-q", "--bare", str(bare)], tmp_path)

    repo = _init_repo(tmp_path, "repo")
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", branch], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["push", "-q", "-u", "origin", branch], repo)
    return repo


def test_registry_declares_push_outstanding_fire_and_forget():
    """(a) the registry declares push.outstanding `fire_and_forget`."""
    assert OP_COMPLETION_EVIDENCE["push.outstanding"] is EvidenceClass.FIRE_AND_FORGET
    assert evidence_class("push.outstanding") is EvidenceClass.FIRE_AND_FORGET


def test_timed_out_push_returns_unconfirmed_shape_unchanged(monkeypatch, tmp_path):
    """(b) a stubbed timed-out push still returns a non-empty `unconfirmed`
    key with the envelope shape unchanged."""
    repo = _make_repo_with_remote(tmp_path)
    _seed_file(repo, "second.txt", "more")
    _git(["add", "--", "second.txt"], repo)
    _git(["commit", "-q", "-m", "second"], repo)

    def _fake_push_with_retry(root, **kwargs):
        return PushOutcome(
            exit_code=-1,
            unconfirmed=["git push: timed out after 12s (root/repo)"],
            attempts=1,
        )

    monkeypatch.setattr(push_outstanding_mod, "push_with_retry", _fake_push_with_retry)

    envelope = push_outstanding_mod._handler({}, repo_root=repo / ".git")

    assert envelope["unconfirmed"] == ["git push: timed out after 12s (root/repo)"]
    assert envelope["failed"] == []
    assert envelope["acted"] == []
    assert set(envelope) == {
        "exit_code",
        "acted",
        "skipped",
        "failed",
        "unconfirmed",
        "message",
        "pushed_range",
        "pushed_count",
    }


def test_timed_out_push_message_carries_no_reconcile_or_adjudicate_instruction(
    monkeypatch, tmp_path
):
    """(c) the returned `message` carries no reconcile or adjudicate
    instruction -- DR-442's fire-and-forget rule: not observed, not the
    caller's to adjudicate, the next cadence tick re-drives it."""
    repo = _make_repo_with_remote(tmp_path)
    _seed_file(repo, "second.txt", "more")
    _git(["add", "--", "second.txt"], repo)
    _git(["commit", "-q", "-m", "second"], repo)

    def _fake_push_with_retry(root, **kwargs):
        return PushOutcome(
            exit_code=-1,
            unconfirmed=["git push: timed out after 12s (root/repo)"],
            attempts=1,
        )

    monkeypatch.setattr(push_outstanding_mod, "push_with_retry", _fake_push_with_retry)

    envelope = push_outstanding_mod._handler({}, repo_root=repo / ".git")

    message = envelope["message"] or ""
    lowered = message.lower()
    assert "reconcile" not in lowered
    assert "adjudicate" not in lowered
    assert "merge-base" not in lowered

    doc = push_outstanding_mod._handler.__doc__ or ""
    assert "reconcile (see" not in doc.lower()
    assert "git merge-base --is-ancestor" not in doc

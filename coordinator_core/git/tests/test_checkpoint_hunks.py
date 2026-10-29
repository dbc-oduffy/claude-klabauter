"""checkpoint_hunks: a checkpoint's blob carries the run's own hunks only; unknown withholds."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from coordinator_core.git import checkpoint_hunks as ch
from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import peer_hunk_gate, run_hunk_claims, run_token
from coordinator_core.ops.dispatch_emit.run_hunk_claims import Hunk, HunkSplit
from coordinator_core.win_portability import no_console_creationflags

_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
PLAN = "docs/plans/p.md"
MSG = f"checkpoint(wave 1): 1 row — C1\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: abc1234\n"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env={**os.environ, **_ENV}, **no_console_creationflags())


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    (root / "f.txt").write_bytes(b"".join(b"line%d\n" % i for i in range(1, 11)))
    _git(root, "add", "f.txt")
    _git(root, "commit", "-q", "-m", "base")
    lines = (root / "f.txt").read_bytes().splitlines(keepends=True)
    lines[1], lines[8] = b"OWN\n", b"FOREIGN\n"
    (root / "f.txt").write_bytes(b"".join(lines))
    return root


def _blob(root: Path, sha: str) -> bytes:
    got = git_objects.read_object(resolve_git_common_dir(root), sha)
    assert got is not None
    return got[1]


def _stub(monkeypatch, split_fn, holds=()):
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: list(holds))
    monkeypatch.setattr(run_hunk_claims, "own_hunks", split_fn)


def test_own_and_foreign_hunk_blob_holds_own_only(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _stub(
        monkeypatch,
        lambda r, run, path, hunks, holders: HunkSplit(own=[h for h in hunks if h.old_start == 2]),
        holds=[("f.txt", "peer")],
    )
    blobs, withheld = ch.checkpoint_own_blobs(root, "run1", ["f.txt"], MSG)
    assert withheld == []
    text = _blob(root, blobs["f.txt"]).decode()
    assert "OWN" in text and "FOREIGN" not in text and "line9" in text


def test_all_own_needs_no_blob(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _stub(monkeypatch, lambda r, run, path, hunks, holders: HunkSplit(own=list(hunks)))
    assert ch.checkpoint_own_blobs(root, "run1", ["f.txt"], MSG) == ({}, [])


def test_unattributed_hunk_withholds(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _stub(
        monkeypatch,
        lambda r, run, path, hunks, holders: HunkSplit(own=hunks[:1], unattributed=hunks[1:]),
    )
    assert ch.checkpoint_own_blobs(root, "run1", ["f.txt"], MSG) == ({}, ["f.txt"])


def test_overlapping_hunk_withholds(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _stub(
        monkeypatch,
        lambda r, run, path, hunks, holders: HunkSplit(own=hunks[:1], overlapping={"peer": hunks[:1]}),
    )
    assert ch.checkpoint_own_blobs(root, "run1", ["f.txt"], MSG) == ({}, ["f.txt"])


def test_unresolved_run_withholds_path_with_a_diff(tmp_path):
    root = _repo(tmp_path)
    assert ch.checkpoint_own_blobs(root, None, ["f.txt"], MSG) == ({}, ["f.txt"])


def test_untracked_and_unchanged_paths_are_omitted(tmp_path):
    root = _repo(tmp_path)
    (root / "new.txt").write_text("x")
    _git(root, "checkout", "--", "f.txt")
    assert ch.checkpoint_own_blobs(root, None, ["f.txt", "new.txt"], MSG) == ({}, [])


def test_split_failure_withholds_and_never_raises(tmp_path, monkeypatch):
    root = _repo(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", boom)
    assert ch.checkpoint_own_blobs(root, "run1", ["f.txt"], MSG) == ({}, ["f.txt"])


def _runs(monkeypatch, tmp_path, records, plans):
    monkeypatch.setattr(run_token, "live_records", lambda root: iter(records))
    monkeypatch.setattr(ch, "_sizing_plan", lambda root, rel: plans.get(rel, ""))


def test_run_resolves_by_session_and_plan(tmp_path, monkeypatch):
    _runs(
        monkeypatch, tmp_path,
        [
            {"run_id": "a", "session_id": "s1", "sizing_rel": "sa"},
            {"run_id": "b", "session_id": "s2", "sizing_rel": "sb"},
        ],
        {"sa": PLAN, "sb": PLAN},
    )
    assert ch.checkpoint_run_id(tmp_path, MSG, "s1") == "a"
    assert ch.checkpoint_run_id(tmp_path, MSG, "s9") is None
    assert ch.checkpoint_run_id(tmp_path, MSG, None) is None


def test_run_unresolved_when_plan_differs_or_ambiguous(tmp_path, monkeypatch):
    _runs(
        monkeypatch, tmp_path,
        [
            {"run_id": "a", "session_id": "s1", "sizing_rel": "sa"},
            {"run_id": "b", "session_id": "s1", "sizing_rel": "sb"},
        ],
        {"sa": "docs/plans/other.md", "sb": "docs/plans/other.md"},
    )
    assert ch.checkpoint_run_id(tmp_path, MSG, "s1") is None
    _runs(
        monkeypatch, tmp_path,
        [
            {"run_id": "a", "session_id": "s1", "sizing_rel": "sa"},
            {"run_id": "b", "session_id": "s1", "sizing_rel": "sb"},
        ],
        {"sa": PLAN, "sb": PLAN},
    )
    assert ch.checkpoint_run_id(tmp_path, MSG, "s1") is None
    assert ch.checkpoint_run_id(tmp_path, "no plan line", "s1") is None

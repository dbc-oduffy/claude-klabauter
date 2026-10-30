"""dispatch.land_run over a worktree mid-merge: a merge the run started lands as one two-parent
commit; a foreign merge or a marker refuses with HEAD unmoved; a halted hold the merge brings in
is dropped and one it does not bring in still refuses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.git import merge_owner
from coordinator_core.ops.dispatch_emit import peer_hunk_gate
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

A = "src/a.py"
H = "ask-20200101T000000-abc123"
HALT_SUBJECT = f"warp: record halted {H} run artifacts"


def _merging(root: Path, theirs_message: str = "theirs", a: str = A, x: str = "") -> tuple:
    """HEAD and `theirs` both edit `a` (conflict); `theirs` also adds src/b.py (auto-merged).
    With `x`, that path is in the base and HEAD (a foreign commit) adds a line to it."""
    prod._write(root, "state/sizings/s.yaml", "deliverable_id: deliv-xyz\n")
    if x:
        prod._write(root, x, "l1\n")
    prod._write(root, f"state/warp/{H}/halted.json", json.dumps({"status": "halted"}))
    prod._write(root, f"state/warp/{H}/run.json", json.dumps({"sizing_rel": "state/sizings/s.yaml"}))
    prod._write(root, a, "x = 0\n")
    prod._git(["add", "."], root)
    prod._git(["commit", "-q", "-m", "base"], root)
    prod._git(["checkout", "-q", "-b", "theirs"], root)
    prod._write(root, a, "x = 2\n")
    prod._write(root, "src/b.py", "b = 1\n")
    prod._git(["add", "."], root)
    prod._git(["commit", "-q", "-m", theirs_message], root)
    prod._git(["checkout", "-q", "-"], root)
    prod._write(root, a, "x = 1\n")
    if x:
        prod._write(root, x, "l1\nl2\n")
    prod._git(["commit", "-q", "-am", "ours"], root)
    ours = prod._git(["rev-parse", "HEAD"], root).strip()
    theirs = prod._git(["rev-parse", "theirs"], root).strip()
    merge = prod.subprocess.run(
        ["git", "merge", "theirs"], cwd=str(root), capture_output=True, text=True,
        **prod.no_console_creationflags(),
    )
    assert merge.returncode != 0
    assert (root / ".git" / "MERGE_HEAD").exists()
    return ours, theirs


def _resolve(root: Path, text: str = "x = 3\n", a: str = A, stage: bool = True) -> None:
    prod._author(root, a, text)
    if stage:
        prod._git(["add", a], root)
    prod._declare(root, a)
    prod._record_dir(root)


def _own(root: Path, ours: str, theirs: str, run_id: str = prod.RUN) -> None:
    merge_owner.record(root, run_id, theirs, ours)


def test_a_merge_the_run_started_lands_as_one_two_parent_commit(repo):
    ours, theirs = _merging(repo)
    _resolve(repo)
    _own(repo, ours, theirs)

    out = prod._land(repo)

    assert out["committed"] is True, out
    assert out["merge_parent"] == theirs
    merge_sha = prod._git(["rev-list", "--parents", "-n1", "HEAD~1"], repo).split()
    assert merge_sha[1:] == [ours, theirs]
    assert out["sha"] == merge_sha[0]
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert merge_owner.owner(repo) is None
    assert prod._git(["show", f"{out['sha']}:{A}"], repo) == "x = 3\n"
    assert prod._git(["show", f"{out['sha']}:src/b.py"], repo) == "b = 1\n"
    assert out["merge_artifacts"]["committed"] is True, out["merge_artifacts"]
    assert len(prod._git(["rev-list", "--parents", "-n1", "HEAD"], repo).split()) == 2
    assert "(run artifacts)" in prod._message(repo)


def test_a_foreign_merge_head_still_refuses(repo):
    ours, theirs = _merging(repo)
    _resolve(repo)
    _own(repo, ours, theirs, run_id="ask-20200101T000009-other0")

    out = prod._land(repo)

    assert out.get("committed") is not True, out
    assert "partway through a merge" in json.dumps(out)
    assert prod._git(["rev-parse", "HEAD"], repo).strip() == ours
    assert (repo / ".git" / "MERGE_HEAD").read_text(encoding="utf-8").strip() == theirs


def test_no_owner_record_is_foreign(repo):
    ours, theirs = _merging(repo)
    _resolve(repo)

    out = prod._land(repo)

    assert out.get("committed") is not True, out
    assert "partway through a merge" in json.dumps(out)
    assert prod._git(["rev-parse", "HEAD"], repo).strip() == ours


def test_a_conflict_marker_left_in_a_merge_path_refuses_with_head_unmoved(repo):
    ours, theirs = _merging(repo, a="notes/a.txt")
    _resolve(repo, a="notes/a.txt", text="<<<<<<< HEAD\nx = 1\n=======\nx = 2\n>>>>>>> theirs\n", stage=False)
    _own(repo, ours, theirs)

    out = prod._land(repo)

    assert out.get("committed") is not True, out
    assert "merge not finished" in json.dumps(out)
    assert prod._git(["rev-parse", "HEAD"], repo).strip() == ours
    assert (repo / ".git" / "MERGE_HEAD").exists()


def _hold(monkeypatch) -> None:
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [(A, H)])


def test_a_halted_holder_whose_halt_record_is_on_the_merged_branch_does_not_hold(repo, monkeypatch):
    ours, theirs = _merging(repo, HALT_SUBJECT)
    _resolve(repo)
    _own(repo, ours, theirs)
    _hold(monkeypatch)

    out = prod._land(repo)

    assert out["committed"] is True, out
    assert peer_hunk_gate.PEER_HUNKS_CO_DECLARED not in json.dumps(out)


def test_a_halted_holder_whose_commits_are_not_on_the_merged_branch_lands_naming_it_in_provenance(repo, monkeypatch):
    ours, theirs = _merging(repo, "unrelated change")
    _resolve(repo)
    _own(repo, ours, theirs)
    _hold(monkeypatch)

    out = prod._land(repo)

    assert out["committed"] is True, out
    assert out.get("refused") is None, out
    assert H in json.dumps([e for e in out["provenance"] if e["reason"] == peer_hunk_gate.PEER_HUNKS_CO_DECLARED]), out
    assert not (repo / ".git" / "MERGE_HEAD").exists()


def test_since_base_still_guards_a_path_the_merge_does_not_move(repo):
    from coordinator_core.ops.dispatch_emit import land_run

    X = "src/x.py"
    ours, theirs = _merging(repo, x=X)
    _resolve(repo)
    _own(repo, ours, theirs)
    prod._write(repo, X, "l1\n")
    base = prod._git(["rev-parse", "HEAD~1"], repo).strip()

    findings = land_run._integrity(
        repo, {"run_base_sha": base}, [A, X], [], None, prod.RUN, None, own_declared=[A, X],
    )

    overlap = [f for f in findings if f.reason == "since-base-overlap"]
    assert [f.path for f in overlap] == [X], findings


def test_the_two_artifacts_messages_are_pinned_equal(monkeypatch, tmp_path):
    from coordinator_core.git import commit as commit_mod
    from coordinator_core.ops.dispatch_emit import land_run, terminal_commit
    from coordinator_core.ops.dispatch_emit import terminal_commit_refusals

    sha = "a" * 40
    artifacts = {"paths": ["state/r.json"], "deleted": []}
    message = "subject line\n\nbody\n"
    seen: dict = {}

    def fake_commit_paths(root, paths, msg, **kw):
        seen["land_run"] = msg
        return type("O", (), {"sha": "b" * 40})()

    monkeypatch.setattr(commit_mod, "commit_paths", fake_commit_paths)
    land_run._land_merge_artifacts(tmp_path, artifacts, message, sha)

    def fake_commit_v2(params, root):
        seen["terminal"] = params["message"]
        return {"committed": True, "sha": "c" * 40}

    terminal_commit_refusals._land_merge_artifacts(fake_commit_v2, tmp_path, artifacts, message, sha, None)

    assert seen["land_run"] == seen["terminal"] == f"subject line (run artifacts)\n\nFollows merge commit {sha[:12]}.\n"

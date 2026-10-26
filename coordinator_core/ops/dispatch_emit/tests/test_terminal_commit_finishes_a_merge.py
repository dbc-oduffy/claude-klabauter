"""dispatch.terminal_commit over a worktree mid-merge: a resolved merge lands as a two-parent
commit over the merge's paths; markers or foreign declared paths refuse with HEAD unmoved."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
_REVIEWED = reviewed()
_PLAN = "docs/plans/p.md"
_SIZING = "state/sizings/20260101-a.yaml"


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check, **no_console_creationflags()
    ).stdout


@pytest.fixture
def merging(tmp_path):
    """HEAD and `theirs` both edit a.py (conflict); `theirs` also adds b.py (auto-merged)."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "a.py").write_text("x = 0\n", encoding="utf-8")
    plan = root / _PLAN
    plan.parent.mkdir(parents=True)
    plan.write_text(f"---\nsizing_object: {_SIZING}\n---\n# p\n", encoding="utf-8")
    sizing = root / _SIZING
    sizing.parent.mkdir(parents=True)
    sizing.write_text(
        "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: S\n  provisional: true\n"
        "route: dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\npremise:\n"
        "  provenance: read\n  evidence: test fixture\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    _git(root, "checkout", "-q", "-b", "theirs")
    (root / "a.py").write_text("x = 2\n", encoding="utf-8")
    (root / "b.py").write_text("b = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "theirs")
    _git(root, "checkout", "-q", "main")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "ours")
    _git(root, "merge", "theirs", check=False)
    assert (root / ".git" / "MERGE_HEAD").exists()
    return root


@pytest.fixture
def staged_mixed(tmp_path):
    """`theirs` edits state/s.yaml (auto-merges) and coordinator_core/m.py (conflicts); staged by the engine op."""
    from coordinator_core.ops.merge_branch_into_workstream import merge_branch_into_workstream

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    for rel, text in (("state/s.yaml", "s: 0\n"), ("coordinator_core/m.py", "x = 0\n")):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    plan = root / _PLAN
    plan.parent.mkdir(parents=True)
    plan.write_text(f"---\nsizing_object: {_SIZING}\n---\n# p\n", encoding="utf-8")
    sizing = root / _SIZING
    sizing.parent.mkdir(parents=True)
    sizing.write_text(
        "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: S\n  provisional: true\n"
        "route: dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\npremise:\n"
        "  provenance: read\n  evidence: test fixture\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    _git(root, "checkout", "-q", "-b", "theirs")
    (root / "state/s.yaml").write_text("s: 2\n", encoding="utf-8")
    (root / "coordinator_core/m.py").write_text("x = 2\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "theirs")
    _git(root, "checkout", "-q", "main")
    (root / "coordinator_core/m.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "ours")
    reply = merge_branch_into_workstream("theirs", "test", root, stage=True)
    assert reply["staged"] is True and reply["conflict"] is True, reply
    assert reply["conflicted"] == ["coordinator_core/m.py"], reply
    assert (root / ".git" / "MERGE_HEAD").exists()
    return root


def test_an_engine_staged_mixed_merge_lands_one_two_parent_commit(staged_mixed):
    root = staged_mixed
    ours = _git(root, "rev-parse", "HEAD").strip()
    theirs = _git(root, "rev-parse", "theirs").strip()
    (root / "coordinator_core/m.py").write_text("x = 3\n", encoding="utf-8")
    out = _call(root, paths=("state/s.yaml", "coordinator_core/m.py"))
    assert out["committed"] is True, out
    assert _git(root, "rev-list", "--parents", "-n1", out["sha"]).split() == [out["sha"], ours, theirs]
    assert not (root / ".git" / "MERGE_HEAD").exists()
    assert _git(root, "show", f"{out['sha']}:state/s.yaml") == "s: 2\n"
    assert _git(root, "show", f"{out['sha']}:coordinator_core/m.py") == "x = 3\n"
    assert "(process files)" not in _git(root, "log", "-1", "--format=%s", out["sha"])


def _call(root: Path, paths: tuple = ("a.py",)) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=paths),), plan_path=_PLAN)
    (root / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED}
    return terminal_commit._handler(params, repo_root=root / ".git")


def test_a_resolved_merge_lands_two_parents_over_the_merge_paths(merging):
    ours = _git(merging, "rev-parse", "HEAD").strip()
    theirs = _git(merging, "rev-parse", "theirs").strip()
    (merging / "a.py").write_text("x = 3\n", encoding="utf-8")
    out = _call(merging)
    assert out["committed"] is True, out
    assert _git(merging, "rev-list", "--parents", "-n1", out["sha"]).split() == [out["sha"], ours, theirs]
    assert not (merging / ".git" / "MERGE_HEAD").exists()
    assert set(_git(merging, "show", "--name-only", "--format=", "-m", "--first-parent", out["sha"]).split()) == {"a.py", "b.py"}
    assert _git(merging, "show", f"{out['sha']}:b.py") == "b = 1\n"
    assert _git(merging, "show", f"{out['sha']}:a.py") == "x = 3\n"


def test_the_sizing_ship_follows_the_merge_in_a_single_parent_commit(merging):
    (merging / "a.py").write_text("x = 3\n", encoding="utf-8")
    out = _call(merging)
    assert out["committed"] is True, out
    assert out["sizing_shipped"]["sha"] == out["sha"]
    follow = _git(merging, "rev-list", "--parents", "-n1", "HEAD").split()
    assert len(follow) == 2 and follow[1] == out["sha"]
    assert _git(merging, "show", "--name-only", "--format=", "HEAD").split() == [_SIZING]


def test_conflict_markers_refuse_with_head_unmoved(merging):
    head = _git(merging, "rev-parse", "HEAD").strip()
    out = _call(merging)
    assert out["committed"] is False
    assert out["refused"] == "merge_markers", out
    assert _git(merging, "rev-parse", "HEAD").strip() == head
    assert (merging / ".git" / "MERGE_HEAD").exists()


def test_a_declared_path_outside_the_merge_refuses(merging):
    head = _git(merging, "rev-parse", "HEAD").strip()
    (merging / "a.py").write_text("x = 3\n", encoding="utf-8")
    (merging / "c.py").write_text("c = 1\n", encoding="utf-8")
    out = _call(merging, paths=("a.py", "c.py"))
    assert out["committed"] is False
    assert _git(merging, "rev-parse", "HEAD").strip() == head
    assert out.get("refused") == "merge_foreign", out
    assert (merging / ".git" / "MERGE_HEAD").exists()


def test_no_merge_sends_no_finish_merge(tmp_path, monkeypatch):
    root = tmp_path / "plain"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    plan = root / _PLAN
    plan.parent.mkdir(parents=True)
    plan.write_text("# p\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    sent: list = []
    real = terminal_commit.reentrant_dispatch

    def spy(op, params, repo_root=None):
        if op == "ceremony.commit_v2":
            sent.append(dict(params))
        return real(op, params, repo_root=repo_root)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", spy)
    (root / "a.py").write_text("a\n", encoding="utf-8")
    out = _call(root)
    assert out["committed"] is True, out
    assert sent and all("finish_merge" not in p for p in sent)

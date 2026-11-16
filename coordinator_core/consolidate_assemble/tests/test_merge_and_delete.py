"""`apply._dispatch_merge_and_delete` never leaves a merge in progress.

A merge that stops after staging its result -- the message editor failing
with no terminal, a rejecting hook, a conflict -- left `MERGE_HEAD` in a
shared tree, and the next committer's commit consumed it as a merge commit.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.consolidate_assemble import apply as apply_mod
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True,
        check=check, **no_console_creationflags(),
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "work/me")
    _git(root, "config", "user.email", "t@local")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("base\n", encoding="utf-8", newline="\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "seed")
    _git(root, "checkout", "-q", "-b", "work/sibling")
    (root / "b.txt").write_text("sibling\n", encoding="utf-8", newline="\n")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-q", "-m", "sibling")
    _git(root, "checkout", "-q", "work/me")
    (root / "c.txt").write_text("mine\n", encoding="utf-8", newline="\n")
    _git(root, "add", "c.txt")
    _git(root, "commit", "-q", "-m", "mine")
    return root


def test_merges_without_an_editor(repo, monkeypatch):
    monkeypatch.setenv("GIT_EDITOR", "false")
    monkeypatch.setenv("COORDINATOR_OVERRIDE_BRANCH", "work/sibling")
    apply_mod._dispatch_merge_and_delete(["work/sibling", "work/sibling"], repo)
    assert (repo / "b.txt").exists()
    assert not (repo / ".git" / "MERGE_HEAD").exists()


def test_a_merge_stopped_after_staging_is_aborted(repo):
    hooks = repo.parent / "hooks"
    hooks.mkdir()
    (hooks / "commit-msg").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    (hooks / "commit-msg").chmod(0o755)
    _git(repo, "config", "core.hooksPath", str(hooks))
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(Exception):
        apply_mod._dispatch_merge_and_delete(["work/sibling", "work/sibling"], repo)

    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


@pytest.fixture()
def share_repo(repo: Path) -> Path:
    _git(repo, "checkout", "-q", "work/sibling")
    share = repo / ".coordinator-local" / "subagent-share" / "s1"
    share.mkdir(parents=True)
    (share / "r.md").write_text("x\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-f", ".coordinator-local/subagent-share/s1/r.md")
    _git(repo, "commit", "-q", "-m", "adds share")
    _git(repo, "checkout", "-q", "work/me")
    return repo


def test_merge_only_refuses_a_ref_adding_machine_local_paths(share_repo):
    head = _git(share_repo, "rev-parse", "HEAD").stdout.strip()
    with pytest.raises(RuntimeError, match="machine-local path"):
        apply_mod._dispatch_merge_only(["work/sibling", "work/sibling"], share_repo)
    assert _git(share_repo, "rev-parse", "HEAD").stdout.strip() == head
    assert not (share_repo / ".git" / "MERGE_HEAD").exists()


def test_merge_and_delete_refusal_keeps_the_branch(share_repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_OVERRIDE_BRANCH", "work/sibling")
    with pytest.raises(RuntimeError, match="machine-local path"):
        apply_mod._dispatch_merge_and_delete(["work/sibling", "work/sibling"], share_repo)
    assert _git(share_repo, "rev-parse", "--verify", "work/sibling").returncode == 0


def test_refused_delete_still_lands_the_merge(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_OVERRIDE_BRANCH", raising=False)
    _git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "work/sibling")
    with pytest.raises(RuntimeError, match="held by"):
        apply_mod._dispatch_merge_and_delete(["work/sibling", "work/sibling"], repo)
    assert (repo / "b.txt").exists()
    assert _git(repo, "merge-base", "--is-ancestor", "work/sibling", "HEAD", check=False).returncode == 0
    assert _git(repo, "rev-parse", "--verify", "work/sibling").returncode == 0


def test_free_branch_merge_and_delete_needs_no_override(repo, monkeypatch):
    monkeypatch.delenv("COORDINATOR_OVERRIDE_BRANCH", raising=False)
    result = apply_mod._dispatch_merge_and_delete(["work/sibling", "work/sibling"], repo)
    assert result["local_deleted"] == "work/sibling"
    assert _git(repo, "rev-parse", "--verify", "work/sibling", check=False).returncode != 0


def test_merge_ours_records_the_ref_without_its_content(repo):
    sha = _git(repo, "rev-parse", "work/me").stdout.strip()
    result = apply_mod._dispatch_merge_ours(["work/sibling", "work/sibling", sha], repo)

    assert result == {"cli": "merge-ours", "superseded_by": sha}
    assert not (repo / "b.txt").exists()
    assert _git(repo, "merge-base", "--is-ancestor", "work/sibling", "HEAD", check=False).returncode == 0
    assert f"absorb work/sibling: superseded by {sha}" in _git(repo, "log", "-1", "--format=%s").stdout
    assert _git(repo, "rev-parse", "--verify", "work/sibling").returncode == 0


def test_merge_ours_does_not_refuse_machine_local_additions(share_repo):
    apply_mod._dispatch_merge_ours(["work/sibling", "work/sibling", "abc123"], share_repo)
    assert not (share_repo / ".coordinator-local").exists()


@pytest.fixture()
def conflict_repo(repo: Path) -> Path:
    (repo / "a.txt").write_text("mine\n", encoding="utf-8", newline="\n")
    _git(repo, "commit", "-q", "-am", "mine edits a")
    _git(repo, "checkout", "-q", "work/sibling")
    (repo / "a.txt").write_text("theirs\n", encoding="utf-8", newline="\n")
    _git(repo, "commit", "-q", "-am", "sibling edits a")
    _git(repo, "checkout", "-q", "work/me")
    return repo


@pytest.mark.parametrize("verb", ["merge-only", "merge-and-delete", "merge-ours"])
def test_conflicted_merge_aborts_keeps_branch_and_names_ref(conflict_repo, monkeypatch, verb):
    monkeypatch.setenv("COORDINATOR_OVERRIDE_BRANCH", "work/sibling")
    head = _git(conflict_repo, "rev-parse", "HEAD").stdout.strip()
    # merge -s ours never conflicts; a conflict there needs the strategy forced off.
    if verb == "merge-ours":
        real = apply_mod._run_git
        monkeypatch.setattr(
            apply_mod, "_run_git",
            lambda a, c: real(["merge", "--no-ff", "--no-edit", a[-1]] if a[:3] == ["merge", "-s", "ours"] else a, c),
        )
    handler = apply_mod._CLI_DISPATCH[verb]
    with pytest.raises(RuntimeError, match="work/sibling"):
        handler(["work/sibling", "work/sibling", "abc123"], conflict_repo)

    assert not (conflict_repo / ".git" / "MERGE_HEAD").exists()
    assert _git(conflict_repo, "rev-parse", "HEAD").stdout.strip() == head
    assert _git(conflict_repo, "status", "--porcelain").stdout.strip() == ""
    assert _git(conflict_repo, "rev-parse", "--verify", "work/sibling").returncode == 0


def test_retired_cherry_pick_verbs_are_not_dispatchable():
    assert not {"cherry-pick-and-delete", "cherry-pick-only"} & set(apply_mod._CLI_DISPATCH)


# Delete only when the tip is reachable from origin/<base> (ported from the
# retired cherry-pick test module; the delete path is unchanged).

def _origin_with_branch(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    root = tmp_path / "repo2"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@local")
    _git(root, "config", "user.name", "t")
    (root / "base.txt").write_text("base\n", encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "checkout", "-q", "-b", "feat")
    (root / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "a")
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "-q", "--ff-only", "feat")
    _git(root, "push", "-q", "origin", "main", "feat")
    return root


def test_snapshot_merged_then_advanced_branch_is_refused(tmp_path, monkeypatch):
    from coordinator_core.consolidate_assemble import live_holders as lh

    monkeypatch.delenv("COORDINATOR_OVERRIDE_BRANCH", raising=False)
    monkeypatch.setattr(lh, "_cross_box_holders", lambda run_git, root, branches: {b: [] for b in branches})
    root = _origin_with_branch(tmp_path)
    _git(root, "checkout", "-q", "feat")
    for n in ("b", "c"):
        (root / f"{n}.txt").write_text(n, encoding="utf-8", newline="\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", n)
    _git(root, "push", "-q", "origin", "feat")
    _git(root, "checkout", "-q", "main")
    tip = _git(root, "rev-parse", "origin/feat").stdout.strip()

    with pytest.raises(RuntimeError) as exc:
        apply_mod._delete_branch("feat", True, root)

    assert tip in str(exc.value)
    assert "is not an ancestor of HEAD" in str(exc.value)
    assert _git(root, "ls-remote", "origin", "feat").stdout.strip()
    assert "feat" in _git(root, "branch").stdout


def test_fully_merged_branch_is_deleted_local_and_remote(tmp_path, monkeypatch):
    from coordinator_core.consolidate_assemble import live_holders as lh

    monkeypatch.delenv("COORDINATOR_OVERRIDE_BRANCH", raising=False)
    monkeypatch.setattr(lh, "_cross_box_holders", lambda run_git, root, branches: {b: [] for b in branches})
    root = _origin_with_branch(tmp_path)

    detail = apply_mod._delete_branch("feat", True, root)

    assert detail["local_deleted"] == "feat"
    assert detail["remote_deleted"] == "feat"
    assert not _git(root, "ls-remote", "origin", "feat").stdout.strip()


def test_clean_path_check_is_two_spawns(tmp_path, monkeypatch):
    root = _origin_with_branch(tmp_path)
    calls: list[list[str]] = []
    real = apply_mod._run_git
    monkeypatch.setattr(apply_mod, "_run_git", lambda a, c: (calls.append(a), real(a, c))[1])

    assert apply_mod._refuse_unmerged_tips("feat", True, root) is None
    assert len(calls) == 2


def _origin_with_branches(tmp_path: Path, names: list[str]) -> Path:
    root = _origin_with_branch(tmp_path)
    for n in names:
        _git(root, "branch", n, "main")
    _git(root, "push", "-q", "origin", *names)
    return root


def _run_batched(root: Path, names: list[str]) -> tuple[dict, list[list[str]]]:
    calls: list[list[str]] = []
    real = apply_mod._run_git
    apply_mod._run_git = lambda a, c: (calls.append(a), real(a, c))[1]
    batch: list = []
    apply_mod._remote_batch = batch
    results = {}
    try:
        for n in names:
            try:
                results[n] = apply_mod._dispatch_delete_only([n, "origin"], root)
            except RuntimeError as exc:
                results[n] = str(exc)
    finally:
        apply_mod._remote_batch = None
        summary = apply_mod._flush_remote_batch(batch, root) if batch else None
        apply_mod._run_git = real
    return {"results": results, "summary": summary}, calls


def test_remote_deletes_of_one_apply_leave_in_one_push(tmp_path, monkeypatch):
    from coordinator_core.consolidate_assemble import live_holders as lh

    monkeypatch.delenv("COORDINATOR_OVERRIDE_BRANCH", raising=False)
    monkeypatch.setattr(lh, "_cross_box_holders", lambda run_git, root, branches: {b: [] for b in branches})
    root = _origin_with_branches(tmp_path, ["b1", "b2", "b3"])

    out, calls = _run_batched(root, ["b1", "b2", "b3"])

    pushes = [c for c in calls if c[:1] == ["push"]]
    assert len(pushes) == 1
    assert pushes[0] == ["push", "origin", "--delete", "b1", "b2", "b3"]
    assert out["summary"]["deleted"] == ["b1", "b2", "b3"]
    for n in ("b1", "b2", "b3"):
        assert out["results"][n]["remote_deleted"] == n
        assert "remote_pending" not in out["results"][n]
        assert not _git(root, "ls-remote", "origin", n).stdout.strip()


def test_a_refused_branch_is_not_in_the_batch_and_stays(tmp_path, monkeypatch):
    from coordinator_core.consolidate_assemble import live_holders as lh

    monkeypatch.delenv("COORDINATOR_OVERRIDE_BRANCH", raising=False)
    monkeypatch.setattr(lh, "_cross_box_holders", lambda run_git, root, branches: {b: [] for b in branches})
    root = _origin_with_branches(tmp_path, ["b1", "b2"])
    _git(root, "checkout", "-q", "b2")
    (root / "x.txt").write_text("x", encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "x")
    _git(root, "push", "-q", "origin", "b2")
    _git(root, "checkout", "-q", "main")

    out, calls = _run_batched(root, ["b1", "b2"])

    pushes = [c for c in calls if c[:1] == ["push"]]
    assert pushes == [["push", "origin", "--delete", "b1"]]
    assert "is not an ancestor of HEAD" in out["results"]["b2"]
    assert _git(root, "ls-remote", "origin", "b2").stdout.strip()
    assert not _git(root, "ls-remote", "origin", "b1").stdout.strip()


def test_failed_batch_push_reports_which_names_survive(tmp_path, monkeypatch):
    from coordinator_core.consolidate_assemble import live_holders as lh

    monkeypatch.delenv("COORDINATOR_OVERRIDE_BRANCH", raising=False)
    monkeypatch.setattr(lh, "_cross_box_holders", lambda run_git, root, branches: {b: [] for b in branches})
    root = _origin_with_branches(tmp_path, ["b1", "b2"])
    hooks = tmp_path / "origin.git" / "hooks"
    (hooks / "pre-receive").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    (hooks / "pre-receive").chmod(0o755)

    out, _ = _run_batched(root, ["b1", "b2"])

    assert out["summary"]["error"]
    assert out["summary"]["deleted"] == []
    assert "remote_deleted" not in out["results"]["b1"]
    assert _git(root, "ls-remote", "origin", "b1").stdout.strip()


def _apply_with_delete_directives(root: Path, names: list[str], monkeypatch) -> tuple[int, dict, list[list[str]]]:
    from coordinator_core.consolidate_assemble import live_holders as lh

    monkeypatch.delenv("COORDINATOR_OVERRIDE_BRANCH", raising=False)
    monkeypatch.setattr(lh, "_cross_box_holders", lambda run_git, root, branches: {b: [] for b in branches})
    directives = [
        {"id": f"d-delete-{n}", "cli": "delete-only", "args": [n, "origin"],
         "depends_on": None, "already_satisfied": False}
        for n in names
    ]
    monkeypatch.setattr(
        apply_mod, "brief", lambda **_kw: {"directives": directives, "judgment_points": []}
    )
    monkeypatch.setattr(apply_mod, "flush_composition_record", lambda *a, **k: None)
    calls: list[list[str]] = []
    real = apply_mod._run_git
    monkeypatch.setattr(apply_mod, "_run_git", lambda a, c: (calls.append(a), real(a, c))[1])
    code, report = apply_mod.apply(session_id="sid-test", repo_root=root)
    return code, report, calls


def test_apply_batches_remote_deletes_into_one_push(tmp_path, monkeypatch):
    root = _origin_with_branches(tmp_path, ["b1", "b2"])

    code, report, calls = _apply_with_delete_directives(root, ["b1", "b2"], monkeypatch)

    assert code == apply_mod.APPLY_EXIT_OK
    assert [c for c in calls if c[:1] == ["push"]] == [["push", "origin", "--delete", "b1", "b2"]]
    assert report["remote_delete"]["deleted"] == ["b1", "b2"]
    assert apply_mod._remote_batch is None
    for n in ("b1", "b2"):
        assert not _git(root, "ls-remote", "origin", n).stdout.strip()


def test_apply_failed_batch_push_exits_partial_mutation(tmp_path, monkeypatch):
    root = _origin_with_branches(tmp_path, ["b1", "b2"])
    hook = tmp_path / "origin.git" / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)

    code, report, _ = _apply_with_delete_directives(root, ["b1", "b2"], monkeypatch)

    assert code == apply_mod.APPLY_EXIT_PARTIAL_MUTATION
    assert report["remote_delete"]["error"]
    assert report["error"] == report["remote_delete"]["error"]
    assert apply_mod._remote_batch is None
    assert _git(root, "ls-remote", "origin", "b1").stdout.strip()


def test_judged_recent_waives_only_the_recent_push_hold(tmp_path, monkeypatch):
    from coordinator_core.consolidate_assemble import live_holders as lh

    monkeypatch.delenv("COORDINATOR_OVERRIDE_BRANCH", raising=False)
    monkeypatch.setattr(
        lh, "_cross_box_holders", lambda run_git, root, branches: {b: [f"recent push to origin/{b}"] for b in branches}
    )
    root = _origin_with_branches(tmp_path, ["r1", "r2"])
    with pytest.raises(RuntimeError, match="held by recent push"):
        apply_mod._dispatch_delete_only(["r1", "origin"], root)
    detail = apply_mod._dispatch_delete_only(["r2", "origin", "judged-recent"], root)
    assert detail["remote_deleted"] == "r2"
    monkeypatch.setattr(
        lh, "_cross_box_holders", lambda run_git, root, branches: {b: [f"worktree /elsewhere/{b}"] for b in branches}
    )
    with pytest.raises(RuntimeError, match="held by worktree"):
        apply_mod._dispatch_delete_only(["r1", "origin", "judged-recent"], root)

"""resolve_receiver_landing / apply_receiver_landing / cas_head_symref on tmp_path repos.

Repos are built with git_objects (no git process), and Popen is patched to raise for the
whole test: the landing path is zero-spawn.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_objects
from coordinator_core.ops.fleet import _receiver_landing as RL
from coordinator_core.session import day_branch_cut_lock

DAY = "work/testbox/2026-10-10"
DAY_REF = f"refs/heads/{DAY}"
WHO = "T <t@example.com> 1 +0000"


def _commit(root: Path, message: str, parent: str | None = None) -> str:
    gitdir = root / ".git"
    tree = git_objects.build_tree(gitdir, {})
    lines = [f"tree {tree}"] + ([f"parent {parent}"] if parent else [])
    lines += [f"author {WHO}", f"committer {WHO}", "", message + "\n"]
    return git_objects.write_object(gitdir, b"commit", "\n".join(lines).encode())


def _set_ref(root: Path, ref: str, sha: str) -> None:
    path = root / ".git" / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sha + "\n")


def _set_head(root: Path, value: str) -> None:
    (root / ".git" / "HEAD").write_text(value + "\n")


def _ref(root: Path, ref: str) -> str | None:
    return git_objects._read_ref_raw(root / ".git" / ref)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_MACHINE", "testbox")
    monkeypatch.setattr(RL.daily_day, "local_day", lambda root=None: "2026-10-10")
    root = tmp_path / "recv"
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "refs" / "heads").mkdir(parents=True)
    (root / ".git" / "config").write_text(
        "[core]\n\tbare = false\n[user]\n\tname = T\n\temail = t@example.com\n"
    )
    _set_head(root, "ref: refs/heads/main")
    _set_ref(root, "refs/heads/main", _commit(root, "init"))
    (root / ".git" / "index").write_bytes(b"INDEX-BYTES")
    (root / "a.txt").write_text("a\n")
    return root


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn on the landing path")

    monkeypatch.setattr(subprocess, "Popen", boom)


def _snapshot(root: Path) -> dict:
    return {
        "main": _ref(root, "refs/heads/main"),
        "head": (root / ".git" / "HEAD").read_text(),
        "index": (root / ".git" / "index").read_bytes(),
        "a": (root / "a.txt").read_bytes(),
        "ls": sorted(p.name for p in root.iterdir()),
    }


def test_resolve_alone_mutates_nothing(repo):
    before = _snapshot(repo)
    landing = RL.resolve_receiver_landing(repo)
    assert (landing.mode, landing.ref_relpath, landing.minted, landing.switched) == (
        RL.MODE_SWITCHED, DAY_REF, True, True,
    )
    assert _snapshot(repo) == before
    assert _ref(repo, DAY_REF) is None
    assert day_branch_cut_lock.read_record(repo) is None


def test_head_on_work_branch_is_head_mode(repo):
    _set_ref(repo, "refs/heads/work/other/2026-01-01", _ref(repo, "refs/heads/main"))
    _set_head(repo, "ref: refs/heads/work/other/2026-01-01")
    landing = RL.resolve_receiver_landing(repo)
    assert landing.mode == RL.MODE_HEAD
    assert landing.ref_relpath == "refs/heads/work/other/2026-01-01"
    assert RL.apply_receiver_landing(landing) == landing


def test_head_on_configured_day_branch_is_head_mode(repo):
    _set_ref(repo, "refs/heads/feature/x", _ref(repo, "refs/heads/main"))
    _set_head(repo, "ref: refs/heads/feature/x")
    with open(repo / ".git" / "config", "a") as fh:
        fh.write("[coordinator]\n\tdayBranch = feature/x\n")
    assert RL.resolve_receiver_landing(repo).mode == RL.MODE_HEAD


def test_main_absent_day_branch_mints_and_switches(repo):
    before = _snapshot(repo)
    applied = RL.apply_receiver_landing(RL.resolve_receiver_landing(repo))
    assert (applied.mode, applied.minted, applied.switched) == (RL.MODE_SWITCHED, True, True)
    assert (repo / ".git" / "HEAD").read_text().strip() == f"ref: {DAY_REF}"
    assert _ref(repo, DAY_REF) == before["main"]
    after = _snapshot(repo)
    for key in ("main", "index", "a", "ls"):
        assert after[key] == before[key]
    assert day_branch_cut_lock.read_record(repo) is None
    assert before["main"] in (repo / ".git" / "logs" / "HEAD").read_text()


def test_main_day_branch_at_head_switches_without_mint(repo):
    _set_ref(repo, DAY_REF, _ref(repo, "refs/heads/main"))
    landing = RL.resolve_receiver_landing(repo)
    assert (landing.mode, landing.minted, landing.switched) == (RL.MODE_SWITCHED, False, True)
    applied = RL.apply_receiver_landing(landing)
    assert applied.switched and not applied.minted
    assert (repo / ".git" / "HEAD").read_text().strip() == f"ref: {DAY_REF}"
    assert day_branch_cut_lock.read_record(repo) is None


def test_main_day_branch_elsewhere_is_ref_direct(repo):
    old = _ref(repo, "refs/heads/main")
    _set_ref(repo, DAY_REF, old)
    _set_ref(repo, "refs/heads/main", _commit(repo, "second", old))
    before = _snapshot(repo)
    landing = RL.resolve_receiver_landing(repo)
    assert (landing.mode, landing.ref_relpath, landing.switched) == (
        RL.MODE_REF_DIRECT, DAY_REF, False,
    )
    applied = RL.apply_receiver_landing(landing)
    assert applied.mode == RL.MODE_REF_DIRECT and not applied.switched and not applied.minted
    assert _snapshot(repo) == before
    assert _ref(repo, DAY_REF) == old
    assert day_branch_cut_lock.read_record(repo) is None


@pytest.mark.parametrize("marker", ["MERGE_HEAD", "BISECT_LOG", "CHERRY_PICK_HEAD", "REVERT_HEAD"])
def test_mid_operation_main_mints_at_main_tip_ref_direct(repo, marker):
    tip = _ref(repo, "refs/heads/main")
    (repo / ".git" / marker).write_text(tip + "\n")
    before = _snapshot(repo)
    landing = RL.resolve_receiver_landing(repo)
    assert landing.mode == RL.MODE_REF_DIRECT and landing.minted
    applied = RL.apply_receiver_landing(landing)
    assert applied.mode == RL.MODE_REF_DIRECT and applied.minted
    assert _ref(repo, DAY_REF) == tip
    assert _snapshot(repo) == before
    assert day_branch_cut_lock.read_record(repo) is None


def test_rebase_dir_marker_is_mid_operation(repo):
    (repo / ".git" / "rebase-merge").mkdir()
    assert RL.resolve_receiver_landing(repo).mode == RL.MODE_REF_DIRECT


def test_feature_branch_mints_at_local_main_tip_and_never_moves_head(repo):
    main_tip = _ref(repo, "refs/heads/main")
    _set_ref(repo, "refs/heads/feature/x", _commit(repo, "feat", main_tip))
    _set_head(repo, "ref: refs/heads/feature/x")
    before_head = (repo / ".git" / "HEAD").read_text()
    applied = RL.apply_receiver_landing(RL.resolve_receiver_landing(repo))
    assert applied.mode == RL.MODE_REF_DIRECT and applied.minted
    assert _ref(repo, DAY_REF) == main_tip
    assert (repo / ".git" / "HEAD").read_text() == before_head
    assert _ref(repo, "refs/heads/main") == main_tip


def test_detached_head_is_ref_direct_minted_at_main_tip(repo):
    tip = _ref(repo, "refs/heads/main")
    _set_head(repo, tip)
    applied = RL.apply_receiver_landing(RL.resolve_receiver_landing(repo))
    assert applied.mode == RL.MODE_REF_DIRECT
    assert _ref(repo, DAY_REF) == tip
    assert (repo / ".git" / "HEAD").read_text().strip() == tip


def test_feature_branch_existing_day_branch_used_as_is(repo):
    tip = _ref(repo, "refs/heads/main")
    _set_ref(repo, DAY_REF, tip)
    _set_head(repo, "ref: refs/heads/release/1")
    landing = RL.resolve_receiver_landing(repo)
    assert (landing.mode, landing.minted) == (RL.MODE_REF_DIRECT, False)


def test_packed_day_branch_is_seen(repo):
    tip = _ref(repo, "refs/heads/main")
    (repo / ".git" / "packed-refs").write_text(f"# pack-refs\n{tip} {DAY_REF}\n")
    landing = RL.resolve_receiver_landing(repo)
    assert (landing.mode, landing.minted) == (RL.MODE_SWITCHED, False)


def test_lost_cut_lock_degrades_to_ref_direct_and_leaves_foreign_lock(repo, monkeypatch):
    monkeypatch.setattr(
        day_branch_cut_lock,
        "acquire",
        lambda root, **k: day_branch_cut_lock.CutLockVerdict(False, 1, "x", "held"),
    )
    released = []
    monkeypatch.setattr(day_branch_cut_lock, "release", lambda root, **k: released.append(root))
    before = _snapshot(repo)
    applied = RL.apply_receiver_landing(RL.resolve_receiver_landing(repo))
    assert applied.mode == RL.MODE_REF_DIRECT and not applied.switched
    assert _snapshot(repo) == before
    assert released == []


def test_lost_symref_cas_degrades_to_ref_direct_and_releases_lock(repo, monkeypatch):
    monkeypatch.setattr(git_objects, "cas_head_symref", lambda *a, **k: False)
    before = _snapshot(repo)
    applied = RL.apply_receiver_landing(RL.resolve_receiver_landing(repo))
    assert applied.mode == RL.MODE_REF_DIRECT and not applied.switched and applied.minted
    assert _snapshot(repo) == before
    assert day_branch_cut_lock.read_record(repo) is None


def test_main_moved_since_decision_degrades(repo):
    landing = RL.resolve_receiver_landing(repo)
    old = _ref(repo, "refs/heads/main")
    _set_ref(repo, "refs/heads/main", _commit(repo, "second", old))
    applied = RL.apply_receiver_landing(landing)
    assert applied.mode == RL.MODE_REF_DIRECT
    assert (repo / ".git" / "HEAD").read_text().strip() == "ref: refs/heads/main"
    assert day_branch_cut_lock.read_record(repo) is None


def test_cas_head_symref_requires_expected_target(repo):
    head_gitdir = repo / ".git"
    assert not git_objects.cas_head_symref(head_gitdir, "refs/heads/nope", DAY_REF)
    assert (head_gitdir / "HEAD").read_text().strip() == "ref: refs/heads/main"
    assert not (head_gitdir / "HEAD.lock").exists()


def test_cas_head_symref_loses_to_held_lock(repo):
    head_gitdir = repo / ".git"
    (head_gitdir / "HEAD.lock").write_text("")
    assert not git_objects.cas_head_symref(head_gitdir, "refs/heads/main", DAY_REF)
    assert (head_gitdir / "HEAD.lock").exists()
    assert (head_gitdir / "HEAD").read_text().strip() == "ref: refs/heads/main"


def test_cas_head_symref_writes_reflog_line(repo):
    head_gitdir = repo / ".git"
    sha = _ref(repo, "refs/heads/main")
    assert git_objects.cas_head_symref(
        head_gitdir, "refs/heads/main", DAY_REF,
        reflog_committer=WHO, reflog_message="checkout: x", sha=sha,
    )
    assert "checkout: x" in (head_gitdir / "logs" / "HEAD").read_text()
    assert git_objects._head_symref_target(head_gitdir) == DAY_REF

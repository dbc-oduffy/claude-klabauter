"""resolve_receiver_landing / apply_receiver_landing on tmp_path repos.

Repos are built with git_objects (no git process), and Popen is patched to raise for the
whole test: the landing path is zero-spawn.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.git import git_objects
from coordinator_core.ops.fleet import _receiver_landing as RL

STALE = "work/other/2026-10-01"
LIVE = "work/machine-a/2026-10-20"
STALE_REF = f"refs/heads/{STALE}"
LIVE_REF = f"refs/heads/{LIVE}"
LIVE_TRACKING = f"refs/remotes/origin/{LIVE}"


def _commit(root: Path, message: str, parent: str | None = None, when: int = 1) -> str:
    gitdir = root / ".git"
    tree = git_objects.build_tree(gitdir, {})
    who = f"T <t@example.com> {when} +0000"
    lines = [f"tree {tree}"] + ([f"parent {parent}"] if parent else [])
    lines += [f"author {who}", f"committer {who}", "", message + "\n"]
    return git_objects.write_object(gitdir, b"commit", "\n".join(lines).encode())


def _set_ref(root: Path, ref: str, sha: str) -> None:
    path = root / ".git" / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sha + "\n")


def _set_head(root: Path, value: str) -> None:
    (root / ".git" / "HEAD").write_text(value + "\n")


def _ref(root: Path, ref: str) -> str | None:
    return git_objects.read_ref_loose_or_packed(root / ".git", ref)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "recv"
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "refs" / "heads").mkdir(parents=True)
    (root / ".git" / "config").write_text(
        "[core]\n\tbare = false\n[user]\n\tname = T\n\temail = t@example.com\n"
    )
    _set_head(root, "ref: refs/heads/main")
    _set_ref(root, "refs/heads/main", _commit(root, "init", when=1))
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
        "head": (root / ".git" / "HEAD").read_text(),
        "index": (root / ".git" / "index").read_bytes(),
        "a": (root / "a.txt").read_bytes(),
        "main": _ref(root, "refs/heads/main"),
        "heads": sorted(p.relative_to(root).as_posix() for p in (root / ".git" / "refs" / "heads").rglob("*")),
    }


def test_resolve_alone_mutates_nothing(repo):
    _set_ref(repo, LIVE_REF, _commit(repo, "live", when=50))
    before = _snapshot(repo)
    RL.resolve_receiver_landing(repo)
    assert _snapshot(repo) == before


def test_stale_head_work_branch_loses_to_fresher_tracking_branch(repo):
    _set_ref(repo, STALE_REF, _commit(repo, "stale", when=10))
    _set_head(repo, f"ref: {STALE_REF}")
    live_tip = _commit(repo, "live", when=99)
    _set_ref(repo, LIVE_TRACKING, live_tip)
    before = _snapshot(repo)

    landing = RL.resolve_receiver_landing(repo)

    assert (landing.mode, landing.ref_relpath, landing.switched) == (RL.MODE_REF_DIRECT, LIVE_REF, False)
    assert (landing.minted, landing.mint_sha) == (True, live_tip)
    assert _snapshot(repo) == before


def test_head_on_the_live_branch_is_head_mode(repo):
    _set_ref(repo, STALE_REF, _commit(repo, "stale", when=10))
    _set_ref(repo, LIVE_REF, _commit(repo, "live", when=99))
    _set_head(repo, f"ref: {LIVE_REF}")
    landing = RL.resolve_receiver_landing(repo)
    assert (landing.mode, landing.ref_relpath, landing.minted) == (RL.MODE_HEAD, LIVE_REF, False)
    assert RL.apply_receiver_landing(landing) == landing


def test_clean_main_never_switches_or_mints_a_sender_branch(repo):
    _set_ref(repo, LIVE_TRACKING, _commit(repo, "live", when=99))
    before = _snapshot(repo)
    applied = RL.apply_receiver_landing(RL.resolve_receiver_landing(repo))
    assert applied.mode == RL.MODE_REF_DIRECT and not applied.switched
    assert _ref(repo, LIVE_REF) == _ref(repo, LIVE_TRACKING)
    after = _snapshot(repo)
    assert after["head"] == before["head"]
    work_files = [h for h in after["heads"] if (repo / h).is_file() and "/work/" in h]
    assert work_files == [f".git/{LIVE_REF}"]


def test_local_branch_with_tracking_is_not_minted(repo):
    _set_ref(repo, LIVE_REF, _commit(repo, "old", when=5))
    _set_ref(repo, LIVE_TRACKING, _commit(repo, "new", when=99))
    landing = RL.resolve_receiver_landing(repo)
    assert (landing.ref_relpath, landing.minted, landing.mint_sha) == (LIVE_REF, False, None)
    assert RL.apply_receiver_landing(landing) == landing


def test_local_branch_behind_tracking_ancestor_is_fast_forwarded_to_the_winning_tip(repo):
    old = _commit(repo, "old", when=5)
    new = _commit(repo, "new", parent=old, when=99)
    _set_ref(repo, LIVE_REF, old)
    _set_ref(repo, LIVE_TRACKING, new)
    landing = RL.resolve_receiver_landing(repo)
    assert (landing.ref_relpath, landing.minted, landing.mint_sha, landing.advance_from) == (
        LIVE_REF, True, new, old,
    )
    assert RL.apply_receiver_landing(landing).minted is True
    assert _ref(repo, LIVE_REF) == new


def test_diverged_local_branch_is_never_rewound_to_the_remote_tip(repo):
    local = _commit(repo, "local", when=5)
    _set_ref(repo, LIVE_REF, local)
    _set_ref(repo, LIVE_TRACKING, _commit(repo, "remote", when=99))
    landing = RL.resolve_receiver_landing(repo)
    assert landing.advance_from is None and landing.minted is False
    RL.apply_receiver_landing(landing)
    assert _ref(repo, LIVE_REF) == local


def test_packed_refs_only_tip_is_seen(repo):
    tip = _commit(repo, "packed", when=70)
    (repo / ".git" / "packed-refs").write_text(
        f"# pack-refs\n{tip} {LIVE_TRACKING}\n{_ref(repo, 'refs/heads/main')} refs/remotes/origin/main\n"
    )
    landing = RL.resolve_receiver_landing(repo)
    assert (landing.ref_relpath, landing.minted, landing.mint_sha) == (LIVE_REF, True, tip)


def test_packed_work_branch_competes_with_loose(repo):
    packed_tip = _commit(repo, "packed", when=200)
    (repo / ".git" / "packed-refs").write_text(f"# pack-refs\n{packed_tip} {STALE_REF}\n")
    _set_ref(repo, LIVE_REF, _commit(repo, "loose", when=100))
    assert RL.resolve_receiver_landing(repo).ref_relpath == STALE_REF


def test_symref_remote_head_is_skipped(repo):
    _set_ref(repo, LIVE_REF, _commit(repo, "live", when=50))
    (repo / ".git" / "refs" / "remotes" / "origin").mkdir(parents=True)
    (repo / ".git" / "refs" / "remotes" / "origin" / "HEAD").write_text("ref: refs/remotes/origin/main\n")
    assert RL.resolve_receiver_landing(repo).ref_relpath == LIVE_REF


def test_non_work_branches_are_never_candidates(repo):
    _set_ref(repo, "refs/heads/feature/x", _commit(repo, "feat", when=999))
    _set_ref(repo, "refs/remotes/origin/release/1", _commit(repo, "rel", when=999))
    landing = RL.resolve_receiver_landing(repo)
    assert (landing.mode, landing.ref_relpath) == (RL.MODE_HEAD, "refs/heads/main")


def test_equal_times_tie_break_to_head_branch_then_lexical(repo):
    tip = _commit(repo, "same", when=40)
    _set_ref(repo, "refs/heads/work/b/2026-10-20", tip)
    _set_ref(repo, "refs/heads/work/a/2026-10-20", tip)
    assert RL.resolve_receiver_landing(repo).ref_relpath == "refs/heads/work/a/2026-10-20"
    _set_head(repo, "ref: refs/heads/work/b/2026-10-20")
    landing = RL.resolve_receiver_landing(repo)
    assert (landing.mode, landing.ref_relpath) == (RL.MODE_HEAD, "refs/heads/work/b/2026-10-20")


def test_no_work_tip_falls_back_to_configured_day_branch(repo):
    main_tip = _ref(repo, "refs/heads/main")
    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write("[coordinator]\n\tdayBranch = feature/x\n")
    landing = RL.resolve_receiver_landing(repo)
    assert (landing.mode, landing.ref_relpath, landing.minted, landing.mint_sha) == (
        RL.MODE_REF_DIRECT, "refs/heads/feature/x", True, main_tip,
    )
    _set_ref(repo, "refs/heads/feature/x", main_tip)
    _set_head(repo, "ref: refs/heads/feature/x")
    assert RL.resolve_receiver_landing(repo).mode == RL.MODE_HEAD


def test_main_only_receiver_lands_on_its_head_branch_without_minting(repo):
    before = _snapshot(repo)
    landing = RL.resolve_receiver_landing(repo)
    assert (landing.mode, landing.ref_relpath, landing.minted, landing.mint_sha) == (
        RL.MODE_HEAD, "refs/heads/main", False, None,
    )
    assert RL.apply_receiver_landing(landing) == landing
    assert _snapshot(repo) == before


def test_head_on_a_non_work_branch_lands_there_not_on_main(repo):
    _set_ref(repo, "refs/heads/feature/x", _commit(repo, "feat", when=5))
    _set_head(repo, "ref: refs/heads/feature/x")
    landing = RL.resolve_receiver_landing(repo)
    assert (landing.mode, landing.ref_relpath) == (RL.MODE_HEAD, "refs/heads/feature/x")


def test_detached_head_falls_to_main_ref_direct(repo):
    _set_head(repo, _ref(repo, "refs/heads/main"))
    landing = RL.resolve_receiver_landing(repo)
    assert (landing.mode, landing.ref_relpath, landing.minted) == (
        RL.MODE_REF_DIRECT, "refs/heads/main", False,
    )


def test_unborn_head_with_only_master_lands_on_master(repo):
    _set_ref(repo, "refs/heads/master", _ref(repo, "refs/heads/main"))
    (repo / ".git" / "refs" / "heads" / "main").unlink()
    _set_head(repo, "ref: refs/heads/trunk")
    landing = RL.resolve_receiver_landing(repo)
    assert (landing.mode, landing.ref_relpath) == (RL.MODE_REF_DIRECT, "refs/heads/master")


def test_receiver_with_no_branch_at_all_is_the_none_landing(repo):
    (repo / ".git" / "refs" / "heads" / "main").unlink()
    landing = RL.resolve_receiver_landing(repo)
    assert landing.mode == RL.MODE_NONE and landing.ref_relpath == ""
    assert RL.apply_receiver_landing(landing) == landing


def test_unreadable_tip_object_still_resolves_with_time_zero(repo):
    _set_ref(repo, STALE_REF, "a" * 40)
    _set_ref(repo, LIVE_REF, _commit(repo, "live", when=5))
    assert RL.resolve_receiver_landing(repo).ref_relpath == LIVE_REF


def test_lost_mint_cas_is_benign_when_ref_now_exists(repo, monkeypatch):
    _set_ref(repo, LIVE_TRACKING, _commit(repo, "live", when=99))
    landing = RL.resolve_receiver_landing(repo)
    _set_ref(repo, LIVE_REF, landing.mint_sha)
    applied = RL.apply_receiver_landing(landing)
    assert not applied.minted and _ref(repo, LIVE_REF) == landing.mint_sha


def test_cas_head_symref_requires_expected_target(repo):
    head_gitdir = repo / ".git"
    assert not git_objects.cas_head_symref(head_gitdir, "refs/heads/nope", LIVE_REF)
    assert (head_gitdir / "HEAD").read_text().strip() == "ref: refs/heads/main"
    assert not (head_gitdir / "HEAD.lock").exists()

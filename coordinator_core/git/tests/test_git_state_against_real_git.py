"""Tests for `coordinator_core.git.git_state` that assert against REAL git.

Split out of the sibling `test_git_state.py` on 2026-08-23, and the split is the
point: every test here spawns `git` through a module-level helper
(`_git`/`_init_repo`/`_ls_tree_one_dir`), where a per-function `pytest.mark` is
inert because pytest applies marks only to what it collects. The spawn-ratchet's
only accepted remediation for that shape is a module-level `pytestmark`, which
tiers the WHOLE file onto cadence -- and in the combined file that meant exiling
22 spawn-free tests (0.6s) to move 9 spawning ones (8.8s). Separating the two
populations pays the ratchet honestly and costs the fast tier nothing.

The alternative -- faking git -- was rejected here and the reason is the same one
`test_git_state.py`'s own docstring gives for synthesising index bytes: these
tests exist to cross-check this repo's hand-rolled git-plumbing readers against
real `git ls-files` / `rev-parse` / `ls-tree` output. A faked oracle re-asserts
the module under test against itself.

Negative spec: nothing here may be de-tiered by faking its git. A test that stops
needing a real repo belongs in `test_git_state.py`, not here with its mark removed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.git.git_state import (  # noqa: E402
    head_blobs,
    head_tree_sha,
    read_index,
    read_tree_spine,
)
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# ---------------------------------------------------------------------------
# Equality against `git ls-files -s` over this repo's own live index


def test_read_index_matches_git_ls_files_over_live_repo():
    repo_root = str(Path(__file__).resolve().parents[3])
    out = subprocess.run(
        ["git", "-C", repo_root, "ls-files", "-s"],
        capture_output=True,
        text=True,
        timeout=30,
        **no_console_creationflags(),
    ).stdout

    git_map = {}
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        mode_str, sha, stage_str = meta.split(" ")
        git_map[path] = (int(mode_str, 8), sha, int(stage_str))

    snap = read_index(repo_root)

    only_in_git = [p for p in git_map if p not in snap]
    only_in_parse = [p for p in snap if p not in git_map]
    mismatched = [
        p
        for p, v in git_map.items()
        if p in snap and (snap[p].mode, snap[p].sha, snap[p].stage) != v
    ]

    assert only_in_git == []
    assert only_in_parse == []
    assert mismatched == []
    assert len(snap) == len(git_map)


# ---------------------------------------------------------------------------
# head_blobs -- the one retained spawn


def test_head_blobs_reads_this_repos_own_head_tree():
    # `git_state.py` itself is uncommitted while this chunk lands, so this
    # probes a file already present in HEAD -- `run.py`, its sibling module.
    repo_root = str(Path(__file__).resolve().parents[3])
    result = head_blobs(repo_root, ["coordinator_core/git/run.py"])

    assert "coordinator_core/git/run.py" in result
    mode, sha = result["coordinator_core/git/run.py"]
    assert mode in (0o100644, 0o100755)
    assert len(sha) == 40


def test_head_blobs_admits_gitlink_160000(tmp_path):
    """A submodule gitlink at HEAD MUST appear in `head_blobs`'s result --
    filtering it out (obj_type == "commit", not "blob") makes any caller
    that treats an absent `head_entry` as "no HEAD counterpart" misreport a
    genuinely dirty tracked gitlink as staged (the regression this test
    pins). SYNTHESISED via a direct index write, per this file's module
    docstring -- this repo's own index carries no 160000 entry to build a
    fixture from."""
    repo = tmp_path / "repo"
    repo.mkdir()
    kwargs = dict(cwd=str(repo), check=True, capture_output=True, text=True, **no_console_creationflags())
    subprocess.run(["git", "init", "-q"], **kwargs)
    subprocess.run(["git", "config", "user.email", "t@t.example"], **kwargs)
    subprocess.run(["git", "config", "user.name", "t"], **kwargs)
    (repo / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "--", "README.md"], **kwargs)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], **kwargs)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], **kwargs
    ).stdout.strip()
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"160000,{head_sha},vendor/sub"],
        **kwargs,
    )
    subprocess.run(["git", "commit", "-q", "-m", "add gitlink"], **kwargs)

    result = head_blobs(str(repo), ["vendor/sub"])

    assert "vendor/sub" in result
    mode, sha = result["vendor/sub"]
    assert mode == 0o160000
    assert sha == head_sha


# ---------------------------------------------------------------------------
# head_tree_sha / read_tree_spine -- real repos, real git.
#
# `head_sha` above is exercised against synthesised `.git/HEAD` bytes; these
# two readers are exercised against a real `git init` repo because the
# assertion IS "matches real git's own commit/tree object encoding and
# `git ls-tree` output", per this chunk's test-surface brief -- a
# synthesised commit/tree object would just be re-asserting this module's
# own encoding against itself.


def _git(args, *, cwd):
    kwargs = dict(cwd=str(cwd), check=True, capture_output=True, text=True, **no_console_creationflags())
    return subprocess.run(["git", *args], **kwargs)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "t@t.example"], cwd=repo)
    _git(["config", "user.name", "t"], cwd=repo)


def _ls_tree_one_dir(repo: Path, dirpath: str) -> dict:
    """`{name: (mode, sha)}` for one directory's immediate children, via
    `git ls-tree HEAD -- <dirpath>` (non-recursive) -- the independent
    oracle `read_tree_spine`'s output is asserted against."""
    target = dirpath + "/" if dirpath else "./"
    out = _git(["ls-tree", "HEAD", target], cwd=repo).stdout
    entries = {}
    for line in out.splitlines():
        if not line:
            continue
        meta, _, name = line.partition("\t")
        mode_str, _obj_type, sha = meta.split(" ")
        entries[name.rsplit("/", 1)[-1]] = (int(mode_str, 8), sha)
    return entries


def test_head_tree_sha_matches_git_rev_parse_loose(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    _git(["add", "--", "a.txt"], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)

    expected = _git(["rev-parse", "HEAD^{tree}"], cwd=repo).stdout.strip()
    assert head_tree_sha(repo) == expected


def test_head_tree_sha_matches_git_rev_parse_after_gc_packs_head(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    _git(["add", "--", "a.txt"], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)
    _git(["gc", "-q", "--aggressive"], cwd=repo)

    expected = _git(["rev-parse", "HEAD^{tree}"], cwd=repo).stdout.strip()
    assert head_tree_sha(repo) == expected


def test_read_tree_spine_deep_path_matches_ls_tree(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "root.txt").write_text("r", encoding="utf-8")
    nested = repo / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "leaf.txt").write_text("l", encoding="utf-8")
    _git(["add", "--", "."], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)

    spine = read_tree_spine(repo, ["a/b/c/leaf.txt"])

    assert spine is not None
    for dirpath in ("", "a", "a/b", "a/b/c"):
        assert spine[dirpath] == _ls_tree_one_dir(repo, dirpath), dirpath
    assert "leaf.txt" in spine["a/b/c"]
    assert "root.txt" in spine[""]


def test_read_tree_spine_root_path_matches_ls_tree(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "root.txt").write_text("r", encoding="utf-8")
    _git(["add", "--", "."], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)

    spine = read_tree_spine(repo, ["root.txt"])

    assert spine is not None
    assert spine[""] == _ls_tree_one_dir(repo, "")
    assert "root.txt" in spine[""]


def test_read_tree_spine_linked_worktree(tmp_path):
    main_repo = tmp_path / "main"
    _init_repo(main_repo)
    nested = main_repo / "dir"
    nested.mkdir()
    (nested / "f.txt").write_text("f", encoding="utf-8")
    _git(["add", "--", "."], cwd=main_repo)
    _git(["commit", "-q", "-m", "seed"], cwd=main_repo)

    wt_path = tmp_path / "wt"
    _git(["worktree", "add", "-q", str(wt_path), "-b", "wt-branch"], cwd=main_repo)

    spine = read_tree_spine(wt_path, ["dir/f.txt"])

    assert spine is not None
    assert spine[""] == _ls_tree_one_dir(wt_path, "")
    assert spine["dir"] == _ls_tree_one_dir(wt_path, "dir")
    assert "f.txt" in spine["dir"]


def test_read_tree_spine_gitlink_leaf_not_descended_into(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("x", encoding="utf-8")
    _git(["add", "--", "README.md"], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)
    submodule_sha = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    _git(
        ["update-index", "--add", "--cacheinfo", f"160000,{submodule_sha},vendor/sub"],
        cwd=repo,
    )
    _git(["commit", "-q", "-m", "add gitlink"], cwd=repo)

    spine = read_tree_spine(repo, ["vendor/sub"])

    assert spine is not None
    assert spine["vendor"] == _ls_tree_one_dir(repo, "vendor")
    mode, sha = spine["vendor"]["sub"]
    assert mode == 0o160000
    assert sha == submodule_sha
    assert "vendor/sub" not in spine

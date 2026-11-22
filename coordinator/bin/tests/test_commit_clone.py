"""commit_clone: the publisher-code closure of one commit written from the commit tree, warm hits
with zero spawns, a refusal naming a runnable retry when the clone cannot be built, and an entry
run from the clone rather than the caller's tree. All trees live under tmp_path."""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from types import ModuleType

pytestmark = [pytest.mark.complete]

_MODULE = Path(__file__).resolve().parents[1] / "lib" / "commit_clone.py"


@pytest.fixture()
def cc() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_commit_clone_under_test", _MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def _no_real_cache() -> Path:
        raise AssertionError("a test reached the real managed commit-clone cache; pass a tmp_path cache")

    module.default_cache_root = _no_real_cache
    return module


_ORPHAN: dict[str, str] = {}


def test_a_legacy_marker_still_names_the_source(cc, tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / cc._LEGACY_MARKER).write_text(f'{{"sha": "{"a" * 40}", "source_root": "{(tmp_path / "src").as_posix()}"}}', encoding="utf-8")
    assert cc.is_commit_clone(clone)
    assert cc.source_repo_root(clone) == tmp_path / "src"


def test_objects_dir_follows_a_linked_worktree_to_the_common_store(cc, tmp_path: Path) -> None:
    common = tmp_path / "main" / ".git"
    (common / "objects").mkdir(parents=True)
    gitdir = common / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {gitdir.as_posix()}\n", encoding="utf-8")
    assert cc._objects_dir(wt) == (common / "objects").resolve()


def test_prune_keeps_dest_and_newest(cc, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    names = [f"{i:040x}" for i in range(1, 6)]
    for i, name in enumerate(names):
        d = cache / name
        d.mkdir()
        os.utime(d, (1000 + i, 1000 + i))
    (cache / "abc123-staging").mkdir()
    keep = cache / names[0]  # oldest, still kept
    cc._prune(cache, keep=keep)
    left = {p.name for p in cache.iterdir()}
    assert names[0] in left
    assert "abc123-staging" in left
    assert {names[4], names[3]} <= left
    assert names[1] not in left and names[2] not in left


def test_prune_spares_a_recently_used_clone_beyond_retention(cc, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    names = [f"{i:040x}" for i in range(1, 7)]
    now = time.time()
    for i, name in enumerate(names):
        d = cache / name
        d.mkdir()
        os.utime(d, (now - 10 + i, now - 10 + i))
    cc._prune(cache, keep=cache / names[5])
    assert {p.name for p in cache.iterdir()} == set(names)


def _aged_clones(cc, cache: Path, count: int) -> list[Path]:
    """`count` sha-named clones, all far older than the grace window, oldest first."""
    cache.mkdir(exist_ok=True)
    out = []
    for i in range(1, count + 1):
        d = cache / f"{i:040x}"
        d.mkdir()
        os.utime(d, (1000 + i, 1000 + i))
        out.append(d)
    return out


def _lease(clone: Path, pid: int) -> Path:
    path = clone / ".git" / "in-use" / str(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    os.utime(clone, (1000, 1000))  # creating `.git` freshened the clone; the tests need it aged
    return path


def test_prune_spares_a_clone_with_a_live_lease_whatever_its_mtime(cc, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    clones = _aged_clones(cc, cache, 6)
    _lease(clones[1], os.getpid())
    cc._prune(cache, keep=clones[5])
    left = {p.name for p in cache.iterdir()}
    assert clones[1].name in left
    assert clones[0].name not in left and clones[2].name not in left


def test_prune_removes_a_clone_whose_lease_pid_is_dead(cc, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "cache"
    clones = _aged_clones(cc, cache, 6)
    _lease(clones[0], 424242)
    monkeypatch.setattr(cc, "_pid_alive", lambda pid: False)
    cc._prune(cache, keep=clones[5])
    assert not clones[0].exists()


def test_prune_spares_a_clone_leased_by_another_live_pid(cc, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "cache"
    clones = _aged_clones(cc, cache, 6)
    _lease(clones[0], 424242)
    monkeypatch.setattr(cc, "_pid_alive", lambda pid: pid == 424242)
    cc._prune(cache, keep=clones[5])
    assert clones[0].exists()


def test_clone_in_use_reads_the_real_pid_liveness(cc, tmp_path: Path) -> None:
    """The unpatched `_pid_alive` (session_registry loaded by path): our pid lives, an unused one does not."""
    clone = tmp_path / "clone"
    _lease(clone, 2**22 + 12345)
    assert not cc._clone_in_use(clone)
    assert not cc._clone_in_use(tmp_path / "no-such-clone")
    (clone / ".git" / "in-use" / "not-a-pid").write_bytes(b"")
    assert not cc._clone_in_use(clone)
    _lease(clone, os.getpid())
    assert cc._clone_in_use(clone)


def test_run_entry_holds_a_lease_while_running_and_drops_it_after(cc, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / cc._MARKER).write_text(f'{{"sha": "{"a" * 40}", "source_root": "{tmp_path.as_posix()}", "format": {cc._FORMAT}}}', encoding="utf-8")
    entry = clone / "entry.py"
    entry.write_text("import os, pathlib\nassert (pathlib.Path(__file__).parent / '.git' / 'in-use' / str(os.getpid())).is_file()\n", encoding="utf-8")
    monkeypatch.setattr(cc, "fill_paths", lambda c, rels: [])
    monkeypatch.setattr(cc, "_write_site", lambda c: c / ".git")
    monkeypatch.setattr(cc, "_record_extra", lambda c: None)
    monkeypatch.setattr(cc.sys, "path", list(cc.sys.path))
    monkeypatch.setattr(cc.sys, "meta_path", list(cc.sys.meta_path))
    assert cc.run_entry_from_clone(clone, "entry.py", ["entry.py"]) == 0
    assert not (clone / ".git" / "in-use" / str(os.getpid())).exists()


@pytest.fixture()
def fake_build(cc, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """A repo stand-in: the git reads of `materialise_commit_clone` answered without a spawn."""
    sha = "b" * 40
    objects = tmp_path / "src" / ".git" / "objects"
    objects.mkdir(parents=True)
    monkeypatch.setattr(cc, "_objects_dir", lambda repo: objects)
    monkeypatch.setattr(cc, "_seed_entries", lambda repo, s, extra: [("id1", "coordinator/bin/publish.py")])
    monkeypatch.setattr(cc, "_read_blobs", lambda repo, queries: [b"SEED = 1\n"])
    return tmp_path / "src", sha


def test_materialise_rebuilds_an_emptied_shell(cc, tmp_path: Path, fake_build) -> None:
    repo, sha = fake_build
    cache = tmp_path / "cache"
    (cache / sha).mkdir(parents=True)
    dest = cc.materialise_commit_clone(repo, sha, cache)
    assert cc._usable_marker(dest, sha) is not None
    assert (dest / "coordinator" / "bin" / "publish.py").read_bytes() == b"SEED = 1\n"


def test_materialise_fills_a_pinned_shell_in_place(cc, tmp_path: Path, fake_build, monkeypatch: pytest.MonkeyPatch) -> None:
    """A shell that cannot be removed or replaced (a live cwd on Windows) is filled where it stands."""
    repo, sha = fake_build
    cache = tmp_path / "cache"
    shell = cache / sha
    (shell / ".git" / "objects").mkdir(parents=True)  # a half-built shell: .git skeleton, no marker
    (shell / "stale.txt").write_text("old", encoding="utf-8")
    real_rmtree, real_replace = cc.shutil.rmtree, cc.os.replace

    def pinned_rmtree(path, *a, **kw):
        if Path(path) != shell:
            real_rmtree(path, *a, **kw)

    def pinned_replace(src, dst):
        if Path(dst) == shell:
            raise PermissionError("pinned")
        real_replace(src, dst)

    monkeypatch.setattr(cc.shutil, "rmtree", pinned_rmtree)
    monkeypatch.setattr(cc.os, "replace", pinned_replace)
    dest = cc.materialise_commit_clone(repo, sha, cache)
    assert dest == shell
    assert cc._usable_marker(dest, sha) is not None
    assert (dest / "coordinator" / "bin" / "publish.py").read_bytes() == b"SEED = 1\n"


def test_materialise_refuses_with_a_retry_when_the_shell_cannot_be_filled(cc, tmp_path: Path, fake_build, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, sha = fake_build
    cache = tmp_path / "cache"
    (cache / sha).mkdir(parents=True)

    def always_fail(src, dst):
        raise PermissionError("pinned")

    monkeypatch.setattr(cc.os, "replace", always_fail)
    with pytest.raises(cc.CommitCloneError, match="Retry: python"):
        cc.materialise_commit_clone(repo, sha, cache)


def test_every_engine_file_the_publisher_loads_by_path_is_seeded(cc: ModuleType) -> None:
    """A by-path load bypasses the clone's import filler, so the file must be in the static seed."""
    import re

    bin_dir = _MODULE.parents[1]
    loaded = {
        f"coordinator_core/{name}"
        for entry in cc._SEED_FILES
        if entry.startswith("coordinator/bin/") and entry.endswith(".py")
        for name in re.findall(r'"coordinator_core" / "([A-Za-z_]+\.py)"', (bin_dir / entry.split("/")[-1]).read_text(encoding="utf-8"))
    }
    assert loaded, "the scan found no by-path load; the pattern no longer matches the publisher"
    assert sorted(p for p in loaded if not cc._is_wanted(p, frozenset())) == []

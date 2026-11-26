"""An engine export seeded from a cached export of an earlier commit equals a full `git archive`.

Run: python -m pytest coordinator/bin/tests/test_publish_export_seed.py -q
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_export_seed_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _run(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True, creationflags=_NO_WINDOW)
    return out.stdout.decode().strip()


def _commit(repo: Path, files: dict[str, str | None], msg: str) -> str:
    for rel, body in files.items():
        p = repo / rel
        if body is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
    return _run(repo, "rev-parse", "HEAD")


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _archive(repo: Path, sha: str, dest: Path) -> None:
    arc = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", sha, "--", *publish._SOURCE_REF_IMPORTED_PATHS],
                         capture_output=True, check=True, creationflags=_NO_WINDOW)
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(arc.stdout)) as tar:
        tar.extractall(path=dest, filter="tar")


@contextlib.contextmanager
def _no_tree_walk():
    """os.walk/os.link are refused only inside the block: pytest's own teardown walks."""
    def _refuse(*_a, **_k):
        raise AssertionError("a seed must not walk or link the stored tree")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(publish.os, "walk", _refuse)
        mp.setattr(publish.os, "link", _refuse)
        yield


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=_NO_WINDOW)
    proc.wait()
    return proc.pid


def _export_fixture(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q")
    sha1 = _commit(repo, {
        "coordinator_core/__init__.py": "",
        "coordinator_core/a.py": "A1\n",
        "coordinator_core/gone.py": "bye\n",
        "coordinator_core/sub/b.py": "B\n",
        "coordinator/lib/c.py": "C1\n",
        "outside.txt": "not exported\n",
    }, "one")
    sha2 = _commit(repo, {
        "coordinator_core/a.py": "A2\n",
        "coordinator_core/gone.py": None,
        "coordinator_core/new/d.py": "D\n",
        "coordinator/lib/c.py": "C2\n",
    }, "two")
    base = tmp_path / "cache"
    _archive(repo, sha1, base / sha1)
    publish._write_export_marker(base / sha1, sha1)
    staging = base / "staging"
    staging.mkdir()

    def git(*args: str) -> "subprocess.CompletedProcess[bytes]":
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, creationflags=_NO_WINDOW)

    return repo, sha1, sha2, base, staging, git


def test_export_seed_claims_the_cached_export_without_walking_it(tmp_path: Path) -> None:
    repo, sha1, sha2, base, staging, git = _export_fixture(tmp_path)
    with _no_tree_walk():
        assert publish._seed_export_from_cache(git, base, sha2, staging) is True
    expected = tmp_path / "expected"
    _archive(repo, sha2, expected)
    assert _tree(staging) == _tree(expected)
    assert not (base / sha1).exists()
    assert not (staging / "coordinator_core" / "gone.py").exists()


def test_export_held_by_a_live_process_is_never_claimed(tmp_path: Path) -> None:
    _repo, sha1, sha2, base, staging, git = _export_fixture(tmp_path)
    publish._hold_tree(base / sha1)
    before = _tree(base / sha1)

    assert publish._seed_export_from_cache(git, base, sha2, staging) is False
    assert _tree(base / sha1) == before
    assert list(staging.iterdir()) == []


def test_export_hold_of_a_dead_process_does_not_block_and_is_stripped(tmp_path: Path) -> None:
    repo, sha1, sha2, base, staging, git = _export_fixture(tmp_path)
    (base / sha1 / f"{publish._HOLD_PREFIX}{_dead_pid()}").write_bytes(b"")

    assert publish._seed_export_from_cache(git, base, sha2, staging) is True
    assert not [p for p in staging.iterdir() if p.name.startswith(publish._HOLD_PREFIX)]
    expected = tmp_path / "expected"
    _archive(repo, sha2, expected)
    assert _tree(staging) == _tree(expected)


def test_export_seed_failure_after_the_claim_leaves_empty_staging(tmp_path: Path) -> None:
    _repo, _sha1, sha2, base, staging, real_git = _export_fixture(tmp_path)

    def git(*args: str) -> "subprocess.CompletedProcess[bytes]":
        if args[0] == "archive":
            return subprocess.CompletedProcess(args, 1, b"", b"boom")
        return real_git(*args)

    assert publish._seed_export_from_cache(git, base, sha2, staging) is False
    assert staging.is_dir() and list(staging.iterdir()) == []


def _engine_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q")
    sha1 = _commit(repo, {
        "coordinator_core/__init__.py": "",
        "coordinator/lib/percolate/__init__.py": "P1\n",
        "coordinator/lib/other.py": "O\n",
    }, "one")
    sha2 = _commit(repo, {"coordinator/lib/other.py": "O2\n"}, "two")
    return repo, sha1, sha2


def _managed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    base = tmp_path / "managed" / "claude-klabauter-publish-engine"
    monkeypatch.setattr(publish, "_engine_export_base", lambda: base)
    return base


def test_export_writes_a_marker_and_reuses_a_valid_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    publish._bootstrap_engine()
    repo, sha1, _ = _engine_repo(tmp_path)
    base = _managed(monkeypatch, tmp_path)
    dest = publish._export_engine_at_ref(repo, sha1)
    assert dest == base / sha1
    assert publish._export_is_complete(dest, sha1)
    (dest / "sentinel").write_text("kept", encoding="utf-8")
    assert publish._export_engine_at_ref(repo, sha1) == dest
    assert (dest / "sentinel").exists()


def test_export_missing_percolate_is_neither_reused_nor_seeded_from(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    publish._bootstrap_engine()
    repo, sha1, sha2 = _engine_repo(tmp_path)
    base = _managed(monkeypatch, tmp_path)
    first = publish._export_engine_at_ref(repo, sha1)
    shutil.rmtree(first / "coordinator" / "lib" / "percolate")

    assert not publish._export_is_complete(first, sha1)
    second = publish._export_engine_at_ref(repo, sha2)
    assert (second / "coordinator" / "lib" / "percolate" / "__init__.py").read_text(encoding="utf-8") == "P1\n"
    assert not (base / sha1).exists()

    third = publish._export_engine_at_ref(repo, sha1)
    assert (third / "coordinator" / "lib" / "percolate").is_dir()
    assert publish._export_is_complete(third, sha1)


def test_export_with_emptied_files_below_a_directory_is_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    publish._bootstrap_engine()
    repo, sha1, _ = _engine_repo(tmp_path)
    _managed(monkeypatch, tmp_path)
    first = publish._export_engine_at_ref(repo, sha1)
    assert publish._export_is_complete(first, sha1)
    (first / "coordinator" / "lib" / "percolate" / "__init__.py").unlink()

    assert not publish._export_is_complete(first, sha1)


def test_markerless_and_staging_dirs_are_never_seed_candidates(tmp_path: Path) -> None:
    repo, sha1, sha2 = _engine_repo(tmp_path)
    base = tmp_path / "cache"
    _archive(repo, sha1, base / sha1)  # markerless
    _archive(repo, sha1, base / f"{sha1[:12]}-abcd1234")
    staging = base / "staging"
    staging.mkdir()

    def git(*_args: str) -> "subprocess.CompletedProcess[bytes]":
        raise AssertionError("no git call without a valid cached export")

    assert publish._seed_export_from_cache(git, base, sha2, staging) is False
    assert list(staging.iterdir()) == []
    assert not (base / sha1).exists()
    assert (base / f"{sha1[:12]}-abcd1234").exists()


def test_no_cached_export_means_no_seed(tmp_path: Path) -> None:
    base = tmp_path / "cache"
    staging = base / "staging"
    staging.mkdir(parents=True)

    def git(*_args: str) -> "subprocess.CompletedProcess[bytes]":
        raise AssertionError("no git call without a cached export")

    assert publish._seed_export_from_cache(git, base, "f" * 40, staging) is False
    assert list(staging.iterdir()) == []


def test_shadow_seeded_from_the_store_matches_a_full_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    publish._bootstrap_engine()
    from coordinator_core import temp_layout
    from coordinator_core.percolate.shadow_store import COMPLETE_MARKER

    monkeypatch.setattr(temp_layout, "managed_tempdir", lambda: tmp_path / "managed")
    (tmp_path / "managed").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q")
    sha1 = _commit(repo, {"coordinator_core/a.py": "A1\n", "coordinator_core/gone.py": "x\n", "coordinator/lib/c.py": "C\n"}, "one")
    sha2 = _commit(repo, {"coordinator_core/a.py": "A2\n", "coordinator_core/gone.py": None, "coordinator_core/n.py": "N\n"}, "two")
    pathspec = list(publish._SOURCE_REF_IMPORTED_PATHS)

    store_root = tmp_path / "store"
    prev = store_root / "entry1"
    _archive(repo, sha1, prev)
    (prev / COMPLETE_MARKER).write_text("k1", encoding="utf-8")
    (prev / publish._SHADOW_SOURCE_NAME).write_text(
        publish.json.dumps({"sha": sha1, "pathspec": pathspec, "withheld": False}), encoding="utf-8")
    class _Store:
        root = store_root

    # A different pathspec, or a withheld-overlay entry, never seeds and is never claimed.
    assert publish._seed_shadow_from_store(_Store(), repo, sha2, ["coordinator_core"]) is None
    (prev / publish._SHADOW_SOURCE_NAME).write_text(
        publish.json.dumps({"sha": sha1, "pathspec": pathspec, "withheld": True}), encoding="utf-8")
    assert publish._seed_shadow_from_store(_Store(), repo, sha2, pathspec) is None
    assert prev.is_dir()
    (prev / publish._SHADOW_SOURCE_NAME).write_text(
        publish.json.dumps({"sha": sha1, "pathspec": pathspec, "withheld": False}), encoding="utf-8")

    with _no_tree_walk():
        seeded = publish._seed_shadow_from_store(_Store(), repo, sha2, pathspec)
    assert seeded is not None
    expected = tmp_path / "expected"
    _archive(repo, sha2, expected)
    assert _tree(seeded) == _tree(expected)
    assert not prev.exists()
    assert seeded.parent == tmp_path / "managed"


def _shadow_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    publish._bootstrap_engine()
    from coordinator_core import temp_layout
    from coordinator_core.percolate.shadow_store import COMPLETE_MARKER

    monkeypatch.setattr(temp_layout, "managed_tempdir", lambda: tmp_path / "managed")
    (tmp_path / "managed").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q")
    sha1 = _commit(repo, {"coordinator_core/a.py": "A1\n", "coordinator_core/sub/gone.py": "x\n", "coordinator/lib/c.py": "C\n"}, "one")
    sha2 = _commit(repo, {"coordinator_core/a.py": "A2\n", "coordinator_core/sub/gone.py": None, "coordinator_core/n.py": "N\n"}, "two")
    pathspec = list(publish._SOURCE_REF_IMPORTED_PATHS)
    store_root = tmp_path / "store"
    prev = store_root / "entry1"
    _archive(repo, sha1, prev)
    (prev / COMPLETE_MARKER).write_text("k1", encoding="utf-8")
    (prev / publish._SHADOW_SOURCE_NAME).write_text(
        publish.json.dumps({"sha": sha1, "pathspec": pathspec, "withheld": False}), encoding="utf-8")

    class _Store:
        root = store_root

    return repo, sha2, pathspec, prev, _Store()


def test_shadow_held_by_a_live_process_is_never_claimed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, sha2, pathspec, prev, store = _shadow_fixture(tmp_path, monkeypatch)
    publish._hold_tree(prev)
    before = _tree(prev)

    assert publish._seed_shadow_from_store(store, repo, sha2, pathspec) is None
    assert _tree(prev) == before

    publish._release_tree(prev)
    (prev / f"{publish._HOLD_PREFIX}{_dead_pid()}").write_bytes(b"")
    seeded = publish._seed_shadow_from_store(store, repo, sha2, pathspec)
    assert seeded is not None
    assert not [p for p in seeded.iterdir() if p.name.startswith(publish._HOLD_PREFIX)]


def test_shadow_hold_taken_between_check_and_claim_returns_the_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, sha2, pathspec, prev, store = _shadow_fixture(tmp_path, monkeypatch)
    real_claim = publish._claim_tree
    calls = []

    def _claim_then_reader_holds(src: Path, dest: Path) -> bool:
        ok = real_claim(src, dest)
        if not calls:
            publish._hold_tree(dest)
        calls.append((src, dest))
        return ok

    monkeypatch.setattr(publish, "_claim_tree", _claim_then_reader_holds)

    assert publish._seed_shadow_from_store(store, repo, sha2, pathspec) is None
    assert (prev / publish._SHADOW_SOURCE_NAME).is_file()
    assert list((tmp_path / "managed").iterdir()) == []


def test_export_hold_taken_between_check_and_claim_returns_the_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo, sha1, sha2, base, staging, git = _export_fixture(tmp_path)
    real_claim = publish._claim_tree
    calls = []

    def _claim_then_reader_holds(src: Path, dest: Path) -> bool:
        ok = real_claim(src, dest)
        if not calls:
            publish._hold_tree(dest)
        calls.append((src, dest))
        return ok

    monkeypatch.setattr(publish, "_claim_tree", _claim_then_reader_holds)

    assert publish._seed_export_from_cache(git, base, sha2, staging) is False
    assert (base / sha1 / "coordinator_core" / "a.py").read_text().strip() == "A1"
    assert staging.is_dir() and list(staging.iterdir()) == []


def test_export_failed_rollback_leaves_staging_empty_and_the_held_tree_aside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo, sha1, sha2, base, staging, git = _export_fixture(tmp_path)
    real_claim = publish._claim_tree
    calls = []

    def _claim_then_reader_holds_and_rollback_fails(src: Path, dest: Path) -> bool:
        if calls:
            return False
        calls.append((src, dest))
        ok = real_claim(src, dest)
        publish._hold_tree(dest)
        return ok

    monkeypatch.setattr(publish, "_claim_tree", _claim_then_reader_holds_and_rollback_fails)

    assert publish._seed_export_from_cache(git, base, sha2, staging) is False
    assert staging.is_dir() and list(staging.iterdir()) == []
    aside = [p for p in base.iterdir() if p.name.startswith("orphan-held-")]
    assert len(aside) == 1 and (aside[0] / "coordinator_core" / "a.py").read_text().strip() == "A1"
    assert not publish._EXPORT_SHA_RE.fullmatch(aside[0].name)


def test_shadow_seed_prunes_emptied_directories_like_a_full_extraction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, sha2, pathspec, _prev, store = _shadow_fixture(tmp_path, monkeypatch)

    seeded = publish._seed_shadow_from_store(store, repo, sha2, pathspec)
    assert seeded is not None
    assert not (seeded / "coordinator_core" / "sub").exists()


def test_shadow_seed_failure_after_the_claim_leaves_nothing_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, sha2, pathspec, prev, store = _shadow_fixture(tmp_path, monkeypatch)

    def _boom(*_a, **_k):
        raise publish.GitMaterializeError("boom")

    monkeypatch.setattr(publish, "_materialize_tree_paths", _boom)

    assert publish._seed_shadow_from_store(store, repo, sha2, pathspec) is None
    assert not prev.exists()
    assert list((tmp_path / "managed").iterdir()) == []


def test_the_adopted_seed_is_the_next_rounds_seed_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.percolate.shadow_store import ShadowStore

    repo, sha2, pathspec, prev, _unused = _shadow_fixture(tmp_path, monkeypatch)
    store = ShadowStore(repo, root=prev.parent)
    sha3 = _commit(repo, {"coordinator_core/n.py": "N3\n"}, "three")

    seeded = publish._seed_shadow_from_store(store, repo, sha2, pathspec)
    assert seeded is not None and not prev.exists()
    (seeded / publish._SHADOW_SOURCE_NAME).write_text(
        publish.json.dumps({"sha": sha2, "pathspec": pathspec, "withheld": False}), encoding="utf-8")
    key = store.key(repo, sha2, pathspec)
    entry = store.adopt(key, seeded)
    assert store.lookup(key) == entry and not seeded.exists()

    again = publish._seed_shadow_from_store(store, repo, sha3, pathspec)
    expected = tmp_path / "expected3"
    _archive(repo, sha3, expected)
    assert again is not None and _tree(again) == _tree(expected)
    assert not entry.exists()


def test_the_link_pool_is_gone() -> None:
    assert not hasattr(publish, "_SHADOW_SEED_LINK_WORKERS")

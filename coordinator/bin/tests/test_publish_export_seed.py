"""An engine export seeded from a cached export of an earlier commit equals a full `git archive`.

Run: python -m pytest coordinator/bin/tests/test_publish_export_seed.py -q
"""

from __future__ import annotations

import importlib.util
import io
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


def test_seeded_export_matches_a_full_archive_and_leaves_the_cache_untouched(tmp_path: Path) -> None:
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
    before = _tree(base / sha1)
    staging = base / "staging"
    staging.mkdir()

    def git(*args: str) -> "subprocess.CompletedProcess[bytes]":
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, creationflags=_NO_WINDOW)

    assert publish._seed_export_from_cache(git, base, sha2, staging) is True
    expected = tmp_path / "expected"
    _archive(repo, sha2, expected)
    assert _tree(staging) == _tree(expected)
    assert _tree(base / sha1) == before


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
    before = _tree(prev)

    class _Store:
        root = store_root

    seeded = publish._seed_shadow_from_store(_Store(), repo, sha2, pathspec)
    assert seeded is not None
    expected = tmp_path / "expected"
    _archive(repo, sha2, expected)
    assert _tree(seeded) == _tree(expected)
    assert _tree(prev) == before

    # A different pathspec, or a withheld-overlay entry, never seeds.
    assert publish._seed_shadow_from_store(_Store(), repo, sha2, ["coordinator_core"]) is None
    (prev / publish._SHADOW_SOURCE_NAME).write_text(
        publish.json.dumps({"sha": sha1, "pathspec": pathspec, "withheld": True}), encoding="utf-8")
    assert publish._seed_shadow_from_store(_Store(), repo, sha2, pathspec) is None

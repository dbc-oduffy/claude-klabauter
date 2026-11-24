"""Contract pin for `publish.py :: _extract_git_archive` materialization: LF blob bytes, 100755
modes, no tarfile, and an exact spawn count.

Fixture repo runs under `core.autocrlf=true` / `core.eol=crlf` (the invoking-repo state that
corrupts a bare checkout) with one 100755 script, an attribute-free LF file, a `text=auto` LF file
and a nested dir. `_required_pathspec_for_toplevel` is pinned to a fixed scope so the spawn count
is the materialization's own.

Run: python -m pytest coordinator/bin/tests/test_publish_materialize_checkout_index.py -q
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# read-tree + checkout-index + the one ls-tree that lists the scope (cached per toplevel+sha).
EXPECTED_SPAWNS = 3

_LF_BODY = b"line one\nline two\nline three\n"
_SCOPE = ("scripts", "plain", "auto", "nested", ".gitattributes")
_FILES = {
    "scripts/run.sh": b"#!/bin/sh\necho hi\n",
    "plain/LICENSE": _LF_BODY,
    "auto/notes.txt": _LF_BODY,
    "nested/deep/er/file.txt": _LF_BODY,
    "outside/never.txt": _LF_BODY,
}


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_materialize_checkout_index_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _git(root: Path, *args: str, capture: bool = False):
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, check=True,
        creationflags=_NO_WINDOW, text=not capture,
    )


def _init_risky_repo(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "main")
    for key, value in (
        ("user.email", "t@claude-klabauter.test"), ("user.name", "t"), ("commit.gpgsign", "false"),
        ("core.autocrlf", "true"), ("core.eol", "crlf"), ("core.fileMode", "true"),
    ):
        _git(root, "config", key, value)
    for rel, body in _FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    (root / ".gitattributes").write_bytes(b"auto/** text=auto\n")
    _git(root, "add", "-A")
    _git(root, "add", "--chmod=+x", "--", "scripts/run.sh")
    _git(root, "commit", "-q", "-m", "fixture")
    return _git(root, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def fixture_repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    sha = _init_risky_repo(root)
    publish._TRACKED_PATHS_AT_SHA_CACHE.clear()
    publish._REQUIRED_PATHSPEC_CACHE.clear()
    monkeypatch.setattr(publish, "_required_pathspec_for_toplevel", lambda toplevel, sha=None: _SCOPE)
    return root, sha


def _materialize(root: Path, sha: str) -> Path:
    return publish._extract_git_archive(root, sha)


def test_materialized_bytes_equal_blob_bytes(fixture_repo):
    root, sha = fixture_repo
    shadow = _materialize(root, sha)
    try:
        for rel in ("scripts/run.sh", "plain/LICENSE", "auto/notes.txt", "nested/deep/er/file.txt", ".gitattributes"):
            blob = _git(root, "cat-file", "blob", f"{sha}:{rel}", capture=True).stdout
            assert (shadow / rel).read_bytes() == blob, rel
        assert b"\r" not in (shadow / "auto/notes.txt").read_bytes()
        assert b"\r" not in (shadow / "plain/LICENSE").read_bytes()
    finally:
        shutil.rmtree(shadow, ignore_errors=True)


def test_scope_excludes_paths_outside_the_pathspec(fixture_repo):
    root, sha = fixture_repo
    shadow = _materialize(root, sha)
    try:
        assert not (shadow / "outside").exists()
    finally:
        shutil.rmtree(shadow, ignore_errors=True)


def test_executable_bit_is_kept(fixture_repo):
    root, sha = fixture_repo
    shadow = _materialize(root, sha)
    try:
        assert _git(root, "ls-tree", sha, "scripts/run.sh").stdout.startswith("100755")
        if os.name != "nt":
            assert os.stat(shadow / "scripts/run.sh").st_mode & 0o111
            assert not os.stat(shadow / "plain/LICENSE").st_mode & 0o111
    finally:
        shutil.rmtree(shadow, ignore_errors=True)


def test_materialization_never_opens_a_tarfile(fixture_repo, monkeypatch):
    root, sha = fixture_repo

    def _refuse(*a, **kw):
        raise AssertionError("tarfile.open called during materialization")

    monkeypatch.setattr(tarfile, "open", _refuse)
    shadow = _materialize(root, sha)
    try:
        assert (shadow / "scripts/run.sh").is_file()
    finally:
        shutil.rmtree(shadow, ignore_errors=True)


def test_cold_materialization_spawn_count_is_pinned(fixture_repo, monkeypatch):
    root, sha = fixture_repo
    spawned: list[list[str]] = []
    original = subprocess.Popen

    class _CountingPopen(original):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **kw):
            spawned.append([str(x) for x in args] if not isinstance(args, str) else [args])
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", _CountingPopen)
    shadow = _materialize(root, sha)
    monkeypatch.setattr(subprocess, "Popen", original)
    try:
        assert len(spawned) == EXPECTED_SPAWNS, spawned
        assert not any("archive" in argv for argv in spawned), spawned
    finally:
        shutil.rmtree(shadow, ignore_errors=True)

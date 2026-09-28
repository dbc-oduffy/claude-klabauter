"""C2: a (path, content-hash) pair in protected_paths.json must be kept outright by
strip_repo -- never scanned, never rewritten, and the check is not repo-identity-based
(no origin remote required)."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from coordinator_core.comment_strip import engine

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

# Portable Windows console-suppression flag -- resolves to CREATE_NO_WINDOW.
_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, creationflags=_NO_CONSOLE)


def _git_repo(tmp_path: Path, files: dict[str, str], configure_origin: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "T", cwd=repo)
    if configure_origin:
        _git("remote", "add", "origin", "https://example.invalid/repo.git", cwd=repo)
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


@pytest.fixture(autouse=True)
def _reset_protected_cache():
    engine._PROTECTED_PATHS_CACHE = None
    yield
    engine._PROTECTED_PATHS_CACHE = None


def _stub_protected_paths(monkeypatch: pytest.MonkeyPatch, path: str, content_hash: str) -> None:
    mapping = {path: {content_hash}}
    monkeypatch.setattr(engine, "_load_protected_paths", lambda: mapping)


def test_protected_file_untouched_after_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    text = "# comment that would normally be stripped\nx = 1\n"
    repo = _git_repo(tmp_path, {"a.py": text}, configure_origin=True)
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    _stub_protected_paths(monkeypatch, "a.py", content_hash)

    summary = engine.strip_repo(repo, apply=True)

    assert (repo / "a.py").read_text(encoding="utf-8") == text
    entry = next(f for f in summary["files"] if f["path"] == "a.py")
    assert entry["changed"] is False
    assert entry["skipped_reason"] == "protected"
    assert summary["files_changed"] == 0


def test_protected_file_untouched_with_no_origin_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keying is (path, content hash) only -- a repo with no `origin` remote is covered
    identically, since no slug is derived or stored."""
    text = "# comment that would normally be stripped\ny = 2\n"
    repo = _git_repo(tmp_path, {"b.py": text}, configure_origin=False)
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    _stub_protected_paths(monkeypatch, "b.py", content_hash)

    summary = engine.strip_repo(repo, apply=True)

    assert (repo / "b.py").read_text(encoding="utf-8") == text
    entry = next(f for f in summary["files"] if f["path"] == "b.py")
    assert entry["changed"] is False
    assert entry["skipped_reason"] == "protected"


def test_unprotected_file_with_same_path_but_different_hash_is_still_scanned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The hash must match too -- same path, different current bytes, is not protected."""
    text = "# comment that would normally be stripped\nz = 3\n"
    repo = _git_repo(tmp_path, {"c.py": text})
    wrong_hash = hashlib.sha256(b"unrelated content").hexdigest()
    _stub_protected_paths(monkeypatch, "c.py", wrong_hash)

    summary = engine.strip_repo(repo, apply=True)

    entry = next(f for f in summary["files"] if f["path"] == "c.py")
    assert entry["skipped_reason"] != "protected"


def test_is_excluded_path_has_no_protected_set_awareness(monkeypatch: pytest.MonkeyPatch):
    """`is_excluded_path` is the plain suffix/dir/substring check; protected-set
    membership is `is_protected`'s job, not this function's."""
    _stub_protected_paths(monkeypatch, "d.py", "deadbeef")
    assert engine.is_excluded_path("d.py") is False


def test_is_protected_with_matching_hash_is_protected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "d.py").write_bytes(b"content")
    content_hash = hashlib.sha256(b"content").hexdigest()
    _stub_protected_paths(monkeypatch, "d.py", content_hash)
    assert engine.is_protected("d.py", repo) is True


def test_is_protected_with_no_matching_entry_is_not_protected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "d.py").write_bytes(b"content")
    _stub_protected_paths(monkeypatch, "e.py", "deadbeef")
    assert engine.is_protected("d.py", repo) is False


def test_real_protected_paths_json_loads_and_covers_47_files():
    mapping = engine._load_protected_paths()
    total = sum(len(hashes) for hashes in mapping.values())
    assert total == 47


# --------------------------------------------------------------------------
# Fail-closed on a corrupt/malformed protected_paths.json -- a safety list
# that cannot be trusted must abort the run, never silently substitute
# `{"sources": []}` (fail-open: nothing protected, everything re-strippable).
# --------------------------------------------------------------------------


def test_load_protected_paths_raises_on_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "_PROTECTED_PATHS_FILE", tmp_path / "does-not-exist.json")
    with pytest.raises(OSError):
        engine._load_protected_paths()


def test_load_protected_paths_raises_on_malformed_json(monkeypatch, tmp_path):
    bad_file = tmp_path / "protected_paths.json"
    bad_file.write_text("not valid json {{{", encoding="utf-8")
    monkeypatch.setattr(engine, "_PROTECTED_PATHS_FILE", bad_file)
    with pytest.raises(ValueError):
        engine._load_protected_paths()


@pytest.mark.parametrize(
    "raw",
    [
        {"sources": "not-a-list"},
        {"sources": [{"files": "not-a-dict"}]},
        {"sources": [{"files": {"a.py": "not-a-valid-sha256"}}]},
        {"sources": [{"files": {"a.py": "deadbeef"}}]},  # too short to be sha256
        {"sources": [{"files": {"": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}}]},
    ],
)
def test_load_protected_paths_raises_on_malformed_shape(monkeypatch, tmp_path, raw):
    bad_file = tmp_path / "protected_paths.json"
    bad_file.write_text(__import__("json").dumps(raw), encoding="utf-8")
    monkeypatch.setattr(engine, "_PROTECTED_PATHS_FILE", bad_file)
    with pytest.raises(ValueError):
        engine._load_protected_paths()


def test_strip_repo_aborts_without_writes_on_corrupt_protected_paths(monkeypatch, tmp_path):
    """A corrupt protected_paths.json must abort `strip_repo` before any bytes are
    written -- fail closed, never a partial/uncontrolled strip."""
    text = "# comment that would normally be stripped\nx = 1\n"
    repo = _git_repo(tmp_path, {"a.py": text})
    engine._PROTECTED_PATHS_CACHE = None
    bad_file = tmp_path / "protected_paths.json"
    bad_file.write_text("not valid json {{{", encoding="utf-8")
    monkeypatch.setattr(engine, "_PROTECTED_PATHS_FILE", bad_file)

    with pytest.raises(ValueError):
        engine.strip_repo(repo, apply=True)

    assert (repo / "a.py").read_text(encoding="utf-8") == text

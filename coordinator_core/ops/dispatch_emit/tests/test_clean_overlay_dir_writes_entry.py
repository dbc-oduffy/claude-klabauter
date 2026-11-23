"""A clean export whose declared paths name a directory holds every worktree file under it, untracked included."""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

FIXTURE_DIR = "tests/fixtures/x"


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, "keep.txt", "keep\n")
    _write(root, f"{FIXTURE_DIR}/tracked.json", "{}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_a_directory_entry_exports_its_untracked_files(tmp_path):
    root = _repo(tmp_path)
    _write(root, f"{FIXTURE_DIR}/old_output.json", '{"new": true}\n')
    _write(root, f"{FIXTURE_DIR}/sub/deep.json", "[]\n")
    _write(root, f"{FIXTURE_DIR}/__pycache__/junk.pyc", "x")

    with cov.clean_export(root, "run1", [FIXTURE_DIR]) as dest:
        assert (dest / FIXTURE_DIR / "old_output.json").read_text(encoding="utf-8") == '{"new": true}\n'
        assert (dest / FIXTURE_DIR / "sub" / "deep.json").is_file()
        assert (dest / FIXTURE_DIR / "tracked.json").is_file()
        assert not (dest / FIXTURE_DIR / "__pycache__").exists()


def test_a_file_entry_still_exports_only_that_file(tmp_path):
    root = _repo(tmp_path)
    _write(root, f"{FIXTURE_DIR}/old_output.json", "{}\n")
    _write(root, f"{FIXTURE_DIR}/other.json", "{}\n")

    with cov.clean_export(root, "run1", [f"{FIXTURE_DIR}/old_output.json"]) as dest:
        assert (dest / FIXTURE_DIR / "old_output.json").is_file()
        assert not (dest / FIXTURE_DIR / "other.json").exists()


def test_an_untracked_new_file_entry_exports_its_bytes(tmp_path):
    root = _repo(tmp_path)
    _write(root, "pkg/tests/test_new.py", "x = 1\n")

    with cov.clean_export(root, "run1", ["pkg/tests/test_new.py"]) as dest:
        assert (dest / "pkg/tests/test_new.py").read_text(encoding="utf-8") == "x = 1\n"


def test_a_deleted_directory_entry_exports_as_deletions_of_its_tracked_files(tmp_path):
    root = _repo(tmp_path)
    with cov.clean_export(root, "run0", []) as dest:
        assert (dest / FIXTURE_DIR / "tracked.json").is_file()
    (root / FIXTURE_DIR / "tracked.json").unlink()

    with cov.clean_export(root, "run1", [FIXTURE_DIR]) as dest:
        assert not (dest / FIXTURE_DIR / "tracked.json").exists()
        assert (dest / "keep.txt").is_file()


def test_expand_dirs_passes_files_and_missing_untracked_paths_through(tmp_path):
    root = _repo(tmp_path)

    assert cov._expand_dirs(root, ("keep.txt", "never/existed.txt")) == ("keep.txt", "never/existed.txt")

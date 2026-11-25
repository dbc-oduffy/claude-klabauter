"""The default `--export-run` reverify entry collects and runs a run's own untracked new test file named in its command, and still holds a tracked file's peer dirt at HEAD."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "runUntrackedTest"
SEED = "pkg/tests/test_seed.py"
NEW = "pkg/tests/test_new_untracked.py"
DIRTY_TRACKED = "pkg/tests/test_dirty_tracked.py"
HEAD_SRC = "def test_dirty_tracked():\n    assert True\n"
DIRTY_SRC = "def test_dirty_tracked():\n    assert False, 'peer dirt reached the mirror'\n"


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _put(root, rel, text):
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def run_repo(tmp_path, monkeypatch):
    """A committed repo; the run's untracked new test is declared nowhere (no manifest path, no own-written
    path, no snapshot scope), so only the command that runs it can carry it into the mirror."""
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    _put(root, "pytest.ini", "[pytest]\n")
    _put(root, "pkg/__init__.py", "")
    _put(root, "pkg/tests/__init__.py", "")
    _put(root, SEED, "def test_seed():\n    assert True\n")
    _put(root, DIRTY_TRACKED, HEAD_SRC)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")

    _put(root, NEW, "def test_new_untracked():\n    assert True\n")
    _put(root, DIRTY_TRACKED, DIRTY_SRC)

    record = cov.measured_rev_record_path(root, RUN)
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"paths": [SEED]}), encoding="utf-8")
    record.with_name("manifest.json").write_text(json.dumps({"review_declared_paths": [SEED]}), encoding="utf-8")
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {})
    monkeypatch.setattr(cov, "_own_written", lambda *_a, **_k: [])
    monkeypatch.setattr(cov, "_snapshot_scope", lambda *_a, **_k: [])
    return root


def _reverify(root, *targets, capfd):
    code = cov.main(["--export-run", "--run-id", RUN, "--repo-root", str(root), "--",
                     sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *targets])
    captured = capfd.readouterr()
    return code, captured.out + captured.err


def test_an_untracked_new_test_file_named_in_the_command_is_collected_and_run(run_repo, capfd):
    code, out = _reverify(run_repo, NEW, capfd=capfd)

    assert code == 0, out
    assert "1 passed" in out and "not found" not in out


def test_an_untracked_new_test_node_id_is_collected_and_run(run_repo, capfd):
    code, out = _reverify(run_repo, f"{NEW}::test_new_untracked", capfd=capfd)

    assert code == 0, out
    assert "1 passed" in out


def test_a_tracked_file_the_command_names_stays_at_head_not_peer_dirt(run_repo, capfd):
    code, out = _reverify(run_repo, DIRTY_TRACKED, capfd=capfd)

    assert code == 0, out
    assert "peer dirt reached the mirror" not in out


def test_argv_named_untracked_names_only_untracked_files_inside_the_repo(run_repo):
    named = cov.argv_named_untracked(
        run_repo, [sys.executable, "-m", "pytest", "-q", NEW, DIRTY_TRACKED, SEED, "pkg/tests/test_absent.py", "../x.py", "-k", "expr"],
    )

    assert named == [NEW]

"""The live overlay carries the dirty import closure of the tests it overlays, never a test without its module."""
from __future__ import annotations

import json
import subprocess
import tempfile

import pytest

from coordinator_core.ops import test_leg_run as tlr
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "runImportClosure"
SEED = "pkg/tests/test_seed.py"
OWNED = "pkg/tests/test_owned_new.py"
PEER = "pkg/peer.py"
HOP = "pkg/hop.py"
UNRELATED = "pkg/unrelated.py"


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
    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    _put(root, "pytest.ini", "[pytest]\n")
    _put(root, "pkg/__init__.py", "")
    _put(root, PEER, "VALUE = 1\n")
    _put(root, HOP, "BASE = 1\n")
    _put(root, UNRELATED, "X = 1\n")
    _put(root, "pkg/tests/__init__.py", "")
    _put(root, SEED, "def test_seed():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    monkeypatch.chdir(root)
    return root, _git(root, "rev-parse", "HEAD")


def _leg(root, base, out):
    _put(root, f"state/warp/{RUN}/manifest.json", json.dumps({"doc_writes": [OWNED]}))
    tlr.main(["--repo-root", str(root), "--run-id", RUN, "--base-sha", base, "--out", str(out), "--", OWNED])
    return json.loads(out.read_text(encoding="utf-8"))


def test_a_dirty_module_an_overlaid_test_imports_is_carried_and_the_test_runs(run_repo, tmp_path):
    root, base = run_repo
    _put(root, PEER, "VALUE = 1\nPEER_ONLY = 2\n")
    _put(root, OWNED, "from pkg.peer import PEER_ONLY\n\n\ndef test_owned():\n    assert PEER_ONLY == 2\n")

    receipt = _leg(root, base, tmp_path / "o.json")

    assert receipt["tests_run"] >= 1, receipt
    assert "zero_run_cause" not in receipt, receipt
    assert PEER in receipt["own_tree"]["overlaid"], receipt


def test_the_closure_follows_a_dirty_module_through_another_dirty_module(run_repo, tmp_path):
    root, base = run_repo
    _put(root, HOP, "from pkg.peer import PEER_ONLY\n\nHOPPED = PEER_ONLY\n")
    _put(root, PEER, "VALUE = 1\nPEER_ONLY = 2\n")
    _put(root, OWNED, "from pkg.hop import HOPPED\n\n\ndef test_owned():\n    assert HOPPED == 2\n")

    receipt = _leg(root, base, tmp_path / "o.json")

    assert receipt["tests_run"] >= 1 and "zero_run_cause" not in receipt, receipt
    assert {HOP, PEER} <= set(receipt["own_tree"]["overlaid"]), receipt


def test_a_dirty_module_no_overlaid_file_imports_stays_at_head(run_repo, tmp_path):
    root, base = run_repo
    _put(root, UNRELATED, "X = 1\nPEER_DIRT = 2\n")
    _put(root, OWNED, "def test_owned():\n    assert True\n")

    receipt = _leg(root, base, tmp_path / "o.json")

    assert receipt["tests_run"] >= 1, receipt
    assert UNRELATED not in receipt["own_tree"]["overlaid"], receipt


def test_a_clean_imported_module_is_not_overlaid(run_repo, tmp_path):
    root, base = run_repo
    _put(root, OWNED, "from pkg.peer import VALUE\n\n\ndef test_owned():\n    assert VALUE == 1\n")

    receipt = _leg(root, base, tmp_path / "o.json")

    assert receipt["tests_run"] >= 1, receipt
    assert PEER not in receipt["own_tree"]["overlaid"], receipt

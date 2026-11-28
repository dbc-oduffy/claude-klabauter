"""test_leg_run at default flags overlays the untracked module a tracked target reaches through a tracked importer, and names a carried half of a cross-file pair as a snapshot gap instead of a failing test."""
from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops import test_leg_run as tlr
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "runUntrackedImport"
TEST = "pkg/tests/test_tracked.py"
MOD = "pkg/mod.py"
NEWMOD = "pkg/newmod.py"
OTHER = "pkg/other.py"


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _put(root, rel, text):
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def _repo(tmp_path, monkeypatch, files):
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
    for rel, text in files.items():
        _put(root, rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    monkeypatch.chdir(root)
    return root, _git(root, "rev-parse", "HEAD")


def _leg(root, base, out, target):
    """The receipt of `test_leg_run.main` at default flags (only --run-id and --base-sha) over `target`."""
    tlr.main(["--repo-root", str(root), "--run-id", RUN, "--base-sha", base, "--out", str(out), "--", target])
    return json.loads(out.read_text(encoding="utf-8"))


def test_a_tracked_test_reaching_the_runs_untracked_module_through_a_tracked_importer_runs(tmp_path, monkeypatch):
    root, base = _repo(tmp_path, monkeypatch, {
        MOD: "from pkg.newmod import VALUE\n",
        TEST: "from pkg.mod import VALUE\n\n\ndef test_tracked():\n    assert VALUE == 3\n",
    })
    _put(root, NEWMOD, "VALUE = 3\n")

    receipt = _leg(root, base, tmp_path / "o.json", TEST)

    assert receipt["tests_run"] > 0, receipt
    assert "zero_run_cause" not in receipt, receipt
    assert NEWMOD in receipt["own_tree"]["overlaid"], receipt


def test_a_carried_half_of_a_cross_file_pair_is_named_as_a_snapshot_gap_not_a_failing_test(tmp_path, monkeypatch):
    root, base = _repo(tmp_path, monkeypatch, {
        MOD: "VALUE = 1\n",
        OTHER: "OLD = 1\n",
        TEST: "from pkg.mod import call\n\n\ndef test_tracked():\n    assert call() == 7\n",
    })
    _put(root, MOD, "import importlib\n\n\ndef call():\n    other = importlib.import_module('pkg.' + 'other')\n    return other.NEW_HELPER()\n")
    _put(root, OTHER, "OLD = 1\n\n\ndef NEW_HELPER():\n    return 7\n")
    _put(root, f"state/warp/{RUN}/manifest.json", json.dumps({"doc_writes": [MOD, TEST]}))

    receipt = _leg(root, base, tmp_path / "o.json", TEST)

    gap = receipt.get("snapshot_gap") or {}
    assert gap.get("symbol") == "NEW_HELPER" and gap.get("defining_path") == OTHER, receipt
    assert receipt["failing"] == [], receipt

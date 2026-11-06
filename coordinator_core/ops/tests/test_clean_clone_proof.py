"""Clean-clone proof: green only through an uncommitted edit is red at the terminal sha; committed is green."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.ops import clean_clone_proof
from coordinator_core.ops.clean_clone_proof_contract import verdict_path
from coordinator_core.ops.dispatch_emit import run_close

RUN_ID = "run-ccp-fixture"
TEST_SRC = "import mod\n\n\ndef test_value():\n    assert mod.VALUE == 2\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    monkeypatch.delenv("PYTHONPATH", raising=False)


def _fixture_repo(tmp_path: Path, *, committed_value: int) -> tuple[Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / ".git" / "info" / "exclude").write_text(
        "scratch/\n.coordinator-local/\nstate/\n__pycache__/\n.pytest_cache/\n", encoding="utf-8"
    )
    (root / "README.md").write_text("x\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "mod.py").write_text(f"VALUE = {committed_value}\n", encoding="utf-8")
    (root / "test_mod.py").write_text(TEST_SRC, encoding="utf-8")
    _git(root, "add", "mod.py", "test_mod.py")
    _git(root, "commit", "-q", "-m", "run")
    terminal = _git(root, "rev-parse", "HEAD")
    run_close.write_close_job(
        root, RUN_ID, [], datetime.now(timezone.utc) + timedelta(hours=1),
        run_base_sha=base, terminal_sha=terminal,
    )
    return root, base, terminal


def _criterion(root: Path):
    return json.loads((root / run_close.close_job_path(RUN_ID)).read_text(encoding="utf-8"))["criterion"]


def test_green_only_through_uncommitted_edit_is_red_at_sha(tmp_path):
    root, base, terminal = _fixture_repo(tmp_path, committed_value=1)
    (root / "mod.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / "stray.txt").write_text("untracked\n", encoding="utf-8")

    assert clean_clone_proof.main(["--repo", str(root), "--run-id", RUN_ID]) == 1

    crit = _criterion(root)
    assert crit["status"] == "not_met"
    assert crit["reason"] == "red-at-sha"
    assert crit["sha"] == terminal
    assert any("test_value" in f for f in crit["failing"])
    assert "mod.py" in crit["masked_paths"]
    assert "stray.txt" in crit["masked_paths"]
    verdict = json.loads((root / verdict_path(terminal)).read_text(encoding="utf-8"))
    assert verdict["outcome"] == "failed"
    assert verdict["run_base_sha"] == base
    assert sorted(verdict["selected"]) == ["test_mod.py"]


def test_committed_fix_passes_and_leaves_criterion_unchanged(tmp_path):
    root, _, terminal = _fixture_repo(tmp_path, committed_value=2)
    before = _criterion(root)

    assert clean_clone_proof.main(["--repo", str(root), "--run-id", RUN_ID]) == 0

    verdict = json.loads((root / verdict_path(terminal)).read_text(encoding="utf-8"))
    assert verdict["outcome"] == "passed"
    assert verdict["failing_ids"] == []
    assert _criterion(root) == before


def test_clone_is_torn_down(tmp_path):
    root, _, _ = _fixture_repo(tmp_path, committed_value=2)
    clean_clone_proof.prove(root, RUN_ID)
    leftovers = [p for p in root.rglob("ccp-*") if ".git" not in p.parts]
    assert leftovers == []


def test_missing_close_job_exits_2(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    assert clean_clone_proof.main(["--repo", str(root), "--run-id", "absent"]) == 2


def test_launcher_env_does_not_leak_into_the_clone_run(tmp_path, monkeypatch):
    root, _, terminal = _fixture_repo(tmp_path, committed_value=2)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no_such_launcher_plugin_xyz")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "elsewhere"))

    assert clean_clone_proof.main(["--repo", str(root), "--run-id", RUN_ID]) == 0

    verdict = json.loads((root / verdict_path(terminal)).read_text(encoding="utf-8"))
    assert verdict["outcome"] == "passed"
    assert clean_clone_proof.os.environ["PYTEST_ADDOPTS"] == "-p no_such_launcher_plugin_xyz"

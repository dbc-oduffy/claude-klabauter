"""`plan-spine-check` advisory: a row_build_gate matches a row while another row creates the gate command."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.spawns_process

_MOD = Path(__file__).resolve().parents[1] / "plan-spine-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("plan_spine_check_gate_other", _MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plan_spine_check_gate_other"] = mod
    spec.loader.exec_module(mod)
    return mod


psc = _load()

_MARK = "gate command is created by another row"


@pytest.fixture(autouse=True)
def _no_width(monkeypatch):
    monkeypatch.setattr(psc, "_compute_width", lambda path: (None, "stubbed"))


@pytest.fixture
def repo(tmp_path):
    from coordinator_core.git import ls_files

    ls_files._tracked_files_cached.cache_clear()
    git = ["git", "-C", str(tmp_path)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run([*git, "init", "-q"], check=True, creationflags=flags)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/old_gate.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run([*git, "add", "scripts/old_gate.py"], check=True, creationflags=flags)
    yield tmp_path
    ls_files._tracked_files_cached.cache_clear()


def _plan(repo: Path, glob: str, command: str) -> Path:
    path = repo / "plan.md"
    path.write_text(
        "---\ntitle: t\nrow_build_gate:\n"
        f"  - when: {{surface_glob: \"{glob}\"}}\n"
        f"    command: \"{command}\"\n---\n\n# P\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: A0\n  title: a\n  writes: [src/a.py]\n"
        "- id: V0\n  title: v\n  writes: [scripts/new_gate.py, src/v.py]\n```\n",
        encoding="utf-8",
    )
    return path


def _hits(report):
    return [a for a in report["advisories"] if _MARK in a["error"]]


def test_gate_matching_other_row_flags_it_naming_both_rows(repo):
    found = _hits(psc.check_plan(_plan(repo, "src/*.py", "python scripts/new_gate.py")))
    rows = {a["row"] for a in found}
    assert rows == {"A0"}
    assert found[0]["class"] == "advisory"
    assert found[0]["at"] == "row_build_gate"
    assert "V0" in found[0]["error"] and "scripts/new_gate.py" in found[0]["error"]


def test_gate_matching_only_its_creator_emits_none(repo):
    assert _hits(psc.check_plan(_plan(repo, "src/v.py", "python scripts/new_gate.py"))) == []


def test_command_naming_tracked_file_emits_none(repo):
    assert _hits(psc.check_plan(_plan(repo, "src/*.py", "python scripts/old_gate.py"))) == []


def test_empty_tracked_listing_emits_none(repo, monkeypatch):
    from coordinator_core.git import ls_files

    monkeypatch.setattr(ls_files, "tracked_files", lambda *a, **k: [])
    assert _hits(psc.check_plan(_plan(repo, "src/*.py", "python scripts/new_gate.py"))) == []


def test_tracked_files_listed_once(repo, monkeypatch):
    from coordinator_core.git import ls_files

    calls = []
    real = ls_files.tracked_files
    monkeypatch.setattr(ls_files, "tracked_files", lambda *a, **k: calls.append(a) or real(*a, **k))
    assert len(_hits(psc.check_plan(_plan(repo, "src/*.py", "python scripts/new_gate.py")))) == 1
    assert len(calls) == 1

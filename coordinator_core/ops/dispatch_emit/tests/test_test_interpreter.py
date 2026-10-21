"""test_interpreter: pin ladder, clause rendering, no spawn."""
from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_emit import test_interpreter as ti


def _repo(tmp_path, **keys):
    body = "".join(f'{k}: "{v}"\n' for k, v in keys.items())
    (tmp_path / "coordinator.local.md").write_text(f"---\n{body}---\n", encoding="utf-8")
    return tmp_path


def test_key_pin_resolves_absolute_path(tmp_path):
    repo = _repo(tmp_path, targeted_test_python=sys.executable)
    got = ti.resolve_targeted_test_python(repo)
    assert got == ti.TargetedPython(sys.executable, "targeted_test_python", None)
    clause = ti.interpreter_clause(repo)
    assert sys.executable in clause and "falling back" not in clause


def test_key_pin_bare_name_resolved_by_which(tmp_path, monkeypatch):
    monkeypatch.setattr(ti.shutil, "which", lambda name: "found" if name == "mypy3.99" else None)
    got = ti.resolve_targeted_test_python(_repo(tmp_path, targeted_test_python="mypy3.99"))
    assert got.command == "mypy3.99" and got.defect is None


def test_fast_cmd_absolute_leading_token(tmp_path):
    repo = _repo(tmp_path, fast_test_cmd=f"{sys.executable} -m pytest")
    got = ti.resolve_targeted_test_python(repo)
    assert got.command == sys.executable and got.source == "fast_test_cmd" and got.defect is None
    assert sys.executable in ti.interpreter_clause(repo)


def test_fast_cmd_versioned_name_leading_token(tmp_path, monkeypatch):
    monkeypatch.setattr(ti.shutil, "which", lambda name: "x")
    got = ti.resolve_targeted_test_python(_repo(tmp_path, fast_test_cmd="python3.14 -m pytest"))
    assert (got.command, got.source) == ("python3.14", "fast_test_cmd")


@pytest.mark.parametrize("cmd", ["python3 -m pytest", "python -m pytest", "pytest -q", "make test"])
def test_bare_interpreter_or_non_interpreter_is_not_a_pin(tmp_path, cmd):
    repo = _repo(tmp_path, fast_test_cmd=cmd)
    assert ti.resolve_targeted_test_python(repo) == ti.TargetedPython(None, "none", None)
    assert ti.interpreter_clause(repo) == ti.PORTABLE_CLAUSE


def test_key_outranks_fast_cmd(tmp_path):
    repo = _repo(tmp_path, targeted_test_python=sys.executable, fast_test_cmd="python3.14 -m pytest")
    assert ti.resolve_targeted_test_python(repo).source == "targeted_test_python"


def test_unresolvable_pin_yields_defect_clause(tmp_path):
    missing = str(tmp_path / "no" / "python3.14")
    repo = _repo(tmp_path, targeted_test_python=missing)
    got = ti.resolve_targeted_test_python(repo)
    assert got.command == missing and got.defect
    clause = ti.interpreter_clause(repo)
    assert missing in clause and "interpreter-resolution-defect:" in clause and "tests_run: 0" in clause


def test_unresolvable_bare_pin_yields_defect(tmp_path, monkeypatch):
    monkeypatch.setattr(ti.shutil, "which", lambda name: None)
    got = ti.resolve_targeted_test_python(_repo(tmp_path, targeted_test_python="python9.99"))
    assert got.defect and "python9.99" in got.defect


def test_no_settings_file_and_none_root_are_portable(tmp_path):
    assert ti.interpreter_clause(tmp_path) == ti.PORTABLE_CLAUSE
    assert ti.interpreter_clause(None) == ti.PORTABLE_CLAUSE
    assert "falling back to `python`" in ti.PORTABLE_CLAUSE


def test_no_process_is_spawned(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    repo = _repo(tmp_path, targeted_test_python=sys.executable)
    ti.resolve_targeted_test_python(repo)
    ti.interpreter_clause(repo)
    ti.interpreter_clause(_repo(tmp_path, targeted_test_python=str(tmp_path / "nope")))


def test_launcher_key_outranks_python_pin_and_keeps_repo_args(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run.py").write_text("", encoding="utf-8")
    repo = _repo(tmp_path, targeted_test_launcher="scripts/run.py --tier targeted", targeted_test_python=sys.executable)
    clause = ti.interpreter_clause(repo)
    assert "`python scripts/run.py --tier targeted <test paths>`" in clause
    assert ti.DEFECT_PREFIX not in clause


def test_launcher_missing_or_absolute_is_a_resolution_defect(tmp_path):
    assert ti.DEFECT_PREFIX in ti.interpreter_clause(_repo(tmp_path, targeted_test_launcher="scripts/none.py"))
    assert ti.DEFECT_PREFIX in ti.interpreter_clause(_repo(tmp_path, targeted_test_launcher=sys.executable))

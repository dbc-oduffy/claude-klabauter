"""Tests for coordinator_core.install.fleet_test_env; the subprocess runner and registry walk are faked."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.install import fleet_test_env as fte
from coordinator_core.install import git_perf_config as gpc

PYTEST_INI = "[pytest]\n"


def _proc(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def _repo(tmp_path: Path, name: str, *, pytest_cfg=True, venv=None) -> Path:
    root = tmp_path / name
    root.mkdir()
    if pytest_cfg:
        (root / "pytest.ini").write_text(PYTEST_INI, encoding="utf-8")
    if venv == "win":
        py = root / ".venv" / "Scripts" / "python.exe"
    elif venv == "posix":
        py = root / ".venv" / "bin" / "python"
    else:
        return root
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    return root


@pytest.fixture
def fake(monkeypatch):
    state = {"walk": None, "calls": [], "responder": lambda argv, cwd, timeout: _proc(0, "3 tests collected in 0.1s")}

    monkeypatch.setattr(gpc, "iter_fleet_worktrees", lambda bin_dir: state["walk"])

    def runner(argv, cwd, timeout):
        state["calls"].append((list(argv), Path(cwd), timeout))
        return state["responder"](argv, cwd, timeout)

    monkeypatch.setattr(fte, "_run", runner)

    def walk(*worktrees):
        state["walk"] = gpc.FleetWalkResult(ok=True, roots_count=len(worktrees), items=list(worktrees))

    state["set_walk"] = walk
    return state


def _by_name(items):
    return {i.name: i for i in items}


def test_missing_module_fails_naming_key_and_modules(tmp_path, fake):
    root = _repo(tmp_path, "r")
    fake["set_walk"](("worktree", "doe", str(root)))
    fake["responder"] = lambda *a: _proc(
        2, "E   ModuleNotFoundError: No module named 'yaml'\nE   ModuleNotFoundError: No module named 'foo.bar'\n"
    )
    (item,) = fte.fleet_test_env_items(tmp_path, "engine-py", install=False)
    assert not item.ok
    assert item.name == "fleet-test-env:doe"
    assert item.detail == "missing: yaml, foo.bar"
    assert item.line() == "FAIL [fleet-test-env:doe] missing: yaml, foo.bar"


def test_missing_module_detail_stays_under_100_chars_keeping_first_modules(tmp_path, fake):
    root = _repo(tmp_path, "r")
    fake["set_walk"](("worktree", "doe", str(root)))
    out = "\n".join(f"ModuleNotFoundError: No module named 'module_number_{i}'" for i in range(30))
    fake["responder"] = lambda *a: _proc(2, out)
    (item,) = fte.fleet_test_env_items(tmp_path, "py", install=False)
    assert len(item.detail) < 100
    assert item.detail.startswith("missing: module_number_0, module_number_1")


def test_collected_passes_with_count(tmp_path, fake):
    root = _repo(tmp_path, "r")
    fake["set_walk"](("worktree", "mk", str(root)))
    (item,) = fte.fleet_test_env_items(tmp_path, "py", install=False)
    assert item.ok and item.line() == "PASS [fleet-test-env:mk] collected 3"
    argv = fake["calls"][0][0]
    assert argv[:3] == ["py", "-m", "pytest"] and "--collect-only" in argv
    assert argv[argv.index("--confcutdir") + 1] == str(root.resolve())


def test_nonmodule_collection_error_fails_with_first_error_line(tmp_path, fake):
    root = _repo(tmp_path, "r")
    fake["set_walk"](("worktree", "mk", str(root)))
    fake["responder"] = lambda *a: _proc(2, "E   SyntaxError: invalid syntax\n")
    (item,) = fte.fleet_test_env_items(tmp_path, "py", install=False)
    assert not item.ok and "SyntaxError" in item.detail


def test_zero_collected_fails(tmp_path, fake):
    root = _repo(tmp_path, "r")
    fake["set_walk"](("worktree", "mk", str(root)))
    fake["responder"] = lambda *a: _proc(0, "no tests collected in 0.01s")
    (item,) = fte.fleet_test_env_items(tmp_path, "py", install=False)
    assert not item.ok


@pytest.mark.parametrize("reason", ["helpers_unavailable", "registry_error", "no_roots"])
def test_walk_not_ok_fails_once_naming_reason(tmp_path, fake, reason):
    fake["walk"] = gpc.FleetWalkResult(ok=False, reason=reason)
    (item,) = fte.fleet_test_env_items(tmp_path, "py", install=False)
    assert not item.ok and item.name == "fleet-test-env" and reason in item.detail
    assert fake["calls"] == []


def test_missing_and_error_items_fail_naming_key(tmp_path, fake):
    fake["set_walk"](("missing", "gone", str(tmp_path / "x")), ("error", "bad", str(tmp_path / "y"), "boom"))
    items = _by_name(fte.fleet_test_env_items(tmp_path, "py", install=False))
    assert not items["fleet-test-env:gone"].ok
    assert not items["fleet-test-env:bad"].ok and "boom" in items["fleet-test-env:bad"].detail


def test_timeout_fails_never_ran(tmp_path, fake):
    root = _repo(tmp_path, "r")
    fake["set_walk"](("worktree", "mk", str(root)))

    def boom(argv, cwd, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    fake["responder"] = boom
    (item,) = fte.fleet_test_env_items(tmp_path, "py", install=False)
    assert not item.ok and item.detail.startswith("never ran:")


def test_absent_interpreter_fails_never_ran(tmp_path, fake):
    root = _repo(tmp_path, "r")
    fake["set_walk"](("worktree", "mk", str(root)))

    def boom(argv, cwd, timeout):
        raise FileNotFoundError("no python")

    fake["responder"] = boom
    (item,) = fte.fleet_test_env_items(tmp_path, "py", install=False)
    assert not item.ok and item.detail.startswith("never ran:")


def test_budget_exhausted_fails_never_ran_never_silent_skip(tmp_path, fake, monkeypatch):
    a, b = _repo(tmp_path, "a"), _repo(tmp_path, "b")
    fake["set_walk"](("worktree", "a", str(a)), ("worktree", "b", str(b)))
    clock = iter([0.0, 1.0, fte.FLEET_COLLECT_TOTAL_BUDGET_S + 1])
    monkeypatch.setattr(fte.time, "monotonic", lambda: next(clock))
    items = _by_name(fte.fleet_test_env_items(tmp_path, "py", install=False))
    assert items["fleet-test-env:a"].ok
    assert not items["fleet-test-env:b"].ok
    assert items["fleet-test-env:b"].detail == "never ran: fleet budget exhausted"
    assert len(fake["calls"]) == 1


def test_non_python_repo_passes_without_spawn(tmp_path, fake):
    root = _repo(tmp_path, "node", pytest_cfg=False)
    fake["set_walk"](("worktree", "node", str(root)))
    (item,) = fte.fleet_test_env_items(tmp_path, "py", install=True)
    assert item.ok and item.detail == "no python test config"
    assert fake["calls"] == []


def test_conftest_only_repo_is_python_test_repo(tmp_path, fake):
    root = _repo(tmp_path, "c", pytest_cfg=False)
    (root / "conftest.py").write_text("", encoding="utf-8")
    fake["set_walk"](("worktree", "c", str(root)))
    (item,) = fte.fleet_test_env_items(tmp_path, "py", install=False)
    assert item.ok and item.detail == "collected 3"


@pytest.mark.parametrize(
    "fname,content",
    [
        ("pyproject.toml", "[tool.pytest.ini_options]\ntestpaths=['t']\n"),
        ("tox.ini", "[pytest]\n"),
        ("setup.cfg", "[tool:pytest]\n"),
    ],
)
def test_pytest_config_shapes_detected(tmp_path, fname, content):
    root = _repo(tmp_path, "r", pytest_cfg=False)
    (root / fname).write_text(content, encoding="utf-8")
    assert fte._is_python_test_repo(root)


def test_pyproject_without_pytest_table_is_not_python_test_repo(tmp_path):
    root = _repo(tmp_path, "r", pytest_cfg=False)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert not fte._is_python_test_repo(root)


def test_declared_test_deps_sources(tmp_path):
    root = _repo(tmp_path, "r")
    (root / "pyproject.toml").write_text(
        "[project]\nname='x'\n[project.optional-dependencies]\ntest=['pytest>=7','yaml']\ndocs=['sphinx']\n"
        "[dependency-groups]\ndev=['ruff', {include-group='test'}]\n",
        encoding="utf-8",
    )
    (root / "requirements-dev.txt").write_text("# c\n-r base.txt\nhypothesis  # inline\npytest>=7\n", encoding="utf-8")
    assert fte._declared_test_deps(root) == ["pytest>=7", "yaml", "ruff", "hypothesis"]


@pytest.mark.parametrize("layout", ["win", "posix"])
def test_venv_python_layouts_used_for_collect(tmp_path, fake, layout):
    root = _repo(tmp_path, "r", venv=layout)
    fake["set_walk"](("worktree", "mk", str(root)))
    (item,) = fte.fleet_test_env_items(tmp_path, "engine-py", install=False)
    assert item.ok
    assert Path(fake["calls"][0][0][0]) == fte._venv_python(root)
    assert fake["calls"][0][0][0] != "engine-py"


def test_install_runs_pip_in_venv_then_collects(tmp_path, fake):
    root = _repo(tmp_path, "r", venv="posix")
    (root / "requirements-dev.txt").write_text("yaml\n", encoding="utf-8")
    fake["set_walk"](("worktree", "mk", str(root)))
    (item,) = fte.fleet_test_env_items(tmp_path, "engine-py", install=True)
    assert item.ok
    pip, collect = fake["calls"][0][0], fake["calls"][1][0]
    assert pip[1:4] == ["-m", "pip", "install"] and pip[4:] == ["yaml"]
    assert "--break-system-packages" not in pip
    assert collect[2] == "pytest"


def test_install_failure_fails_naming_repo_and_skips_collect(tmp_path, fake):
    root = _repo(tmp_path, "r", venv="posix")
    (root / "requirements-dev.txt").write_text("nonesuch\n", encoding="utf-8")
    fake["set_walk"](("worktree", "mk", str(root)))
    fake["responder"] = lambda *a: _proc(1, "", "ERROR: No matching distribution found for nonesuch\n")
    (item,) = fte.fleet_test_env_items(tmp_path, "py", install=True)
    assert not item.ok and item.name == "fleet-test-env:mk"
    assert item.detail.startswith("dep install failed:")
    assert len(fake["calls"]) == 1


def test_install_with_no_venv_issues_no_pip_call(tmp_path, fake):
    root = _repo(tmp_path, "r")
    (root / "requirements-dev.txt").write_text("yaml\n", encoding="utf-8")
    fake["set_walk"](("worktree", "mk", str(root)))
    fte.fleet_test_env_items(tmp_path, "engine-py", install=True)
    assert all("pip" not in argv for argv, _, _ in fake["calls"])
    assert fake["calls"][0][0][0] == "engine-py"


def test_install_false_issues_no_pip_call(tmp_path, fake):
    root = _repo(tmp_path, "r", venv="win")
    (root / "requirements-dev.txt").write_text("yaml\n", encoding="utf-8")
    fake["set_walk"](("worktree", "mk", str(root)))
    fte.fleet_test_env_items(tmp_path, "py", install=False)
    assert all("pip" not in argv for argv, _, _ in fake["calls"])


def test_total_budget_constants_pinned():
    assert fte.FLEET_COLLECT_TIMEOUT_S == 45 and fte.FLEET_COLLECT_TOTAL_BUDGET_S == 240

"""targeted_run: selection, admission, the five outcomes and the record schema."""
from __future__ import annotations

import json
import subprocess
import sys

import jsonschema
import pytest

from coordinator_core.bug_blitz import targeted_run
from coordinator_core.bug_blitz.contract import EXIT_CODES, record_schema_path
from coordinator_core.bug_blitz.tests.test_runner_shapes_admitted import ADMITTED_HEADS
from coordinator_core.win_portability import no_console_creationflags


@pytest.fixture(autouse=True)
def _settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pkg").mkdir()
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (root / "tests" / "test_good.py").write_text("def test_ok():\n    assert True\n")
    (root / "tests" / "test_bad.py").write_text("def test_nope():\n    assert False\n")
    (root / "tests" / "test_widget.py").write_text("def test_w():\n    assert True\n")
    (root / "pkg" / "widget.py").write_text("X = 1\n")
    return root


def _row(repo, surface, row_id="row-1"):
    path = repo / f"{row_id}.yaml"
    path.write_text(f"id: {row_id}\nsurface: {surface}\n")
    return path


def _validate(rec):
    schema = json.loads(record_schema_path().read_text())
    jsonschema.validate(rec.to_json(), schema)


def test_table_matches_s0_admitted_heads():
    assert ADMITTED_HEADS["pytest"] == [sys.executable, "-m", "pytest"]
    assert sorted(targeted_run._VITEST_HEADS.values()) == sorted(
        v for k, v in ADMITTED_HEADS.items() if k.startswith("vitest")
    )
    assert list(targeted_run._JEST_HEADS.values()) == [ADMITTED_HEADS["jest-pnpm"]]


def test_select_unions_surface_touched_tests_and_covering_tests(repo):
    (repo / "tests" / "test_extra.py").write_text("")
    row = _row(repo, "tests/test_good.py")
    selected, dropped = targeted_run.select(
        repo, row, ["pkg/widget.py", "tests/test_extra.py", "pkg", str(repo / "tests" / "test_bad.py")]
    )
    assert selected == [
        "tests/test_bad.py",
        "tests/test_extra.py",
        "tests/test_good.py",
        "tests/test_widget.py",
    ]
    assert dropped == ["pkg"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_select_js_covering_tests_from_git_index(tmp_path):
    root = tmp_path / "js"
    (root / "src").mkdir(parents=True)
    (root / "src" / "foo.ts").write_text("")
    (root / "src" / "foo.test.ts").write_text("")
    (root / "src" / "bar.spec.tsx").write_text("")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, **no_console_creationflags())
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, **no_console_creationflags())
    row = _row(root, "src/bar.spec.tsx")
    selected, _ = targeted_run.select(root, row, ["src/foo.ts"])
    assert selected == ["src/bar.spec.tsx", "src/foo.test.ts"]


@pytest.mark.spawns_process
def test_passed_run_writes_valid_record_and_exit_zero(repo):
    row = _row(repo, "tests/test_good.py")
    record = repo / "out" / "row-1.tests.json"
    code = targeted_run.main(
        ["--repo", str(repo), "--row", str(row), "--touched", "pkg/widget.py", "--record", str(record)]
    )
    assert code == EXIT_CODES["passed"] == 0
    rec = json.loads(record.read_text())
    assert rec["outcome"] == "passed"
    assert rec["selected"] == ["tests/test_good.py", "tests/test_widget.py"]
    assert rec["argv"][-2:] == rec["selected"]
    _validate(targeted_run.run(repo, row, ["pkg/widget.py"]))


@pytest.mark.spawns_process
def test_failed_run_names_failing_ids(repo):
    row = _row(repo, "tests/test_bad.py")
    rec = targeted_run.run(repo, row)
    assert rec.outcome == "failed"
    assert rec.exit_code not in (0, None)
    assert any("test_nope" in i for i in rec.failing_ids)
    _validate(rec)


def test_empty_selection_spawns_nothing(repo, monkeypatch):
    spawned = []
    monkeypatch.setattr(targeted_run, "_spawn", lambda *a, **k: spawned.append(a) or (0, ""))
    rec = targeted_run.run(repo, _row(repo, "docs/readme.md"))
    assert rec.outcome == "nothing-to-run"
    assert rec.exit_code is None and rec.argv == ()
    assert spawned == []
    _validate(rec)


def test_refused_argv_starts_no_process(repo, monkeypatch):
    spawned = []
    monkeypatch.setattr(targeted_run, "_spawn", lambda *a, **k: spawned.append(a) or (0, ""))
    monkeypatch.setattr(targeted_run, "admit", lambda argv, root: False)
    rec = targeted_run.run(repo, _row(repo, "tests/test_good.py"))
    assert rec.outcome == "refused"
    assert rec.exit_code is None
    assert spawned == []
    _validate(rec)


def test_admit_refuses_a_suite_shaped_argv(repo):
    assert targeted_run.admit([sys.executable, "-m", "pytest", "tests/test_good.py"], repo)
    assert not targeted_run.admit([sys.executable, "-m", "pytest"], repo)
    assert not targeted_run.admit(["pnpm", "test"], repo)


def test_no_runner_when_package_manager_missing(tmp_path, monkeypatch):
    root = tmp_path / "js2"
    root.mkdir()
    (root / "a.test.ts").write_text("")
    (root / "package.json").write_text("{}")
    monkeypatch.setattr(targeted_run.shutil, "which", lambda name: None)
    rec = targeted_run.run(root, _row(root, "a.test.ts"))
    assert rec.outcome == "no-runner"
    assert rec.exit_code is None
    _validate(rec)


def test_runner_holds_a_tier_t_slot_during_the_run(repo, monkeypatch):
    from coordinator_core.testing import tier_t_slots

    seen = []
    monkeypatch.setattr(
        targeted_run, "_spawn", lambda *a, **k: seen.append(tier_t_slots.occupancy()[0]) or (0, "")
    )
    rec = targeted_run.run(repo, _row(repo, "tests/test_good.py"))
    assert rec.outcome == "passed"
    assert seen == [1]
    assert tier_t_slots.occupancy()[0] == 0

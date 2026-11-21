"""targeted_run: selection, admission, the five outcomes and the record schema."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import jsonschema
import pytest

from coordinator_core.bug_blitz import targeted_run
from coordinator_core.bug_blitz.contract import EXIT_CODES, record_schema_path
from coordinator_core.bug_blitz.tests.test_runner_shapes_admitted import ADMITTED_HEADS
from pathlib import Path

pytestmark = pytest.mark.complete


@pytest.fixture(autouse=True)
def _settings_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pkg").mkdir()
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (root / "tests" / "test_good.py").write_text("def test_ok():\n    assert True\n")
    (root / "tests" / "test_bad.py").write_text("def test_nope():\n    assert False\n")
    (root / "tests" / "test_widget.py").write_text("def test_w():\n    assert True\n")
    (root / "pkg" / "widget.py").write_text("X = 1\n")
    return root


def _row(repo, surface, row_id: str = "row-1"):
    path = repo / f"{row_id}.yaml"
    path.write_text(f"id: {row_id}\nsurface: {surface}\n")
    return path


def _validate(rec) -> None:
    schema = json.loads(record_schema_path().read_text())
    jsonschema.validate(rec.to_json(), schema)


def test_table_matches_s0_admitted_heads() -> None:
    assert ADMITTED_HEADS["pytest"] == [sys.executable, "-m", "pytest"]
    assert sorted(targeted_run._VITEST_HEADS.values()) == sorted(
        v for k, v in ADMITTED_HEADS.items() if k.startswith("vitest")
    )
    assert list(targeted_run._JEST_HEADS.values()) == [ADMITTED_HEADS["jest-pnpm"]]


def test_select_unions_surface_touched_tests_and_covering_tests(repo) -> None:
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
def test_passed_run_writes_valid_record_and_exit_zero(repo) -> None:
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
def test_failed_run_names_failing_ids(repo) -> None:
    row = _row(repo, "tests/test_bad.py")
    rec = targeted_run.run(repo, row)
    assert rec.outcome == "failed"
    assert rec.exit_code not in (0, None)
    assert any("test_nope" in i for i in rec.failing_ids)
    _validate(rec)


def test_empty_selection_spawns_nothing(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    spawned = []
    monkeypatch.setattr(targeted_run, "_spawn", lambda *a, **k: spawned.append(a) or (0, ""))
    rec = targeted_run.run(repo, _row(repo, "docs/readme.md"))
    assert rec.outcome == "nothing-to-run"
    assert rec.exit_code is None and rec.argv == ()
    assert spawned == []
    _validate(rec)


def test_refused_argv_starts_no_process(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    spawned = []
    monkeypatch.setattr(targeted_run, "_spawn", lambda *a, **k: spawned.append(a) or (0, ""))
    monkeypatch.setattr(targeted_run, "admit", lambda argv, root: False)
    rec = targeted_run.run(repo, _row(repo, "tests/test_good.py"))
    assert rec.outcome == "refused"
    assert rec.exit_code is None
    assert spawned == []
    _validate(rec)


def test_admit_refuses_a_suite_shaped_argv(repo) -> None:
    assert targeted_run.admit([sys.executable, "-m", "pytest", "tests/test_good.py"], repo)
    assert not targeted_run.admit([sys.executable, "-m", "pytest"], repo)
    assert not targeted_run.admit(["pnpm", "test"], repo)


def test_no_runner_when_package_manager_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "js2"
    root.mkdir()
    (root / "a.test.ts").write_text("")
    (root / "package.json").write_text("{}")
    monkeypatch.setattr(targeted_run.shutil, "which", lambda name: None)
    rec = targeted_run.run(root, _row(root, "a.test.ts"))
    assert rec.outcome == "no-runner"
    assert rec.exit_code is None
    _validate(rec)


def test_runner_holds_a_tier_t_slot_during_the_run(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.testing import tier_t_slots

    seen = []
    monkeypatch.setattr(
        targeted_run, "_spawn", lambda *a, **k: seen.append(tier_t_slots.occupancy()[0]) or (0, "")
    )
    rec = targeted_run.run(repo, _row(repo, "tests/test_good.py"))
    assert rec.outcome == "passed"
    assert seen == [1]
    assert tier_t_slots.occupancy()[0] == 0


def test_pytest_spawn_runs_in_capped_job_with_depth_sentinel_and_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.diagnostics import contained_run
    from coordinator_core.testing import spawn_depth

    seen = {"closed": 0}

    class _P:
        returncode = 0

        def communicate(self, timeout=None):
            return "ok", None

    def fake(argv, *, popen_kwargs=None, **kw):
        seen["argv"], seen["kw"] = list(argv), dict(popen_kwargs)

        def close() -> None:
            seen["closed"] += 1

        return _P(), close

    monkeypatch.setattr(contained_run, "spawn_in_capped_job", fake)
    rc, out = targeted_run._spawn([sys.executable, "-m", "pytest", "t.py"], tmp_path, 5)
    assert (rc, out) == (0, "ok")
    assert seen["kw"]["env"][spawn_depth.ENV_KEY] == str(spawn_depth.child_depth(os.environ))
    assert seen["closed"] >= 1


def test_pytest_spawn_timeout_closes_the_job_and_reports_124(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.diagnostics import contained_run

    closed = []

    class _P:
        returncode = None

        def __init__(self) -> None:
            self.n = 0

        def communicate(self, timeout=None):
            self.n += 1
            if self.n == 1:
                raise subprocess.TimeoutExpired("x", timeout, output="partial")
            return "", None

    monkeypatch.setattr(contained_run, "spawn_in_capped_job", lambda a, **k: (_P(), lambda: closed.append(1)))
    rc, out = targeted_run._spawn([sys.executable, "-m", "pytest", "t.py"], tmp_path, 1)
    assert rc == 124 and "[TIMEOUT]" in out and "partial" in out
    assert closed


def test_non_pytest_spawn_bypasses_the_capped_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.diagnostics import contained_run

    monkeypatch.setattr(
        contained_run, "spawn_in_capped_job", lambda *a, **k: pytest.fail("capped job used for non-pytest")
    )
    rc, out = targeted_run._spawn([sys.executable, "-c", "print('hi')"], tmp_path, 30)
    assert rc == 0 and "hi" in out


def test_head_for_uses_the_repo_pin_admitted_by_the_classifier_and_refuses_an_unresolved_pin(repo, tmp_path):
    assert targeted_run.head_for(repo, ["tests/test_good.py"]) == ("pytest", [sys.executable, "-m", "pytest"])
    pin = tmp_path / "bin" / "python3.13"
    pin.parent.mkdir(exist_ok=True)
    pin.write_text("")
    pin.chmod(0o755)
    local = repo / "coordinator.local.md"
    local.write_text(f'---\ntargeted_test_python: "{pin}"\n---\n', encoding="utf-8")
    _, head = targeted_run.head_for(repo, ["tests/test_good.py"])
    assert head == [str(pin), "-m", "pytest"]
    assert targeted_run.admit([*head, "tests/test_good.py"], repo)
    local.write_text('---\ntargeted_test_python: "/no/such/python-xyz"\n---\n', encoding="utf-8")
    assert targeted_run.head_for(repo, ["tests/test_good.py"]) is None

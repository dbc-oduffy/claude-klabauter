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
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.complete


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


def test_pytest_spawn_runs_in_capped_job_with_depth_sentinel_and_closes(tmp_path, monkeypatch):
    from coordinator_core.diagnostics import contained_run
    from coordinator_core.testing import spawn_depth

    seen = {"closed": 0}

    class _P:
        returncode = 0

        def communicate(self, timeout=None):
            return "ok", None

    def fake(argv, *, popen_kwargs=None, **kw):
        seen["argv"], seen["kw"] = list(argv), dict(popen_kwargs)

        def close():
            seen["closed"] += 1

        return _P(), close

    monkeypatch.setattr(contained_run, "spawn_in_capped_job", fake)
    rc, out = targeted_run._spawn([sys.executable, "-m", "pytest", "t.py"], tmp_path, 5)
    assert (rc, out) == (0, "ok")
    assert seen["kw"]["env"][spawn_depth.ENV_KEY] == str(spawn_depth.child_depth(os.environ))
    assert seen["closed"] >= 1


def test_pytest_spawn_timeout_closes_the_job_and_reports_124(tmp_path, monkeypatch):
    from coordinator_core.diagnostics import contained_run

    closed = []

    class _P:
        returncode = None

        def __init__(self):
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


def test_non_pytest_spawn_bypasses_the_capped_job(tmp_path, monkeypatch):
    from coordinator_core.diagnostics import contained_run

    monkeypatch.setattr(
        contained_run, "spawn_in_capped_job", lambda *a, **k: pytest.fail("capped job used for non-pytest")
    )
    rc, out = targeted_run._spawn([sys.executable, "-c", "print('hi')"], tmp_path, 30)
    assert rc == 0 and "hi" in out


def _ext_row(root):
    """A row yaml outside the repo so it is not itself foreign dirt."""
    path = root.parent / "row-ext.yaml"
    path.write_text("id: row-ext\nsurface: tests/test_dep.py\n")
    return path


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, check=True, capture_output=True, **no_console_creationflags(),
    )


@pytest.fixture
def git_repo(tmp_path):
    """Committed HEAD where tests/test_dep.py fails (VALUE = 0); a dirty pkg/dep.py would make it pass."""
    root = tmp_path / "grepo"
    (root / "tests").mkdir(parents=True)
    (root / "pkg").mkdir()
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (root / "pkg" / "dep.py").write_text("VALUE = 0\n")
    (root / "tests" / "test_dep.py").write_text(
        "from pkg.dep import VALUE\n\n\ndef test_value():\n    assert VALUE == 1\n"
    )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


@pytest.mark.spawns_process
def test_foreign_uncommitted_pass_reports_distinct_outcome_with_holder(git_repo, monkeypatch, tmp_path):
    from coordinator_core.session import scope

    monkeypatch.setattr(
        scope, "contested_by_live_peers", lambda paths, sid, cwd=None: {"pkg/dep.py": ["peer-sess-1"]}
    )
    (git_repo / "pkg" / "dep.py").write_text("VALUE = 1\n")
    row = _ext_row(git_repo)
    record = tmp_path / "rec.json"
    code = targeted_run.main(["--repo", str(git_repo), "--row", str(row), "--record", str(record)])
    assert code == EXIT_CODES["passed-on-foreign-uncommitted"] == 6
    rec = json.loads(record.read_text())
    assert rec["outcome"] == "passed-on-foreign-uncommitted"
    assert rec["foreign_holders"] == {"pkg/dep.py": ["peer-sess-1"]}
    assert any("test_value" in i for i in rec["failing_ids"])
    jsonschema.validate(rec, json.loads(record_schema_path().read_text()))


@pytest.mark.spawns_process
def test_own_edit_is_judged_not_treated_as_foreign(git_repo):
    (git_repo / "pkg" / "dep.py").write_text("VALUE = 1\n")
    rec = targeted_run.run(git_repo, _ext_row(git_repo), ["pkg/dep.py"])
    assert rec.outcome == "passed"
    assert rec.foreign_holders == ()


@pytest.mark.spawns_process
def test_failing_at_head_and_live_stays_failed_despite_foreign_dirt(git_repo):
    (git_repo / "notes.txt").write_text("unrelated foreign dirt\n")
    rec = targeted_run.run(git_repo, _ext_row(git_repo))
    assert rec.outcome == "failed"
    assert rec.foreign_holders == ()
    assert any("test_value" in i for i in rec.failing_ids)


@pytest.mark.spawns_process
def test_passing_at_head_with_foreign_dirt_is_passed(git_repo):
    (git_repo / "tests" / "test_dep.py").write_text("def test_value():\n    assert True\n")
    _git(git_repo, "commit", "-q", "-am", "fix")
    (git_repo / "notes.txt").write_text("unrelated foreign dirt\n")
    rec = targeted_run.run(git_repo, _ext_row(git_repo))
    assert rec.outcome == "passed"
    assert rec.foreign_holders == ()


@pytest.mark.spawns_process
def test_committed_head_row_owned_dirty_hunk_does_not_read_passed(git_repo):
    (git_repo / "pkg" / "dep.py").write_text("VALUE = 1\n")
    row = _ext_row(git_repo)
    own = targeted_run.run(git_repo, row, ["pkg/dep.py"])
    assert own.outcome == "passed"
    rec = targeted_run.run(git_repo, row, ["pkg/dep.py"], committed_head=True)
    assert rec.outcome == "failed"
    assert any("test_value" in i for i in rec.failing_ids)


@pytest.mark.spawns_process
def test_committed_head_cli_flag_reads_red_and_exit_code_is_failed(git_repo, tmp_path):
    (git_repo / "pkg" / "dep.py").write_text("VALUE = 1\n")
    record = tmp_path / "rec.json"
    code = targeted_run.main(
        ["--repo", str(git_repo), "--row", str(_ext_row(git_repo)), "--record", str(record),
         "--touched", "pkg/dep.py", "--committed-head"]
    )
    assert code == EXIT_CODES["failed"]
    assert json.loads(record.read_text())["outcome"] == "failed"


@pytest.mark.spawns_process
def test_committed_head_green_at_head_is_passed_with_dirty_tree(git_repo):
    (git_repo / "tests" / "test_dep.py").write_text("def test_value():\n    assert True\n")
    _git(git_repo, "commit", "-q", "-am", "fix")
    (git_repo / "pkg" / "dep.py").write_text("VALUE = 5\n")
    rec = targeted_run.run(git_repo, _ext_row(git_repo), ["pkg/dep.py"], committed_head=True)
    assert rec.outcome == "passed"


@pytest.mark.spawns_process
def test_committed_head_clean_tree_judged_in_place(git_repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("clean tree must not build a HEAD tree")

    monkeypatch.setattr(targeted_run, "head_tree", boom)
    rec = targeted_run.run(git_repo, _ext_row(git_repo), committed_head=True)
    assert rec.outcome == "failed"


@pytest.mark.spawns_process
def test_committed_head_never_falls_back_to_live_tree(git_repo, monkeypatch):
    (git_repo / "pkg" / "dep.py").write_text("VALUE = 1\n")

    def boom(*a, **k):
        raise RuntimeError("no head tree")

    monkeypatch.setattr(targeted_run, "head_tree", boom)
    rec = targeted_run.run(git_repo, _ext_row(git_repo), ["pkg/dep.py"], committed_head=True)
    assert rec.outcome == "no-runner"

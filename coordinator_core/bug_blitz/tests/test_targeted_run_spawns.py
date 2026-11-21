"""targeted_run: selection, admission, the five outcomes and the record schema."""
from __future__ import annotations

import json
import subprocess

import jsonschema
import pytest

from coordinator_core.bug_blitz import targeted_run
from coordinator_core.bug_blitz.contract import EXIT_CODES, record_schema_path
from coordinator_core.win_portability import no_console_creationflags
from pathlib import Path

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]


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


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_select_js_covering_tests_from_git_index(tmp_path: Path) -> None:
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


def _ext_row(root):
    """A row yaml outside the repo so it is not itself foreign dirt."""
    path = root.parent / "row-ext.yaml"
    path.write_text("id: row-ext\nsurface: tests/test_dep.py\n")
    return path


def _git(root, *args) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, check=True, capture_output=True, **no_console_creationflags(),
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
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
def test_foreign_uncommitted_pass_reports_distinct_outcome_with_holder(git_repo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
def test_own_edit_is_judged_not_treated_as_foreign(git_repo) -> None:
    (git_repo / "pkg" / "dep.py").write_text("VALUE = 1\n")
    rec = targeted_run.run(git_repo, _ext_row(git_repo), ["pkg/dep.py"])
    assert rec.outcome == "passed"
    assert rec.foreign_holders == ()


@pytest.mark.spawns_process
def test_failing_at_head_and_live_stays_failed_despite_foreign_dirt(git_repo) -> None:
    (git_repo / "notes.txt").write_text("unrelated foreign dirt\n")
    rec = targeted_run.run(git_repo, _ext_row(git_repo))
    assert rec.outcome == "failed"
    assert rec.foreign_holders == ()
    assert any("test_value" in i for i in rec.failing_ids)


@pytest.mark.spawns_process
def test_passing_at_head_with_foreign_dirt_is_passed(git_repo) -> None:
    (git_repo / "tests" / "test_dep.py").write_text("def test_value():\n    assert True\n")
    _git(git_repo, "commit", "-q", "-am", "fix")
    (git_repo / "notes.txt").write_text("unrelated foreign dirt\n")
    rec = targeted_run.run(git_repo, _ext_row(git_repo))
    assert rec.outcome == "passed"
    assert rec.foreign_holders == ()


@pytest.mark.spawns_process
def test_committed_head_row_owned_dirty_hunk_does_not_read_passed(git_repo) -> None:
    (git_repo / "pkg" / "dep.py").write_text("VALUE = 1\n")
    row = _ext_row(git_repo)
    own = targeted_run.run(git_repo, row, ["pkg/dep.py"])
    assert own.outcome == "passed"
    rec = targeted_run.run(git_repo, row, ["pkg/dep.py"], committed_head=True)
    assert rec.outcome == "failed"
    assert any("test_value" in i for i in rec.failing_ids)


@pytest.mark.spawns_process
def test_committed_head_cli_flag_reads_red_and_exit_code_is_failed(git_repo, tmp_path: Path) -> None:
    (git_repo / "pkg" / "dep.py").write_text("VALUE = 1\n")
    record = tmp_path / "rec.json"
    code = targeted_run.main(
        ["--repo", str(git_repo), "--row", str(_ext_row(git_repo)), "--record", str(record),
         "--touched", "pkg/dep.py", "--committed-head"]
    )
    assert code == EXIT_CODES["failed"]
    assert json.loads(record.read_text())["outcome"] == "failed"


@pytest.mark.spawns_process
def test_committed_head_green_at_head_is_passed_with_dirty_tree(git_repo) -> None:
    (git_repo / "tests" / "test_dep.py").write_text("def test_value():\n    assert True\n")
    _git(git_repo, "commit", "-q", "-am", "fix")
    (git_repo / "pkg" / "dep.py").write_text("VALUE = 5\n")
    rec = targeted_run.run(git_repo, _ext_row(git_repo), ["pkg/dep.py"], committed_head=True)
    assert rec.outcome == "passed"


@pytest.mark.spawns_process
def test_committed_head_clean_tree_judged_in_place(git_repo, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise AssertionError("clean tree must not build a HEAD tree")

    monkeypatch.setattr(targeted_run, "head_tree", boom)
    rec = targeted_run.run(git_repo, _ext_row(git_repo), committed_head=True)
    assert rec.outcome == "failed"


@pytest.mark.spawns_process
def test_committed_head_never_falls_back_to_live_tree(git_repo, monkeypatch: pytest.MonkeyPatch) -> None:
    (git_repo / "pkg" / "dep.py").write_text("VALUE = 1\n")

    def boom(*a, **k):
        raise RuntimeError("no head tree")

    monkeypatch.setattr(targeted_run, "head_tree", boom)
    rec = targeted_run.run(git_repo, _ext_row(git_repo), ["pkg/dep.py"], committed_head=True)
    assert rec.outcome == "no-runner"

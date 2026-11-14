"""The export pass of the land retest resolves the run repo's ``fast_test_cmd`` from an isolated-export root,
and an unresolved command names the root it read."""
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_export as le
from coordinator_core.ops.dispatch_emit import retest_targeted as rt

RUN_ID = "warp-x-20261113T000000Z-abcdef"
CMD = "python -m pytest -q"


@pytest.fixture(autouse=True)
def _no_env_cmd(monkeypatch):
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)


def _repo(tmp_path: Path, *, configured: bool) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    if configured:
        (repo / "coordinator.local.md").write_text(f'---\nfast_test_cmd: "{CMD}"\n---\n', encoding="utf-8")
    return repo


def _export(repo: Path) -> Path:
    export = le.run_export_root(repo, RUN_ID)
    export.mkdir(parents=True)
    (export / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    return export


def test_export_root_resolves_the_repos_fast_test_cmd(tmp_path):
    export = _export(_repo(tmp_path, configured=True))
    plan = rt._launch_plan(export, ["test_a.py"])
    assert not isinstance(plan, str), plan
    assert "pytest" in plan[0]


def test_export_pass_runs_the_resolved_command_in_the_export(tmp_path):
    export = _export(_repo(tmp_path, configured=True))
    seen = []

    def run(argv, cwd, **_kw):
        seen.append((argv, Path(cwd)))
        return 0

    status, reason, codes, _used = rt._run_pass(run, export, ["test_a.py"], tmp_path / "pass.log", 1e12)
    assert (status, codes) == ("pass", [0]), reason
    assert seen[0][1] == export and "pytest" in seen[0][0]


def test_export_own_config_wins_over_the_repos(tmp_path):
    repo = _repo(tmp_path, configured=True)
    export = _export(repo)
    (export / "coordinator.local.md").write_text('---\nfast_test_cmd: "python -m pytest -x"\n---\n', encoding="utf-8")
    assert rt._config_root(export) == export
    assert "-x" in rt._launch_plan(export, ["test_a.py"])[0]


def test_unresolved_message_names_the_root_it_read(tmp_path):
    repo = _repo(tmp_path, configured=False)
    export = _export(repo)
    plan = rt._launch_plan(export, ["test_a.py"])
    assert isinstance(plan, str) and plan.startswith("fast_test_cmd unresolved")
    assert export.as_posix() in plan


def test_unconfigured_repo_message_names_the_repo_root(tmp_path):
    repo = _repo(tmp_path, configured=False)
    plan = rt._launch_plan(repo, ["test_a.py"])
    assert isinstance(plan, str) and repo.as_posix() in plan


def test_non_export_root_without_config_is_not_redirected(tmp_path):
    other = tmp_path / "scratch" / "elsewhere"
    other.mkdir(parents=True)
    (tmp_path / "coordinator.local.md").write_text(f'---\nfast_test_cmd: "{CMD}"\n---\n', encoding="utf-8")
    assert rt._config_root(other) == other

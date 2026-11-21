"""The reverify test leg runs in the run's repo, never the engine clone its bound command starts in."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops import test_leg_run as tlr
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt

TARGET = "tests/test_only_in_repo.py::test_passes"


@pytest.fixture
def two_roots(tmp_path, monkeypatch):
    """(engine, repo): the cwd is a git checkout without the target; the target lives only in `repo`."""
    engine, repo = tmp_path / "engine", tmp_path / "repo"
    (engine / ".git").mkdir(parents=True)
    (repo / ".git").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repo / "tests" / "test_only_in_repo.py").write_text("def test_passes():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(engine)
    return engine, repo


def _main(argv: list[str], out: Path) -> tuple[int, dict]:
    code = tlr.main([*argv[: argv.index("--")], "--out", str(out), *argv[argv.index("--"):]])
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_an_explicit_repo_root_collects_and_runs_targets_that_exist_only_there(two_roots, tmp_path):
    _engine, repo = two_roots
    code, data = _main(["--repo-root", str(repo), "--", TARGET], tmp_path / "o.json")
    assert code == tlr.EXIT_OK
    assert "missing_targets" not in data and "zero_run_cause" not in data
    assert (data["tests_run"], data["status"]) == (1, "pass")


def test_without_a_repo_root_the_cwd_checkout_is_the_leg_root_and_the_target_is_absent(two_roots, tmp_path):
    _code, data = _main(["--", TARGET], tmp_path / "o.json")
    assert data["missing_targets"] == [TARGET]
    assert data["zero_run_cause"] == ltt.ZERO_RUN_MISSING_TARGET


def test_a_repo_root_that_is_not_a_checkout_is_refused_not_replaced_by_the_cwd(two_roots, tmp_path):
    engine, _repo = two_roots
    (engine / "tests").mkdir()
    (engine / "tests" / "test_only_in_repo.py").write_text("def test_passes():\n    assert True\n", encoding="utf-8")
    code, data = _main(["--repo-root", str(tmp_path / "nowhere"), "--", TARGET], tmp_path / "o.json")
    assert code == tlr.EXIT_USAGE
    assert data["zero_run_cause"] == tlr.ZERO_RUN_NO_REPO
    assert data["tests_run"] == 0 and data["status"] == "error"


def test_the_review_reverify_test_leg_clause_pins_the_runs_repo(tmp_path):
    from coordinator_core.ops.review_mint.execute_review import _test_leg_clause

    pinned = _test_leg_clause("a" * 40, "scratch/b.txt", "scratch/t.json", str(tmp_path))
    assert f'--repo-root "{tmp_path.as_posix()}"' in pinned
    assert "--repo-root" not in _test_leg_clause("a" * 40, "scratch/b.txt", "scratch/t.json")


def test_the_halt_reverify_leg_pins_the_runs_repo(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_compose

    prompts: dict[str, str] = {}

    def capture(prompt, *, label, **_kwargs):
        prompts[label] = prompt
        return "null"

    monkeypatch.setattr(ask_compose, "_agent", capture)
    ask_compose._review_recover_js(
        head="", anchor="", session_tail="", record_dir="scratch/warp/run-1",
        run_id="run-1", repo_root=str(tmp_path), run_base_sha="a" * 40,
    )
    assert f'--repo-root "{tmp_path.as_posix()}"' in prompts[ask_compose.HALT_REVERIFY_LABEL]


def test_the_verify_tests_leg_pins_the_runs_repo(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_compose

    prompts: list[str] = []
    monkeypatch.setattr(ask_compose, "_agent", lambda prompt, **_kw: prompts.append(prompt) or "null")
    ask_compose._verify_tests_call(
        head="h", anchor="a", test_js="''", run_id="run-1", repo_root=str(tmp_path), out_path="scratch/o.json",
        base_sha="a" * 40,
    )
    assert f'--repo-root "{tmp_path.as_posix()}"' in "".join(prompts)

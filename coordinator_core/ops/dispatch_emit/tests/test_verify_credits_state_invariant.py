"""A run landing state/* run artifacts whose verify passed lands without a hand retest."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import retest_targeted
from coordinator_core.ops.dispatch_emit.landing_retest import fresh_verdict_path
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.session.record_homes import home_dir

pytestmark = [pytest.mark.spawns_process]

RUN_ID = "ask-20261027T000000-abc123"
INVARIANT = "coordinator_core/session/tests/test_machinery_paths.py"
NODE = f"{INVARIANT}::test_every_tracked_state_first_segment_is_on_the_allowlist"
SIZING = "state/sizings/2026-10-27-fixture.yaml"
PRODUCT = "state/lessons/2026-10-27-fixture.md"
PARAMS = {"targeted_tests": {"status": "fail", "baseline": None}}
PARAMS_PASS = {"targeted_tests": {"status": "pass"}}


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def landing(tmp_path, monkeypatch):
    """A repo whose uncommitted landing is the run's sizing and its state/warp/<run> artifacts."""
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _write(root, INVARIANT, "def test_every_tracked_state_first_segment_is_on_the_allowlist():\n    pass\n")
    _git(root, "add", INVARIANT)
    _git(root, "commit", "-q", "-m", "init")
    run_dir = Path(home_dir(str(root), "warp")) / RUN_ID
    run_rel = run_dir.relative_to(root).as_posix()
    _write(root, SIZING, "title: fixture\n")
    _write(root, f"{run_rel}/run.json", json.dumps({"sizing_rel": SIZING}))
    _write(root, f"{run_rel}/run-stage.json", "{}")
    _write(root, PRODUCT, "note\n")
    paths = [SIZING, f"{run_rel}/run-stage.json"]
    task_output = tmp_path / "task.output"
    task_output.write_text(
        json.dumps({"next_action": {"params": {"run_id": RUN_ID, **PARAMS}}}), encoding="utf-8")
    return root, paths, task_output


def _verify_stage_retest(root: Path, task_output: Path) -> int:
    return retest_targeted.main(
        ["--from-task-output", str(task_output), "--tests", INVARIANT, "--repo-root", str(root), "--detached-child"],
        run=lambda argv, cwd, **_kw: 0,
    )


def _targeted_failures(findings) -> list:
    return [f for f in findings if f.reason == li.TARGETED_TESTS_FAILED]


def test_run_artifacts_alone_owe_no_invariant_and_no_retest(landing):
    root, paths, _ = landing
    assert li.landing_invariant_nodes(root, paths, RUN_ID) == []
    assert not fresh_verdict_path(root, RUN_ID).exists()
    findings, _check = li.landing_test_gate(root, PARAMS_PASS, RUN_ID, li.run_landing_paths(root, paths))
    assert _targeted_failures(findings) == []


def test_a_state_product_selects_the_state_invariant(landing):
    root, paths, _ = landing
    assert li.landing_invariant_nodes(root, [*paths, PRODUCT], RUN_ID) == [NODE]


def test_a_passed_verify_retest_credits_the_state_landing(landing):
    root, paths, task_output = landing
    paths = [*paths, PRODUCT]
    assert _verify_stage_retest(root, task_output) == 0
    record = li.fresh_targeted_verdict(root, RUN_ID)
    assert record is not None and record["status"] == "pass"
    assert NODE in record["tests"] or INVARIANT in record["tests"]
    findings, check = li.landing_test_gate(root, PARAMS, RUN_ID, li.run_landing_paths(root, paths))
    assert _targeted_failures(findings) == []
    assert check == "pass-on-retest"


def test_without_a_record_the_invariant_is_still_owed(landing):
    root, paths, _ = landing
    paths = [*paths, PRODUCT]
    assert not fresh_verdict_path(root, RUN_ID).exists()
    findings, check = li.landing_test_gate(root, PARAMS_PASS, RUN_ID, li.run_landing_paths(root, paths))
    assert [f.path for f in _targeted_failures(findings)] == [INVARIANT]
    assert check == "failed"


@pytest.mark.skip(reason="default-leg wiring (R1) withheld; carried by ask-20261027T172717-78e815")
def test_the_default_ask_verify_leg_launches_and_polls_the_retest():
    script = ask_compose.compose_ask_script(
        repo_root="REPO",
        prompt="Prove the landing credits its invariants",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda text: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )
    assert script.count("retest_targeted --from-task-output") == 1
    assert "retest_targeted --poll --from-task-output" in script

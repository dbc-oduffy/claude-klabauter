"""P-D replay: repo-wide invariant tests are selected for the diffs that change their measured set.

Witnesses: doe's every-session floor (first red dc548166dd) and review-findings pin (first red
b6f0694c43), and the blitz 20261024T235752Z base red test_machinery_paths (tracked state/ segments
apm-rulings, rulings, superseding-reviews off the allowlist). The doe tests are vendored as empty
fixture files; nothing reads another repo. Selection is asserted through retest_targeted.main.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import landing_targeted_tests
from coordinator_core.ops.dispatch_emit import landing_retest
from coordinator_core.ops.dispatch_emit import retest_targeted

pytestmark = pytest.mark.spawns_process

RUN_ID = "run-pd"
MACHINERY = "coordinator_core/session/tests/test_machinery_paths.py"
MACHINERY_NODE = MACHINERY + "::test_every_tracked_state_first_segment_is_on_the_allowlist"
RAW_WRITES = "coordinator_core/tests/test_raw_writes_have_a_disposition.py"
FLOOR = "coordinator/tests/test_every_session_floor.py"
PIN = "coordinator/tests/test_review_findings_pin.py"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    for rel in ("test_a.py", MACHINERY, RAW_WRITES, FLOOR, PIN):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("def test_x():\n    pass\n", encoding="utf-8")
    (root / "invariant-measured-paths.json").write_text(json.dumps({
        FLOOR: ["coordinator/skills/*", "coordinator/commands/*", "coordinator/hooks/*"],
        PIN: ["coordinator/agents/*", "coordinator/skills/*", "coordinator/docs/wiki/*"],
    }), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    return root


def _task_output(tmp_path: Path) -> Path:
    out = tmp_path / "task.output"
    out.write_text(json.dumps({"next_action": {"params": {"run_id": RUN_ID}}}), encoding="utf-8")
    return out


def _selected(repo: Path, tmp_path: Path, diff: list[str]) -> list[str]:
    seen: list[list[str]] = []
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py",
         "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: seen.append(argv) or 0,
        dirty=lambda root: diff,
    )
    assert rc == 0 and seen
    return seen[0]


@pytest.mark.parametrize(
    "diff, expected",
    [
        (["coordinator/skills/foo/SKILL.md"], FLOOR),
        (["coordinator/agents/reviewer.md"], PIN),
        (["state/apm-rulings/2026-10-24-x.md"], MACHINERY_NODE),
        (["state/superseding-reviews/x.md"], MACHINERY_NODE),
        (["coordinator_core/ops/dispatch_emit/stage_receipts.py"], RAW_WRITES + "::test_the_live_gate_is_green"),
    ],
)
def test_a_diff_selects_the_invariant_it_touches(repo, tmp_path, diff, expected):
    argv = _selected(repo, tmp_path, diff)
    assert expected in argv and "test_a.py" in argv


def test_a_hand_built_join_in_an_engine_module_selects_the_record_home_invariant():
    from coordinator_core.ops.dispatch_emit import invariant_measured_paths as imp

    node = imp.invariants_for_paths(["coordinator_core/ops/dispatch_emit/stage_receipts.py"])
    assert any("test_no_hand_built_record_home_joins" in n for n in node)
    assert imp.invariants_for_paths(["coordinator_core/ops/tests/test_other.py"]) == []


def test_an_unrelated_diff_selects_no_invariant(repo, tmp_path):
    argv = _selected(repo, tmp_path, ["docs/plans/x.md"])
    assert [a for a in argv if "test_machinery_paths" in a or "test_raw_writes" in a or "coordinator/tests" in a] == []


def test_a_test_file_edit_elsewhere_does_not_select_raw_writes(repo, tmp_path):
    argv = _selected(repo, tmp_path, ["coordinator_core/ops/tests/test_other.py"])
    assert not any("test_raw_writes" in a for a in argv)


def test_the_seed_names_no_foreign_invariant_and_a_repo_without_a_declaration_selects_none(repo, tmp_path):
    from coordinator_core.ops.dispatch_emit import invariant_measured_paths as imp

    assert FLOOR not in imp.SEED_MAP and PIN not in imp.SEED_MAP
    (repo / imp.DECLARATION_FILE).unlink()
    argv = _selected(repo, tmp_path, ["coordinator/skills/foo/SKILL.md"])
    assert FLOOR not in argv and PIN not in argv


def test_a_malformed_declaration_contributes_nothing(repo):
    from coordinator_core.ops.dispatch_emit import invariant_measured_paths as imp

    (repo / imp.DECLARATION_FILE).write_text('{"x.py": "not-a-list"', encoding="utf-8")
    assert imp.load_map(repo) == imp.SEED_MAP
    (repo / imp.DECLARATION_FILE).write_text('{"x.py": "not-a-list"}', encoding="utf-8")
    assert imp.load_map(repo) == imp.SEED_MAP


def test_an_invariant_whose_file_is_absent_is_not_selected(repo, tmp_path):
    (repo / PIN).unlink()
    argv = _selected(repo, tmp_path, ["coordinator/agents/reviewer.md"])
    assert PIN not in argv


def _write_verdict(repo: Path, tests: list[str], status: str = "pass") -> None:
    _git(repo, "add", "-A")
    record = {
        "run_id": RUN_ID, "head": _head(repo), "tests": tests,
        "covered": landing_retest.landing_fingerprint(repo, ["test_a.py"]), "status": status,
    }
    path = landing_retest.fresh_verdict_path(repo, RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def _head(repo: Path) -> str:
    from coordinator_core.git.git_state import head_sha

    return head_sha(repo)


def test_landing_refuses_a_touched_invariant_no_retest_ran(repo):
    _write_verdict(repo, ["test_a.py"])
    found = li.invariant_findings(repo, ["state/apm-rulings/x.md"], RUN_ID, {})
    assert [f.path for f in found] == [MACHINERY]
    assert found[0].reason == landing_targeted_tests.TARGETED_TESTS_FAILED


def test_landing_accepts_when_the_fresh_retest_ran_the_invariant(repo):
    _write_verdict(repo, ["test_a.py", MACHINERY_NODE])
    assert li.invariant_findings(repo, ["state/apm-rulings/x.md"], RUN_ID, {}) == []


def test_landing_gate_adds_the_finding_only_when_landed_paths_given(repo):
    _write_verdict(repo, ["test_a.py"])
    params = {"targeted_tests": {"status": "pass", "tests_failed": 0, "baseline": None}}
    assert li.landing_test_gate(repo, params, RUN_ID)[1] == "pass-on-retest"
    findings, check = li.landing_test_gate(repo, params, RUN_ID, ["state/apm-rulings/x.md"])
    assert check == "failed" and findings and findings[0].path == MACHINERY
    assert li.landing_test_gate(repo, params, RUN_ID, ["docs/plans/x.md"])[1] == "pass-on-retest"

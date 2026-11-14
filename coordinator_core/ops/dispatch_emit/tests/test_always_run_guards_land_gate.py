"""Land gate: always-run guards join the owed set and a red guard is judged by new touched-file hits."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit.landing_retest import fresh_verdict_path

pytestmark = [pytest.mark.spawns_process]

RUN_ID = "ask-20261113T000000-abc123"
GUARD_FILE = "tests/test_guard.py"
GUARD = f"{GUARD_FILE}::test_no_literal"
TOUCHED = "src/a.py"
PARAMS = {"targeted_tests": {"status": "fail", "baseline": None}}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _write(root, "coordinator.local.md", f'---\nalways_run_guards: ["{GUARD}"]\n---\n')
    _write(root, GUARD_FILE, "def test_no_literal():\n    pass\n")
    _write(root, TOUCHED, "x = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _record(root: Path, hits: dict | None, *, pre_existing: bool = True, red_id: str = GUARD) -> None:
    """A fresh fail retest record in the shape ``fresh_targeted_verdict`` reads; the guard is red as ``red_id``."""
    baseline = {
        "caused": [] if pre_existing else [red_id],
        "pre_existing": [red_id] if pre_existing else [],
        "foreign": [], "unverified": [],
        "evidence": [red_id] if pre_existing else [],
    }
    record = {
        "run_id": RUN_ID, "status": "fail", "head": _git(root, "rev-parse", "HEAD"), "covered": {},
        "tests": [], "baseline": baseline,
    }
    if hits is not None:
        record["guard_hits"] = {GUARD: hits}
    path = fresh_verdict_path(root, RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def _guard_findings(findings) -> list:
    return [f for f in findings if f.path == GUARD_FILE]


def test_a_guard_is_selected_when_unrelated_files_are_touched(repo):
    assert li.landing_invariant_nodes(repo, ["unrelated.txt"], None) == [GUARD]
    assert li.landing_invariant_nodes(repo, ["unrelated.txt"], RUN_ID) == [GUARD]


def test_a_misspelled_guard_key_is_a_named_finding_through_the_gate(repo):
    _write(repo, "coordinator.local.md", f'---\nalways_run_guard: ["{GUARD}"]\n---\n')
    findings, check = li.landing_test_gate(repo, PARAMS, RUN_ID, [TOUCHED])
    named = [f for f in findings if f.path == "coordinator.local.md"]
    assert [f.reason for f in named] == [li.INVARIANT_UNMEASURED]
    assert "'always_run_guard'" in named[0].detail
    assert check == "failed"


def test_an_unresolvable_guard_id_is_a_named_finding_through_the_gate(repo):
    _write(repo, "coordinator.local.md", f'---\nalways_run_guards: ["{GUARD}", "tests/test_gone.py::test_x"]\n---\n')
    findings, check = li.landing_test_gate(repo, PARAMS, RUN_ID, [TOUCHED])
    named = [f for f in findings if f.path == "coordinator.local.md"]
    assert [f.reason for f in named] == [li.INVARIANT_UNMEASURED]
    assert "'tests/test_gone.py::test_x'" in named[0].detail
    assert check == "failed"


def test_a_valid_declaration_adds_no_config_finding(repo):
    assert [f for f in li.invariant_findings(repo, [TOUCHED], None, PARAMS) if f.path == "coordinator.local.md"] == []


def test_no_landed_paths_select_no_guard(repo):
    assert li.landing_invariant_nodes(repo, [], None) == []


def test_a_guard_already_selected_by_glob_is_listed_once(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import invariant_measured_paths as imp

    monkeypatch.setattr(imp, "invariants_for_paths", lambda paths, root: [GUARD])
    assert li.landing_invariant_nodes(repo, [TOUCHED], None) == [GUARD]


def test_an_unrun_guard_is_a_finding_through_the_gate(repo):
    findings, check = li.landing_test_gate(repo, PARAMS, RUN_ID, ["unrelated.txt"])
    assert [f.reason for f in _guard_findings(findings)] == [li.TARGETED_TESTS_FAILED]
    assert check == "failed"


def test_a_new_hit_in_a_touched_file_is_refused_under_a_red_baseline(repo):
    _record(repo, {"tree": {TOUCHED: 3}, "head": {TOUCHED: 1}})
    findings, check = li.landing_test_gate(repo, PARAMS, RUN_ID, [TOUCHED])
    refused = _guard_findings(findings)
    assert [f.reason for f in refused] == [li.TARGETED_TESTS_FAILED]
    assert GUARD in refused[0].detail and TOUCHED in refused[0].detail
    assert check == "failed"


def test_a_baseline_only_red_passes(repo):
    _record(repo, {"tree": {TOUCHED: 2}, "head": {TOUCHED: 2}})
    foreign: dict = {}
    assert li.invariant_findings(repo, [TOUCHED], RUN_ID, PARAMS, foreign) == []
    assert GUARD in foreign
    findings, _check = li.landing_test_gate(repo, PARAMS, RUN_ID, [TOUCHED])
    assert _guard_findings(findings) == []


def test_a_parametrized_red_id_is_judged_by_hits_not_credited_foreign(repo):
    _record(repo, {"tree": {TOUCHED: 3}, "head": {TOUCHED: 1}}, red_id=f"{GUARD}[case]")
    found = li.invariant_findings(repo, [TOUCHED], RUN_ID, PARAMS)
    assert [f.reason for f in found] == [li.TARGETED_TESTS_FAILED]
    _record(repo, {"tree": {TOUCHED: 1}, "head": {TOUCHED: 1}}, red_id=f"{GUARD}[case]")
    assert li.invariant_findings(repo, [TOUCHED], RUN_ID, PARAMS) == []


def test_a_new_hit_in_an_untouched_file_does_not_refuse(repo):
    _record(repo, {"tree": {"src/other.py": 5}, "head": {}})
    assert li.invariant_findings(repo, [TOUCHED], RUN_ID, PARAMS) == []


def test_a_new_hit_is_refused_when_the_guard_is_red_only_on_the_tree(repo):
    _record(repo, {"tree": {TOUCHED: 1}, "head": {}}, pre_existing=False)
    found = li.invariant_findings(repo, [TOUCHED], RUN_ID, PARAMS)
    assert [f.reason for f in found] == [li.TARGETED_TESTS_FAILED]


def test_a_red_guard_without_measured_hits_is_unmeasured_never_credited(repo):
    _record(repo, None)
    foreign: dict = {}
    found = li.invariant_findings(repo, [TOUCHED], RUN_ID, PARAMS, foreign)
    assert [f.reason for f in found] == [li.INVARIANT_UNMEASURED]
    assert "re-run" in found[0].detail
    assert foreign == {}

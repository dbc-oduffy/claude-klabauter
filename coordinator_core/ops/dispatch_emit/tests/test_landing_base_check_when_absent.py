"""A red with no recorded baseline is measured at the run base by the isolated-export retest child, and
``landing_test_gate`` at default arguments attributes it from that record: red at base lands with a
``failing_at_base`` advisory, green at base still refuses ``targeted-tests-failed``. A real git repo (run
base commit, later HEAD) and real exports; only the pytest runner is a stub that reads the export's files."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import landing_retest, landing_targeted_tests as ltt, retest_targeted

pytestmark = pytest.mark.spawns_process

RUN_ID = "run-base-check"
RED_AT_BASE = "tests/test_red_at_base.py"
MADE_RED = "tests/test_made_red.py"
OWN = "src/own.py"
NODE_RED_AT_BASE = f"{RED_AT_BASE}::test_x"
NODE_MADE_RED = f"{MADE_RED}::test_x"


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return done.stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """``(root, base_sha)``: base commit has RED_AT_BASE red and MADE_RED green; HEAD flips MADE_RED red."""
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, RED_AT_BASE, "RED\n")
    _write(root, MADE_RED, "GREEN\n")
    _write(root, OWN, "v = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    _write(root, MADE_RED, "RED\n")
    _write(root, OWN, "v = 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "landing")
    _write(root, f"state/warp/{RUN_ID}/manifest.json", json.dumps({"review_declared_paths": [OWN]}))
    return root, base


def _stub_run(argv, cwd, *, log=None, timeout=None, append=False):
    """Red for each targeted file whose export copy says RED; collect-only lists one id per file."""
    cwd = Path(cwd)
    files = [a.split("::")[0] for a in argv if a.split("::")[0].endswith(".py") and (cwd / a.split("::")[0]).is_file()]
    if "--collect-only" in argv:
        out = "".join(f"{f}::test_x\n" for f in files)
        code = 0
    else:
        red = [f for f in files if "RED" in (cwd / f).read_text(encoding="utf-8")]
        out = "".join(f"FAILED {f}::test_x - AssertionError\n" for f in red)
        out += f"{len(files) - len(red)} passed, {len(red)} failed in 0.1s\n"
        code = 1 if red else 0
    with open(log, "ab" if append else "wb") as sink:
        sink.write(out.encode())
    return code


def _retest(root: Path, base: str, tests: list[str]) -> dict:
    params = {"run_id": RUN_ID, "run_base_sha": base, "targeted_test_paths": tests}
    task_output = root.parent / "task.output"
    task_output.write_text(json.dumps({"next_action": {"params": params}}), encoding="utf-8")
    retest_targeted.main(
        ["--from-task-output", str(task_output), "--repo-root", str(root), "--isolated-export", "--detached-child"],
        run=_stub_run, dirty=lambda r: [],
    )
    return json.loads(landing_retest.fresh_verdict_path(root, RUN_ID).read_text(encoding="utf-8"))


def _gate_params(base: str) -> dict:
    return {"run_base_sha": base, "targeted_tests": {"status": "fail", "tests_failed": 1, "baseline": None}}


def test_before_the_retest_an_unevidenced_red_refuses_and_names_the_retest(repo):
    root, base = repo

    findings, check = li.landing_test_gate(root, _gate_params(base), RUN_ID)

    assert check == "failed" and findings
    assert findings[0].reason == ltt.TARGETED_TESTS_FAILED
    assert ltt.retest_step(list(findings), _gate_params(base))


def test_red_at_the_run_base_lands_with_a_failing_at_base_advisory(repo):
    root, base = repo
    record = _retest(root, base, [RED_AT_BASE])

    assert record["baseline"]["base_sha"] == base
    assert record["baseline"]["foreign"] == [NODE_RED_AT_BASE] and record["baseline"]["caused"] == []

    findings, check = li.landing_test_gate(root, _gate_params(base), RUN_ID)

    assert not [f for f in findings if f.reason == ltt.TARGETED_TESTS_FAILED]
    assert check == "pre-existing-only"
    assert findings.foreign_evidence == {NODE_RED_AT_BASE: [li.FAILING_AT_BASE]}


def test_green_at_the_run_base_and_red_after_still_refuses(repo):
    root, base = repo
    record = _retest(root, base, [MADE_RED])

    assert record["baseline"]["base_sha"] == base
    assert record["baseline"]["caused"] == [NODE_MADE_RED] and record["baseline"]["foreign"] == []

    findings, check = li.landing_test_gate(root, _gate_params(base), RUN_ID)

    assert check == "failed"
    assert [f.reason for f in findings] == [ltt.TARGETED_TESTS_FAILED]
    assert NODE_MADE_RED in findings[0].detail
    assert not getattr(findings, "foreign_evidence", None)


def test_a_base_red_beside_a_caused_red_refuses_naming_only_the_caused(repo):
    root, base = repo
    record = _retest(root, base, [RED_AT_BASE, MADE_RED])

    assert record["baseline"]["foreign"] == [NODE_RED_AT_BASE]
    assert record["baseline"]["caused"] == [NODE_MADE_RED]

    findings, check = li.landing_test_gate(root, _gate_params(base), RUN_ID)

    assert check == "failed"
    assert any(NODE_MADE_RED in f.detail for f in findings)
    assert not any(NODE_RED_AT_BASE in f.detail for f in findings)

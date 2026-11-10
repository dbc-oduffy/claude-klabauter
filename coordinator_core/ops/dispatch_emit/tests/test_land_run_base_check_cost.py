"""The land_run targeted-tests leg reads the recorded base check: no export, tar extract or pytest on the
op path, under the 500ms process-time bar, with pre_existing versus caused attribution kept."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.ops.dispatch_emit import (
    landing_export,
    landing_integrity,
    landing_retest,
    landing_targeted_tests,
    retest_targeted,
)
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_landing_integrity import (  # noqa: F401
    _git,
    repo,
)
from coordinator_core.testing import baseline_export

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

NODE = "pkg/tests/test_raw_writes_have_a_disposition.py::test_the_live_gate_is_green"
RUN = "r1"
BAR_SECS = 0.5


def _record(repo, baseline: dict) -> dict:
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
    failed = {"status": "fail", "failed_tests": [NODE]}
    record = {
        "run_id": RUN, "head": _git(["rev-parse", "HEAD"], repo), "status": "fail",
        "tests": [NODE.split("::")[0]], "tests_run": 46, "tests_failed": 1,
        "covered": landing_retest.landing_fingerprint(repo, ["ok.py"]),
        "export": failed, "live": failed, "baseline": baseline,
    }
    path = landing_retest.fresh_verdict_path(repo, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return {"run_id": RUN}


def _baseline(head_red: list[str] | None) -> dict:
    base = {"method": "isolated-export", "caused": [NODE], "foreign": [], "evidence": []}
    if head_red is None:
        return base
    return {
        **base, landing_targeted_tests.HEAD_RED_KEY: head_red,
        landing_targeted_tests.HEAD_SHA_KEY: "0123456789abcdef",
    }


@pytest.fixture
def forbidden(monkeypatch):
    calls: list[str] = []

    def spy(name):
        def raiser(*_a, **_k):
            calls.append(name)
            raise AssertionError(f"{name} called on the land_run op path")
        return raiser

    monkeypatch.setattr(baseline_export, "export", spy("baseline_export.export"))
    monkeypatch.setattr(landing_export, "build_head_export", spy("landing_export.build_head_export"))
    monkeypatch.setattr(retest_targeted, "_run_pass", spy("retest_targeted._run_pass"))
    return calls


def _leg(repo, params):
    start = time.process_time()
    findings, check = landing_integrity.landing_test_gate(repo, params, RUN, ["ok.py"])
    return findings, check, time.process_time() - start


@pytest.mark.parametrize("head_red", [[NODE], [], None])
def test_the_leg_runs_no_export_or_pytest_and_fits_the_bar(repo, forbidden, head_red):
    params = _record(repo, _baseline(head_red))

    _, _, elapsed = _leg(repo, params)

    assert forbidden == []
    assert elapsed < BAR_SECS


def test_a_caused_id_red_at_head_is_pre_existing(repo, forbidden):
    findings, check, _ = _leg(repo, _record(repo, _baseline([NODE])))

    assert list(findings) == [] and check == "pre-existing-only"
    assert NODE in (findings.foreign_evidence or {})
    assert forbidden == []


def test_a_caused_id_green_at_head_stays_caused(repo, forbidden):
    findings, check, _ = _leg(repo, _record(repo, _baseline([])))

    assert check == "failed"
    assert [f.reason for f in findings] == [landing_targeted_tests.TARGETED_TESTS_FAILED]
    assert forbidden == []


def test_an_unmeasured_id_stays_caused_and_names_the_retest_remedy(repo, forbidden):
    params = _record(repo, _baseline(None))

    findings, check, _ = _leg(repo, params)

    assert check == "failed"
    assert landing_integrity.retest_remedy(params) in findings[0].detail
    assert forbidden == []

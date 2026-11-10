"""The landing test gate judges HEAD plus the run's landing blobs (the export pass), not the live
worktree: a peer's live-tree ratchet red leaves the gate clear, and a ratchet red in the export still
yields a ``targeted-tests-failed`` finding."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity, landing_retest, landing_targeted_tests
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_landing_integrity import (  # noqa: F401
    _git,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RATCHET = "pkg/tests/test_raw_writes_have_a_disposition.py::test_the_live_gate_is_green"
RUN = "r1"


def _dual_record(repo, *, export: dict, live: dict, baseline: dict | None = None) -> dict:
    """A fresh isolated-export retest record for run ``r1``: top-level status is the export's."""
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
    record = {
        "run_id": RUN, "head": _git(["rev-parse", "HEAD"], repo), "status": export["status"],
        "tests": [RATCHET.split("::")[0]], "tests_run": 46,
        "covered": landing_retest.landing_fingerprint(repo, ["ok.py"]),
        "export": export, "live": live,
        **({"tests_failed": len(export["failed_tests"]), "baseline": baseline} if baseline else {}),
    }
    path = landing_retest.fresh_verdict_path(repo, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return {"run_id": RUN}


def _caused(head_red: list[str] | None = (), head_sha: str = "0123456789abcdef") -> dict:
    """A baseline with RATCHET caused; ``head_red`` the ids the child measured red at HEAD, None for unmeasured."""
    base = {"method": "isolated-export", "caused": [RATCHET], "foreign": [], "evidence": []}
    if head_red is None:
        return base
    return {**base, landing_targeted_tests.HEAD_RED_KEY: list(head_red), landing_targeted_tests.HEAD_SHA_KEY: head_sha}


def test_a_peer_unregistered_raw_write_reding_only_the_live_tree_leaves_the_gate_clear(repo):
    params = _dual_record(
        repo, export={"status": "pass", "failed_tests": []}, live={"status": "fail", "failed_tests": [RATCHET]},
    )

    findings, check = landing_integrity.landing_test_gate(repo, params, RUN, ["ok.py"])

    assert list(findings) == [] and check == "pass-on-retest"


def test_a_ratchet_red_in_the_export_yields_a_finding_naming_the_node(repo):
    params = _dual_record(
        repo, export={"status": "fail", "failed_tests": [RATCHET]},
        live={"status": "fail", "failed_tests": [RATCHET]}, baseline=_caused(),
    )

    findings, check = landing_integrity.landing_test_gate(repo, params, RUN, ["ok.py"])

    assert check == "failed"
    assert [(f.reason, RATCHET in f.detail) for f in findings] == [(landing_targeted_tests.TARGETED_TESTS_FAILED, True)]


def _foreign(owed_cleared: list[str] | None = None) -> dict:
    base = {"method": "isolated-export", "caused": [], "foreign": [RATCHET], "evidence": []}
    return base if owed_cleared is None else {**base, landing_targeted_tests.OWED_CLEARED_KEY: owed_cleared}


def test_an_unevidenced_foreign_red_the_recorded_owed_check_cleared_leaves_the_gate_clear(repo):
    params = _dual_record(
        repo, export={"status": "fail", "failed_tests": [RATCHET]},
        live={"status": "fail", "failed_tests": [RATCHET]}, baseline=_foreign([RATCHET]),
    )

    findings, check = landing_integrity.landing_test_gate(repo, params, RUN, ["ok.py"])

    assert list(findings) == [] and check != "failed"


@pytest.mark.parametrize("owed_cleared", [None, []])
def test_an_unevidenced_foreign_red_the_recorded_owed_check_does_not_clear_still_refuses(repo, owed_cleared):
    params = _dual_record(
        repo, export={"status": "fail", "failed_tests": [RATCHET]},
        live={"status": "fail", "failed_tests": [RATCHET]}, baseline=_foreign(owed_cleared),
    )

    findings, check = landing_integrity.landing_test_gate(repo, params, RUN, ["ok.py"])

    assert check == "failed"
    assert [f.reason for f in findings] == [landing_targeted_tests.TARGETED_TESTS_FAILED]
    assert all("owed base check" in f.detail and landing_integrity.retest_remedy(params) in f.detail for f in findings)


def test_a_caused_red_also_red_at_the_base_is_attributed_pre_existing_and_leaves_the_gate_clear(repo):
    params = _dual_record(
        repo, export={"status": "fail", "failed_tests": [RATCHET]},
        live={"status": "fail", "failed_tests": [RATCHET]}, baseline=_caused([RATCHET]),
    )

    findings, check = landing_integrity.landing_test_gate(repo, params, RUN, ["ok.py"])

    assert list(findings) == [] and check == "pre-existing-only"
    assert RATCHET in (findings.foreign_evidence or {})


def test_a_caused_red_green_at_the_base_still_refuses_and_names_the_base_check(repo):
    params = _dual_record(
        repo, export={"status": "fail", "failed_tests": [RATCHET]},
        live={"status": "fail", "failed_tests": [RATCHET]}, baseline=_caused(),
    )

    findings, check = landing_integrity.landing_test_gate(repo, params, RUN, ["ok.py"])

    assert check == "failed"
    assert [f.reason for f in findings] == [landing_targeted_tests.TARGETED_TESTS_FAILED]
    assert "base check: ran 1 caused id(s) at HEAD 0123456789: 0 red there" in findings[0].detail


def test_an_unmeasured_caused_id_stays_caused_and_names_the_retest_remedy(repo):
    params = _dual_record(
        repo, export={"status": "fail", "failed_tests": [RATCHET]},
        live={"status": "fail", "failed_tests": [RATCHET]}, baseline=_caused(None),
    )

    findings, check = landing_integrity.landing_test_gate(repo, params, RUN, ["ok.py"])

    assert check == "failed"
    assert "base check not run at HEAD" in findings[0].detail
    assert landing_integrity.retest_remedy(params) in findings[0].detail


def test_a_dirty_registry_greening_the_live_tree_does_not_clear_an_export_red(repo):
    # The measured tree is HEAD plus the run's blobs; a registry hunk present only in the worktree is not in it.
    params = _dual_record(
        repo, export={"status": "fail", "failed_tests": [RATCHET]},
        live={"status": "pass", "failed_tests": []}, baseline=_caused(),
    )

    findings, check = landing_integrity.landing_test_gate(repo, params, RUN, ["ok.py"])

    assert check == "failed"
    assert [f.reason for f in findings] == [landing_targeted_tests.TARGETED_TESTS_FAILED]

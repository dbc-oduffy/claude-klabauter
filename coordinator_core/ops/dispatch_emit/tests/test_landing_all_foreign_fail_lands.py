"""A measured retest verdict reading ``fail`` whose ``baseline.caused`` is empty and whose every red is foreign
(an uncaused reason, credited in ``baseline.evidence``) lands; a non-empty ``caused`` or an uncredited red
does not."""
from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt

A, B = "test_a.py::t0", "test_b.py::t1"


def _verdict(**baseline) -> dict:
    return {"status": "fail", "tests_run": 4, "baseline": {"caused": [], "foreign": [], **baseline}}


@pytest.mark.parametrize("reason", [ltt.PEER_HELD_PATH, ltt.UNDECLARED_DIRT])
def test_fail_with_empty_caused_and_every_red_foreign_lands(reason):
    verdict = _verdict(foreign=[A, B], foreign_reason={A: reason, B: reason}, evidence=[A, B])

    assert ltt.failed_targeted_tests(verdict) == ([], "pre-existing-only")


def test_a_foreign_reason_alone_lands_without_evidence():
    verdict = _verdict(foreign=[A], foreign_reason={A: ltt.PEER_HELD_PATH})

    assert ltt.failed_targeted_tests(verdict) == ([], "pre-existing-only")


def test_the_landing_gate_lets_a_fail_with_empty_caused_and_foreign_reds_land(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import landing_integrity

    verdict = _verdict(foreign=[A], foreign_reason={A: ltt.PEER_HELD_PATH}, evidence=[A])
    monkeypatch.setattr(landing_integrity, "fresh_targeted_verdict", lambda root, run_id: verdict)

    findings, check = landing_integrity._landing_test_gate_body(tmp_path, {}, "run-x")
    assert check == "pre-existing-only" and not list(findings)


def test_a_non_empty_caused_still_refuses_beside_foreign_reds():
    verdict = _verdict(
        caused=[B], foreign=[A], foreign_reason={A: ltt.PEER_HELD_PATH}, evidence=[A],
    )

    findings, check = ltt.failed_targeted_tests(verdict)
    assert check == "failed" and [f.path for f in findings] == ["test_b.py"]


def test_a_foreign_red_with_no_uncaused_reason_and_no_evidence_does_not_read_pre_existing():
    verdict = _verdict(foreign=[A])

    assert ltt.failed_targeted_tests(verdict)[1] == ltt.UNKNOWN_ONLY


def test_a_fail_naming_no_red_at_all_still_refuses():
    findings, check = ltt.failed_targeted_tests(_verdict())

    assert check == "failed" and findings

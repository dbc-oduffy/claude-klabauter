"""A measured retest reading ``fail`` with empty ``baseline.caused`` whose every failing id the record itself names
``foreign`` lands even with no evidence or uncaused reason on the id and the owed base check measured; an
unevidenced ``unverified`` or ``pre_existing`` id beside it, or a ``caused`` id, still refuses."""
from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt

A, B, C = "pkg/tests/test_a.py::t0", "pkg/tests/test_b.py::t1", "pkg/tests/test_c.py::t2"


def _record(**baseline) -> dict:
    return {
        "status": "fail",
        "baseline": {"caused": [], "evidence": [], "head_red": [], "owed_cleared": [], **baseline},
    }


def _gate(tmp_path, monkeypatch, record):
    monkeypatch.setattr(landing_integrity, "fresh_targeted_verdict", lambda root, run_id: record)
    findings, check = landing_integrity._landing_test_gate_body(tmp_path, {}, "run-x")
    return list(findings), check


def test_every_red_foreign_with_a_measured_owed_check_lands(tmp_path, monkeypatch):
    findings, check = _gate(tmp_path, monkeypatch, _record(foreign=[A, B]))

    assert findings == [] and check == ltt.UNKNOWN_ONLY


def test_the_landing_entry_lands_the_same_record(tmp_path, monkeypatch):
    monkeypatch.setattr(landing_integrity, "fresh_targeted_verdict", lambda root, run_id: _record(foreign=[A]))

    findings, check = landing_integrity.landing_test_gate(tmp_path, {}, "run-x")

    assert list(findings) == [] and check == ltt.UNKNOWN_ONLY and findings.unknown_ids == [A]


@pytest.mark.parametrize("key", ["unverified", "pre_existing"])
def test_an_unevidenced_unverified_or_pre_existing_red_still_refuses(tmp_path, monkeypatch, key):
    findings, check = _gate(tmp_path, monkeypatch, _record(**{key: [A]}))

    assert check == "failed" and [f.path for f in findings] == ["pkg/tests/test_a.py"]


def test_a_foreign_red_does_not_credit_an_unverified_red_beside_it(tmp_path, monkeypatch):
    findings, check = _gate(tmp_path, monkeypatch, _record(foreign=[A], unverified=[B]))

    assert check == "failed" and [f.path for f in findings] == ["pkg/tests/test_b.py"]


def test_a_caused_id_still_refuses_beside_foreign_reds(tmp_path, monkeypatch):
    findings, check = _gate(tmp_path, monkeypatch, _record(foreign=[A], caused=[C]))

    assert check == "failed" and [f.path for f in findings] == ["pkg/tests/test_c.py"]


def test_the_unit_still_reads_a_bare_foreign_id_unknown(tmp_path):
    record = _record(foreign=[A])

    assert ltt.failed_targeted_tests(record) == ([], ltt.UNKNOWN_ONLY)

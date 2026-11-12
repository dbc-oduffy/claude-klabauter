"""``charged_status`` sets aside the evidenced ``foreign`` ids of a ``head-node-measure`` baseline and still
charges a caused, unverified, unevidenced or unmeasured red."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit import reverify_delivery as rd

RATCHET = "test_a.py::t0"
DIRT = "test_b.py::dirt"


def _tests(baseline: dict, *, method: str | None = None, **extra) -> dict:
    out = {"status": "fail", "baseline": {"method": "head-node-measure", **baseline}, **extra}
    if method:
        out["baseline_method"] = method
    return out


def test_head_red_foreign_evidenced_ids_clear():
    tests = _tests({"caused": [], "foreign": [RATCHET], "evidence": [RATCHET]}, failed=1)
    assert rd.charged_status(tests) == "pass"


def test_undeclared_dirt_foreign_clears():
    tests = _tests({"caused": [], "foreign": [DIRT], "evidence": [], "foreign_reason": {DIRT: "undeclared-dirt"}}, failed=1)
    assert rd.charged_status(tests) == "pass"


def test_a_caused_id_keeps_the_run_charged():
    tests = _tests({"caused": ["t::new"], "foreign": [RATCHET], "evidence": [RATCHET]}, failed=2)
    assert rd.charged_status(tests) == "fail"


def test_an_unverified_id_keeps_the_run_charged():
    tests = _tests({"caused": [], "unverified": ["t::u"], "foreign": [RATCHET], "evidence": [RATCHET]}, failed=2)
    assert rd.charged_status(tests) == "fail"


def test_unevidenced_foreign_id_stays_charged():
    assert rd.charged_status(_tests({"caused": [], "foreign": [RATCHET], "evidence": []}, failed=1)) == "fail"


def test_a_failing_id_outside_the_baseline_stays_charged():
    tests = _tests({"caused": [], "foreign": [RATCHET], "evidence": [RATCHET]}, failing=[RATCHET, "t::new"])
    assert rd.charged_status(tests) == "fail"


def test_foreign_without_the_node_measure_method_is_not_credited():
    tests = {"status": "fail", "baseline_method": "export", "failed": 1,
             "baseline": {"caused": [], "foreign": [RATCHET], "evidence": [RATCHET]}}
    assert rd.charged_status(tests) == "fail"

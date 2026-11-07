"""wiring_contract: one part per status, built-path, evidence classifier, evidence sha, absent key."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops import wiring_contract as wc
from coordinator_core.ops.dispatch_emit.run_truth_contract import STATUS_UNWIRED

H = "a" * 40
_REAL = "state/warp/run/receipts/r.json"


def _part(tmp_path, **over):
    (tmp_path / "x.py").write_text("")
    part = {
        "id": "p",
        "status": "wired",
        "built_path": "x.py",
        "runs_evidence": [wc.evidence_entry(_REAL, H, False)],
    }
    part.update(over)
    return part


def _one(tmp_path, **over):
    return wc.part_verdicts({"wiring": [_part(tmp_path, **over)]}, tmp_path, H)[0]


def test_wired_part_passes(tmp_path):
    fm = {"wiring": [_part(tmp_path)]}
    assert wc.part_verdicts(fm, tmp_path, H)[0]["status"] == "wired"
    assert wc.refusal(fm, tmp_path, H) is None


def test_not_built_and_blocked_verbatim(tmp_path):
    assert _one(tmp_path, status="not_built")["status"] == "not_built"
    v = _one(tmp_path, status="blocked", blocked_on="DoE schema")
    assert (v["status"], v["reason"]) == ("blocked", "DoE schema")


def test_missing_built_path_is_unwired(tmp_path):
    v = _one(tmp_path, built_path="nope.py")
    assert (v["status"], v["reason"]) == (STATUS_UNWIRED, "built-path-missing")
    assert _one(tmp_path, built_path="")["reason"] == "built-path-missing"


def test_empty_runs_evidence_is_stub_only(tmp_path):
    v = _one(tmp_path, runs_evidence=[])
    assert (v["status"], v["reason"]) == (STATUS_UNWIRED, "stub-only-evidence")


@pytest.mark.parametrize(
    "path",
    [
        "a/fixtures/r.json", "a/fixture/r.json", "a/stubs/r.json", "a/stub/r.json",
        "a/fakes/r.json", "a/fake/r.json", "a/mocks/r.json", "a/mock/r.json",
        "a/conftest.py", "a/stub_r.json", "a/fake_r.json", "a/mock_r.json",
        "a\\fixtures\\r.json",
    ],
)
def test_each_stub_pattern_is_stub_only(tmp_path, path):
    assert wc.is_stub_evidence(path)
    ev = [wc.evidence_entry(path, H, False)]
    assert _one(tmp_path, runs_evidence=ev)["reason"] == "stub-only-evidence"


def test_real_evidence_among_stubs_is_wired(tmp_path):
    ev = [
        wc.evidence_entry("a/fixtures/r.json", H, False),
        wc.evidence_entry("state/r.json", H, False),
    ]
    assert _one(tmp_path, runs_evidence=ev)["status"] == "wired"


@pytest.mark.parametrize("path", ["state/r.json", "a/fixturesx/r.json", "a/mocking.json"])
def test_non_stub_paths(path):
    assert not wc.is_stub_evidence(path)


def test_absent_wiring_key_is_not_refused(tmp_path):
    assert wc.refusal({}, tmp_path, H) is None
    assert wc.part_verdicts({}, tmp_path, H) == []


def test_non_list_wiring_is_malformed_refusal(tmp_path):
    assert wc.refusal({"wiring": "oops"}, tmp_path, H) == "wiring: malformed"
    assert wc.refusal({"wiring": {"id": "a"}}, tmp_path, H) == "wiring: malformed"


def test_refusal_sentence_names_each_bad_part(tmp_path):
    fm = {"wiring": [_part(tmp_path), _part(tmp_path, id="q", status="not_built")]}
    assert wc.refusal(fm, tmp_path, H) == "wiring: q not_built (declared)"


def test_malformed_part_does_not_crash(tmp_path):
    assert wc.refusal({"wiring": ["junk"]}, tmp_path, H).startswith("wiring: ? malformed")


def test_evidence_entry_shape():
    assert wc.evidence_entry("p", H, True) == {"path": "p", "sha": H, "dirty": True}


def test_evidence_from_a_different_sha_does_not_count(tmp_path):
    ev = [wc.evidence_entry(_REAL, "b" * 40, False)]
    v = _one(tmp_path, runs_evidence=ev)
    assert (v["status"], v["reason"]) == (STATUS_UNWIRED, "evidence-sha-mismatch")
    fm = {"wiring": [_part(tmp_path, runs_evidence=ev)]}
    assert "evidence-sha-mismatch" in wc.refusal(fm, tmp_path, H)


def test_dirty_evidence_does_not_count(tmp_path):
    ev = [wc.evidence_entry(_REAL, H, True)]
    assert _one(tmp_path, runs_evidence=ev)["reason"] == "evidence-dirty-tree"
    missing_flag = [{"path": _REAL, "sha": H}]
    assert _one(tmp_path, runs_evidence=missing_flag)["reason"] == "evidence-dirty-tree"


def test_bare_string_or_blank_sha_is_sha_missing(tmp_path):
    assert _one(tmp_path, runs_evidence=[_REAL])["reason"] == "evidence-sha-missing"
    blank = [wc.evidence_entry(_REAL, "", False)]
    assert _one(tmp_path, runs_evidence=blank)["reason"] == "evidence-sha-missing"


def test_off_route_with_reason_is_reported_never_refused(tmp_path):
    fm = {"wiring": [_part(tmp_path), {"id": "inj", "status": "off_route", "off_route_reason": "example-game-repo"}]}
    assert wc.refusal(fm, tmp_path, H) is None
    v = wc.part_verdicts(fm, tmp_path, H)[1]
    assert (v["id"], v["status"], v["reason"]) == ("inj", "off_route", "example-game-repo")


@pytest.mark.parametrize("why", [None, "", "   ", 3])
def test_off_route_without_reason_is_malformed_and_refuses(tmp_path, why):
    part = {"id": "inj", "status": "off_route"}
    if why is not None:
        part["off_route_reason"] = why
    fm = {"wiring": [part]}
    assert wc.part_verdicts(fm, tmp_path, H)[0]["status"] == "malformed"
    assert "wiring: inj malformed" in wc.refusal(fm, tmp_path, H)


def test_off_route_consumes_not_required_and_produces_do_not_feed(tmp_path):
    off = {"id": "inj", "status": "off_route", "off_route_reason": "r", "consumes": ["x"], "produces": ["y"]}
    assert wc.refusal({"wiring": [off]}, tmp_path, H) is None
    fm = {"wiring": [off, _part(tmp_path, id="B", consumes=["y"])]}
    msg = wc.refusal(fm, tmp_path, H)
    assert "B" in msg and "unfed-consumes" in msg and "y" in msg


def _coverage(tmp_path, spine, ids=("R1", "R2", "R3"), exclude=()):
    return wc.register_coverage_refusal(spine, [{"id": i} for i in ids], tmp_path, exclude)


def test_register_coverage_names_unclaimed_and_writes_absent(tmp_path):
    (tmp_path / "a.py").write_text("")
    spine = [
        {"id": "C1", "writes": ["a.py"], "register_rows": ["R1"]},
        {"id": "C2", "writes": ["gone.py"], "register_rows": ["R3"]},
    ]
    assert _coverage(tmp_path, spine) == (
        "register: R2 not_built (unclaimed); register: R3 not_built (writes-absent)"
    )


def test_register_coverage_passes_when_every_row_has_a_present_write(tmp_path):
    (tmp_path / "a.py").write_text("")
    spine = [{"id": "C1", "writes": ["gone.py", "a.py"], "register_rows": ["R1", "R2"]}]
    assert _coverage(tmp_path, spine, ids=("R1", "R2")) is None


def test_register_coverage_row_with_empty_writes_claims_nothing_built(tmp_path):
    spine = [{"id": "C1", "writes": [], "register_rows": ["R1"]}]
    assert _coverage(tmp_path, spine, ids=("R1",)) == "register: R1 not_built (writes-absent)"


def test_register_coverage_ruled_out_row_and_wont_do_claimant(tmp_path):
    (tmp_path / "a.py").write_text("")
    spine = [{"id": "C1", "writes": ["a.py"], "register_rows": ["R2"], "disposition": "wont_do"}]
    assert _coverage(tmp_path, spine, ids=("R2",), exclude=("R2",)) is None
    assert _coverage(tmp_path, spine, ids=("R2",)) == "register: R2 not_built (unclaimed)"


def test_one_good_entry_outweighs_a_stale_one(tmp_path):
    ev = [
        wc.evidence_entry(_REAL, "b" * 40, False),
        wc.evidence_entry(_REAL, H, False),
    ]
    assert _one(tmp_path, runs_evidence=ev)["status"] == "wired"


def _seam(tmp_path, *parts):
    return {"wiring": [_part(tmp_path, id=pid, **keys) for pid, keys in parts]}


def test_unfed_consumes_is_refused_naming_part_and_entry(tmp_path):
    fm = _seam(tmp_path, ("A", {"consumes": ["x"]}))
    msg = wc.refusal(fm, tmp_path, H)
    assert "unfed-consumes" in msg and "A" in msg and "x" in msg


def test_consumes_fed_by_another_part_is_accepted(tmp_path):
    fm = _seam(tmp_path, ("A", {"consumes": ["x"]}), ("B", {"produces": ["x"]}))
    assert wc.refusal(fm, tmp_path, H) is None


def test_part_cannot_feed_itself(tmp_path):
    fm = _seam(tmp_path, ("A", {"consumes": ["x"], "produces": ["x"]}))
    assert "unfed-consumes" in wc.refusal(fm, tmp_path, H)


def test_consumes_external_is_exempt(tmp_path):
    fm = _seam(tmp_path, ("A", {"consumes": ["x"], "consumes_external": ["x", "plan-9"]}))
    assert wc.refusal(fm, tmp_path, H) is None


def test_unconsumed_produces_is_reported_never_refused(tmp_path):
    fm = _seam(tmp_path, ("A", {"produces": ["out"]}))
    assert wc.refusal(fm, tmp_path, H) is None
    assert wc.unconsumed_produces(fm) == [("A", "out")]


def test_legacy_parts_without_seam_keys_unchanged(tmp_path):
    fm = _seam(tmp_path, ("A", {}), ("B", {}))
    assert wc.refusal(fm, tmp_path, H) is None
    assert wc.unfed_consumes(fm) == [] and wc.unconsumed_produces(fm) == []


def test_unfed_consumes_joins_other_part_refusals(tmp_path):
    fm = _seam(tmp_path, ("A", {"consumes": ["x"]}), ("B", {"status": "not_built"}))
    msg = wc.refusal(fm, tmp_path, H)
    assert "B not_built (declared)" in msg and "unfed-consumes" in msg


@pytest.mark.parametrize("key", ["consumes", "produces", "consumes_external"])
@pytest.mark.parametrize("value", ["x", {"a": 1}, [1]])
def test_non_list_seam_key_is_malformed(tmp_path, key, value):
    v = _one(tmp_path, **{key: value})
    assert v["status"] == "malformed" and key in v["reason"]
    assert wc.refusal({"wiring": [_part(tmp_path, **{key: value})]}, tmp_path, H)

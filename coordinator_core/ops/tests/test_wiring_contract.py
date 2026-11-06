"""wiring_contract: one part per status, built-path, evidence classifier, evidence sha, absent key."""

from __future__ import annotations

import pytest

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


def test_one_good_entry_outweighs_a_stale_one(tmp_path):
    ev = [
        wc.evidence_entry(_REAL, "b" * 40, False),
        wc.evidence_entry(_REAL, H, False),
    ]
    assert _one(tmp_path, runs_evidence=ev)["status"] == "wired"

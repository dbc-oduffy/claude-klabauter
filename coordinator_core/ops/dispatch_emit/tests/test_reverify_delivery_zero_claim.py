"""reverify-delivery record on a null verdict: zero-claim settled results persist, others refuse."""

import pytest

from coordinator_core.ops.dispatch_emit import reverify_delivery as rd
from coordinator_core.ops.dispatch_emit.verdict_supersession import latest_delivery_supersession

MET = {"status": "met", "observation": "ok"}
PASS = {"status": "pass", "run": 3, "failed": 0, "sidecar": None}


def _record(tmp_path, **kw):
    args = dict(
        repo_root=tmp_path, supersedes="runs/r.md", plan_id="pln-x", head_sha="abc1234",
        verdict=None, unbacked=[], criterion=MET, tests=PASS,
    )
    args.update(kw)
    return rd.record_delivery_verdict(**args)


def test_zero_claim_null_verdict_persists_a_record_mint_reads(tmp_path):
    rel = _record(tmp_path)
    assert (tmp_path / rel).is_file()
    block = latest_delivery_supersession(tmp_path, "runs/r.md")
    assert block is not None and block["verdict"] is None and block["unbacked"] == []


def test_null_verdict_with_claims_refuses(tmp_path):
    with pytest.raises(rd.ReverifyRefused, match="unbacked claims"):
        _record(tmp_path, unbacked=[{"claim": "c", "anchor": "a"}])


@pytest.mark.parametrize(
    "kw",
    [
        {"criterion": {"status": "not_met"}},
        {"criterion": None},
        {"tests": {"status": "fail"}},
        {"tests": None},
    ],
)
def test_null_verdict_unsettled_refuses_naming_remedy(tmp_path, kw):
    with pytest.raises(rd.ReverifyRefused, match="review-stamp rejudge"):
        _record(tmp_path, **kw)


def test_bad_verdict_still_refuses(tmp_path):
    with pytest.raises(rd.ReverifyRefused, match="neither PASS nor FAIL"):
        _record(tmp_path, verdict="MAYBE")

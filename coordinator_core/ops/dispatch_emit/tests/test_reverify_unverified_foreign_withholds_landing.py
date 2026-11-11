"""reverify_unverified_reds lists a fail record's foreign_unverified ids; it adds no landing refusal."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import checkpoint_reds, landing_targeted_tests as ltt, terminal_commit_refusals

_ID = "tests/test_pool_worker.py::test_worker_init_calls_the_binder_before_anything_else"
_ID2 = "tests/test_pool_worker.py::test_worker_init_lowers_priority_before_registry_preload"
_LANDED = "pkg/worker_init.py"


def _params(**record) -> dict:
    return {"inline_review": {ltt.REVERIFY_KEY: {"status_after": "fail", **record}}}


def test_a_fail_record_lists_its_foreign_unverified_ids_sorted_and_deduped():
    p = _params(failing=[_ID, _ID2], foreign=[_ID, _ID2], foreign_unverified=[_ID2, _ID, "./" + _ID])
    assert ltt.reverify_unverified_reds(p) == sorted([_ID, _ID2])


def test_an_id_also_in_pre_existing_is_not_listed():
    p = _params(failing=[_ID], foreign=[_ID], foreign_unverified=[_ID], pre_existing=[_ID])
    assert ltt.reverify_unverified_reds(p) == []


def test_foreign_alone_is_not_listed():
    assert ltt.reverify_unverified_reds(_params(failing=[_ID], foreign=[_ID])) == []


def test_a_non_fail_or_absent_record_lists_nothing():
    p = {"inline_review": {ltt.REVERIFY_KEY: {"status_after": "pass", "foreign_unverified": [_ID]}}}
    assert ltt.reverify_unverified_reds(p) == []
    assert ltt.reverify_unverified_reds({}) == []


def test_reverify_red_findings_still_adds_no_refusal_for_the_unverified_id(tmp_path):
    p = _params(failing=[_ID], foreign=[_ID], foreign_unverified=[_ID])
    assert ltt.reverify_red_findings(tmp_path, p, [_LANDED]) == []


def test_a_review_blocked_halt_lists_a_foreign_unverified_id_introduced_and_mints_a_fix_row(tmp_path):
    digest = {
        "run_id": "warp-unverified-halt",
        "halted_by": {"halted": "review_blocked", "blocker": "failing_test", "failing": [_ID]},
        "ledger": {"items": []},
        "review": {
            "wave": [],
            "prep": None,
            "delivery": None,
            "reverify": {
                "status_before": "fail", "status_after": "fail", "failing": [_ID], "pre_existing": [],
                "foreign": [_ID], "foreign_unverified": [_ID],
            },
        },
    }
    params = terminal_commit_refusals._review_halt_commit_params(digest, tmp_path)
    assert f"red: {_ID} (introduced)" in params["message"]
    assert [r["test_id"] for r in checkpoint_reds.open_introduced_fix_rows(tmp_path)] == [_ID]

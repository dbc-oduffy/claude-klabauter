"""review.reverify classes ids recorded in the run's owned_red.jsonl as owned_red, never reverted or unattributed."""

from __future__ import annotations

import json

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit.requirements_ledger import OWNED_RED_FILE, run_dir
from coordinator_core.ops.tests.test_review_reverify_base_measured import (  # noqa: F401
    BASE,
    FAILING,
    PREFIX,
    SESSION,
    SIDECARS,
    _call,
    _mod_bytes,
    world,
)

RUN = "run-owned-1"


def _record(root, ids):
    d = run_dir(root, RUN)
    d.mkdir(parents=True, exist_ok=True)
    (d / OWNED_RED_FILE).write_text(
        "".join(json.dumps({"nodeid": i, "owner_run_id": "other"}) + "\n" for i in ids), encoding="utf-8"
    )


def test_owned_id_is_owned_red_not_reverted_or_unattributed(world):
    _record(world, FAILING)
    before = _mod_bytes(world)
    out = _call(world, failing=FAILING, sidecars=SIDECARS, run_key=RUN)
    assert out["error"] is None
    assert out["owned_red"] == FAILING
    assert out["reverted"] == [] and out["unattributed"] == [] and out["pre_existing"] == []
    assert _mod_bytes(world) == before
    record = json.loads((world / out["record"]).read_text(encoding="utf-8"))
    assert record["owned_red"] == FAILING


def test_without_run_key_owned_file_is_ignored(world):
    _record(world, FAILING)
    out = _call(world, failing=FAILING, sidecars=SIDECARS)
    assert out["owned_red"] == []
    assert out["reverted"] != [] or out["unattributed"] != []


def test_unrecorded_id_still_handled_with_run_key(world):
    _record(world, ["tests/test_other.py::test_x"])
    out = _call(world, failing=FAILING, sidecars=SIDECARS, run_key=RUN)
    assert out["owned_red"] == []
    assert out["reverted"] != [] or out["unattributed"] != []


def test_owned_id_vouched_by_failing_before_is_not_unattributed(world):
    _record(world, FAILING)
    out = _call(world, failing=FAILING, failing_before=FAILING, sidecars=SIDECARS, run_key=RUN)
    assert out["owned_red"] == FAILING and out["unattributed"] == []


def test_explicit_base_id_stays_pre_existing(world):
    _record(world, FAILING)
    out = _call(world, failing=FAILING, failing_at_base=FAILING, sidecars=SIDECARS, run_key=RUN)
    assert out["pre_existing"] == FAILING and out["owned_red"] == []


def test_unsafe_run_key_refused(world):
    out = _call(world, failing=FAILING, sidecars=SIDECARS, run_key="../x")
    assert out["error"] and out["owned_red"] == []


def test_handler_threads_run_key(world):
    _record(world, FAILING)
    out = rr._handler(
        {"base": BASE, "slice_prefix": PREFIX, "session_id": SESSION, "failing": FAILING, "run_key": RUN}, world
    )
    assert out["owned_red"] == FAILING


def test_zero_ran_owned_id_blocks_whatever_its_file():
    from coordinator_core.ops import review_test_blocker as rtb

    # `src/mod.py` is the only own path: the slow test's file is not in it, yet it blocks.
    result = {
        "failing": ["tests/test_slow.py::test_owned", "tests/test_other.py::test_x"],
        "failing_detail": [
            {"node_id": "tests/test_slow.py::test_owned", "message": rtb.ZERO_RAN_MESSAGE},
            {"node_id": "tests/test_other.py::test_x", "message": "assert 0"},
        ],
    }
    blocked = rtb.own_diff_blockers(result, ["src/mod.py"])
    assert [b.node_id for b in blocked] == ["tests/test_slow.py::test_owned"]


def test_not_found_owned_id_still_never_blocks():
    from coordinator_core.ops import review_test_blocker as rtb

    result = {
        "failing": ["tests/test_slow.py::test_gone"],
        "failing_detail": [{"node_id": "tests/test_slow.py::test_gone", "message": "ERROR: not found: x"}],
    }
    assert rtb.own_diff_blockers(result, ["tests/test_slow.py"]) == []

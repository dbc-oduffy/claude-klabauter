"""
coordinator_core.workstream_complete.test_chain_partition_verdict_store —
unit tests for the persistence seam that carries the chain-terminal
brightline verdict from producer (`wsc-coverage-gate-runner.py::cmd_
brightline_gate`) to consumer (`workstream_complete.brief()`) without an EM
hand-typing it.

Spec backlink: cross-repo/inbox/2026-08-04-example-retrieval-repo-em-brightline-
partition-mandatory-does-not-halt.md, "mechanism 2".

Run scoped only:
  python -m pytest coordinator_core/workstream_complete/test_chain_partition_verdict_store.py -q
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.workstream_complete import chain_partition_verdict_store as store


def test_write_then_read_round_trips_the_verdict_verbatim(tmp_path):
    store.write_verdict_record(
        tmp_path,
        session_id="sid-1",
        verdict="PARTITION-MANDATORY",
        from_handoff="state/handoffs/x.md",
        git_range=None,
        basis="plan_oracle=4(...) tier=B",
        tier="B",
    )
    verdict = store.read_verdict_record(tmp_path, session_id="sid-1")
    assert verdict == "PARTITION-MANDATORY"


def test_write_creates_the_expected_directory_and_a_readable_json_file(tmp_path):
    path = store.write_verdict_record(
        tmp_path,
        session_id="sid-2",
        verdict="single-reviewer-ok",
        from_handoff="state/handoffs/y.md",
        git_range="a..b",
        basis="basis text",
        tier="none",
    )
    assert path.exists()
    assert path.parent == tmp_path / store.VERDICT_STORE_RELDIR
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["session_id"] == "sid-2"
    assert body["verdict"] == "single-reviewer-ok"
    assert body["from_handoff"] == "state/handoffs/y.md"
    assert body["git_range"] == "a..b"
    assert body["basis"] == "basis text"
    assert body["tier"] == "none"


def test_verdict_store_path_is_a_pure_function_of_repo_root_and_session_id(tmp_path):
    p1 = store.verdict_store_path(tmp_path, "same-sid")
    p2 = store.verdict_store_path(tmp_path, "same-sid")
    p3 = store.verdict_store_path(tmp_path, "different-sid")
    assert p1 == p2
    assert p1 != p3


def test_read_with_expected_from_handoff_matching_succeeds(tmp_path):
    store.write_verdict_record(
        tmp_path,
        session_id="sid-3",
        verdict="PARTITION-MANDATORY",
        from_handoff="state/handoffs/z.md",
        git_range=None,
        basis="",
        tier="B",
    )
    verdict = store.read_verdict_record(
        tmp_path, session_id="sid-3", expected_from_handoff="state/handoffs/z.md"
    )
    assert verdict == "PARTITION-MANDATORY"


def test_read_with_mismatched_from_handoff_returns_none(tmp_path):
    store.write_verdict_record(
        tmp_path,
        session_id="sid-4",
        verdict="PARTITION-MANDATORY",
        from_handoff="state/handoffs/z.md",
        git_range=None,
        basis="",
        tier="B",
    )
    verdict = store.read_verdict_record(
        tmp_path, session_id="sid-4", expected_from_handoff="state/handoffs/DIFFERENT.md"
    )
    assert verdict is None


def test_read_with_mismatched_session_id_returns_none(tmp_path):
    store.write_verdict_record(
        tmp_path,
        session_id="sid-real",
        verdict="PARTITION-MANDATORY",
        from_handoff="state/handoffs/z.md",
        git_range=None,
        basis="",
        tier="B",
    )
    # Same path is not even reachable for a different session id (path is
    # keyed by session id), but assert the read-side contract directly too.
    verdict = store.read_verdict_record(tmp_path, session_id="sid-imposter")
    assert verdict is None


def test_read_of_absent_record_returns_none(tmp_path):
    assert store.read_verdict_record(tmp_path, session_id="never-written") is None


def test_read_of_corrupt_json_returns_none_never_raises(tmp_path):
    path = store.verdict_store_path(tmp_path, "sid-corrupt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    assert store.read_verdict_record(tmp_path, session_id="sid-corrupt") is None


@pytest.mark.parametrize(
    "verdict_value",
    ["", "PARTITION-mandatory", "single-reviewer-OK", "maybe", 42, None],
)
def test_read_rejects_any_verdict_string_outside_the_known_two(tmp_path, verdict_value):
    path = store.verdict_store_path(tmp_path, "sid-unknown")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": "sid-unknown",
                "verdict": verdict_value,
                "from_handoff": "state/handoffs/x.md",
                "git_range": None,
                "basis": "",
                "tier": "B",
                "written_at": "2026-08-04T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    assert store.read_verdict_record(tmp_path, session_id="sid-unknown") is None


def test_read_of_a_non_dict_json_body_returns_none(tmp_path):
    path = store.verdict_store_path(tmp_path, "sid-list")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert store.read_verdict_record(tmp_path, session_id="sid-list") is None


def test_second_write_for_the_same_session_overwrites_in_place(tmp_path):
    store.write_verdict_record(
        tmp_path,
        session_id="sid-5",
        verdict="single-reviewer-ok",
        from_handoff="state/handoffs/x.md",
        git_range=None,
        basis="first",
        tier="none",
    )
    store.write_verdict_record(
        tmp_path,
        session_id="sid-5",
        verdict="PARTITION-MANDATORY",
        from_handoff="state/handoffs/x.md",
        git_range=None,
        basis="second",
        tier="B",
    )
    verdict = store.read_verdict_record(tmp_path, session_id="sid-5")
    assert verdict == "PARTITION-MANDATORY"
    # Overwrite-in-place, not a second file.
    matches = list((tmp_path / store.VERDICT_STORE_RELDIR).glob("*.json"))
    assert len(matches) == 1


def test_known_verdicts_matches_the_cross_repo_wire_contract_literals():
    assert store.KNOWN_VERDICTS == frozenset({"PARTITION-MANDATORY", "single-reviewer-ok"})

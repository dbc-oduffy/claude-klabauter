"""Pins the commit-halt pointer fields, the run-outcome record and the resumed-run key in ask_contract."""

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract as c


def _commit_halt(outcome="state/warp/r1/run-outcome.json"):
    return c.LedgerHalt("r1", "docs/plans/p.md", "state/sizings/s.yaml", (), halted="commit", outcome=outcome)


def test_default_pointer_bytes_unchanged():
    halt = c.LedgerHalt("r1", "docs/plans/p.md", "state/sizings/s.yaml", ("R1",))
    assert halt.to_json() == {
        "run_id": "r1",
        "plan": "docs/plans/p.md",
        "sizing": "state/sizings/s.yaml",
        "blocking": ["R1"],
    }


def test_commit_pointer_round_trip(tmp_path):
    path = tmp_path / "ledger-halt.json"
    c.write_ledger_halt(path, _commit_halt())
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["halted"] == "commit" and raw["outcome"] == "state/warp/r1/run-outcome.json"
    assert c.read_ledger_halt(path) == _commit_halt()


def test_legacy_pointer_reads_defaults(tmp_path):
    path = tmp_path / "h.json"
    path.write_text(
        json.dumps({"run_id": "r", "plan": "p.md", "sizing": "s.yaml", "blocking": []}), encoding="utf-8"
    )
    halt = c.read_ledger_halt(path)
    assert halt.halted == c.LEDGER_HALT_KIND and halt.outcome is None


@pytest.mark.parametrize("outcome", ["/etc/o.json", "../o.json", "a/../../o.json"])
def test_unsafe_outcome_reads_none(tmp_path, outcome):
    path = tmp_path / "h.json"
    path.write_text(json.dumps(_commit_halt(outcome).to_json()), encoding="utf-8")
    assert c.read_ledger_halt(path) is None


def test_mistyped_halted_or_outcome_reads_none(tmp_path):
    path = tmp_path / "h.json"
    base = {"run_id": "r", "plan": "p.md", "sizing": "s.yaml", "blocking": []}
    for extra in ({"halted": 3}, {"outcome": 3}):
        path.write_text(json.dumps({**base, **extra}), encoding="utf-8")
        assert c.read_ledger_halt(path) is None


def test_run_outcome_round_trip_filters_keys(tmp_path):
    outcome = {
        "inline_review": {"slices": 1},
        "incomplete_chunks": [],
        "landed_chunks": ["C1"],
        "ledger_verdict": None,
        "plan_path": "docs/plans/p.md",
        "stray": 1,
    }
    path = c.write_run_outcome(tmp_path, "run-1", outcome)
    assert path == c.record_dir(tmp_path, "run-1") / c.RUN_OUTCOME_FILE
    got = c.read_run_outcome(tmp_path, "run-1")
    assert got == {k: v for k, v in outcome.items() if k != "stray"}


def test_run_outcome_minimal(tmp_path):
    c.write_run_outcome(tmp_path, "run-1", {"inline_review": {}})
    assert c.read_run_outcome(tmp_path, "run-1") == {"inline_review": {}}


def test_write_run_outcome_refuses_missing_review_or_unsafe_id(tmp_path):
    with pytest.raises(ValueError):
        c.write_run_outcome(tmp_path, "run-1", {"landed_chunks": []})
    with pytest.raises(ValueError):
        c.write_run_outcome(tmp_path, "../x", {"inline_review": {}})


def test_read_run_outcome_none_cases(tmp_path):
    assert c.read_run_outcome(tmp_path, "absent") is None
    assert c.read_run_outcome(tmp_path, "../x") is None
    d = c.record_dir(tmp_path, "run-1")
    d.mkdir(parents=True)
    (d / c.RUN_OUTCOME_FILE).write_text("{bad", encoding="utf-8")
    assert c.read_run_outcome(tmp_path, "run-1") is None
    (d / c.RUN_OUTCOME_FILE).write_text(json.dumps({"landed_chunks": []}), encoding="utf-8")
    assert c.read_run_outcome(tmp_path, "run-1") is None


def test_resumed_run_id_key():
    assert c.RESUMED_RUN_ID_KEY == "resumed_run_id"

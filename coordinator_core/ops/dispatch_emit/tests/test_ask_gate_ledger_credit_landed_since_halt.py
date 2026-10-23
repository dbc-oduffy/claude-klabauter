"""ask_gate._resume_ledger credits a blocking item whose serving rows all landed after the halt."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_contract import (
    RESUME_LEDGER_CREDITED_FIELD,
    RESUME_LEDGER_REMAINING_FIELD,
    LedgerHalt,
)
from coordinator_core.ops.dispatch_emit.ask_gate import _resume_ledger

PLAN = "docs/plans/p.md"
SIZING = {
    "requirement_register": {
        "rows": [{"id": "R2", "status": "open"}, {"id": "R3", "status": "open"}]
    }
}
REMAINING = {"R2": {"remaining": ["step two"]}, "R3": {"remaining": ["step three"]}}


def _plan(tmp_path, rows):
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: {d}\n  writes:\n    - some/{i}.py\n  register_rows: [{rr}]\n"
        for i, d, rr in rows
    )
    path = tmp_path / PLAN
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nrun_id: f\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n")


def _halt(since=(), unlanded=()):
    return LedgerHalt(
        run_id="run-1", plan_rel=PLAN, sizing_rel="state/sizings/s.yaml", blocking=("R2", "R3"),
        halt_sha="a" * 40, remaining=REMAINING, unlanded=tuple(unlanded), landed_since_halt=frozenset(since),
    )


def test_item_whose_serving_row_landed_after_halt_is_credited(tmp_path):
    _plan(tmp_path, [("C7", "open", "R2"), ("C9", "open", "R3")])
    out = _resume_ledger(SIZING, _halt(since={"C7"}, unlanded={"C9"}), tmp_path)
    assert out[RESUME_LEDGER_CREDITED_FIELD] == ["R2"]
    assert out["blocking"] == ["R3"]
    assert set(out[RESUME_LEDGER_REMAINING_FIELD]) == {"R3"}


def test_row_landed_at_or_before_halt_is_not_credited(tmp_path):
    _plan(tmp_path, [("C7", "open", "R2"), ("C9", "open", "R3")])
    out = _resume_ledger(SIZING, _halt(since=(), unlanded={"C9"}), tmp_path)
    assert RESUME_LEDGER_CREDITED_FIELD not in out
    assert out["blocking"] == ["R2", "R3"]


def test_any_unlanded_serving_row_blocks_credit(tmp_path):
    _plan(tmp_path, [("C7", "open", "R2"), ("C8", "open", "R2"), ("C9", "open", "R3")])
    out = _resume_ledger(SIZING, _halt(since={"C7"}, unlanded={"C8", "C9"}), tmp_path)
    assert RESUME_LEDGER_CREDITED_FIELD not in out
    assert "R2" in out["blocking"]


def test_item_with_no_serving_row_is_not_credited(tmp_path):
    _plan(tmp_path, [("C7", "open", "R9")])
    out = _resume_ledger(SIZING, _halt(since={"C7"}), tmp_path)
    assert RESUME_LEDGER_CREDITED_FIELD not in out


def test_credit_emptying_blocking_leaves_nothing_to_dispatch(tmp_path):
    _plan(tmp_path, [("C7", "open", "R2"), ("C9", "open", "R3")])
    out = _resume_ledger(SIZING, _halt(since={"C7", "C9"}), tmp_path)
    assert out[RESUME_LEDGER_CREDITED_FIELD] == ["R2", "R3"]
    assert out["blocking"] == [] and out["unlanded"] == []


def test_credit_spawns_no_git(tmp_path, monkeypatch):
    from coordinator_core.git import run as run_mod

    calls = []
    monkeypatch.setattr(run_mod, "run_git", lambda *a, **k: calls.append(a))
    _plan(tmp_path, [("C7", "open", "R2")])
    _resume_ledger(SIZING, _halt(since={"C7"}), tmp_path)
    assert calls == []

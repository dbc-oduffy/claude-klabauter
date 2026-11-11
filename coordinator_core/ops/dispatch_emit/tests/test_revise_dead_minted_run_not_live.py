"""workflow.revise run_live: an unredeemed token with no proven runner or an abandoned halt is not live."""

from __future__ import annotations

import yaml

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import halted_run, run_token
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.warp_run_deliverable import ABANDONED_AT_MINTED

RUN = "ask-20261102T212555-a24ad1"
SIBLING = "warp-2026-11-02-x-20261102T224849-178ba8"
REL = "state/sizings/2026-11-02-dead-minted.yaml"


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sizing = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    return tmp_path


def _tokens(monkeypatch, *, own: bool, sibling: bool):
    monkeypatch.setattr(run_token, "is_live", lambda _r, run_id: own and run_id == RUN)
    records = [{"run_id": SIBLING, "sizing_rel": REL}] if sibling else []
    monkeypatch.setattr(run_token, "live_records", lambda _r: iter(records))


def _revise(root):
    return rl._handler({"run_id": RUN, "verbatim": "also cover Windows"}, repo_root=root)


def test_sibling_token_with_unproven_runner_is_not_live(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _tokens(monkeypatch, own=False, sibling=True)
    monkeypatch.setattr(warp_live, "runner_unproven", lambda _r, run_id: run_id == SIBLING)
    assert _revise(root)["run_live"] is False


def test_sibling_token_with_runner_not_disproven_stays_live(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _tokens(monkeypatch, own=False, sibling=True)
    monkeypatch.setattr(warp_live, "runner_unproven", lambda _r, _id: False)
    assert _revise(root)["run_live"] is True


def test_sibling_token_whose_run_is_abandoned_at_minted_is_not_live(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _tokens(monkeypatch, own=False, sibling=True)
    monkeypatch.setattr(warp_live, "runner_unproven", lambda _r, _id: False)
    halted_run.write_halt_marker(record_dir(root, SIBLING), halted=ABANDONED_AT_MINTED, sizing_rel=REL)
    assert _revise(root)["run_live"] is False


def test_sibling_token_whose_halt_reason_is_abandoned_is_not_live(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _tokens(monkeypatch, own=False, sibling=True)
    monkeypatch.setattr(warp_live, "runner_unproven", lambda _r, _id: False)
    halted_run.write_halt_marker(
        record_dir(root, SIBLING), halted="stalled", sizing_rel=REL, reason=ABANDONED_AT_MINTED
    )
    assert _revise(root)["run_live"] is False


def test_sibling_token_with_an_ordinary_halt_stays_live(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _tokens(monkeypatch, own=False, sibling=True)
    monkeypatch.setattr(warp_live, "runner_unproven", lambda _r, _id: False)
    halted_run.write_halt_marker(record_dir(root, SIBLING), halted="stalled", sizing_rel=REL)
    assert _revise(root)["run_live"] is True


def test_own_token_with_unproven_runner_is_not_live(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _tokens(monkeypatch, own=True, sibling=False)
    monkeypatch.setattr(warp_live, "runner_unproven", lambda _r, run_id: run_id == RUN)
    assert _revise(root)["run_live"] is False


def test_own_token_with_runner_not_disproven_stays_live(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _tokens(monkeypatch, own=True, sibling=False)
    monkeypatch.setattr(warp_live, "runner_unproven", lambda _r, _id: False)
    assert _revise(root)["run_live"] is True


def test_unproven_own_runner_note_never_says_finished(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _tokens(monkeypatch, own=True, sibling=False)
    monkeypatch.setattr(warp_live, "runner_unproven", lambda _r, run_id: run_id == RUN)
    out = _revise(root)
    assert out["run_live"] is False
    assert "run finished" not in out["note"]
    assert "runner unproven" in out["note"] and "will not be applied" in out["note"]


def test_unproven_own_runner_gets_a_recheck_next_never_a_refire(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _tokens(monkeypatch, own=True, sibling=False)
    monkeypatch.setattr(warp_live, "runner_unproven", lambda _r, run_id: run_id == RUN)
    out = _revise(root)
    assert out["next"]["kind"] == "recheck" and out["next"]["run_id"] == RUN
    assert "refire" not in out["note"].replace("no refire is offered", "")


def test_a_finished_run_still_gets_a_refire_next(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _tokens(monkeypatch, own=False, sibling=False)
    monkeypatch.setattr(warp_live, "runner_unproven", lambda _r, _id: True)
    assert _revise(root)["next"]["kind"] == "refire"


def test_run_with_no_token_still_reads_finished(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _tokens(monkeypatch, own=False, sibling=False)
    monkeypatch.setattr(warp_live, "runner_unproven", lambda _r, _id: True)
    out = _revise(root)
    assert out["run_live"] is False
    assert "run finished" in out["note"]
    assert "runner unproven" not in out["note"]


def test_a_read_failure_keeps_the_token_live(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _tokens(monkeypatch, own=True, sibling=False)

    def boom(*_a, **_k):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(warp_live, "runner_unproven", boom)
    assert _revise(root)["run_live"] is True


def test_a_real_own_token_past_the_grace_with_no_launch_is_not_live(tmp_path, monkeypatch):
    import json

    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)
    root = _repo(tmp_path)
    run_token.mint(root, RUN, "s.mjs", session_id="sess-real")
    assert _revise(root)["run_live"] is True
    token = run_token._record_path(root, RUN)
    record = json.loads(token.read_text(encoding="utf-8"))
    record["minted_at"] = record["minted_at"] - warp_live.MINTED_RUNNER_GRACE_SECONDS - 60
    token.write_text(json.dumps(record), encoding="utf-8")
    assert _revise(root)["run_live"] is False

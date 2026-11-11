"""Drain: mint once per spool_id, dedupe a run the engine already took, notice, retry, stale reclaim."""

from __future__ import annotations

import json
import os
import stat
import sys
import time

import pytest

from coordinator_core.ask_spool import drain, record


@pytest.fixture(autouse=True)
def _isolated_settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))


def _payload(tuid="tu1", sid="sess1"):
    return json.dumps(
        {"tool_name": "Workflow", "session_id": sid, "tool_use_id": tuid, "tool_input": {"name": "ask"}}
    ).encode()


def _spool(root, tuid="tu1", sid="sess1", spooled_at=None):
    spool_id, created = record.spool_ask(
        str(root), _payload(tuid, sid), session_id=sid, tool_use_id=tuid, reason="engine_unreachable", spooled_at=spooled_at
    )
    assert created
    return spool_id


def _write_submission(root, run_id, **fields):
    d = root / "scratch" / "warp" / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "submission.json").write_text(json.dumps({"run_id": run_id, **fields}), encoding="utf-8")


def _counts(**kw):
    return {"minted": 0, "skipped": 0, "failed": 0, "notified": 0, "coalesced": 0, "withdrawn": 0, **kw}


class Engine:
    """Stands in for submit_engine_ask.evaluate: mints a run and writes its submission record."""

    def __init__(self, root, refuse=False):
        self.root, self.calls, self.refuse = root, 0, refuse

    def __call__(self, payload):
        self.calls += 1
        if self.refuse:
            return {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "HandoverPending"}}
        run_id = f"ask-20261104T14{self.calls:04d}-{self.calls:06x}"
        _write_submission(self.root, run_id, session_id=payload["session_id"], tool_use_id=payload["tool_use_id"])
        return {
            "hookSpecificOutput": {
                "permissionDecision": "allow",
                "updatedInput": {"scriptPath": "x"},
                "permissionDecisionReason": f"Submitted to the engine as run {run_id}.",
            }
        }


def _notices(root, session="sess1"):
    path = root / ".coordinator-local" / "ask-spool" / "notices" / f"{session}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_drain_mints_one_run_per_entry_and_second_call_mints_zero(tmp_path):
    ids = [_spool(tmp_path, tuid=f"tu{n}") for n in range(3)]
    engine = Engine(tmp_path)
    first = drain.run(str(tmp_path), evaluate=engine)
    assert first == _counts(minted=3, notified=3)
    again = drain.run(str(tmp_path), evaluate=engine)
    assert again == _counts()
    assert engine.calls == 3
    for sid in ids:
        assert os.path.exists(record.entry_path(str(tmp_path), sid, record.MINTED_SUFFIX))
        assert not os.path.exists(record.entry_path(str(tmp_path), sid))


def test_notice_names_spool_id_and_run_id_for_the_submitting_session(tmp_path):
    sid = _spool(tmp_path)
    drain.run(str(tmp_path), evaluate=Engine(tmp_path))
    (notice,) = _notices(tmp_path)
    assert notice["spool_id"] == sid and notice["status"] == "minted"
    assert notice["run_id"].startswith("ask-")
    header, _ = record.read_entry(record.entry_path(str(tmp_path), sid, record.MINTED_SUFFIX))
    assert header["run_id"] == notice["run_id"]


def test_run_stamped_with_spool_id_is_found_not_reminted(tmp_path):
    sid = _spool(tmp_path)
    _write_submission(tmp_path, "ask-20261104T140000-aaaaaa", session_id="other", tool_use_id="zz", spool_id=sid)
    engine = Engine(tmp_path)
    counts = drain.run(str(tmp_path), evaluate=engine)
    assert counts["skipped"] == 1 and counts["minted"] == 0 and engine.calls == 0
    assert _notices(tmp_path)[0]["run_id"] == "ask-20261104T140000-aaaaaa"


def test_post_delivery_run_matches_on_session_and_tool_use_id(tmp_path):
    _spool(tmp_path, tuid="tuX")
    _write_submission(tmp_path, "ask-20261104T140001-bbbbbb", session_id="sess1", tool_use_id="tuX")
    engine = Engine(tmp_path)
    counts = drain.run(str(tmp_path), evaluate=engine)
    assert counts["skipped"] == 1 and engine.calls == 0


def test_a_peer_run_of_the_same_session_with_another_tool_use_id_does_not_block_the_mint(tmp_path):
    _spool(tmp_path, tuid="tuX")
    _write_submission(tmp_path, "ask-20261104T140002-cccccc", session_id="sess1", tool_use_id="tuOther")
    engine = Engine(tmp_path)
    counts = drain.run(str(tmp_path), evaluate=engine)
    assert counts["minted"] == 1 and engine.calls == 1


def test_stamp_lands_on_the_minted_runs_submission_record(tmp_path):
    sid = _spool(tmp_path)
    drain.run(str(tmp_path), evaluate=Engine(tmp_path))
    (notice,) = _notices(tmp_path)
    sub = tmp_path / "scratch" / "warp" / notice["run_id"] / "submission.json"
    assert json.loads(sub.read_text())["spool_id"] == sid


def test_drainer_killed_after_mint_then_rerun_yields_exactly_one_run(tmp_path):
    sid = _spool(tmp_path)
    engine = Engine(tmp_path)

    def killed_after_mint(payload):
        engine(payload)
        raise SystemExit("killed between mint and rename")

    with pytest.raises(SystemExit):
        drain.run(str(tmp_path), evaluate=killed_after_mint)
    assert os.path.exists(record.entry_path(str(tmp_path), sid, record.CLAIMED_SUFFIX))
    counts = drain.run(str(tmp_path), evaluate=engine, stale_after_s=0)
    assert counts["skipped"] == 1 and counts["minted"] == 0
    assert engine.calls == 1
    runs = [p for p in (tmp_path / "scratch" / "warp").iterdir()]
    assert len(runs) == 1


def test_drainer_killed_after_claim_is_reclaimed_and_mints_once(tmp_path):
    sid = _spool(tmp_path)
    assert record.claim(str(tmp_path), sid)
    engine = Engine(tmp_path)
    assert drain.run(str(tmp_path), evaluate=engine)["minted"] == 0  # a fresh claim is a live peer's
    old = time.time() - 3600
    os.utime(record.entry_path(str(tmp_path), sid, record.CLAIMED_SUFFIX), (old, old))
    assert drain.run(str(tmp_path), evaluate=engine)["minted"] == 1
    assert engine.calls == 1


def test_refused_mint_keeps_the_entry_pending_with_attempts_and_a_failure_notice(tmp_path):
    sid = _spool(tmp_path)
    counts = drain.run(str(tmp_path), evaluate=Engine(tmp_path, refuse=True))
    assert counts == _counts(failed=1, notified=1)
    path = record.entry_path(str(tmp_path), sid)
    header, payload = record.read_entry(path)
    assert header["attempts"] == 1 and "HandoverPending" in header["last_error"]
    assert payload == _payload()
    (notice,) = _notices(tmp_path)
    assert notice["status"] == "failed" and notice["attempts"] == 1
    again = drain.run(str(tmp_path), evaluate=Engine(tmp_path))
    assert again["minted"] == 1
    assert not os.path.exists(path)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_failed_entry_stays_0600(tmp_path):
    sid = _spool(tmp_path)
    drain.run(str(tmp_path), evaluate=Engine(tmp_path, refuse=True))
    assert stat.S_IMODE(os.stat(record.entry_path(str(tmp_path), sid)).st_mode) == 0o600


def test_malformed_entry_is_failed_not_deleted(tmp_path):
    spool = tmp_path / ".coordinator-local" / "ask-spool"
    spool.mkdir(parents=True)
    (spool / "deadbeefdeadbeef.json").write_bytes(b"not a header\n")
    counts = drain.run(str(tmp_path), evaluate=Engine(tmp_path))
    assert counts["failed"] == 1
    assert (spool / "deadbeefdeadbeef.json").exists()


def test_empty_or_absent_spool_drains_to_zero(tmp_path):
    assert drain.run(str(tmp_path), evaluate=Engine(tmp_path)) == _counts()


def test_unsafe_session_id_writes_no_notice_outside_the_notices_dir(tmp_path):
    sid, _ = record.spool_ask(
        str(tmp_path), _payload(sid="../evil"), session_id="../evil", tool_use_id="tu1", reason="r"
    )
    counts = drain.run(str(tmp_path), evaluate=Engine(tmp_path))
    assert counts["minted"] == 1 and counts["notified"] == 0
    assert not (tmp_path / ".coordinator-local" / "evil.jsonl").exists()


def test_run_on_an_empty_spool_drains_nothing(tmp_path):
    assert drain.run(str(tmp_path)) == _counts()


def test_run_executes_queued_remote_failover_and_leaves_counts_unchanged(tmp_path, monkeypatch):
    from coordinator_core.remote_engine import background

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
    seen = []
    monkeypatch.setattr(
        "coordinator_core.remote_engine.fallback.try_remote",
        lambda op, params: seen.append((op, params)) or {"served_by": "far", "reply": {"ok": 1}},
    )
    key = background.launch("session.peer_roster", {"a": 1})
    assert drain.run(str(tmp_path)) == _counts()
    assert seen == [("session.peer_roster", {"a": 1})]
    assert background.read_handle(key)["status"] == "done"


SIZING = "state/sizings/2026-11/s1.yaml"
PLAN = "docs/plans/2026-11/p1.md"


def _sizing_payload(tuid, sizing=SIZING, sid="sess1"):
    return json.dumps(
        {
            "tool_name": "Workflow",
            "session_id": sid,
            "tool_use_id": tuid,
            "tool_input": {"name": "ask", "args": {"sizing": sizing}},
        }
    ).encode()


def _spool_sizing(root, tuid, sizing=SIZING, sid="sess1"):
    spool_id, created = record.spool_ask(
        str(root), _sizing_payload(tuid, sizing, sid), session_id=sid, tool_use_id=tuid, reason="engine_unreachable"
    )
    assert created
    return spool_id


def _write_sizing(root, plan=PLAN, register=None):
    path = root / SIZING
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"schema": "sizing-object"}
    if plan:
        doc["plan"] = plan
    if register is not None:
        doc["requirement_register"] = {"rows": register}
    path.write_text(json.dumps(doc), encoding="utf-8")


def _write_plan(root, dispositions, rel=PLAN):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = "".join(f"- id: C{n}\n  title: t\n  disposition: {d}\n" for n, d in enumerate(dispositions, 1))
    path.write_text(f"# P\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8")


def _no_popen(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise AssertionError("the drain spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)


def test_two_entries_of_one_sizing_mint_once_and_the_second_coalesces(tmp_path):
    _write_sizing(tmp_path)
    _write_plan(tmp_path, ["open"])
    first = _spool_sizing(tmp_path, "tuA")
    second = _spool_sizing(tmp_path, "tuB")
    engine = Engine(tmp_path)
    counts = drain.run(str(tmp_path), evaluate=engine)
    assert engine.calls == 1
    assert counts["minted"] == 1 and counts["coalesced"] == 1 and counts["notified"] == 2
    a, _ = record.read_entry(record.entry_path(str(tmp_path), first, record.MINTED_SUFFIX))
    b, _ = record.read_entry(record.entry_path(str(tmp_path), second, record.MINTED_SUFFIX))
    assert b["coalesced_into"] == a["run_id"] == b["run_id"] and "coalesced_into" not in a
    statuses = sorted(n["status"] for n in _notices(tmp_path))
    assert statuses == ["coalesced", "minted"]
    sub = tmp_path / "scratch" / "warp" / a["run_id"] / "submission.json"
    assert json.loads(sub.read_text())["spool_id"] == a["spool_id"]


def test_entries_of_different_sizings_each_mint(tmp_path):
    _write_sizing(tmp_path, plan=None)
    other = "state/sizings/2026-11/s2.yaml"
    (tmp_path / other).write_text(json.dumps({"schema": "sizing-object"}), encoding="utf-8")
    _spool_sizing(tmp_path, "tuA")
    _spool_sizing(tmp_path, "tuB", sizing=other)
    engine = Engine(tmp_path)
    counts = drain.run(str(tmp_path), evaluate=engine)
    assert engine.calls == 2 and counts["minted"] == 2 and counts["coalesced"] == 0


def test_entry_of_a_sizing_an_earlier_drain_minted_coalesces_on_sizing_rel(tmp_path):
    _write_sizing(tmp_path, plan=None)
    _write_submission(tmp_path, "ask-20261104T150000-dddddd", session_id="other", tool_use_id="zz", sizing_rel=SIZING)
    sid = _spool_sizing(tmp_path, "tuA")
    engine = Engine(tmp_path)
    counts = drain.run(str(tmp_path), evaluate=engine)
    assert engine.calls == 0 and counts["coalesced"] == 1 and counts["minted"] == 0
    header, _ = record.read_entry(record.entry_path(str(tmp_path), sid, record.MINTED_SUFFIX))
    assert header["coalesced_into"] == "ask-20261104T150000-dddddd"
    sub = tmp_path / "scratch" / "warp" / "ask-20261104T150000-dddddd" / "submission.json"
    assert "spool_id" not in json.loads(sub.read_text())


def _live_refire(root, run_id, sizing=SIZING, live=True):
    """A refired run under an older run dir: its dir mtime is past the drain's lookback; a live token when `live`."""
    import subprocess

    from coordinator_core.ops.dispatch_emit import run_token

    subprocess.run(["git", "init", "-q", str(root)], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _write_submission(root, run_id, session_id="other", tool_use_id="zz", sizing_rel=sizing)
    old = time.time() - 6 * 3600
    os.utime(root / "scratch" / "warp" / run_id, (old, old))
    if live:
        run_token.mint(root, run_id, "scratch/warp/x.js", session_id="other", sizing_rel=sizing)


def test_spool_of_a_sizing_whose_refired_run_is_live_coalesces_with_no_second_launch(tmp_path):
    _write_sizing(tmp_path, plan=None)
    run_id = "ask-20261104T150000-eeeeee"
    _live_refire(tmp_path, run_id)
    sid = _spool_sizing(tmp_path, "tuA")
    engine = Engine(tmp_path)
    counts = drain.run(str(tmp_path), evaluate=engine)
    assert engine.calls == 0 and counts["coalesced"] == 1 and counts["minted"] == 0
    header, _ = record.read_entry(record.entry_path(str(tmp_path), sid, record.MINTED_SUFFIX))
    assert header["coalesced_into"] == run_id


def test_spool_of_a_sizing_whose_old_run_holds_no_live_token_still_mints(tmp_path):
    _write_sizing(tmp_path, plan=None)
    _live_refire(tmp_path, "ask-20261104T150000-eeeeee", live=False)
    _spool_sizing(tmp_path, "tuA")
    engine = Engine(tmp_path)
    counts = drain.run(str(tmp_path), evaluate=engine)
    assert engine.calls == 1 and counts["minted"] == 1 and counts["coalesced"] == 0


def test_bare_asks_do_not_coalesce(tmp_path):
    _spool(tmp_path, tuid="tuA")
    _spool(tmp_path, tuid="tuB")
    engine = Engine(tmp_path)
    assert drain.run(str(tmp_path), evaluate=engine)["minted"] == 2


def _spool_ask(root, tuid, text, sid="sess1"):
    payload = json.dumps(
        {"tool_name": "Workflow", "session_id": sid, "tool_use_id": tuid, "tool_input": {"name": "ask", "args": text}}
    ).encode()
    spool_id, created = record.spool_ask(
        str(root), payload, session_id=sid, tool_use_id=tuid, reason="engine_unreachable"
    )
    assert created
    return spool_id


def _token_of_ask(root, run_id, text, session="sess1", minted_at=None):
    """A live run token recording `text`'s normalized ask hash, as the submit hook's marker mint writes it."""
    import subprocess

    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.extant_work import normalized_ask_hash

    subprocess.run(["git", "init", "-q", str(root)], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    run_token.mint(
        root, run_id, f"scratch/warp/{run_id}.workflow.mjs", session_id=session, marker=True,
        ask_sha256=normalized_ask_hash(text),
    )
    if minted_at is not None:
        path = run_token._record_path(root, run_id)
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec["minted_at"] = minted_at
        path.write_text(json.dumps(rec, sort_keys=True), encoding="utf-8")


def test_bare_ask_whose_session_already_holds_the_same_normalized_ask_coalesces_with_no_mint(tmp_path):
    run_id = "ask-20261104T160000-ffffff"
    _token_of_ask(tmp_path, run_id, "Fix the  drain  bug")
    sid = _spool_ask(tmp_path, "tuRetry", "fix the drain BUG")
    engine = Engine(tmp_path)
    counts = drain.run(str(tmp_path), evaluate=engine)
    assert engine.calls == 0 and counts["coalesced"] == 1 and counts["minted"] == 0
    header, _ = record.read_entry(record.entry_path(str(tmp_path), sid, record.MINTED_SUFFIX))
    assert header["coalesced_into"] == run_id == header["run_id"]
    assert [n["status"] for n in _notices(tmp_path)] == ["coalesced"]


def test_bare_ask_of_another_session_or_another_text_or_outside_the_window_still_mints(tmp_path):
    _token_of_ask(tmp_path, "ask-20261104T160001-aaaaaa", "fix the drain bug", session="other")
    _token_of_ask(tmp_path, "ask-20261104T160002-bbbbbb", "a different ask")
    _token_of_ask(tmp_path, "ask-20261104T160003-cccccc", "stale ask", minted_at=time.time() - 3600)
    for rid in ("ask-20261104T160002-bbbbbb", "ask-20261104T160003-cccccc"):
        _write_submission(tmp_path, rid, session_id="sess1", tool_use_id="old-" + rid)
    _spool_ask(tmp_path, "tuA", "fix the drain bug")
    _spool_ask(tmp_path, "tuB", "fix the drain bug now")
    _spool_ask(tmp_path, "tuC", "stale ask")
    engine = Engine(tmp_path)
    counts = drain.run(str(tmp_path), evaluate=engine)
    assert engine.calls == 3 and counts["minted"] == 3 and counts["coalesced"] == 0


def _withdrawn(root, sid):
    header, _ = record.read_entry(record.entry_path(str(root), sid, record.WITHDRAWN_SUFFIX))
    return header


def test_absent_sizing_withdraws_with_a_notice_and_no_mint(tmp_path, monkeypatch):
    _no_popen(monkeypatch)
    sid = _spool_sizing(tmp_path, "tuA")
    engine = Engine(tmp_path)
    counts = drain.run(str(tmp_path), evaluate=engine)
    assert engine.calls == 0 and counts["withdrawn"] == 1 and counts["minted"] == 0 and counts["notified"] == 1
    assert _withdrawn(tmp_path, sid)["withdraw_reason"].startswith("stale: ")
    assert not os.path.exists(record.entry_path(str(tmp_path), sid, record.CLAIMED_SUFFIX))
    (n,) = _notices(tmp_path)
    assert n["status"] == "withdrawn" and n["reason"].startswith("stale: ")


def test_plan_edge_naming_an_absent_file_withdraws(tmp_path, monkeypatch):
    _no_popen(monkeypatch)
    _write_sizing(tmp_path)
    sid = _spool_sizing(tmp_path, "tuA")
    engine = Engine(tmp_path)
    assert drain.run(str(tmp_path), evaluate=engine)["withdrawn"] == 1
    assert engine.calls == 0 and "stale: plan" in _withdrawn(tmp_path, sid)["withdraw_reason"]


def test_all_closed_spine_withdraws(tmp_path, monkeypatch):
    _no_popen(monkeypatch)
    _write_sizing(tmp_path)
    _write_plan(tmp_path, ["coded", "wont_do"])
    sid = _spool_sizing(tmp_path, "tuA")
    engine = Engine(tmp_path)
    assert drain.run(str(tmp_path), evaluate=engine)["withdrawn"] == 1
    assert engine.calls == 0 and "every row closed" in _withdrawn(tmp_path, sid)["withdraw_reason"]


def test_open_row_sizing_without_plan_edge_and_resumable_cases_are_not_stale(tmp_path):
    _write_sizing(tmp_path)
    _write_plan(tmp_path, ["coded", "open"])
    _spool_sizing(tmp_path, "tuA")
    engine = Engine(tmp_path)
    assert drain.run(str(tmp_path), evaluate=engine)["minted"] == 1

    _write_sizing(tmp_path, plan=None)
    _spool_sizing(tmp_path, "tuB", sid="sess2")
    assert drain.run(str(tmp_path), evaluate=engine)["withdrawn"] == 0


def test_all_closed_spine_with_a_rulable_register_row_or_halt_pointer_still_resumes(tmp_path):
    _write_plan(tmp_path, ["coded"])
    _write_sizing(tmp_path, register=[{"id": "R1", "status": "open"}])
    _spool_sizing(tmp_path, "tuA")
    engine = Engine(tmp_path)
    assert drain.run(str(tmp_path), evaluate=engine)["minted"] == 1

    _write_sizing(tmp_path)
    from coordinator_core.ops.dispatch_emit import ask_contract

    halt = ask_contract.ledger_halt_path(tmp_path, PLAN)
    halt.parent.mkdir(parents=True)
    halt.write_text("{}", encoding="utf-8")
    _spool_sizing(tmp_path, "tuB", sid="sess2")
    assert drain.run(str(tmp_path), evaluate=engine)["withdrawn"] == 0


def test_run_key_entry_for_a_gone_run_withdraws(tmp_path):
    payload = json.dumps(
        {
            "tool_name": "Workflow",
            "session_id": "sess1",
            "tool_use_id": "tuA",
            "tool_input": {"name": "ask", "args": {"resume_run_id": "ask-20261104T150000-eeeeee"}},
        }
    ).encode()
    sid, _ = record.spool_ask(str(tmp_path), payload, session_id="sess1", tool_use_id="tuA", reason="r")
    engine = Engine(tmp_path)
    assert drain.run(str(tmp_path), evaluate=engine)["withdrawn"] == 1
    assert engine.calls == 0 and "is gone" in _withdrawn(tmp_path, sid)["withdraw_reason"]


def test_a_failover_drain_error_never_fails_the_spool_drain(tmp_path, monkeypatch):
    from coordinator_core.remote_engine import background

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(background, "drain_pending", boom)
    assert drain.run(str(tmp_path)) == _counts()

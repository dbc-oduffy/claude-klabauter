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
    assert first == {"minted": 3, "skipped": 0, "failed": 0, "notified": 3}
    again = drain.run(str(tmp_path), evaluate=engine)
    assert again == {"minted": 0, "skipped": 0, "failed": 0, "notified": 0}
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
    assert counts == {"minted": 0, "skipped": 0, "failed": 1, "notified": 1}
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
    assert drain.run(str(tmp_path), evaluate=Engine(tmp_path)) == {"minted": 0, "skipped": 0, "failed": 0, "notified": 0}


def test_unsafe_session_id_writes_no_notice_outside_the_notices_dir(tmp_path):
    sid, _ = record.spool_ask(
        str(tmp_path), _payload(sid="../evil"), session_id="../evil", tool_use_id="tu1", reason="r"
    )
    counts = drain.run(str(tmp_path), evaluate=Engine(tmp_path))
    assert counts["minted"] == 1 and counts["notified"] == 0
    assert not (tmp_path / ".coordinator-local" / "evil.jsonl").exists()


def test_run_on_an_empty_spool_drains_nothing(tmp_path):
    assert drain.run(str(tmp_path)) == {"minted": 0, "skipped": 0, "failed": 0, "notified": 0}


def test_run_executes_queued_remote_failover_and_leaves_counts_unchanged(tmp_path, monkeypatch):
    from coordinator_core.remote_engine import background

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
    seen = []
    monkeypatch.setattr(
        "coordinator_core.remote_engine.fallback.try_remote",
        lambda op, params: seen.append((op, params)) or {"served_by": "far", "reply": {"ok": 1}},
    )
    key = background.launch("session.peer_roster", {"a": 1})
    assert drain.run(str(tmp_path)) == {"minted": 0, "skipped": 0, "failed": 0, "notified": 0}
    assert seen == [("session.peer_roster", {"a": 1})]
    assert background.read_handle(key)["status"] == "done"


def test_a_failover_drain_error_never_fails_the_spool_drain(tmp_path, monkeypatch):
    from coordinator_core.remote_engine import background

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(background, "drain_pending", boom)
    assert drain.run(str(tmp_path)) == {"minted": 0, "skipped": 0, "failed": 0, "notified": 0}

"""unimatrix-send.py points at SendMessage when the router is unreachable."""
import importlib.util
import re
import tempfile
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("unimatrix_send_c3", Path(__file__).resolve().parents[1] / "unimatrix-send.py")
us = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(us)


_ENTRIES = [
    {"name": "mac-claude", "pid": 1, "sessionId": "s1"},
    {"name": "codex-x", "pid": 2, "sessionId": "peer-codex-x-2", "unimatrixVendor": "codex"},
    {"name": "dp-claude", "pid": 3, "sessionId": "s3", "unimatrixBox": "machine-b"},
]


def _rows(out):
    return [ln.split("\t") for ln in out.strip().split("\n")[1:-1]]


def test_list_names_a_far_box_by_member_host_never_an_ip_octet(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    entries = [{"name": "far-1", "pid": 4, "sessionId": "s4", "unimatrixBox": "192.168.1.20"},
               {"name": "far-2", "pid": 5, "sessionId": "s5", "unimatrixBox": "10.9.9.9"}]
    monkeypatch.setattr(us, "_session_entries", lambda: entries)
    monkeypatch.setattr(us, "_member_ip_hosts", lambda: {"192.168.1.20": "machine-b.local"})
    with tempfile.TemporaryDirectory() as d:
        assert us.main(["--list", "--endpoint-dir", d]) == 0
    rows = _rows(capsys.readouterr().out)
    assert rows[0][1] == "machine-b"
    assert rows[1][1] == "10.9.9.9"


def test_list_without_router_lists_sessions_dir_and_succeeds(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(us, "_session_entries", lambda: _ENTRIES)
    # An entry with no unimatrixBox takes the local box; pin it so a host named
    # "machine-b" cannot collide with the dp-claude fixture's explicit box.
    contract = us._sibling("unimatrix-peer-contract.py", "unimatrix_peer_contract_send")
    monkeypatch.setattr(contract, "local_box", lambda: "mac")
    monkeypatch.setattr(us, "_sibling", lambda f, m: contract)
    with tempfile.TemporaryDirectory() as d:
        rc = us.main(["--list", "--endpoint-dir", d])
    cap = capsys.readouterr()
    rows = _rows(cap.out)
    assert rc == 0 and cap.out.split("\n")[0] == "NAME\tBOX\tTHIS-BOX\tVENDOR\tSTATE\tPENDING"
    assert [r[0] for r in rows] == ["mac-claude", "codex-x", "dp-claude"]
    assert [r[3] for r in rows] == ["claude", "codex", "claude"]
    assert rows[2][1] == "machine-b" and rows[0][1] == rows[1][1] != "machine-b"
    assert [r[2] for r in rows] == ["yes", "yes", "no"]
    assert all(r[4] == "via SendMessage" for r in rows)
    assert "SendMessage" in cap.out.strip().split("\n")[-1]


def test_list_on_windows_uses_router_when_available(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(us, "_windows", lambda: True)
    monkeypatch.setattr(us, "_session_entries", lambda: _ENTRIES[:1])
    fake = _FakeRouter({"ok": True, "peers": ["codex-x"], "reachable": ["codex-x"],
                        "agents": [{"name": "codex-x", "box": "machine-a", "vendor": "codex", "this_box": True}]})
    monkeypatch.setattr(us, "_router", lambda: fake)
    rc = us.main(["--list"])
    assert rc == 0 and "codex-x\tmachine-a\tyes\tcodex\tready\t" in capsys.readouterr().out


def test_session_entries_reads_live_pid_entries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import json
    import os
    (tmp_path / "a.json").write_text(json.dumps({"name": "live", "pid": os.getpid()}))
    (tmp_path / "b.json").write_text(json.dumps({"name": "dead", "pid": 2**22 + 12345}))
    (tmp_path / "c.json").write_text("not json")
    roster = us._sibling("unimatrix-roster.py", "unimatrix_roster_send_t")
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(tmp_path))
    monkeypatch.setattr(us, "_sibling", lambda f, m: roster)
    assert [e["name"] for e in us._session_entries()] == ["live"]


class _FakeRouter:
    ROUTER_ENDPOINT_SEED = "x"

    def __init__(self, resp) -> None:
        self.resp, self.reqs = resp, []

    def endpoint_path(self, seed, d=None) -> str:
        return "ctl"

    def request_line(self, ctl, req):
        self.reqs.append(req)
        return self.resp


def _run(monkeypatch, resp, argv):
    fake = _FakeRouter(resp)
    monkeypatch.setattr(us, "_router", lambda: fake)
    return us.main(argv), fake


def test_held_tty_write_does_not_send_again(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(us, "_deliver_held_grok", lambda name, text: {"ok": True, "written": True, "msg_id": "m"})
    rc, fake = _run(monkeypatch, {"ok": True, "msg_id": "router", "written": True}, ["--to", "x", "hi"])
    cap = capsys.readouterr()
    assert rc == 0 and cap.out.strip() == "sent to x" and cap.err == ""
    assert fake.reqs == []


def test_send_prints_sent_only_when_written(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True}, ["--to", "x", "hi"])
    assert rc == 0 and "sent to x" in capsys.readouterr().out


def test_send_receipt_names_the_session_the_router_reached(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True, "reached": "em.macmini",
                               "reached_box": "macmini"}, ["--to", "em", "hi"])
    assert rc == 0 and "sent to em (reached em.macmini on macmini)" in capsys.readouterr().out


def test_send_ok_with_msg_id_is_confirmed_even_without_written_marker(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run(monkeypatch, {"ok": True, "msg_id": "m"}, ["--to", "x", "hi"])
    cap = capsys.readouterr()
    assert rc == 0 and "sent to x" in cap.out and cap.err == ""


def test_send_unconfirmed_is_worded_unconfirmed_and_exits_zero_so_no_retry_block(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run(monkeypatch, {"ok": True}, ["--to", "x", "hi"])
    cap = capsys.readouterr()
    assert rc == 0 and "sent to" not in cap.out
    assert "unconfirmed" in cap.err and "may have been delivered" in cap.err
    assert "not delivered" not in cap.err and "not confirmed" not in cap.err


def test_own_target_prefers_codex_and_agy_env_over_grok() -> None:
    boom = lambda: "grok-sid"
    assert us.own_target({"CODEX_THREAD_ID": "t"}, grok=boom) == "t"
    assert us.own_target({"ANTIGRAVITY_CONVERSATION_ID": "c"}, grok=boom) == "c"
    assert us.own_target({"UNIMATRIX_GROK_SESSION_ID": "g-env"}, grok=boom) == "g-env"
    assert us.own_target({}, grok=boom) == "grok-sid"
    assert us.own_target({}, grok=lambda: "") == ""


@pytest.mark.parametrize("key", ["CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"])
def test_claude_shell_supplies_native_identity_for_router_verification(monkeypatch, key):
    for name in ("CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID",
                 "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(key, "native-claude-session")
    monkeypatch.setattr(us, "_grok_target", lambda: "")
    rc, router = _run(monkeypatch, {"ok": True, "msg_id": "m"}, ["--to", "x", "nonce"])
    assert rc == 0
    assert router.reqs[0]["as_target"] == "native-claude-session"
    assert router.reqs[0]["as"] == "cli"


@pytest.mark.parametrize("binding", ["missing", "ambiguous", "stale"])
def test_claude_shell_does_not_claim_delivery_for_refused_binding(monkeypatch, capsys, binding):
    monkeypatch.setattr(us, "own_target", lambda: "native-session")
    rc, router = _run(monkeypatch, {"ok": False, "error": f"{binding} sender binding"},
                      ["--to", "x", "nonce"])
    captured = capsys.readouterr()
    assert rc == 1 and "sent to" not in captured.out
    assert binding in captured.err
    assert router.reqs[0]["as_target"] == "native-session"


def test_grok_shell_send_carries_its_session_id_as_sender_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import json
    import os
    sid = "01a1c000-0000-7000-8000-00000000c0c0"
    (tmp_path / "active_sessions.json").write_text(json.dumps(
        [{"session_id": sid, "pid": os.getpid(), "cwd": str(tmp_path)}]), encoding="utf-8")
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("UNIMATRIX_PEER_NAME", raising=False)
    for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "UNIMATRIX_GROK_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    rc, fake = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True}, ["--to", "x", "hi"])
    assert rc == 0 and fake.reqs[0]["as_target"] == sid and fake.reqs[0]["as"] == "cli"


def test_non_grok_shell_send_has_empty_sender_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("UNIMATRIX_PEER_NAME", raising=False)
    for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "UNIMATRIX_GROK_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    rc, fake = _run(monkeypatch, {"ok": True, "msg_id": "m"}, ["--to", "x", "hi"])
    assert fake.reqs[0]["as_target"] == ""


def test_send_refusal_names_reason(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run(monkeypatch, {"ok": False, "error": "x did not accept: ENOINBOX"}, ["--to", "x", "hi"])
    cap = capsys.readouterr()
    assert rc == 1 and "did not accept: ENOINBOX" in cap.err and "sent to" not in cap.out


def test_send_peer_reply_still_printed(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run(monkeypatch, {"ok": True, "reply": "[p] hello"}, ["--to", "p", "hi"])
    assert rc == 0 and "[p] hello" in capsys.readouterr().out


def test_list_prints_agents_rows_with_box_and_vendor(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    resp = {"ok": True, "peers": ["p", "q"], "reachable": ["p", "sess"], "sessions": ["sess", "stale"],
            "ready": {"p": True, "q": False},
            "agents": [{"name": "p", "box": "mac", "vendor": "codex", "this_box": False},
                       {"name": "sess", "box": "machine-b", "vendor": "claude", "this_box": True}]}
    rc, fake = _run(monkeypatch, resp, ["--list"])
    out = capsys.readouterr().out.split("\n")
    assert out[0] == "NAME\tBOX\tTHIS-BOX\tVENDOR\tSTATE\tPENDING"
    assert rc == 0 and out[1:3] == ["p\tmac\tno\tcodex\tready\t-", "sess\tmachine-b\tyes\tclaude\tsession\t-"]
    assert "stale" not in "".join(out) and "q" not in out
    assert fake.reqs[0]["as"] == "cli"


def test_list_renders_each_codex_peers_pending_count_and_oldest_age(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    resp = {"ok": True, "peers": ["cx", "idle"], "reachable": ["cx", "idle", "sess"],
            "agents": [{"name": "cx", "box": "mac", "vendor": "codex"}, {"name": "idle", "box": "mac", "vendor": "codex"},
                       {"name": "sess", "box": "mac", "vendor": "claude"}],
            "codex_pending": {"cx": {"pending": 7, "oldest_age_s": 125.4, "failed": []},
                              "idle": {"pending": 0, "oldest_age_s": 0.0, "failed": []}}}
    rc, _ = _run(monkeypatch, resp, ["--list"])
    out = capsys.readouterr().out.split("\n")
    assert rc == 0 and [row.split("\t")[5] for row in out[1:4]] == ["7, oldest 2m05s", "0", "-"]


def test_send_the_router_held_prints_held_not_sent_and_exits_zero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run(monkeypatch, {"ok": True, "held": True, "pending": 4, "oldest_age_s": 42.0, "failed": []}, ["--to", "cx", "hi"])
    cap = capsys.readouterr()
    assert rc == 0 and cap.out.strip() == "held for cx: 4 pending, oldest 42s"
    assert "sent to" not in cap.out and cap.err == ""


def test_as_flag_is_rejected_as_unknown_argument(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, {"ok": True}, ["--as", "me", "--to", "x", "hi"])
    assert exc.value.code == 2 and "unrecognized arguments: --as" in capsys.readouterr().err


def test_list_marks_a_router_stale_agent_row_stale(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    resp = {"ok": True, "peers": ["p", "q"], "reachable": ["p", "q"],
            "agents": [{"name": "p", "box": "mac", "vendor": "codex", "stale": True},
                       {"name": "q", "box": "mac", "vendor": "codex", "stale": False}]}
    rc, _ = _run(monkeypatch, resp, ["--list"])
    out = capsys.readouterr().out.split("\n")
    assert rc == 0 and out[1:3] == ["p\tmac\t-\tcodex\tstale\t-", "q\tmac\t-\tcodex\tready\t-"]


def test_send_the_router_refuses_exits_nonzero_with_nothing_sent(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run(monkeypatch, {"ok": False, "error": "p has no live Codex process"}, ["--to", "p", "hi"])
    cap = capsys.readouterr()
    assert rc == 1 and "no live Codex process" in cap.err and "sent to" not in cap.out


def test_list_from_older_router_renders_dashes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    resp = {"ok": True, "peers": ["p"], "reachable": ["p", "sess"]}
    rc, _ = _run(monkeypatch, resp, ["--list"])
    out = capsys.readouterr().out.split("\n")
    assert rc == 0 and out[1:3] == ["p\t-\t-\t-\tready\t-", "sess\t-\t-\t-\tsession\t-"]


def test_list_without_reachable_field_fails(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run(monkeypatch, {"ok": True, "peers": ["p"], "sessions": ["s"]}, ["--list"])
    cap = capsys.readouterr()
    assert rc == 1 and cap.out == "" and "reachable" in cap.err


def test_send_without_router_names_sendmessage_and_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with tempfile.TemporaryDirectory() as d:
        rc = us.main(["--to", "x", "hi", "--endpoint-dir", d])
    err = capsys.readouterr().err
    assert rc != 0
    assert "SendMessage" in err


class _DownRouter(_FakeRouter):
    def request_line(self, ctl, req):
        raise FileNotFoundError(2, "The system cannot find the file specified", None, 2, None)


def test_list_with_router_down_names_the_local_records_fallback_on_stderr(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(us, "_router", lambda: _DownRouter({}))
    monkeypatch.setattr(us, "_list_from_sessions", lambda: print("ROWS"))
    assert us.main(["--list"]) == 0
    cap = capsys.readouterr()
    assert cap.out == "ROWS\n"
    assert "local session records" in cap.err and "no router is listening on ctl" in cap.err


def test_send_with_router_down_names_a_missing_endpoint_not_a_bare_winerror(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(us, "_router", lambda: _DownRouter({}))
    assert us.main(["--to", "x", "hi"]) == 1
    err = capsys.readouterr().err
    assert "no router is listening on ctl" in err and "SendMessage" in err


def test_a_non_missing_oserror_keeps_its_own_text_only() -> None:
    assert us._unreachable_why(PermissionError(13, "denied"), "ctl") == str(PermissionError(13, "denied"))


def test_send_to_a_mirror_whose_far_relay_failed_is_undelivered_not_sent(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(us, "_is_mirror", lambda n: True)
    monkeypatch.setattr(us, "relay_outcome", lambda n, since: {"ok": False, "error": "far relay lacks send-session; upgrade it"})
    rc, _ = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True}, ["--to", "fe", "hi"])
    cap = capsys.readouterr()
    assert rc == 1 and "sent to" not in cap.out
    assert "undelivered: fe: far relay lacks send-session" in cap.err


def test_send_to_a_mirror_the_relay_delivered_is_sent(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(us, "_is_mirror", lambda n: True)
    monkeypatch.setattr(us, "relay_outcome", lambda n, since: {"ok": True})
    rc, _ = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True}, ["--to", "fe", "hi"])
    assert rc == 0 and "sent to fe" in capsys.readouterr().out


def test_send_to_a_mirror_with_no_recorded_outcome_is_undelivered_and_exits_one(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(us, "_is_mirror", lambda n: True)
    monkeypatch.setattr(us, "relay_outcome", lambda n, since: None)
    rc, _ = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True}, ["--to", "fe", "hi"])
    cap = capsys.readouterr()
    assert rc == 1 and "sent to" not in cap.out
    assert "unconfirmed: fe: no far ingest ack within" in cap.err and "receiver enqueue: unknown" in cap.err
    assert "`receipts fe " in cap.err


def test_a_refused_row_says_not_enqueued_and_an_unconfirmed_row_says_unknown(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(us, "_is_mirror", lambda n: True)
    for row, state in (({"ok": False, "error": "relay: target mismatch"}, "not enqueued"),
                       ({"ok": False, "error": "ssh exit 255: x; far write unconfirmed; check", "unconfirmed": True}, "unknown")):
        monkeypatch.setattr(us, "relay_outcome", lambda n, since, row=row: row)
        rc, _ = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True}, ["--to", "fe", "hi"])
        assert rc == 1 and f"receiver enqueue: {state}" in capsys.readouterr().err
    assert us.enqueue_state({"ok": True}) == "enqueued" and us.enqueue_state(None) == "unknown"


def test_hostless_mirror_rows_still_count_as_mirrors(monkeypatch: pytest.MonkeyPatch) -> None:
    base = {"name": "grp.farbox", "unimatrixFarName": "grp", "pid": 7}
    for extra in ({"macSocketPath": "/far/s.sock"}, {"unimatrixBox": "machine-a"}, {"unimatrixRemote": "farbox"}):
        monkeypatch.setattr(us, "_session_entries", lambda extra=extra: [{**base, **extra}])
        assert us._mirror_name("grp") == "grp.farbox" and us._is_mirror("grp.farbox")


def test_a_plain_local_row_is_not_a_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(us, "_session_entries", lambda: [{"name": "grp", "pid": 7}])
    assert us._mirror_name("grp") == "" and not us._is_mirror("grp")


def test_short_machine_a_alias_uses_canonical_delivery_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(us, "_session_entries", lambda: [
        {"name": "grp.machine-a.local", "unimatrixFarName": "grp", "unimatrixBox": "machine-a", "pid": 7}
    ])
    assert us._mirror_name("grp.machine-a") == "grp.machine-a.local"


def test_send_to_a_non_mirror_never_waits_for_a_relay_outcome(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(us, "_is_mirror", lambda n: False)
    monkeypatch.setattr(us, "relay_outcome", lambda n, since: (_ for _ in ()).throw(AssertionError("waited")))
    rc, _ = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True}, ["--to", "x", "hi"])
    assert rc == 0 and "sent to x" in capsys.readouterr().out


def _deliveries(tmp_path, monkeypatch, rows) -> None:
    import json
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    p = tmp_path / "unimatrix" / "deliveries.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_relay_outcome_reads_the_named_targets_row_after_the_send_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _deliveries(tmp_path, monkeypatch, [
        {"at": 50, "target": "fe (pid 9, session s)", "ok": True},
        {"at": 150, "target": "other (pid 9, session s)", "ok": False, "error": "x"},
        {"at": 150, "target": "fe (pid 9, session s)", "ok": False, "error": "far relay lacks send-session; upgrade it"},
    ])
    out = us.relay_outcome("fe", 100, wait=0)
    assert out["ok"] is False and "send-session" in out["error"]
    assert us.relay_outcome("fe", 200, wait=0) is None


def test_relay_outcome_waits_for_a_row_the_bridge_has_not_written_yet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _deliveries(tmp_path, monkeypatch, [])
    clock = {"t": 0.0}
    p = tmp_path / "unimatrix" / "deliveries.jsonl"

    def sleep(s) -> None:
        clock["t"] += s
        if clock["t"] >= 1:
            p.write_text('{"at": 5, "target": "fe (pid 9, session s)", "ok": true}\n', encoding="utf-8")

    out = us.relay_outcome("fe", 1, wait=10, sleep=sleep, now=lambda: clock["t"])
    assert out == {"at": 5, "target": "fe (pid 9, session s)", "ok": True}


def _mirror_unmirrored_by_the_send(monkeypatch, tmp_path, row) -> None:
    """A live labelled mirror that the roster sync drops (misroute) as the router answers; the bridge's row is `row`."""
    import time
    state = {"sent": False}
    entry = {"name": "grp.farbox", "unimatrixFarName": "grp", "unimatrixRemote": "farbox", "pid": 7}
    monkeypatch.setattr(us, "_session_entries", lambda: [] if state["sent"] else [entry])
    _deliveries(tmp_path, monkeypatch, [{"at": time.time() + 60, "target": "grp.farbox (pid 9, session s)", **row}])

    class Router(_FakeRouter):
        def request_line(self, ctl, req):
            state["sent"] = True
            return super().request_line(ctl, req)

    router = Router({"ok": True, "msg_id": "m", "written": True})
    monkeypatch.setattr(us, "_router", lambda: router)
    monkeypatch.setattr(us, "RELAY_SETTLE_SECONDS", 0)


def test_a_mirror_unmirrored_during_the_send_still_reports_the_bridges_failed_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _mirror_unmirrored_by_the_send(monkeypatch, tmp_path, {"ok": False, "error": "addressed grp.farbox, endpoint is grp; re-mirror"})
    assert us.main(["--to", "grp", "hi"]) == 1
    cap = capsys.readouterr()
    assert "sent to" not in cap.out and "undelivered: grp: addressed grp.farbox" in cap.err


def test_a_mirror_unmirrored_during_the_send_still_confirms_the_bridges_ok_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _mirror_unmirrored_by_the_send(monkeypatch, tmp_path, {"ok": True})
    assert us.main(["--to", "grp", "hi"]) == 0
    assert "sent to grp" in capsys.readouterr().out


def test_sent_and_undelivered_lines_name_the_recorded_box_and_transport(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(us, "_is_mirror", lambda n: True)
    monkeypatch.setattr(us, "relay_outcome", lambda n, since: {"ok": True, "box": "macmini", "transport": "tunnel"})
    rc, _ = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True}, ["--to", "fe", "hi"])
    assert rc == 0 and "sent to fe [box macmini, transport tunnel]" in capsys.readouterr().out
    monkeypatch.setattr(us, "relay_outcome", lambda n, since: {"ok": False, "error": "boom", "box": "macmini", "transport": "ssh"})
    rc, _ = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True}, ["--to", "fe", "hi"])
    assert rc == 1 and "undelivered: fe: boom [box macmini, transport ssh]" in capsys.readouterr().err


def test_a_row_from_an_older_bridge_prints_box_unknown(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(us, "_is_mirror", lambda n: True)
    monkeypatch.setattr(us, "relay_outcome", lambda n, since: {"ok": True})
    rc, _ = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True}, ["--to", "fe", "hi"])
    assert rc == 0 and "sent to fe [box unknown]" in capsys.readouterr().out


def test_settle_wait_outlasts_the_bridges_far_leg() -> None:
    bridge = (Path(us.__file__).parent / "unimatrix-bridge.py").read_text(encoding="utf-8")
    leg = int(re.search(r"\.run\(command, data, timeout=(\d+)\)", bridge).group(1))
    assert us.RELAY_SETTLE_SECONDS > leg


def test_own_session_name_is_the_ancestor_session_and_cli_send_carries_it(monkeypatch: pytest.MonkeyPatch) -> None:
    parents = {50: 40, 40: 30, 30: 1}
    entries = [{"name": "coordinator-content-repo-3b", "pid": 30}, {"name": "other", "pid": 99}]
    assert us.own_session_name(entries, parents.get, 50) == "coordinator-content-repo-3b"
    assert us.own_session_name(entries, parents.get, 1) == ""
    monkeypatch.setattr(us, "own_session_name", lambda: "coordinator-content-repo-3b")
    monkeypatch.setattr(us, "own_target", lambda: "")
    rc, fake = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True}, ["--to", "fe", "hi"])
    assert fake.reqs[0]["as_session"] == "coordinator-content-repo-3b"


def test_mirror_label_is_stable_across_ssh_alias_generations_and_old_name_stays_an_alias() -> None:
    roster = us._sibling("unimatrix-roster.py", "unimatrix_roster_label_t")
    contract = us._sibling("unimatrix-peer-contract.py", "unimatrix_contract_label_t")
    assert roster.labelled("coordinator-content-repo-3b", "Mac-mini.local") == roster.labelled("coordinator-content-repo-3b", "u@Mac-mini") == "coordinator-content-repo-3b.Mac-mini"
    row = {"name": "coordinator-content-repo-3b.Mac-mini", "unimatrixRemote": "Mac-mini"}
    assert "coordinator-content-repo-3b.Mac-mini.local" in contract.mirror_addresses(row)
    assert "coordinator-content-repo-3b.Mac-mini" in contract.mirror_addresses({**row, "name": "coordinator-content-repo-3b.Mac-mini.local"})
    bridge = us._sibling("unimatrix-bridge.py", "unimatrix_bridge_label_t")
    assert bridge._roster_wrote_name("coordinator-content-repo-3b.Mac-mini", "coordinator-content-repo-3b", "Mac-mini.local")
    assert bridge._roster_wrote_name("coordinator-content-repo-3b.Mac-mini.local", "coordinator-content-repo-3b", "Mac-mini.local")

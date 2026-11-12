"""unimatrix-send.py points at SendMessage when the router is unreachable."""
import importlib.util
import tempfile
from pathlib import Path

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


def test_list_names_a_far_box_by_member_host_never_an_ip_octet(monkeypatch, capsys):
    entries = [{"name": "far-1", "pid": 4, "sessionId": "s4", "unimatrixBox": "192.168.1.20"},
               {"name": "far-2", "pid": 5, "sessionId": "s5", "unimatrixBox": "10.9.9.9"}]
    monkeypatch.setattr(us, "_session_entries", lambda: entries)
    monkeypatch.setattr(us, "_member_ip_hosts", lambda: {"192.168.1.20": "machine-b.local"})
    with tempfile.TemporaryDirectory() as d:
        assert us.main(["--list", "--endpoint-dir", d]) == 0
    rows = _rows(capsys.readouterr().out)
    assert rows[0][1] == "machine-b"
    assert rows[1][1] == "10.9.9.9"


def test_list_without_router_lists_sessions_dir_and_succeeds(monkeypatch, capsys):
    monkeypatch.setattr(us, "_session_entries", lambda: _ENTRIES)
    with tempfile.TemporaryDirectory() as d:
        rc = us.main(["--list", "--endpoint-dir", d])
    cap = capsys.readouterr()
    rows = _rows(cap.out)
    assert rc == 0 and cap.out.split("\n")[0] == "NAME\tBOX\tVENDOR\tSTATE"
    assert [r[0] for r in rows] == ["mac-claude", "codex-x", "dp-claude"]
    assert [r[2] for r in rows] == ["claude", "codex", "claude"]
    assert rows[2][1] == "machine-b" and rows[0][1] == rows[1][1] != "machine-b"
    assert all(r[3] == "via SendMessage" for r in rows)
    assert "SendMessage" in cap.out.strip().split("\n")[-1]


def test_list_on_windows_uses_router_when_available(monkeypatch, capsys):
    monkeypatch.setattr(us, "_windows", lambda: True)
    monkeypatch.setattr(us, "_session_entries", lambda: _ENTRIES[:1])
    fake = _FakeRouter({"ok": True, "peers": ["codex-x"], "reachable": ["codex-x"],
                        "agents": [{"name": "codex-x", "box": "machine-a", "vendor": "codex"}]})
    monkeypatch.setattr(us, "_router", lambda: fake)
    rc = us.main(["--list"])
    assert rc == 0 and "codex-x\tmachine-a\tcodex\tready" in capsys.readouterr().out


def test_session_entries_reads_live_pid_entries(monkeypatch, tmp_path):
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

    def __init__(self, resp):
        self.resp, self.reqs = resp, []

    def endpoint_path(self, seed, d=None):
        return "ctl"

    def request_line(self, ctl, req):
        self.reqs.append(req)
        return self.resp


def _run(monkeypatch, resp, argv):
    fake = _FakeRouter(resp)
    monkeypatch.setattr(us, "_router", lambda: fake)
    return us.main(argv), fake


def test_send_prints_sent_only_when_written(monkeypatch, capsys):
    rc, _ = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True}, ["--to", "x", "hi"])
    assert rc == 0 and "sent to x" in capsys.readouterr().out


def test_send_ok_with_msg_id_is_confirmed_even_without_written_marker(monkeypatch, capsys):
    rc, _ = _run(monkeypatch, {"ok": True, "msg_id": "m"}, ["--to", "x", "hi"])
    cap = capsys.readouterr()
    assert rc == 0 and "sent to x" in cap.out and cap.err == ""


def test_send_unconfirmed_is_worded_unconfirmed_and_exits_zero_so_no_retry_block(monkeypatch, capsys):
    rc, _ = _run(monkeypatch, {"ok": True}, ["--to", "x", "hi"])
    cap = capsys.readouterr()
    assert rc == 0 and "sent to" not in cap.out
    assert "unconfirmed" in cap.err and "may have been delivered" in cap.err
    assert "not delivered" not in cap.err and "not confirmed" not in cap.err


def test_own_target_prefers_codex_and_agy_env_over_grok():
    boom = lambda: "grok-sid"
    assert us.own_target({"CODEX_THREAD_ID": "t"}, grok=boom) == "t"
    assert us.own_target({"ANTIGRAVITY_CONVERSATION_ID": "c"}, grok=boom) == "c"
    assert us.own_target({}, grok=boom) == "grok-sid"
    assert us.own_target({}, grok=lambda: "") == ""


def test_grok_shell_send_carries_its_session_id_as_sender_target(monkeypatch, tmp_path, capsys):
    import json
    import os
    sid = "01a1c000-0000-7000-8000-00000000c0c0"
    (tmp_path / "active_sessions.json").write_text(json.dumps(
        [{"session_id": sid, "pid": os.getpid(), "cwd": str(tmp_path)}]), encoding="utf-8")
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("UNIMATRIX_PEER_NAME", raising=False)
    rc, fake = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True}, ["--to", "x", "hi"])
    assert rc == 0 and fake.reqs[0]["as_target"] == sid and fake.reqs[0]["as"] == "cli"


def test_non_grok_shell_send_has_empty_sender_target(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("UNIMATRIX_PEER_NAME", raising=False)
    rc, fake = _run(monkeypatch, {"ok": True, "msg_id": "m"}, ["--to", "x", "hi"])
    assert fake.reqs[0]["as_target"] == ""


def test_send_refusal_names_reason(monkeypatch, capsys):
    rc, _ = _run(monkeypatch, {"ok": False, "error": "x did not accept: ENOINBOX"}, ["--to", "x", "hi"])
    cap = capsys.readouterr()
    assert rc == 1 and "did not accept: ENOINBOX" in cap.err and "sent to" not in cap.out


def test_send_peer_reply_still_printed(monkeypatch, capsys):
    rc, _ = _run(monkeypatch, {"ok": True, "reply": "[p] hello"}, ["--to", "p", "hi"])
    assert rc == 0 and "[p] hello" in capsys.readouterr().out


def test_list_prints_agents_rows_with_box_and_vendor(monkeypatch, capsys):
    resp = {"ok": True, "peers": ["p", "q"], "reachable": ["p", "sess"], "sessions": ["sess", "stale"],
            "ready": {"p": True, "q": False},
            "agents": [{"name": "p", "box": "mac", "vendor": "codex"},
                       {"name": "sess", "box": "machine-b", "vendor": "claude"}]}
    rc, fake = _run(monkeypatch, resp, ["--list", "--as", "me"])
    out = capsys.readouterr().out.split("\n")
    assert rc == 0 and out[1:3] == ["p\tmac\tcodex\tready", "sess\tmachine-b\tclaude\tsession"]
    assert "stale" not in "".join(out) and "q" not in out
    assert fake.reqs[0]["as"] == "me"


def test_list_marks_a_router_stale_agent_row_stale(monkeypatch, capsys):
    resp = {"ok": True, "peers": ["p", "q"], "reachable": ["p", "q"],
            "agents": [{"name": "p", "box": "mac", "vendor": "codex", "stale": True},
                       {"name": "q", "box": "mac", "vendor": "codex", "stale": False}]}
    rc, _ = _run(monkeypatch, resp, ["--list"])
    out = capsys.readouterr().out.split("\n")
    assert rc == 0 and out[1:3] == ["p\tmac\tcodex\tstale", "q\tmac\tcodex\tready"]


def test_send_the_router_refuses_exits_nonzero_with_nothing_sent(monkeypatch, capsys):
    rc, _ = _run(monkeypatch, {"ok": False, "error": "p has no live Codex process"}, ["--to", "p", "hi"])
    cap = capsys.readouterr()
    assert rc == 1 and "no live Codex process" in cap.err and "sent to" not in cap.out


def test_list_from_older_router_renders_dashes(monkeypatch, capsys):
    resp = {"ok": True, "peers": ["p"], "reachable": ["p", "sess"]}
    rc, _ = _run(monkeypatch, resp, ["--list"])
    out = capsys.readouterr().out.split("\n")
    assert rc == 0 and out[1:3] == ["p\t-\t-\tready", "sess\t-\t-\tsession"]


def test_list_without_reachable_field_fails(monkeypatch, capsys):
    rc, _ = _run(monkeypatch, {"ok": True, "peers": ["p"], "sessions": ["s"]}, ["--list"])
    cap = capsys.readouterr()
    assert rc == 1 and cap.out == "" and "reachable" in cap.err


def test_send_without_router_names_sendmessage_and_fails(capsys):
    with tempfile.TemporaryDirectory() as d:
        rc = us.main(["--to", "x", "hi", "--endpoint-dir", d])
    err = capsys.readouterr().err
    assert rc != 0
    assert "SendMessage" in err


def test_send_to_a_mirror_whose_far_relay_failed_is_undelivered_not_sent(monkeypatch, capsys):
    monkeypatch.setattr(us, "_is_mirror", lambda n: True)
    monkeypatch.setattr(us, "relay_outcome", lambda n, since: {"ok": False, "error": "far relay lacks send-session; upgrade it"})
    rc, _ = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True}, ["--to", "fe", "hi"])
    cap = capsys.readouterr()
    assert rc == 1 and "sent to" not in cap.out
    assert "undelivered: fe: far relay lacks send-session" in cap.err


def test_send_to_a_mirror_the_relay_delivered_is_sent(monkeypatch, capsys):
    monkeypatch.setattr(us, "_is_mirror", lambda n: True)
    monkeypatch.setattr(us, "relay_outcome", lambda n, since: {"ok": True})
    rc, _ = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True}, ["--to", "fe", "hi"])
    assert rc == 0 and "sent to fe" in capsys.readouterr().out


def test_send_to_a_mirror_with_no_recorded_outcome_is_unconfirmed_never_sent(monkeypatch, capsys):
    monkeypatch.setattr(us, "_is_mirror", lambda n: True)
    monkeypatch.setattr(us, "relay_outcome", lambda n, since: None)
    rc, _ = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True}, ["--to", "fe", "hi"])
    cap = capsys.readouterr()
    assert rc == 0 and "sent to" not in cap.out and "unconfirmed" in cap.err


def test_send_to_a_non_mirror_never_waits_for_a_relay_outcome(monkeypatch, capsys):
    monkeypatch.setattr(us, "_is_mirror", lambda n: False)
    monkeypatch.setattr(us, "relay_outcome", lambda n, since: (_ for _ in ()).throw(AssertionError("waited")))
    rc, _ = _run(monkeypatch, {"ok": True, "msg_id": "m", "written": True}, ["--to", "x", "hi"])
    assert rc == 0 and "sent to x" in capsys.readouterr().out


def _deliveries(tmp_path, monkeypatch, rows):
    import json
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    p = tmp_path / "unimatrix" / "deliveries.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_relay_outcome_reads_the_named_targets_row_after_the_send_only(monkeypatch, tmp_path):
    _deliveries(tmp_path, monkeypatch, [
        {"at": 50, "target": "fe (pid 9, session s)", "ok": True},
        {"at": 150, "target": "other (pid 9, session s)", "ok": False, "error": "x"},
        {"at": 150, "target": "fe (pid 9, session s)", "ok": False, "error": "far relay lacks send-session; upgrade it"},
    ])
    out = us.relay_outcome("fe", 100, wait=0)
    assert out["ok"] is False and "send-session" in out["error"]
    assert us.relay_outcome("fe", 200, wait=0) is None


def test_relay_outcome_waits_for_a_row_the_bridge_has_not_written_yet(monkeypatch, tmp_path):
    _deliveries(tmp_path, monkeypatch, [])
    clock = {"t": 0.0}
    p = tmp_path / "unimatrix" / "deliveries.jsonl"

    def sleep(s):
        clock["t"] += s
        if clock["t"] >= 1:
            p.write_text('{"at": 5, "target": "fe (pid 9, session s)", "ok": true}\n', encoding="utf-8")

    out = us.relay_outcome("fe", 1, wait=10, sleep=sleep, now=lambda: clock["t"])
    assert out == {"at": 5, "target": "fe (pid 9, session s)", "ok": True}


def _mirror_unmirrored_by_the_send(monkeypatch, tmp_path, row):
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


def test_a_mirror_unmirrored_during_the_send_still_reports_the_bridges_failed_row(monkeypatch, tmp_path, capsys):
    _mirror_unmirrored_by_the_send(monkeypatch, tmp_path, {"ok": False, "error": "addressed grp.farbox, endpoint is grp; re-mirror"})
    assert us.main(["--to", "grp", "hi"]) == 1
    cap = capsys.readouterr()
    assert "sent to" not in cap.out and "undelivered: grp: addressed grp.farbox" in cap.err


def test_a_mirror_unmirrored_during_the_send_still_confirms_the_bridges_ok_row(monkeypatch, tmp_path, capsys):
    _mirror_unmirrored_by_the_send(monkeypatch, tmp_path, {"ok": True})
    assert us.main(["--to", "grp", "hi"]) == 0
    assert "sent to grp" in capsys.readouterr().out

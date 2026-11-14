"""Behavior pins for the unimatrix relay `send-to <pid> <digest>` verb."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

RELAY = Path(__file__).resolve().parents[1] / "unimatrix-relay.py"
PIPE_A = "\\\\.\\pipe\\cc-a"
PIPE_B = "\\\\.\\pipe\\cc-b"


def load():
    spec = importlib.util.spec_from_file_location("unimatrix_relay_send_to", RELAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = load()


def digest(path):
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


class _Stdin:
    def __init__(self, data):
        self.buffer = io.BytesIO(data)


@pytest.fixture(autouse=True)
def no_live_router(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(R, "_router_resolve", lambda name: None)


def test_router_without_the_resolve_op_leaves_local_checks_standing(monkeypatch):
    import types
    router = types.SimpleNamespace(ROUTER_ENDPOINT_SEED="s", endpoint_path=lambda seed: "ctl",
                                   request_line=lambda ctl, req, t: {"ok": False, "error": "unknown op"})
    monkeypatch.undo()
    monkeypatch.setattr(R, "_load_sibling", lambda *a: router)
    assert R._router_resolve("g") is None
    router.request_line = lambda ctl, req, t: {"ok": False, "error": "no unique live session"}
    assert R._router_resolve("g") == {"ok": False, "error": "no unique live session"}


@pytest.fixture
def written(monkeypatch):
    sent = []
    monkeypatch.setattr(R, "_write_line", lambda path, line: sent.append((path, line)))
    return sent


def serve(monkeypatch, kind, path, tmp_path):
    """One live session, pid 7, at `path`; `kind` picks the endpoint family."""
    monkeypatch.setattr(R, "entries", lambda: iter([(7, {"messagingSocketPath": path, "name": "real-owner"}, "7.k.key")]))
    monkeypatch.setattr(R, "endpoint_kind", lambda platform=None: kind)
    if kind == "socket":
        monkeypatch.setattr(R, "SOCKS", str(tmp_path))
        monkeypatch.setattr(R.stat, "S_ISSOCK", lambda mode: True)


def endpoint(kind, tmp_path):
    if kind == "pipe":
        return PIPE_A
    p = tmp_path / "a.sock"
    p.write_bytes(b"")
    return str(p)


def test_parse_mode_send_to():
    d = digest(PIPE_A)
    assert R.parse_mode(f"send-to 12 {d}") == ("send-to", (12, d))
    assert R.parse_mode("send-to 12") is None
    assert R.parse_mode("send-to x " + d) is None
    assert R.parse_mode("send-to 12 " + d.upper()) is None
    assert R.parse_mode("send-to 12 abc") is None
    assert R.parse_mode(f"send-to 12 {d} extra") is None


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_matching_digest_delivers_batched_replies(monkeypatch, tmp_path, capsys, written, kind):
    path = endpoint(kind, tmp_path)
    serve(monkeypatch, kind, path, tmp_path)
    monkeypatch.setattr(R.sys, "stdin", _Stdin(b'{"a":1}\n\n{"b":2}\n'))
    R.send_batch(7, digest(path))
    replies = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [r["ok"] for r in replies] == [True, False, True]
    assert [p for p, _ in written] == [path, path]


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_mismatched_digest_exits_before_reading_or_writing(monkeypatch, tmp_path, written, kind):
    path = endpoint(kind, tmp_path)
    serve(monkeypatch, kind, path, tmp_path)
    stdin = _Stdin(b'{"a":1}\n')
    monkeypatch.setattr(R.sys, "stdin", stdin)
    with pytest.raises(SystemExit) as exc:
        R.send_batch(7, digest(PIPE_B if kind == "pipe" else path + "x"))
    assert str(exc.value) == "target mismatch: pid 7 holds real-owner"
    assert stdin.buffer.tell() == 0
    assert written == []


def test_send_batch_without_digest_still_serves_by_pid(monkeypatch, tmp_path, capsys, written):
    serve(monkeypatch, "pipe", PIPE_A, tmp_path)
    monkeypatch.setattr(R.sys, "stdin", _Stdin(b'{"a":1}\n'))
    R.send_batch(7)
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_main_dispatches_send_to_with_the_digest(monkeypatch):
    seen = []
    monkeypatch.setattr(R, "send_batch", lambda pid, dg=None: seen.append((pid, dg)))
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"send-to 7 {digest(PIPE_A)}")
    R.main()
    assert seen == [(7, digest(PIPE_A))]


def ndigest(name):
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def serve_many(monkeypatch, kind, rows, tmp_path):
    """Live sessions `rows` = [(pid, entry)]; sockets are created under tmp_path."""
    monkeypatch.setattr(R, "entries", lambda: iter([(p, d, f"{p}.k.key") for p, d in rows]))
    monkeypatch.setattr(R, "endpoint_kind", lambda platform=None: kind)
    if kind == "socket":
        monkeypatch.setattr(R, "SOCKS", str(tmp_path))
        monkeypatch.setattr(R.stat, "S_ISSOCK", lambda mode: True)


def paths(kind, tmp_path):
    if kind == "pipe":
        return PIPE_A, PIPE_B
    a, b = tmp_path / "a.sock", tmp_path / "b.sock"
    a.write_bytes(b"")
    b.write_bytes(b"")
    return str(a), str(b)


def test_name_digest_and_parse_mode_send_named():
    assert R.name_digest("group-em-ad") == ndigest("group-em-ad")
    d, n = digest(PIPE_A), ndigest("x")
    assert R.parse_mode(f"send-named 12 {d} {n}") == ("send-named", (12, d, n))
    assert R.parse_mode(f"send-named 12 {d}") is None
    assert R.parse_mode(f"send-named 12 {d} {n.upper()}") is None
    assert R.parse_mode(f"send-named x {d} {n}") is None
    assert R.parse_mode(f"send-to 12 {d}") == ("send-to", (12, d))


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_send_named_matching_name_delivers(monkeypatch, tmp_path, capsys, written, kind):
    a, b = paths(kind, tmp_path)
    serve_many(monkeypatch, kind, [(7, {"messagingSocketPath": a, "name": "g"}),
                                   (8, {"messagingSocketPath": b, "name": "other"})], tmp_path)
    monkeypatch.setattr(R.sys, "stdin", _Stdin(b'{"a":1}\n'))
    R.send_batch(7, digest(a), ndigest("g"))
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    assert [p for p, _ in written] == [a]


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_send_named_wrong_name_is_refused_before_reading(monkeypatch, tmp_path, written, kind):
    a, b = paths(kind, tmp_path)
    serve_many(monkeypatch, kind, [(7, {"messagingSocketPath": a, "name": "claude-klabauter-c6"})], tmp_path)
    stdin = _Stdin(b'{"a":1}\n')
    monkeypatch.setattr(R.sys, "stdin", stdin)
    with pytest.raises(SystemExit) as exc:
        R.send_batch(7, digest(a), ndigest("group-em-ad"))
    assert str(exc.value) == "target mismatch: pid 7 holds claude-klabauter-c6, not the addressed session"
    assert stdin.buffer.tell() == 0
    assert written == []


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_send_named_ambiguous_name_is_refused_but_mirrors_do_not_count(monkeypatch, tmp_path, written, kind):
    a, b = paths(kind, tmp_path)
    rows = [(7, {"messagingSocketPath": a, "name": "dup"}),
            (8, {"messagingSocketPath": b, "name": "dup"}),
            (9, {"messagingSocketPath": a + "m", "name": "dup", "unimatrixMirror": True})]
    serve_many(monkeypatch, kind, rows, tmp_path)
    stdin = _Stdin(b'{"a":1}\n')
    monkeypatch.setattr(R.sys, "stdin", stdin)
    with pytest.raises(SystemExit) as exc:
        R.send_batch(7, digest(a), ndigest("dup"))
    assert str(exc.value) == "ambiguous: 2 live sessions named dup (pids 7,8)"
    assert stdin.buffer.tell() == 0
    assert written == []
    serve_many(monkeypatch, kind, [rows[0], rows[2]], tmp_path)
    monkeypatch.setattr(R.sys, "stdin", _Stdin(b'{"a":1}\n'))
    R.send_batch(7, digest(a), ndigest("dup"))
    assert [p for p, _ in written] == [a]


def test_main_dispatches_send_named(monkeypatch):
    seen = []
    monkeypatch.setattr(R, "send_batch", lambda *a: seen.append(a))
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"send-named 7 {digest(PIPE_A)} {ndigest('g')}")
    R.main()
    assert seen == [(7, digest(PIPE_A), ndigest("g"))]


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_roster_drops_entries_sharing_a_socket(monkeypatch, tmp_path, capsys, kind):
    a, b = paths(kind, tmp_path)
    rows = [(7, {"messagingSocketPath": a, "name": "one"}),
            (8, {"messagingSocketPath": a, "name": "two"}),
            (9, {"messagingSocketPath": b, "name": "three"})]
    serve_many(monkeypatch, kind, rows, tmp_path)
    monkeypatch.setattr(R, "SESS", str(tmp_path))
    for p, _ in rows:
        (tmp_path / f"{p}.k.key").write_text("key", encoding="utf-8")
    monkeypatch.setattr(R, "engine_fields", lambda: {})
    monkeypatch.setattr(R, "codex_export_refused", lambda d, cache: False)
    R.roster()
    out, err = capsys.readouterr()
    assert [json.loads(x)["entry"]["name"] for x in out.splitlines()] == ["three"]
    assert err.strip() == f"ambiguous socket {a}: pids 7,8"


def test_parse_mode_send_session():
    d, n = digest(PIPE_A), ndigest("x")
    sid = "82b1c37f-51df-4c61-a71d-fa0644eccbb9"
    assert R.parse_mode(f"send-session 12 {d} {n} {sid}") == ("send-session", (12, d, n, sid))
    assert R.parse_mode(f"send-session 12 {d} {n}") is None
    assert R.parse_mode(f"send-session 12 {d} {n} bad/sid") is None
    assert R.parse_mode(f"send-session 12 {d} {n} {'a' * 129}") is None
    assert R.parse_mode(f"send-session 12 {d} {n} {'a' * 128}") is not None
    assert R.parse_mode(f"send-session x {d} {n} {sid}") is None


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_send_session_matching_session_delivers(monkeypatch, tmp_path, capsys, written, kind):
    a, b = paths(kind, tmp_path)
    serve_many(monkeypatch, kind, [(7, {"messagingSocketPath": a, "name": "g", "sessionId": "sid-g"})], tmp_path)
    monkeypatch.setattr(R.sys, "stdin", _Stdin(b'{"a":1}\n'))
    R.send_batch(7, digest(a), ndigest("g"), "sid-g")
    reply = json.loads(capsys.readouterr().out)
    assert reply["ok"] is True and (reply["reached"], reply["reached_session"], reply["reached_pid"]) == ("g", "sid-g", 7)
    assert [p for p, _ in written] == [a]


@pytest.mark.parametrize("kind", ["pipe", "socket"])
@pytest.mark.parametrize("held", ["sid-other", None])
def test_send_session_mismatch_or_absent_session_is_refused_before_reading(
        monkeypatch, tmp_path, written, kind, held):
    a, b = paths(kind, tmp_path)
    entry = {"messagingSocketPath": a, "name": "g"}
    if held is not None:
        entry["sessionId"] = held
    serve_many(monkeypatch, kind, [(7, entry)], tmp_path)
    stdin = _Stdin(b'{"a":1}\n')
    monkeypatch.setattr(R.sys, "stdin", stdin)
    with pytest.raises(SystemExit) as exc:
        R.send_batch(7, digest(a), ndigest("g"), "sid-g")
    assert str(exc.value) == f"target mismatch: pid 7 holds session {held} (g), not addressed session sid-g"
    assert stdin.buffer.tell() == 0
    assert written == []


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_send_session_still_refuses_wrong_name_and_ambiguity(monkeypatch, tmp_path, written, kind):
    a, b = paths(kind, tmp_path)
    serve_many(monkeypatch, kind, [(7, {"messagingSocketPath": a, "name": "claude-klabauter-c6", "sessionId": "s"})], tmp_path)
    monkeypatch.setattr(R.sys, "stdin", _Stdin(b'{"a":1}\n'))
    with pytest.raises(SystemExit) as exc:
        R.send_batch(7, digest(a), ndigest("group-em-ad"), "s")
    assert str(exc.value) == "target mismatch: pid 7 holds claude-klabauter-c6, not the addressed session"
    serve_many(monkeypatch, kind, [(7, {"messagingSocketPath": a, "name": "dup", "sessionId": "s"}),
                                   (8, {"messagingSocketPath": b, "name": "dup", "sessionId": "t"})], tmp_path)
    with pytest.raises(SystemExit) as exc:
        R.send_batch(7, digest(a), ndigest("dup"), "s")
    assert str(exc.value) == "ambiguous: 2 live sessions named dup (pids 7,8)"
    assert written == []


def test_main_dispatches_send_session(monkeypatch):
    seen = []
    monkeypatch.setattr(R, "send_batch", lambda *a: seen.append(a))
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"send-session 7 {digest(PIPE_A)} {ndigest('g')} sid-g")
    R.main()
    assert seen == [(7, digest(PIPE_A), ndigest("g"), "sid-g")]

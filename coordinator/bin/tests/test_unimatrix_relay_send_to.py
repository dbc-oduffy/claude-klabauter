"""Behavior pins for the unimatrix relay `send-to <pid> <digest>` verb."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

pytestmark = pytest.mark.complete

RELAY = Path(__file__).resolve().parents[1] / "unimatrix-relay.py"
PIPE_A = "\\\\.\\pipe\\cc-a"
PIPE_B = "\\\\.\\pipe\\cc-b"


def load() -> "ModuleType":
    spec = importlib.util.spec_from_file_location("unimatrix_relay_send_to", RELAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = load()


def digest(path) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


class _Stdin:
    def __init__(self, data) -> None:
        self.buffer = io.BytesIO(data)


@pytest.fixture(autouse=True)
def no_live_router(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(R, "_router_resolve", lambda name: None)


def test_router_without_the_resolve_op_leaves_local_checks_standing(monkeypatch: pytest.MonkeyPatch) -> None:
    import types
    router = types.SimpleNamespace(ROUTER_ENDPOINT_SEED="s", endpoint_path=lambda seed: "ctl",
                                   request_line=lambda ctl, req, t: {"ok": False, "error": "unknown op"})
    monkeypatch.undo()
    monkeypatch.setattr(R, "_load_sibling", lambda *a: router)
    assert R._router_resolve("g") is None
    router.request_line = lambda ctl, req, t: {"ok": False, "error": "no unique live session"}
    assert R._router_resolve("g") == {"ok": False, "error": "no unique live session"}


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch):
    sent = []
    monkeypatch.setattr(R, "_write_line", lambda path, line: sent.append((path, line)))
    return sent


def serve(monkeypatch, kind, path, tmp_path) -> None:
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


def test_parse_mode_send_to() -> None:
    d = digest(PIPE_A)
    assert R.parse_mode(f"send-to 12 {d}") == ("send-to", (12, d))
    assert R.parse_mode("send-to 12") is None
    assert R.parse_mode("send-to x " + d) is None
    assert R.parse_mode("send-to 12 " + d.upper()) is None
    assert R.parse_mode("send-to 12 abc") is None
    assert R.parse_mode(f"send-to 12 {d} extra") is None


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_matching_digest_delivers_batched_replies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], written, kind) -> None:
    path = endpoint(kind, tmp_path)
    serve(monkeypatch, kind, path, tmp_path)
    monkeypatch.setattr(R.sys, "stdin", _Stdin(b'{"a":1}\n\n{"b":2}\n'))
    R.send_batch(7, digest(path))
    replies = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [r["ok"] for r in replies] == [True, False, True]
    assert [p for p, _ in written] == [path, path]


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_mismatched_digest_exits_before_reading_or_writing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, written, kind) -> None:
    path = endpoint(kind, tmp_path)
    serve(monkeypatch, kind, path, tmp_path)
    stdin = _Stdin(b'{"a":1}\n')
    monkeypatch.setattr(R.sys, "stdin", stdin)
    with pytest.raises(SystemExit) as exc:
        R.send_batch(7, digest(PIPE_B if kind == "pipe" else path + "x"))
    assert str(exc.value) == "target mismatch: pid 7 holds real-owner"
    assert stdin.buffer.tell() == 0
    assert written == []


def test_send_batch_without_digest_still_serves_by_pid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], written) -> None:
    serve(monkeypatch, "pipe", PIPE_A, tmp_path)
    monkeypatch.setattr(R.sys, "stdin", _Stdin(b'{"a":1}\n'))
    R.send_batch(7)
    assert json.loads(capsys.readouterr().out) == {"ok": True, "written": True}


def test_main_dispatches_send_to_with_the_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(R, "send_batch", lambda pid, dg=None: seen.append((pid, dg)))
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"send-to 7 {digest(PIPE_A)}")
    R.main()
    assert seen == [(7, digest(PIPE_A))]


def ndigest(name) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def serve_many(monkeypatch, kind, rows, tmp_path) -> None:
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


def test_name_digest_and_parse_mode_send_named() -> None:
    assert R.name_digest("group-em-ad") == ndigest("group-em-ad")
    d, n = digest(PIPE_A), ndigest("x")
    assert R.parse_mode(f"send-named 12 {d} {n}") == ("send-named", (12, d, n))
    assert R.parse_mode(f"send-named 12 {d}") is None
    assert R.parse_mode(f"send-named 12 {d} {n.upper()}") is None
    assert R.parse_mode(f"send-named x {d} {n}") is None
    assert R.parse_mode(f"send-to 12 {d}") == ("send-to", (12, d))


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_send_named_matching_name_delivers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], written, kind) -> None:
    a, b = paths(kind, tmp_path)
    serve_many(monkeypatch, kind, [(7, {"messagingSocketPath": a, "name": "g"}),
                                   (8, {"messagingSocketPath": b, "name": "other"})], tmp_path)
    monkeypatch.setattr(R.sys, "stdin", _Stdin(b'{"a":1}\n'))
    R.send_batch(7, digest(a), ndigest("g"))
    assert json.loads(capsys.readouterr().out) == {"ok": True, "written": True}
    assert [p for p, _ in written] == [a]


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_send_named_wrong_name_is_refused_before_reading(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, written, kind) -> None:
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
def test_send_named_ambiguous_name_is_refused_but_mirrors_do_not_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, written, kind) -> None:
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


def test_main_dispatches_send_named(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(R, "send_batch", lambda *a: seen.append(a))
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"send-named 7 {digest(PIPE_A)} {ndigest('g')}")
    R.main()
    assert seen == [(7, digest(PIPE_A), ndigest("g"))]


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_roster_drops_entries_sharing_a_socket(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], kind) -> None:
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


def test_parse_mode_send_session() -> None:
    d, n = digest(PIPE_A), ndigest("x")
    sid = "82b1c37f-51df-4c61-a71d-fa0644eccbb9"
    assert R.parse_mode(f"send-session 12 {d} {n} {sid}") == ("send-session", (12, d, n, sid))
    assert R.parse_mode(f"send-session 12 {d} {n}") is None
    assert R.parse_mode(f"send-session 12 {d} {n} bad/sid") is None
    assert R.parse_mode(f"send-session 12 {d} {n} {'a' * 129}") is None
    assert R.parse_mode(f"send-session 12 {d} {n} {'a' * 128}") is not None
    assert R.parse_mode(f"send-session x {d} {n} {sid}") is None


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_send_session_matching_session_delivers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], written, kind) -> None:
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
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, written, kind, held) -> None:
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
def test_send_session_still_refuses_wrong_name_and_ambiguity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, written, kind) -> None:
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


def test_main_dispatches_send_session(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(R, "send_batch", lambda *a: seen.append(a))
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"send-session 7 {digest(PIPE_A)} {ndigest('g')} sid-g")
    R.main()
    assert seen == [(7, digest(PIPE_A), ndigest("g"), "sid-g")]


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_send_session_on_a_mux_pid_picks_the_entry_by_session_before_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], written, kind) -> None:
    a, b = paths(kind, tmp_path)
    mux = [(7, {"messagingSocketPath": a, "name": "g", "sessionId": "sid-g"}),
           (7, {"messagingSocketPath": b, "name": "h", "sessionId": "sid-h"})]
    serve_many(monkeypatch, kind, mux, tmp_path)
    monkeypatch.setattr(R.sys, "stdin", _Stdin(b'{"a":1}\n'))
    R.send_batch(7, None, None, "sid-h")
    assert json.loads(capsys.readouterr().out)["reached_session"] == "sid-h"
    assert [p for p, _ in written] == [b]


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_send_session_on_a_mux_pid_holding_no_such_session_is_refused_naming_both(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, written, kind) -> None:
    a, b = paths(kind, tmp_path)
    mux = [(7, {"messagingSocketPath": a, "name": "g", "sessionId": "sid-g"}),
           (7, {"messagingSocketPath": b, "name": "h", "sessionId": "sid-h"})]
    serve_many(monkeypatch, kind, mux, tmp_path)
    stdin = _Stdin(b'{"a":1}\n')
    monkeypatch.setattr(R.sys, "stdin", stdin)
    with pytest.raises(SystemExit) as exc:
        R.send_batch(7, digest(a), ndigest("g"), "sid-stale")
    assert str(exc.value) == "target mismatch: pid 7 holds sessions sid-g,sid-h, not addressed session sid-stale"
    assert stdin.buffer.tell() == 0 and written == []


def test_confirm_server_refusal_names_the_serving_session_and_the_addressed_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "entries", lambda: iter([(7, {"sessionId": "sid-g"}, "k"), (9, {"sessionId": "sid-h"}, "k")]))
    monkeypatch.setattr(R, "_REACHED", {"pid": 7, "session": "sid-g"})
    with pytest.raises(R.TargetMismatch) as refused:
        R._confirm_server(9)
    assert str(refused.value) == ("target mismatch: endpoint is served by pid 9 (session sid-h), "
                                  "not addressed pid 7 (session sid-g)")
    R._confirm_server(7)
    R._confirm_server(None)

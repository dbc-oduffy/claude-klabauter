"""Fake `codex app-server proxy` for codex_app_server tests (pure Python, any OS).

`fake_app_server.py <scenario>` speaks WebSocket over stdio and answers each client request with
the server lines captured for that scenario in codex_app_server.jsonl (ids rewritten to the
request's id), followed by the notifications captured after that response. Scenario `busy` reports a running turn then accepts `turn/steer`; `busy_stale` refuses the steer (so `turn/start` follows). Scenario `dead` exits
before replying; `drop` answers the handshake then closes the connection.
"""
from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path

CAPTURE = Path(__file__).with_name("codex_app_server.jsonl")
BUSY_TURN = "busy-turn-0001"
SCRIPT = str(Path(__file__).resolve())


def _read(n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sys.stdin.buffer.read(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return buf


def _read_frame() -> bytes:
    b0, b1 = _read(2)
    n = b1 & 0x7F
    if n == 126:
        (n,) = struct.unpack("!H", _read(2))
    elif n == 127:
        (n,) = struct.unpack("!Q", _read(8))
    mask = _read(4)
    return bytes(b ^ mask[i % 4] for i, b in enumerate(_read(n)))


def _send(obj: dict) -> None:
    payload = json.dumps(obj).encode()
    n = len(payload)
    head = bytes([0x81, n]) if n < 126 else bytes([0x81, 126]) + struct.pack("!H", n)
    sys.stdout.buffer.write(head + payload)
    sys.stdout.buffer.flush()


def _replies(scenario: str) -> dict[str, list[dict]]:
    """method -> [response, *notifications that followed it] from the capture."""
    rows = [json.loads(line) for line in CAPTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [r for r in rows if r["scenario"] == scenario]
    out: dict[str, list[dict]] = {}
    for i, row in enumerate(rows):
        msg = row["msg"]
        if row["dir"] != "client" or "id" not in msg:
            continue
        seq: list[dict] = []
        for nxt in rows[i + 1:]:
            if nxt["dir"] == "client":
                break
            seq.append(nxt["msg"])
        out.setdefault(msg["method"], seq)
    return out


def main(argv: list[str]) -> int:
    scenario = argv[1]
    if scenario == "dead":
        return 3
    replies = _replies("owner" if scenario in ("drop", "queue") else "non_owner" if scenario.startswith("busy") else scenario)
    turns_calls = 0
    head = b""
    while not head.endswith(b"\r\n\r\n"):
        head += _read(1)
    sys.stdout.buffer.write(
        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
    )
    sys.stdout.buffer.flush()
    try:
        while True:
            req = json.loads(_read_frame())
            if scenario == "queue":
                wire = os.environ.get("FAKE_CODEX_QUEUE_LOG")
                if wire:
                    with open(wire, "a", encoding="utf-8") as log:
                        log.write(json.dumps(req) + "\n")
                if req.get("method") == "thread/queue/add":
                    _send({"id": req["id"], "result": {"queuedSubmission": {"id": "queued-real-wire"}}})
                    continue
            if "id" not in req:
                continue
            if scenario.startswith("busy") and req["method"] in ("thread/turns/list", "turn/steer"):
                tid = BUSY_TURN
                turns = replies["thread/turns/list"][0]["result"]["data"]
                if req["method"] == "thread/turns/list":
                    turns_calls += 1
                    if turns_calls == 1:
                        _send({"id": req["id"], "result": {"data": [{"id": tid, "status": "inProgress", "items": []}]}})
                    else:
                        _send({"id": req["id"], "result": {"data": [turns[0] if scenario == "busy_stale" else {**turns[0], "id": tid}]}})
                    continue
                _send({"id": req["id"], "result": {"turnId": tid}} if scenario == "busy" else
                      {"id": req["id"], "error": {"code": -32600, "message": "expected turn id mismatch"}})
                if scenario == "busy":
                    for st in ("idle",):
                        _send({"method": "thread/status/changed", "params": {"threadId": req["params"]["threadId"], "status": {"type": st}}})
                continue
            seq = replies.get(req["method"])
            if seq is None:
                _send({"id": req["id"], "error": {"code": -32600, "message": f"unknown {req['method']}"}})
                continue
            _send({**seq[0], "id": req["id"]})
            if scenario == "drop" and req["method"] == "initialize":
                return 0
            for note in seq[1:]:
                _send(note)
    except EOFError:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

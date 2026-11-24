"""Reduce native transport diagnostics to inbound provider completion identities."""
import json
import re
import subprocess
import sys
from pathlib import Path

RECEIVED = re.compile(rb"^\d{4}-\d\d-\d\dT[\d:.]+Z\s+TRACE (?:tungstenite::protocol: Received message |codex_api::sse::responses: SSE event: )(\{.*\})\s*$")
ANSI = re.compile(rb"\x1b\[[0-9;]*m")


def reduce_line(raw):
    if b"response.completed" not in raw:
        return None
    match = RECEIVED.fullmatch(ANSI.sub(b"", raw))
    if match is None:
        return None
    try:
        event = json.loads(match[1])
    except (ValueError, UnicodeError):
        return None
    if not isinstance(event, dict) or event.get("type") != "response.completed":
        return None
    body = event.get("response")
    if not isinstance(body, dict) or not isinstance(body.get("id"), str):
        return None
    model = body.get("model")
    return {"type": "response.completed", "response": {
        "id": body["id"], "model": model if isinstance(model, str) else None}}


def spawn_collector(identity, stdin, stdout):
    """Start this module as the reducer over a codex child's trace stream.

    Lives here, not in workflow.py: the collector is a plain Python parser that never runs codex, so the
    codex spawn-site ratchet (test_every_codex_spawn_site_shaped) must not see it as an unshaped child.
    """
    return subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__)), json.dumps(identity)],
        stdin=stdin, stdout=stdout, close_fds=True,
    )


if __name__ == "__main__":
    identity = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    for line in sys.stdin.buffer:
        result = reduce_line(line)
        if result is not None:
            print(json.dumps(dict(result, **identity)), flush=True)

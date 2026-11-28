"""Reduce native transport diagnostics to inbound terminal response identities and usage."""
import json
import re
import subprocess
import sys
from pathlib import Path

RECEIVED = re.compile(rb"^\d{4}-\d\d-\d\dT[\d:.]+Z\s+TRACE (?:tungstenite::protocol: Received message |codex_api::sse::responses: SSE event: )(\{.*\})\s*$")
ANSI = re.compile(rb"\x1b\[[0-9;]*m")


TERMINAL_EVENTS = ("response.completed", "response.failed", "response.incomplete")
TERMINAL_MARKERS = tuple(event.encode() for event in TERMINAL_EVENTS)


def _pricing_usage(usage):
    details = usage.get("input_tokens_details")
    if not isinstance(details, dict):
        details = {}
    retention = details.get("cache_write_tokens_details")
    if not isinstance(retention, dict):
        retention = {}
    counters = {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cached_input_tokens": details.get("cached_tokens"),
        "cache_write_input_tokens": details.get("cache_write_tokens"),
        "ttl_12h_tokens": retention.get("ttl_12h_tokens"),
    }
    return {name: count for name, count in counters.items() if type(count) is int and count >= 0}


def reduce_line(raw):
    if not any(marker in raw for marker in TERMINAL_MARKERS):
        return None
    match = RECEIVED.fullmatch(ANSI.sub(b"", raw))
    if match is None:
        return None
    try:
        event = json.loads(match[1])
    except (ValueError, UnicodeError):
        return None
    if not isinstance(event, dict) or event.get("type") not in TERMINAL_EVENTS:
        return None
    body = event.get("response")
    if not isinstance(body, dict) or not isinstance(body.get("id"), str):
        return None
    model = body.get("model")
    reduced = {"id": body["id"], "model": model if isinstance(model, str) else None}
    if isinstance(body.get("usage"), dict):
        reduced["pricing_usage"] = _pricing_usage(body["usage"])
    return {"type": event["type"], "response": reduced}


def captured_usage(path, receipt, artifact_dir):
    """Read the completed host capture for this exact worker invocation."""
    with path.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024 or not raw.endswith(b"\n"):
        raise ValueError("provider capture exceeds read budget or has a partial tail")
    rows = [json.loads(line) for line in raw.splitlines()]
    if len(rows) < 2 or any(not isinstance(row, dict) for row in rows):
        raise ValueError("missing provider capture boundaries")
    header, end = rows[0], rows[-1]
    if (header.get("type") != "worker.capture" or header.get("artifact_dir") != str(artifact_dir)
            or header.get("request_id") != receipt["broker_request_id"]
            or end.get("type") != "worker.capture_end" or end.get("collector_exit") != 0):
        raise ValueError("provider capture does not match the completed worker attempt")
    expected = (receipt["broker_fire_id"], receipt["pid"], header["stage"])
    responses = {}
    for row in rows:
        if (row.get("fire_id"), row.get("pid"), row.get("stage")) != expected:
            raise ValueError("provider capture identity mismatch")
    for row in rows[1:-1]:
        response = row.get("response")
        if row.get("type") not in TERMINAL_EVENTS or not isinstance(response, dict):
            raise ValueError("invalid captured provider response")
        uid = response.get("id")
        if not isinstance(uid, str) or not re.fullmatch(r"resp_[A-Za-z0-9_-]+", uid):
            raise ValueError("missing provider response identity")
        if responses.setdefault(uid, response) != response:
            raise ValueError("conflicting provider usage for the same response")
    return responses


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

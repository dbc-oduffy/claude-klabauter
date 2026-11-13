"""Per-host hook shim: `python -m coordinator_core.host_hooks.shim --host codex|agy|grok`.

Reads one hook payload on stdin, adapts it, asks the warm engine under a hard budget, and prints
the host-shaped answer. No engine verdict (down, slow, error) falls to the engine-free backstop,
then (shell commands only) the in-process Claude Bash guard chain; a deny from either is the answer,
otherwise the host's neutral fallback plus one degrade row. Any
exception prints the neutral fallback and exits 0. Cold-start imports: stdlib and host_hooks only;
warm.client and warm.telemetry load lazily.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, Dict, Mapping, Optional

from coordinator_core.host_hooks.contract import HOST_AGY, HOST_CODEX, HOST_GROK, HOSTS, HostEvent, neutral_fallback

#: Hard wall for the engine dial, inside the 200ms process budget.
DIAL_BUDGET_SECS = 0.15

ENGINE_METHOD = "warm_guard.evaluate"

_SESSION_ENV = "COORDINATOR_SESSION_ID"


def _adapter(host: str):
    if host == HOST_CODEX:
        from coordinator_core.host_hooks.codex_adapter import CodexAdapter

        return CodexAdapter()
    if host == HOST_GROK:
        from coordinator_core.host_hooks.grok_adapter import GrokAdapter

        return GrokAdapter()
    if host == HOST_AGY:
        from coordinator_core.host_hooks.agy_adapter import AgyAdapter

        return AgyAdapter()
    raise ValueError("unknown host: %r" % (host,))


def _request(engine_event: Mapping[str, Any]) -> Dict[str, Any]:
    payload = dict(engine_event)
    payload.setdefault("env", {})
    payload.setdefault("permission_mode", None)
    request: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": ENGINE_METHOD,
        "params": {"payload": payload},
    }
    cwd = engine_event.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        request["_origin_worktree"] = cwd
    return request


def _dial(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One warm-socket round trip; the JSON-RPC response, or None on any miss."""
    from coordinator_core.warm import client

    return client.try_warm_dispatch(request, read_deadline_secs=DIAL_BUDGET_SECS, hook_path=True)


def _budgeted(request: Dict[str, Any], budget: float) -> tuple:
    """(response, timed_out). Runs `_dial` on a daemon thread so the wall holds whatever it blocks on."""
    box: Dict[str, Any] = {}

    def _run() -> None:
        try:
            box["response"] = _dial(request)
        except Exception:  # noqa: BLE001 -- a dial failure is a miss
            box["response"] = None

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(budget)
    if worker.is_alive():
        return None, True
    return box.get("response"), False


def _verdict(response: Any) -> Optional[Dict[str, Any]]:
    """The engine's flat verdict (`{}` is a real allow), or None when the response is no verdict."""
    if not isinstance(response, dict) or "error" in response:
        return None
    result = response.get("result")
    return result if isinstance(result, dict) else None


def _degrade(kind_name: str, cause: str) -> None:
    try:
        from coordinator_core.warm import telemetry

        telemetry.record_degrade(kind=getattr(telemetry, kind_name), cause=cause)
    except Exception:  # noqa: BLE001 -- telemetry never breaks the hook
        return


def _cold_shell_verdict(event: HostEvent, engine_event: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The in-process Claude Bash guard chain's deny for a shell command, or None.

    Runs only when the engine gave no verdict, so a cold engine denies a codex or agy shell command the way
    the Claude path does (EM powers; a payload carrying `agent_id` gets the subagent limits). Advisories are
    not forwarded. Non-shell tools and any failure return None.
    """
    if event.tool_name != "Bash":
        return None
    try:
        from coordinator_core.host_hooks.tool_policy import evaluate

        verdict = evaluate(dict(engine_event))
    except Exception:  # noqa: BLE001 -- a raising chain is a miss, never a hook failure
        return None
    return verdict if isinstance(verdict, dict) and verdict.get("permissionDecision") == "deny" else None


def decide(host: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    """The host-shaped answer for one payload. May raise; `run` owns the fail-open."""
    if host not in HOSTS:
        raise ValueError("unknown host: %r" % (host,))
    adapter = _adapter(host)
    event: HostEvent = adapter.adapt_event(payload)
    engine_event = adapter.engine_event(event)
    if engine_event is None:
        return adapter.render(event, {})
    previous = os.environ.get(_SESSION_ENV)
    if event.session_id:
        os.environ[_SESSION_ENV] = event.session_id
    try:
        response, timed_out = _budgeted(_request(engine_event), DIAL_BUDGET_SECS)
    finally:
        if previous is None:
            os.environ.pop(_SESSION_ENV, None)
        else:
            os.environ[_SESSION_ENV] = previous
    verdict = _verdict(response)
    if verdict is not None:
        return adapter.render(event, verdict)
    from coordinator_core.host_hooks import backstop

    denied = backstop.match(event)
    if denied is not None:
        return adapter.render(event, denied)
    cold = _cold_shell_verdict(event, engine_event)
    if cold:
        return adapter.render(event, cold)
    if timed_out:
        _degrade("KIND_HOOK_TIMEOUT", "host_hooks.shim %s: engine dial exceeded %.0fms" % (host, DIAL_BUDGET_SECS * 1000))
    else:
        _degrade("KIND_COLD_RUN", "host_hooks.shim %s: no engine verdict" % host)
    return neutral_fallback(host)


def run(host: str, stdin_text: str) -> Dict[str, Any]:
    """`decide` with every failure turned into the host's neutral fallback."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        return decide(host, payload)
    except Exception:  # noqa: BLE001 -- the shim always answers
        return neutral_fallback(host) if host in HOSTS else {}


def main(argv: Optional[list] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    host = ""
    if "--host" in args:
        index = args.index("--host")
        host = args[index + 1] if index + 1 < len(args) else ""
    try:
        stdin_text = sys.stdin.read()
    except Exception:  # noqa: BLE001
        stdin_text = ""
    answer = run(host, stdin_text)
    sys.stdout.write(json.dumps(answer))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)

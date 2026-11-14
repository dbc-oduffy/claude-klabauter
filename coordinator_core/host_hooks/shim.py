"""Per-host hook shim: `python -m coordinator_core.host_hooks.shim --host codex|agy|grok`.

Reads one hook payload on stdin, adapts it, asks the warm engine under a hard budget, and prints
the host-shaped answer. No engine verdict (down, slow, error) falls to the engine-free backstop,
then the in-process Claude guard chain for shell commands (deny only) and mapped file writes (deny or
advisory); a verdict from either is the answer, otherwise the host's neutral fallback plus one degrade
row. Any exception prints the neutral fallback and exits 0. Cold-start imports: stdlib and host_hooks only;
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


_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch"})


def _cold_verdict(event: HostEvent, engine_event: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The in-process Claude guard chain's answer for a shell command or a mapped file write, or None.

    Runs only when the engine gave no verdict. A shell command forwards a deny only; a mapped write also
    forwards the advisory (`additionalContext`, the DIY nag), so a cold engine treats a codex or agy EM
    like the Claude path. A payload carrying `agent_id` gets the subagent limits: the guards advise it
    nothing. Other tools and any failure return None.
    """
    shell = event.tool_name == "Bash"
    if not shell and event.tool_name not in _WRITE_TOOLS:
        return None
    try:
        from coordinator_core.host_hooks.tool_policy import evaluate

        verdict = evaluate(dict(engine_event))
    except Exception:  # noqa: BLE001 -- a raising chain is a miss, never a hook failure
        return None
    if not isinstance(verdict, dict):
        return None
    if verdict.get("permissionDecision") == "deny":
        return verdict
    return None if shell or not verdict.get("additionalContext") else verdict


def _nag_text(host: str, event: HostEvent, engine_event: Mapping[str, Any]) -> str:
    """The DIY nag for this fire, or "". Only a PreToolUse fire from the EM (no `agent_id`) with a cwd gets one.

    The served host stands in as the caller family when the env names none; the nag's own role check
    still silences a worker or reviewer. Any failure is a miss.
    """
    try:
        if event.event != "PreToolUse" or engine_event.get("agent_id") or not event.cwd or not event.session_id:
            return ""
        env = dict(os.environ)
        if not (env.get("COORDINATOR_AGENT_PROVIDER") or env.get("COORDINATOR_AGENT_VENDOR")):
            env["COORDINATOR_AGENT_PROVIDER"] = host
        from coordinator_core.session import op_dispatch_nag

        return op_dispatch_nag.advisory_for_dispatch(event.cwd, event.session_id, env) or ""
    except Exception:  # noqa: BLE001 -- advisory only
        return ""


def _with_nag(host: str, event: HostEvent, engine_event: Mapping[str, Any], verdict: Dict[str, Any]) -> Dict[str, Any]:
    """`verdict` with the DIY nag merged into its `additionalContext`; a deny comes back untouched."""
    if verdict.get("permissionDecision") == "deny" or verdict.get("decision") == "deny":
        return verdict
    nested = verdict.get("hookSpecificOutput")
    if isinstance(nested, Mapping) and nested.get("permissionDecision") == "deny":
        return verdict
    nag = _nag_text(host, event, engine_event)
    if not nag:
        return verdict
    merged = dict(verdict)
    if isinstance(nested, Mapping) and nested.get("additionalContext"):
        merged["hookSpecificOutput"] = {**nested, "additionalContext": "%s\n%s" % (nested["additionalContext"], nag)}
        return merged
    existing = verdict.get("additionalContext")
    merged["additionalContext"] = "%s\n%s" % (existing, nag) if isinstance(existing, str) and existing else nag
    return merged


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
        return adapter.render(event, _with_nag(host, event, engine_event, verdict))
    from coordinator_core.host_hooks import backstop

    denied = backstop.match(event)
    if denied is not None:
        return adapter.render(event, denied)
    cold = _cold_verdict(event, engine_event)
    if cold:
        return adapter.render(event, _with_nag(host, event, engine_event, cold))
    if timed_out:
        _degrade("KIND_HOOK_TIMEOUT", "host_hooks.shim %s: engine dial exceeded %.0fms" % (host, DIAL_BUDGET_SECS * 1000))
    else:
        _degrade("KIND_COLD_RUN", "host_hooks.shim %s: no engine verdict" % host)
    nag = _nag_text(host, event, engine_event)
    return adapter.render(event, {"additionalContext": nag}) if nag else neutral_fallback(host)


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

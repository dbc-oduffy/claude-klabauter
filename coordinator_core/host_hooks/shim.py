"""Per-host hook shim: `python -m coordinator_core.host_hooks.shim --host codex|agy|grok`.

Reads one hook payload on stdin, adapts it, asks the warm engine under a hard budget, and prints
the host-shaped answer. No engine verdict (down, slow, error) falls to the engine-free backstop,
then the in-process guards: the deny-class `cold_chain` for shell commands and `apply_patch` (deny only),
the Claude write chain for other mapped file writes (deny or advisory); a verdict from either is the
answer, otherwise the host's neutral fallback plus one degrade row. Any exception prints the neutral
fallback and exits 0. Cold-start imports: stdlib and host_hooks only;
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
    payload["trace_guards"] = True
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


_SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch"})


def _cold_verdict(event: HostEvent, engine_event: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The in-process guard answer for a shell command or a mapped file write, or None.

    Runs only when the engine gave no verdict. A shell command and an `apply_patch` go through
    `cold_chain`, the deny-class-only chain that fits the host guard's wall; they forward a deny only.
    Any other mapped write runs the full Claude write chain and also forwards the advisory
    (`additionalContext`, the DIY nag), so a cold engine treats a codex or agy EM like the Claude path.
    A chain that ran and did not deny answers `{}`, an allow; None means the chain did not run (other
    tools, a raise, a non-dict answer). A payload carrying `agent_id` gets the subagent limits: the
    guards advise it nothing.
    """
    shell = event.tool_name in _SHELL_TOOLS
    if not shell and event.tool_name not in _WRITE_TOOLS:
        return None
    if shell or event.tool_name == "apply_patch":
        try:
            from coordinator_core.host_hooks import cold_chain

            answer = cold_chain.evaluate(dict(engine_event))
        except Exception:  # noqa: BLE001 -- a raising chain is a miss, never a hook failure
            return None
        if not isinstance(answer, dict):
            return None
        return answer if answer.get("permissionDecision") == "deny" else {}
    try:
        from coordinator_core.host_hooks.tool_policy import evaluate

        verdict = evaluate(dict(engine_event))
    except Exception:  # noqa: BLE001 -- a raising chain is a miss, never a hook failure
        return None
    if not isinstance(verdict, dict):
        return None
    if verdict.get("permissionDecision") == "deny":
        return verdict
    return verdict if verdict.get("additionalContext") else {}


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

        nag = op_dispatch_nag.advisory_for_dispatch(event.cwd, event.session_id, env) or ""
        if nag:
            _pending[:] = [(event.cwd, event.session_id, nag)]
        return nag
    except Exception:  # noqa: BLE001 -- advisory only
        return ""


#: The last nag `_nag_text` produced: (repo_root, session_id, text). Not yet on the wire, so not yet stamped.
_pending: list = []


def take_pending_nag() -> Optional[tuple]:
    """The nag merged into the last answer as (repo_root, session_id, text), cleared; None if none."""
    taken = _pending[0] if _pending else None
    del _pending[:]
    return taken


#: The last `decide`'s evaluation, one dict: route, guards_evaluated, verdicts (ordered [{"guard", "verdict"}]),
#: chain, backstop_class. Read only through `take_evaluation`, which a caller clears before `run`: a bare read
#: would see a previous or timed-out call's.
_evaluation: list = []

CHAIN_FULL = "full"
CHAIN_BACKSTOP_ONLY = "backstop-only"
#: Cold-chain guards whose deny the backstop's own classifiers also reach (`backstop.degrade_match`): a deny by
#: one, or by the backstop itself, carries backstop_class true.
_BACKSTOP_CLASS_GUARDS = frozenset({"destructive-rm", "destructive-git-clean", "block-stash-destruction"})


def take_evaluation() -> Optional[Dict[str, Any]]:
    """The last `decide`'s {route, guards_evaluated, verdicts, chain, backstop_class}, cleared; None if no `decide` reached an evaluation.

    route is "warm" iff the dial returned the engine's verdict, else "cold". guards_evaluated names the
    backstop and every cold_chain guard that ran; a warm answer names the same set with verdict "engine"
    (the warm engine decided; per-guard outcomes are not reported), so warm and cold lists diff by name.
    verdicts carries each outcome in run order. chain is reach only, on both routes: "full" iff evaluation
    reached a guard beyond the backstop (a first-deny short circuit still qualifies), "backstop-only" iff only
    the backstop was evaluated. backstop_class is true iff a deny came from the backstop or from a guard the
    backstop classifiers also reach (`_BACKSTOP_CLASS_GUARDS`); it never changes chain. On the warm route
    the denier is not reported, so it is true iff the engine denied and a class guard was in reach.
    """
    taken = _evaluation[0] if _evaluation else None
    del _evaluation[:]
    return taken


def _note(route: str, verdicts: Optional[list] = None, backstop_class: bool = False) -> None:
    rows = [{"guard": str(g), "verdict": str(v)} for g, v in (verdicts or [])]
    reached = any(not r["guard"].startswith("backstop") for r in rows)
    _evaluation[:] = [{"route": route, "guards_evaluated": [r["guard"] for r in rows], "verdicts": rows,
                       "chain": CHAIN_FULL if reached else CHAIN_BACKSTOP_ONLY, "backstop_class": backstop_class}]


def _chain_trace(event: HostEvent, engine_event: Mapping[str, Any], answer: Any) -> list:
    """Ordered (guard, verdict) pairs for the in-process guards `_cold_verdict` just ran for this event."""
    if event.tool_name == "Bash" or event.tool_name == "apply_patch":
        try:
            from coordinator_core.host_hooks import cold_chain

            traced = cold_chain.take_trace()
            if traced:
                return traced
        except Exception:  # noqa: BLE001 -- telemetry never breaks the hook
            pass
        names = _chain_guards(event, engine_event)
        return [(n, "unrecorded") for n in names]
    if event.tool_name in _WRITE_TOOLS:
        deny = isinstance(answer, Mapping) and answer.get("permissionDecision") == "deny"
        return [("tool_policy", "deny" if deny else "allow")]
    return []


def _chain_guards(event: HostEvent, engine_event: Mapping[str, Any]) -> list:
    """Names of the in-process guards `_cold_verdict` runs for this event."""
    if event.tool_name != "Bash" and event.tool_name not in _WRITE_TOOLS:
        return []
    try:
        from coordinator_core.host_hooks import cold_chain

        tool_input = engine_event.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, Mapping) else None
        if event.tool_name == "Bash":
            return cold_chain.shell_candidates(command, subagent=bool(engine_event.get("agent_id"))) \
                if isinstance(command, str) else []
        if event.tool_name == "apply_patch":
            ran = isinstance(command, str) and any(m in command for m in cold_chain._PATCH_MARKERS)
            return list(cold_chain.PATCH_DENY_GUARDS) if ran else []
    except Exception:  # noqa: BLE001 -- telemetry never breaks the hook
        return []
    return ["tool_policy"]


def _carries(obj: Any, text: str) -> bool:
    if isinstance(obj, str):
        return text in obj
    if isinstance(obj, Mapping):
        return any(_carries(v, text) for v in obj.values())
    return False


def mark_if_delivered(pending: Optional[tuple], answer: Any) -> None:
    """Stamp the nag window once the writer's answer, already written and flushed, still carries the nag."""
    try:
        if pending and _carries(answer, pending[2]):
            from coordinator_core.session import op_dispatch_nag

            op_dispatch_nag.mark_nag_emitted(pending[0], pending[1])
    except Exception:  # noqa: BLE001 -- stamping never breaks the hook
        return


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
    _note("cold")
    if engine_event is None:
        return adapter.render(event, {})
    if host == HOST_CODEX:
        from coordinator_core.host_hooks import branch_creation

        refused = branch_creation.match(event)
        if refused is not None:
            return adapter.render(event, refused)
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
        verdict = dict(verdict)
        named = verdict.pop("guards_evaluated", None)
        warm_guards = [str(g) for g in named] if isinstance(named, list) and named else _chain_guards(event, engine_event)
        nested = verdict.get("hookSpecificOutput")
        warm_deny = "deny" in (verdict.get("permissionDecision"), verdict.get("decision"),
                               nested.get("permissionDecision") if isinstance(nested, Mapping) else None)
        _note("warm", [(g, "engine") for g in ["backstop"] + warm_guards],
              warm_deny and any(g in _BACKSTOP_CLASS_GUARDS for g in warm_guards))
        return adapter.render(event, _with_nag(host, event, engine_event, verdict))
    from coordinator_core.host_hooks import backstop

    denied = backstop.match(event)
    rows = [("backstop", "allow" if denied is None else "deny")]
    if denied is not None:
        rows += [("backstop:%s" % g, v) for g, v in backstop.take_trace() if v == "deny"]
    _note("cold", rows, denied is not None)
    if denied is not None:
        return adapter.render(event, denied)
    # Trap: the nag must answer before the cold guard chain. That chain costs ~800ms of process time, so
    # under the host guard's 500ms wall it was killed every time and the nag never reached any non-Claude EM.
    # The backstop above still holds the irreversible denies, and no other guard may deny recoverable work.
    nag = _nag_text(host, event, engine_event)
    if nag:
        return adapter.render(event, {"additionalContext": nag})
    cold = _cold_verdict(event, engine_event)
    if cold is not None:
        trace = _chain_trace(event, engine_event, cold)
        denier = next((g for g, v in trace if v == "deny"), None)
        _note("cold", [("backstop", "allow")] + trace, denier in _BACKSTOP_CLASS_GUARDS)
    if cold:
        return adapter.render(event, _with_nag(host, event, engine_event, cold))
    if timed_out:
        _degrade("KIND_HOOK_TIMEOUT", "host_hooks.shim %s: engine dial exceeded %.0fms" % (host, DIAL_BUDGET_SECS * 1000))
    else:
        _degrade("KIND_COLD_RUN", "host_hooks.shim %s: no engine verdict" % host)
    if cold is not None:
        return adapter.render(event, cold)
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
    take_pending_nag()
    answer = run(host, stdin_text)
    pending = take_pending_nag()
    sys.stdout.write(json.dumps(answer))
    sys.stdout.flush()
    mark_if_delivered(pending, answer)
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)

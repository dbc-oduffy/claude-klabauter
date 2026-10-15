"""coordinator_core.hooks.submit_bug_blitz — PreToolUse(Workflow): the EM's submission of a bug blitz
IS the running Workflow.

The EM's one act is `Workflow({name: "coordinator:bug-blitz", args: {targets: "<red-set dir | failing-tests
file | backlog>"}})`. The harness resolves `name` to the plugin's `workflows/bug-blitz.js` stub before any
PreToolUse hook runs; this hook then parses the targets, mints one TF row per failing test file, emits the
bug grind through `dispatch.emit`'s queue route (run token, receipt, script), writes `decisions.json`
beside the receipt and answers allow + `updatedInput` = `{scriptPath, args}`, so the call the EM made
launches the emitted run in the background. Its completion notification is the EM's one wake.

`args` shape: `{targets: str}` plus optional `appetite`, `limit` (int), `budget_tokens` (int). Any other key
denies. `targets` names no suite tier and carries no Tier-U decision: verification runs through the
targeted test runner, so no grant record is ever written here.

Negative spec: never launches a process — the harness runs the rewritten call. Never writes a Tier-U grant.
Never denies a Workflow call that does not name the stub. Never runs on main, the default branch or a
detached HEAD.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from coordinator_core.bug_blitz.contract import (
    STUB_NAME,
    MintRows,
    ParseResult,
    ParseTargets,
    TargetSpec,
    run_tag,
)
from coordinator_core.hooks._envelope import deny, no_advisory, payload_of, rewrite_input
from coordinator_core.ipc import register_op

GUARD_NAME = "submit-bug-blitz"
SUBMIT_NAME = f"coordinator:{STUB_NAME}"
PROFILE = "bug"
QUEUE_DIR = "state/bug-backlog"
SCRATCH_ROOT = "state/scratch/bug-blitz"
BACKLOG_TARGET = "backlog"
DECISIONS_FILE = "decisions.json"
_ALLOWED_ARGS = frozenset({"targets", "appetite", "limit", "budget_tokens"})
_ARGS_SHAPE = 'args: {targets: "<red-set dir | failing-tests file | backlog>"}, optional appetite, limit, budget_tokens'

EmitFn = Callable[[dict, Path], dict]
ResolveFn = Callable[[str, Path], TargetSpec]


def is_submission(payload: Mapping[str, Any]) -> bool:
    """True for a Workflow call naming the stub, bare or plugin-namespaced."""
    if payload.get("tool_name") != "Workflow":
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    name = tool_input.get("name")
    return isinstance(name, str) and (name == STUB_NAME or name.endswith(":" + STUB_NAME))


def _refuse(text: str) -> dict:
    return deny("PreToolUse", f"{text}\nSubmit shape: Workflow({{name: \"{SUBMIT_NAME}\", {_ARGS_SHAPE}}}).")


def _check_args(args: Any) -> tuple[Optional[dict], Optional[str]]:
    """(validated args, refusal)."""
    if isinstance(args, str):
        args = {"targets": args}
    if not isinstance(args, dict):
        return None, "args must be an object carrying targets."
    extra = sorted(set(args) - _ALLOWED_ARGS)
    if extra:
        return None, f"unknown args: {', '.join(extra)}."
    targets = args.get("targets")
    if not isinstance(targets, str) or not targets.strip():
        return None, "targets is required."
    appetite = args.get("appetite")
    if appetite is not None and not (isinstance(appetite, str) and appetite.strip()):
        return None, "appetite must be a non-empty string."
    for key in ("limit", "budget_tokens"):
        value = args.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            return None, f"{key} must be a positive integer."
    return {**args, "targets": targets.strip()}, None


def _branch_refusal(root: Path) -> Optional[str]:
    from coordinator_core.daily_branch import is_allowed_branch, read_configured_day_branch
    from coordinator_core.git.git_state import head_branch

    branch = head_branch(root)
    if not branch or branch == "HEAD":
        return "HEAD is detached. Check out the day branch."
    if branch.lower() == "main" or not is_allowed_branch(branch, read_configured_day_branch(root)):
        return f"{branch} is not a work branch. Check out the day branch."
    return None


def _default_resolve(raw: str, root: Path) -> TargetSpec:
    from coordinator_core.bug_blitz.evidence import resolve_target

    return resolve_target(raw, root)


def _default_parse(spec: TargetSpec) -> ParseResult:
    from coordinator_core.bug_blitz.evidence import parse_targets

    return parse_targets(spec)


def _default_mint(root: Path, run_id: str, tests: tuple, *, target_ref: str) -> list[str]:
    from coordinator_core.bug_blitz.mint import mint_tf_rows

    return mint_tf_rows(root, run_id, tests, target_ref=target_ref)


def _default_emit(params: dict, repo_root: Path) -> dict:
    from coordinator_core.ops.dispatch_emit.op import _dispatch_emit

    return _dispatch_emit(params, repo_root)


def _profile_dir() -> str:
    from coordinator_core.warm.caller_context import resolve_caller_context

    plugin_root = resolve_caller_context().plugin_root
    if not plugin_root:
        raise ValueError("the coordinator plugin root did not resolve; the bug profile is unreachable")
    return str(Path(plugin_root) / "queue-profiles")


def _repo_root(cwd: object) -> Optional[Path]:
    from coordinator_core.git.repo_root import show_toplevel

    top = show_toplevel(cwd if isinstance(cwd, str) and cwd else None)
    return Path(top) if top else None


def _write_decisions(run_dir: Path, parsed: Optional[ParseResult], minted: list[str], target_ref: str) -> None:
    record = {
        "commit_authority": {"basis": "submission"},
        "suite": "not-run (targeted-runner)",
        "verify": "targeted-tests",
        "target": target_ref,
        "parse": (
            {
                "parsed": len(parsed.tests),
                "unparsed_count": parsed.unparsed_count,
                "unparsed_sample": list(parsed.unparsed_sample),
            }
            if parsed is not None
            else None
        ),
        "minted": minted,
    }
    (run_dir / DECISIONS_FILE).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def evaluate(
    payload: Mapping[str, Any],
    *,
    parse: Optional[ParseTargets] = None,
    mint: Optional[MintRows] = None,
    emit: Optional[EmitFn] = None,
    resolve: Optional[ResolveFn] = None,
    run_id: Optional[str] = None,
) -> dict:
    """The PreToolUse envelope for a Workflow call: the fire rewrite for a submission, else nothing."""
    if not is_submission(payload):
        return no_advisory()
    if payload.get("agent_id"):
        return deny("PreToolUse", "Only the EM submits a bug blitz; return it in your report.")

    args, refusal = _check_args(payload["tool_input"].get("args"))
    if refusal or args is None:
        return _refuse(refusal or "args malformed.")

    root = _repo_root(payload.get("cwd"))
    if root is None:
        return _refuse(f"{payload.get('cwd') or 'the session cwd'} is in no git repo.")
    branch_refusal = _branch_refusal(root)
    if branch_refusal:
        return _refuse(branch_refusal)

    stamp = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target_raw = args["targets"]
    parsed: Optional[ParseResult] = None
    minted: list[str] = []
    overrides: dict = {}
    if target_raw != BACKLOG_TARGET:
        try:
            spec = (resolve or _default_resolve)(target_raw, root)
            parsed = (parse or _default_parse)(spec)
            if not parsed.tests:
                raise ValueError("no failing test parsed")
            minted = (mint or _default_mint)(root, stamp, parsed.tests, target_ref=target_raw)
        except Exception as exc:  # the parse and mint refusal text is the whole message
            return _refuse(f"targets {target_raw}: {exc}")
        overrides["where"] = [[["status", "==", "open"], ["tags", "contains", run_tag(stamp)]]]
    for key in ("limit", "budget_tokens"):
        if args.get(key) is not None:
            overrides[key] = args[key]
    appetite = args.get("appetite") or ("sweep" if minted else "standard")

    out_path = str(root / SCRATCH_ROOT / stamp / "blitz.workflow.mjs")
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    params: dict = {
        "queue": [QUEUE_DIR],
        "profile": PROFILE,
        "profile_dir": _profile_dir(),
        "appetite": appetite,
        "output_path": out_path,
        "session_id": session_id,
    }
    if overrides:
        params["overrides"] = overrides
    try:
        result = (emit or _default_emit)(params, root)
    except Exception as exc:  # the engine's refusal text is the whole message
        return _refuse(f"The engine refused the submission: {exc}")

    script = result.get("path") if isinstance(result, dict) else None
    if not script or result.get("ok") is False:
        detail = result.get("findings") if isinstance(result, dict) else result
        return _refuse(f"The engine emitted no run: {detail}")

    try:
        _write_decisions(Path(script).parent, parsed, minted, target_raw)
    except OSError as exc:
        return _refuse(f"decisions.json not written: {exc}")

    new_input = {
        "scriptPath": str(script),
        "args": {
            "run_stamp": stamp,
            "script_path": str(script),
            "profile_dir": params["profile_dir"],
            "repo_root": root.as_posix(),
        },
    }
    envelope = rewrite_input(
        "PreToolUse",
        new_input,
        f"Submitted to the engine as run {stamp}. It runs to its commits in the background; its "
        "completion notification is your one wake. End the turn: no polling, dispatch or "
        "verification meanwhile.",
    )
    hso = envelope["hookSpecificOutput"]
    hso["permissionDecision"] = "allow"
    hso["permissionDecisionReason"] = f"Bug blitz submission: emitted {Path(script).name} this call."
    return envelope


@register_op("hooks.submit_bug_blitz")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Workflow) door onto `evaluate`; a deny resolves at the guard-level policy point."""
    payload = payload_of(params)
    from coordinator_core.machine_profile import apply_guard_level

    return apply_guard_level(GUARD_NAME, evaluate(payload)) or no_advisory()


__all__ = ["GUARD_NAME", "SUBMIT_NAME", "evaluate", "is_submission"]

"""coordinator_core.execute_plan_assemble.apply — the MUTATING half of the
`/execute-plan` Phase-1 pre-execution chain `pre_execution.py` (C1) emits.

A CLOSED `_CLI_DISPATCH` table, resolved through `apply_base.resolve_cli`,
dispatched by `apply_base.execute_directives` — the shared directive-
execution engine `pickup_assemble`/`baton_assemble`/`merge_assemble` already
compose (module docstrings § "the compute/apply split"). No new runner: this
module supplies only its own four handlers plus the composition-budget
arming apply_base's every directive-executing caller performs.

Spec: docs/plans/2026-09-11-the-execute-plan-pre-execution-chain-emi.md, C2

Handler shapes, one per directive `pre_execution_directives()` ever emits:
    d1 `pickup-assemble stamp-check <plan-path>` — IN-PROCESS. Calls
       `coordinator_core.pickup_assemble.stamp_check.stamp_check()` directly
       (no spawn — the function is in-process in this repo and returns
       exactly the verdict the halt decision needs); raises on a
       `stale-substantive` verdict ONLY. `match` ("fresh"), `stale-
       bookkeeping`, and a business-fail carrying no
       `execution_authorized_sha` all proceed. Because d1 is first in
       `pre_execution_directives()`'s order, a raise here means d2 never
       dispatches (`apply_base.execute_directives` already stops the run on
       a handler raise, `APPLY_EXIT_PARTIAL_MUTATION` — no halt machinery is
       written here, only this verdict-to-raise mapping).
    d2 `review-exec-auth-stamp authorize-invocation ...` — launcher-spawn
       shape, mirroring `baton_assemble.apply._dispatch_session_claim_cli`:
       `sys.executable` + the resolved `coordinator/bin/*.py`, argv as a
       list, never a shell string.
    d3 `session-claim-cli claim-plan <slug> --for-execution` — same
       launcher-spawn shape as d2.
    d4 the workflow emit — the one piece of genuinely new mechanism.
       `emit-dispatch-workflow.py` is plugin-local in DoE-claude with ZERO
       claude-klabauter launchers, so it cannot be a bare `cli:` string; this handler
       resolves the DoE-claude sibling root itself via
       `coordinator_core.ops.coordinator_doe_root.coordinator_doe_root()`
       and spawns that script directly. `--out` is EXPLICIT and
       session-scoped (never the emitter's plan-relative default — see
       state/memo-outbox/sent/emitter-repo-root-guard-landed-and-a-
       script-path-collision.md § 3), built from the session id this
       module's own `apply()` resolves via `apply_base.session_identity`.
       Raises (rather than skipping) when `coordinator_doe_root()` returns
       `None`.

Negative-spec:
    - Do NOT import or call
      `coordinator_core.ops.dispatch_emit.emit.emit_script` or the
      `dispatch.emit` op anywhere in this module — the emit leg goes
      through DoE's own `emit-dispatch-workflow.py` script, never the
      native op (this is what keeps DoE-claude's auto-fired
      `block-workflow-foreign-emission.py` able to do its job; see the
      plan's Anti-scope and C3's boundary test).
    - Do NOT add `mint-deliverable-id`/`advance-tracker-status` (or any
      other verb) to `_CLI_DISPATCH` — `mint-deliverable-id` and the
      tracker advance belong to other ceremonies (`roadmap-planning`,
      `enrich-and-review`), not `/execute-plan`'s own SKILL.md, and firing
      either from here would fire another ceremony's step out of its own
      ceremony.
    - Do NOT resolve `cli` via `getattr`/`importlib`/any directive-derived
      string — every `_CLI_DISPATCH` key is a literal written here by hand.
    - Do NOT auto-dispatch d3/d4 past an undispositioned or declined
      judgment point — `apply_base.execute_directives` already halts on
      that (`APPLY_EXIT_HALTED_AT_JUDGMENT`); this module passes C1's
      `judgment_points` straight through and owns neither the gate list nor
      the halting.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.contract import apply_base
from coordinator_core.execute_plan_assemble.pre_execution import pre_execution_directives
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.ops.coordinator_doe_root import _REMEDIATION, coordinator_doe_root
from coordinator_core.pickup_assemble.stamp_check import stamp_check
from coordinator_core.telemetry.composition_record import (
    flush_composition_record,
    make_fleet_budget,
)
from coordinator_core.win_portability import no_console_creationflags

# ---------------------------------------------------------------------------
# Exit-code contract — composed from apply_base, shared by every apply/
# dispatch half. NOT inherited from any brief-shaped 0/1/2/3 contract; this
# module has no `brief()` half at all.
# ---------------------------------------------------------------------------
APPLY_EXIT_OK = apply_base.APPLY_EXIT_OK
APPLY_EXIT_HALTED_AT_JUDGMENT = apply_base.APPLY_EXIT_HALTED_AT_JUDGMENT
APPLY_EXIT_CLAIM_DENIED = apply_base.APPLY_EXIT_CLAIM_DENIED
APPLY_EXIT_TRANSPORT_FAIL = apply_base.APPLY_EXIT_TRANSPORT_FAIL
APPLY_EXIT_PARTIAL_MUTATION = apply_base.APPLY_EXIT_PARTIAL_MUTATION

UnrecognizedDirective = apply_base.UnrecognizedDirective

_NO_CONSOLE = no_console_creationflags()

#: `coordinator/bin/` under THIS engine clone — resolved from this module's
#: own file location, never from a target repo's `repo_root`.
_BIN_DIR = Path(__file__).resolve().parents[2] / "coordinator" / "bin"


def _run_bin_py(script_name: str, args: list[str], repo_root: Path) -> subprocess.CompletedProcess:
    """Launcher-spawn shape for an EXISTING `coordinator/bin/<script_name>.py`
    entrypoint — `sys.executable` (never a bare `python`/`python3`
    bareword), a literal argv list, `cwd=repo_root`. Mirrors
    `baton_assemble.apply._dispatch_session_claim_cli`."""
    script = _BIN_DIR / f"{script_name}.py"
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        creationflags=_NO_CONSOLE,
    )


def _dispatch_pickup_assemble(args: list[str], repo_root: Path) -> dict[str, Any]:
    """d1 — `pickup-assemble stamp-check <plan-path>`. IN-PROCESS, no
    subprocess: calls `pickup_assemble.stamp_check.stamp_check()` directly
    and reads its `verdict`. Raises on `stale-substantive` ONLY; `match`,
    `stale-bookkeeping`, and a business-fail carrying no
    `execution_authorized_sha` all proceed — the skill states there is
    nothing to compare yet in that last case."""
    if not args or args[0] != "stamp-check":
        raise UnrecognizedDirective(f"pickup-assemble: unrecognized verb {args[:1]!r}")
    if len(args) != 2:
        raise UnrecognizedDirective("pickup-assemble stamp-check: expected 1 argument")
    plan_path = args[1]
    _exit_code, gate = stamp_check(plan_path, repo_root=repo_root)
    verdict = gate.get("verdict")
    if verdict == "stale-substantive":
        raise RuntimeError(
            f"pickup-assemble stamp-check {plan_path}: stale-substantive — "
            f"{gate.get('next_move', 'surface to the PM before proceeding')}"
        )
    return {"cli": "pickup-assemble", "verb": "stamp-check", "plan_path": plan_path, "gate": gate}


def _dispatch_review_exec_auth_stamp(args: list[str], repo_root: Path) -> dict[str, Any]:
    """d2 — `review-exec-auth-stamp authorize-invocation <plan-path>
    --typed-command /execute-plan`. Launcher-spawn shape (§ `_run_bin_py`)."""
    if not args or args[0] != "authorize-invocation":
        raise UnrecognizedDirective(f"review-exec-auth-stamp: unrecognized verb {args[:1]!r}")
    proc = _run_bin_py("review-exec-auth-stamp", args, repo_root)
    if proc.returncode != 0:
        raise RuntimeError(
            f"review-exec-auth-stamp {args}: failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return {"cli": "review-exec-auth-stamp", "args": list(args)}


def _dispatch_session_claim_cli(args: list[str], repo_root: Path) -> dict[str, Any]:
    """d3 — `session-claim-cli claim-plan <slug> --for-execution`.
    Launcher-spawn shape (§ `_run_bin_py`); mirrors
    `baton_assemble.apply._dispatch_session_claim_cli`."""
    if not args or args[0] != "claim-plan":
        raise UnrecognizedDirective(f"session-claim-cli: unrecognized verb {args[:1]!r}")
    proc = _run_bin_py("session-claim-cli", args, repo_root)
    if proc.returncode != 0:
        raise RuntimeError(
            f"session-claim-cli {args}: failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return {"cli": "session-claim-cli", "args": list(args)}


def _emit_out_path(repo_root: Path, plan_path: str) -> Path:
    """Builds `<plan-basename>.<session-id>.workflow.mjs` under a
    session-scoped directory of `repo_root`, per the sent memo's adopted
    remedy (state/memo-outbox/sent/emitter-repo-root-guard-landed-and-a-
    script-path-collision.md § 3): the emitter's own `--out` default is a
    deterministic function of the plan basename alone, so two sessions
    emitting the same plan would overwrite each other absent an explicit,
    session-differentiated `--out`. Reads the session id off the ACTIVE
    `apply_base.session_identity()` context this module's own `apply()`
    entered — never `os.environ` directly (two overlapping warm dispatches
    must not share this value)."""
    scoped = apply_base.current_session_env()
    session_id = None
    for var in apply_base.SESSION_ENV_VARS:
        val = scoped.get(var)
        if val:
            session_id = val
            break
    if not session_id:
        raise apply_base.NoResolvableSessionId(
            "emit-dispatch-workflow: no session id in scope to build a "
            "collision-free --out path"
        )
    basename = f"{Path(plan_path).stem}.{session_id}.workflow.mjs"
    return repo_root / ".coordinator-local" / "subagent-share" / basename


def _dispatch_emit_dispatch_workflow(args: list[str], repo_root: Path) -> dict[str, Any]:
    """d4 — the workflow emit. `emit-dispatch-workflow.py` has ZERO claude-klabauter
    launchers (plugin-local in DoE-claude), so it cannot be a bare `cli:`
    string; this handler resolves the DoE-claude sibling root itself and
    spawns that script directly — never `dispatch.emit`/`emit_script` (see
    this module's own negative-spec)."""
    if not args or args[0] != "--plan":
        raise UnrecognizedDirective(f"emit-dispatch-workflow: unrecognized args {args!r}")
    if len(args) != 2:
        raise UnrecognizedDirective("emit-dispatch-workflow: expected --plan <plan-path>")
    plan_path = args[1]

    doe_root = coordinator_doe_root()
    if doe_root is None:
        raise RuntimeError(_REMEDIATION)

    script = Path(doe_root) / "coordinator" / "bin" / "emit-dispatch-workflow.py"
    out_path = _emit_out_path(repo_root, plan_path)
    argv = [
        sys.executable,
        str(script),
        "--plan",
        plan_path,
        "--repo-root",
        str(repo_root),
        "--out",
        str(out_path),
    ]
    proc = subprocess.run(argv, cwd=repo_root, capture_output=True, text=True, creationflags=_NO_CONSOLE)
    if proc.returncode != 0:
        raise RuntimeError(
            f"emit-dispatch-workflow --plan {plan_path}: failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    return {"cli": "emit-dispatch-workflow", "args": list(args), "out": str(out_path)}


#: THE closed dispatch table — every key a literal string written here by
#: hand. Its key set is exactly the four Phase-1 verbs
#: `pre_execution_directives()` ever emits, and in particular contains
#: neither `mint-deliverable-id` nor `advance-tracker-status` (§ module
#: docstring negative-spec; C3 pins this from outside).
_CLI_DISPATCH: dict[str, Callable[[list[str], Path], dict[str, Any]]] = {
    "pickup-assemble": _dispatch_pickup_assemble,
    "review-exec-auth-stamp": _dispatch_review_exec_auth_stamp,
    "session-claim-cli": _dispatch_session_claim_cli,
    "emit-dispatch-workflow": _dispatch_emit_dispatch_workflow,
}


def _resolve_cli(cli_name: str) -> Callable[[list[str], Path], dict[str, Any]]:
    """The one seam `directives[].cli` ever passes through. Closed over a
    literal dict — an unrecognized name raises before any directive in the
    run has executed."""
    return apply_base.resolve_cli(_CLI_DISPATCH, cli_name)


def _resolve_repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """Resolves the enclosing git worktree root via
    `coordinator_core.git.repo_root.show_toplevel` — the shared cwd-keyed
    memoized resolution seam every other assembler's own `resolve_repo_root`
    wraps. Returns `None` on any failure rather than raising."""
    cwd = start or Path.cwd()
    top = show_toplevel(str(cwd))
    return Path(top) if top else None


def apply(
    plan_path: str,
    *,
    autonomous: bool = False,
    session_id: Optional[str] = None,
    repo_root: Optional[Path] = None,
    decisions: Optional[dict[str, Any]] = None,
) -> tuple[int, dict[str, Any]]:
    """`apply <plan-path> [--autonomous] [--session-id <id>] [--decisions
    <json>]` — computes `pre_execution_directives(plan_path, autonomous=...)`
    and executes it through the closed dispatch table via
    `apply_base.execute_directives`. Returns `(exit_code, report)`.

    An undispositioned (or DECLINED) judgment point halts d3/d4 dispatch and
    returns `APPLY_EXIT_HALTED_AT_JUDGMENT` — d1/d2 (when not `autonomous`)
    still dispatch in that same run, since they carry no `depends_on`."""
    root = repo_root or _resolve_repo_root()
    if root is None:
        return APPLY_EXIT_TRANSPORT_FAIL, {"error": "could not resolve a git worktree root"}

    resolved_sid = apply_base.resolve_explicit_session_id(session_id)
    if resolved_sid is None:
        return APPLY_EXIT_TRANSPORT_FAIL, {
            "error": (
                "no session id resolvable via --session-id or "
                f"{'/'.join(apply_base.SESSION_ENV_READ_ORDER)} — refusing the "
                "ambient tier-4 sentinel"
            ),
        }

    directives, judgment_points = pre_execution_directives(plan_path, autonomous=autonomous)

    composition_budget = make_fleet_budget("execute_plan_assemble")
    outcome = "directive_failed"
    with apply_base.session_identity(resolved_sid):
        try:
            exit_code, report = apply_base.execute_directives(
                directives,
                judgment_points,
                root,
                _CLI_DISPATCH,
                decisions=decisions,
                composition_budget=composition_budget,
            )
            if exit_code == APPLY_EXIT_OK:
                outcome = "success"
            elif exit_code == APPLY_EXIT_PARTIAL_MUTATION:
                outcome = "partial_mutation"
            elif exit_code == APPLY_EXIT_HALTED_AT_JUDGMENT:
                outcome = "halted_at_judgment"
        finally:
            flush_composition_record(composition_budget, outcome)

    return exit_code, report


def main(argv: list[str]) -> int:
    """`execute-plan-assemble apply <plan-path> [--autonomous]
    [--session-id <id>]`"""
    import json

    if not argv or argv[0] in ("--help", "-h"):
        print(
            "usage: execute-plan-assemble apply <plan-path> [--autonomous] "
            "[--session-id <id>]",
            file=sys.stderr if argv else sys.stdout,
        )
        return apply_base.APPLY_EXIT_OK if argv and argv[0] in ("--help", "-h") else 2

    if argv[0] != "apply":
        print(f"execute-plan-assemble: unrecognized subcommand {argv[0]!r}", file=sys.stderr)
        return 2

    rest = argv[1:]
    plan_path: Optional[str] = None
    autonomous = False
    session_id: Optional[str] = None
    extra: list[str] = []
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--autonomous":
            autonomous = True
        elif arg == "--session-id":
            i += 1
            if i >= len(rest):
                print("execute-plan-assemble apply: --session-id requires a value", file=sys.stderr)
                return 2
            session_id = rest[i]
        elif plan_path is None and not arg.startswith("--"):
            plan_path = arg
        else:
            extra.append(arg)
        i += 1

    if extra:
        print(f"execute-plan-assemble apply: unrecognized argument(s): {extra!r}", file=sys.stderr)
        return 2
    if plan_path is None:
        print("execute-plan-assemble apply: missing required <plan-path>", file=sys.stderr)
        return 2

    exit_code, result = apply(plan_path, autonomous=autonomous, session_id=session_id)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

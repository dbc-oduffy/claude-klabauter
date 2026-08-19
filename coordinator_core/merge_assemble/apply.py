"""coordinator_core.merge_assemble.apply — the MUTATING half `merge_assemble.
brief()`'s read-only decision object hands off to (module docstring §
"the compute/apply split").

Recomputes the brief in-process, then dispatches its `directives[]` through
`coordinator_core.contract.apply_base`'s shared directive-execution engine —
composed, never re-derived (chunk C6 AC): the dependency ordering,
per-directive judgment gating, session-identity propagation, in-repo path
safety, and pathspec-scoped commit discipline all live in `apply_base`; this
module supplies only its own closed `_CLI_DISPATCH` table (one handler per
existing merge CLI the brief names) and the node-ceremony hard-gate's
`--force` bypass.

Every dispatch handler here shells out to an EXISTING atomic `coordinator/
bin/*.py` script (or, for the node ceremony gate, `node --test`) via an
explicit argv list resolved from this module's own file location — never a
brief-derived import, never a shell string built from `directives[].args`.

Contract (frozen, reviewed): DoE-claude coordinator/docs/wiki/computed-skills.md
Spec backlink: DoE-claude:pln-b4-baton-branch-lifecycle-comp-780d48, chunk C6

Negative-spec:
    - Do NOT add a dispatch entry that resolves `cli` via `getattr`,
      `importlib`, or any brief-derived string — every entry in
      `_CLI_DISPATCH` is a literal key written by hand in this file.
    - Do NOT build a subprocess argv via string interpolation/shell=True —
      every handler below passes a literal list to `subprocess.run`.
    - Do NOT special-case merge's real divergence from the `apply_base`
      contract at this call site (the DR-092 anti-pattern) — see
      `apply_base`'s own docstring, "PROVISIONAL through W3": feed a real
      divergence back into `apply_base`'s parameters instead. NOTE (chunk
      C6 handoff to the EM): the node-ceremony `--force` bypass is
      expressed here purely via `directives[].already_satisfied` — no new
      `apply_base` parameter was needed to preserve it. If a future
      consumer's hard-gate bypass does NOT fit the `already_satisfied`
      shape (e.g. needs a distinct exit code from an ordinary skip),
      that is the concrete signal to widen `apply_base` rather than
      re-deriving a second bypass mechanism per-consumer.
    - Do NOT trust a caller-supplied decision object as the mutation plan —
      `apply()` recomputes `brief()` itself.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.ceremony_common.json_payload_flag import (
    detect_conflicting_payload_channels,
    resolve_json_payload_flag,
)
from coordinator_core.contract import apply_base
from coordinator_core.merge_assemble import (
    GATE_DIRECTIVE_IDS,
    NODE_CEREMONY_TEST_RELPATH,
    _CEREMONY_NAME,
    brief,
    build_gate_verdicts_scaffold,
    normalize_decisions,
    resolve_repo_root,
)
from coordinator_core.telemetry.composition_record import (
    flush_composition_record,
    make_fleet_budget,
)

# ---------------------------------------------------------------------------
# Exit-code contract — composed from apply_base, shared by every apply/
# dispatch half. NOT inherited from `brief`'s own 0/1/2/3 contract.
# ---------------------------------------------------------------------------
APPLY_EXIT_OK = apply_base.APPLY_EXIT_OK
APPLY_EXIT_HALTED_AT_JUDGMENT = apply_base.APPLY_EXIT_HALTED_AT_JUDGMENT
APPLY_EXIT_CLAIM_DENIED = apply_base.APPLY_EXIT_CLAIM_DENIED
APPLY_EXIT_TRANSPORT_FAIL = apply_base.APPLY_EXIT_TRANSPORT_FAIL
APPLY_EXIT_PARTIAL_MUTATION = apply_base.APPLY_EXIT_PARTIAL_MUTATION

UnrecognizedDirective = apply_base.UnrecognizedDirective
OutOfRepoPath = apply_base.OutOfRepoPath
NoResolvableSessionId = apply_base.NoResolvableSessionId
DirectiveDependencyCycle = apply_base.DirectiveDependencyCycle
DirectiveResult = apply_base.DirectiveResult

_resolve_explicit_session_id = apply_base.resolve_explicit_session_id
_session_identity = apply_base.session_identity

#: `coordinator/bin/` — resolved from THIS module's own location, never
#: from a target repo's `repo_root` (which may differ from the claude-klabauter
#: install this module ships from).
_BIN_DIR = Path(__file__).resolve().parents[2] / "coordinator" / "bin"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_py_script(script_name: str, args: list[str], repo_root: Path) -> subprocess.CompletedProcess:
    """Runs an EXISTING `coordinator/bin/<script_name>.py` entrypoint via
    `sys.executable` (the interpreter running THIS process — never a bare
    `python`/`python3` bareword, which is not portable across platforms),
    with a literal argv list and `cwd=repo_root`."""
    script_path = _BIN_DIR / f"{script_name}.py"
    return subprocess.run(
        [sys.executable, str(script_path), *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
    )


def _dispatch_result(cli: str, proc: subprocess.CompletedProcess) -> dict[str, Any]:
    if proc.returncode != 0:
        raise RuntimeError(
            f"{cli}: exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip() or '<no output>'}"
        )
    return {"cli": cli, "returncode": proc.returncode, "stdout": proc.stdout.strip()}


def _dispatch_node_ceremony_gate(args: list[str], repo_root: Path) -> dict[str, Any]:
    """`d0` — the node ceremony hard-gate (chunk C6 AC). `--force` never
    reaches this handler: a forced run marks `d0.already_satisfied = True`
    (see `_apply_force_bypass`) so `apply_base.execute_directives` skips
    dispatch entirely, exactly like any other `already_satisfied`
    directive. When this handler DOES run and the suite fails, it raises —
    `apply_base.execute_directives` aborts the whole run immediately
    (`d0` orders first, per `build_directives`), so no later directive
    ever dispatches on a failed ceremony gate."""
    test_path = Path(*NODE_CEREMONY_TEST_RELPATH)
    proc = subprocess.run(
        ["node", "--test", str(test_path)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
    )
    return _dispatch_result("node-ceremony-gate", proc)


def _dispatch_merge_recovery_and_tag_cut(args: list[str], repo_root: Path) -> dict[str, Any]:
    proc = _run_py_script("merge-recovery-and-tag-cut", args, repo_root)
    return _dispatch_result("merge-recovery-and-tag-cut", proc)


def _dispatch_merge_gate_and_pr(args: list[str], repo_root: Path) -> dict[str, Any]:
    proc = _run_py_script("merge-gate-and-pr", args, repo_root)
    return _dispatch_result("merge-gate-and-pr", proc)


def _dispatch_portability_sweep(args: list[str], repo_root: Path) -> dict[str, Any]:
    proc = _run_py_script("portability-sweep", args, repo_root)
    return _dispatch_result("portability-sweep", proc)


def _dispatch_check_no_illegal_paths(args: list[str], repo_root: Path) -> dict[str, Any]:
    proc = _run_py_script("check-no-illegal-paths", args, repo_root)
    return _dispatch_result("check-no-illegal-paths", proc)


def _dispatch_merge_release_notes_derive(args: list[str], repo_root: Path) -> dict[str, Any]:
    proc = _run_py_script("merge-release-notes-derive", args, repo_root)
    return _dispatch_result("merge-release-notes-derive", proc)


def _dispatch_orphan_branch_sweep(args: list[str], repo_root: Path) -> dict[str, Any]:
    proc = _run_py_script("orphan-branch-sweep", args, repo_root)
    return _dispatch_result("orphan-branch-sweep", proc)


def _dispatch_tier_u_grant(args: list[str], repo_root: Path) -> dict[str, Any]:
    """`d_grant_write` / `d_grant_handback` — the ceremony's Tier-U token,
    minted after the ceremony gate and handed back at its close
    (cross-repo/inbox/2026-08-04-doe-claude-em-ceremony-grants-belong-in-
    code-not-prose.md § 3).

    IN-PROCESS, unlike every other handler in this table, and that is the
    point. The grant is a single small JSON write into
    `.git/coordinator-sessions/<sid>/`; spawning an interpreter for it
    would add two cold starts to a ceremony whose per-composition cost is
    already the thing being attacked
    (`state/handoffs/2026-08-19-the-320-second-ceremony.md`). Spawning also
    buys nothing here: `session.core.resolve_session_id` reads env vars
    only (`COORDINATOR_SESSION_ID` / `CLAUDE_SESSION_ID` /
    `CLAUDE_CODE_SESSION_ID`), which a child inherits, so a subprocess
    resolves the SAME sid this process already has.

    Exit-code contract, mapped onto this dispatcher's raise/return split:
    exit 1 is an infra condition (unresolvable sid) and DEGRADES — the
    layer-5 guard fails closed, so an unminted grant refuses the Tier-U
    consumer rather than authorizing it, and aborting the whole ceremony
    over it would be strictly worse than the prose this replaced. Exit 2 is
    a wrong argv shape built by `build_directives` in this same repo, i.e. a
    defect, and RAISES.

    Negative-spec: do not "make this consistent" by routing it through
    `_run_py_script`/`_dispatch_result`. Consistency with the other handlers
    is not worth two cold spawns, and the raise-on-exit-1 that would come
    with it reintroduces "a grant that could not be minted takes the
    ceremony down with it"."""
    from coordinator_core.session.grant_directive import EXIT_USAGE, run_grant_directive

    code, message = run_grant_directive(args)
    if code == EXIT_USAGE:
        raise RuntimeError(f"tier-u-grant: {message}")
    result: dict[str, Any] = {"cli": "tier-u-grant", "returncode": code, "stdout": ""}
    if code != 0:
        result["degraded_reason"] = (
            f"tier-u-grant {args[0] if args else '<no verb>'}: {message} — the "
            "layer-5 guard fails closed, so no Tier-U consumer is authorized by "
            "its absence"
        )
    return result


def _compensate_grant_write(
    directive: dict[str, Any], repo_root: Path, detail: Optional[dict[str, Any]]
) -> Any:
    """Hand the grant back when the ceremony dies before reaching
    `d_grant_handback`.

    Without this, the handback is only as reliable as the ceremony's
    completion: `apply_base.execute_directives` returns
    `APPLY_EXIT_PARTIAL_MUTATION` the moment a handler raises, so every
    later directive — including the handback — never dispatches, and the
    grant outlives the ceremony that minted it. It stays bounded by session
    liveness either way, so the leak degrades to the pre-handback behaviour
    rather than past it; this closes it rather than documenting it.

    Uses the SAME guarded call as the directive it compensates, so the
    compensation cannot destroy a PM grant (or another ceremony's, under
    `/workweek-complete` -> `/merging-to-main` nesting) that the abort
    happened to leave live. Idempotent by construction: revoking an absent
    grant is success, so this running after a handback that already fired
    is a no-op, not a second effect."""
    from coordinator_core.session.grant_directive import run_grant_directive

    return run_grant_directive(["revoke", "--only-ceremony", _CEREMONY_NAME])


#: Per-directive-id compensators, fired in reverse landing order by
#: `apply_base.execute_directives` when a handler raises. Only the grant
#: write registers one: it is the single directive here whose effect
#: OUTLIVES the run (a token in `.git/coordinator-sessions/<sid>/` that a
#: later Tier-U consumer reads), so it is the only one an aborted run can
#: strand.
_COMPENSATORS: dict[str, Any] = {
    "d_grant_write": _compensate_grant_write,
}


#: THE closed dispatch table — every key is a literal string written here
#: by hand, matching `merge_assemble.build_directives`'s `cli` values.
_CLI_DISPATCH: dict[str, Callable[[list[str], Path], dict[str, Any]]] = {
    "node-ceremony-gate": _dispatch_node_ceremony_gate,
    "merge-recovery-and-tag-cut": _dispatch_merge_recovery_and_tag_cut,
    "merge-gate-and-pr": _dispatch_merge_gate_and_pr,
    "portability-sweep": _dispatch_portability_sweep,
    "check-no-illegal-paths": _dispatch_check_no_illegal_paths,
    "merge-release-notes-derive": _dispatch_merge_release_notes_derive,
    "orphan-branch-sweep": _dispatch_orphan_branch_sweep,
    "tier-u-grant": _dispatch_tier_u_grant,
}


def _fill_gate_verdicts(
    report: dict[str, Any], directives: Optional[list[dict[str, Any]]] = None
) -> dict[str, str]:
    """C3 fix — populates the `gates` key `merge_assemble`'s module
    docstring already claimed `apply()` fills in but never did (no `gates`
    key was ever written; this function is the first writer). Reads only
    what `execute_directives` already has in hand in THIS SAME `report` —
    `report["results"]` (one `DirectiveResult.to_report()` per directive
    that actually dispatched) and `report.get("failed_directive")` (set
    only on `APPLY_EXIT_PARTIAL_MUTATION`) — never re-runs or re-derives
    anything. A gate whose directive never reached either list (blocked on
    an unresolved judgment point, or the run halted before it) stays
    `"pending"`, exactly as `build_gate_verdicts_scaffold` seeds it.

    D5 fix: `directives` (the pre-dispatch list `apply()` already computed
    via `_apply_force_bypass(decision["directives"], force)`) is read ONLY
    to recover each `already_satisfied` directive's own `skipped_reason` —
    `DirectiveResult.to_report()` never carries it (`detail` is `None` for
    an `already_satisfied` entry), so this is the one place that reason is
    still in hand. An `already_satisfied` gate directive WITH a
    `skipped_reason` (an absent producer, e.g. `d5` when
    `portability-sweep.py` does not exist) reports `"unavailable"` —
    distinct from both `"passed"` and the scaffold's own `"pending"`. An
    `already_satisfied` gate directive with NO `skipped_reason` (e.g. a
    `--force` bypass, if one is ever added for a gate directive) still
    never asserts `"passed"` for work it did not observe (the original C3
    unobserved-fact-hazard finding) — it stays `"pending"`."""
    gates = build_gate_verdicts_scaffold()
    directive_id_to_gate = {v: k for k, v in GATE_DIRECTIVE_IDS.items()}
    skipped_reason_by_id = {
        d["id"]: d.get("skipped_reason")
        for d in (directives or [])
        if d.get("already_satisfied") and d.get("skipped_reason")
    }
    for result in report.get("results", []):
        gate = directive_id_to_gate.get(result.get("id"))
        if gate is None:
            continue
        if result.get("already_satisfied"):
            if result.get("id") in skipped_reason_by_id:
                gates[gate] = "unavailable"
            continue
        gates[gate] = "passed"
    failed_gate = directive_id_to_gate.get(report.get("failed_directive"))
    if failed_gate is not None:
        gates[failed_gate] = "failed"
    return gates


def _apply_force_bypass(directives: list[dict[str, Any]], force: bool) -> list[dict[str, Any]]:
    """`--force` bypass (chunk C6 AC): marks the node ceremony gate (`d0`)
    `already_satisfied` so it is reported landed without ever dispatching
    its handler — the same mechanism `apply_base` already gives every
    OTHER `already_satisfied` directive, not a bespoke skip path. Every
    other directive is returned unchanged."""
    if not force:
        return directives
    out = []
    for directive in directives:
        if directive["id"] == "d0":
            out.append({**directive, "already_satisfied": True})
        else:
            out.append(directive)
    return out


def apply(
    *,
    session_id: Optional[str] = None,
    repo_root: Optional[Path] = None,
    decisions: Optional[dict[str, Any]] = None,
    force: bool = False,
    tag_prefix: str = "v",
) -> tuple[int, dict[str, Any]]:
    """`apply [--session-id <id>] [--force] [--decisions <json>]` —
    recomputes the brief in-process and executes its `directives[]` through
    `apply_base.execute_directives` against this module's closed dispatch
    table. Returns `(exit_code, report)`; `report["landed"]` names exactly
    which directive ids ran (or were skipped `already_satisfied`, including
    a forced `d0`)."""
    root = repo_root or resolve_repo_root()
    if root is None:
        return APPLY_EXIT_TRANSPORT_FAIL, {"error": "could not resolve a git worktree root"}

    composition_budget = make_fleet_budget("merge_assemble")

    resolved_sid = _resolve_explicit_session_id(session_id)
    if resolved_sid is None:
        return APPLY_EXIT_TRANSPORT_FAIL, {
            "error": (
                "no session id resolvable via --session-id or "
                f"{'/'.join(apply_base.SESSION_ENV_READ_ORDER)} — refusing the "
                "ambient tier-4 sentinel"
            ),
        }

    # Normalized exactly once here — `brief()` re-normalizes idempotently
    # on its own input, so this SAME map is what both `brief()`'s internal
    # override resolution and `execute_directives`'s `disposition_resolves_
    # directive` gate see; they must never be allowed to disagree about
    # what a bare-string `version_bump_final` entry means.
    effective_decisions = normalize_decisions(decisions)

    with _session_identity(resolved_sid, env_vars=apply_base.SESSION_ENV_VARS):
        brief_result = brief(decisions=effective_decisions, repo_root=root, tag_prefix=tag_prefix)
        if brief_result.exit_code != 0:
            return APPLY_EXIT_TRANSPORT_FAIL, {
                "error": brief_result.decision_object.get("error", "brief did not resolve a plan"),
                "landed": [],
            }

        decision = brief_result.decision_object
        directives = _apply_force_bypass(decision.get("directives", []), force)
        judgment_points = decision.get("judgment_points", [])

        outcome = "directive_failed"
        try:
            exit_code, report = apply_base.execute_directives(
                directives,
                judgment_points,
                root,
                _CLI_DISPATCH,
                decisions=effective_decisions,
                composition_budget=composition_budget,
                compensators=_COMPENSATORS,
            )
            if exit_code == apply_base.APPLY_EXIT_OK:
                outcome = "success"
            elif exit_code == apply_base.APPLY_EXIT_PARTIAL_MUTATION:
                outcome = "partial_mutation"
        finally:
            flush_composition_record(composition_budget, outcome)
        # branch_state/release_tag_cut moved into the canonical envelope's
        # `artifact` key (Review: code-reviewer — Finding 1) — no longer
        # top-level siblings of directives/judgment_points.
        artifact = decision.get("artifact") or {}
        report["branch_state"] = artifact.get("branch_state")
        report["release_tag_cut"] = artifact.get("release_tag_cut")
        report["gates"] = _fill_gate_verdicts(report, directives)
        return exit_code, report


def _usage(prog: str) -> int:
    print(
        f"usage: {prog} apply [--session-id <id>] [--force] "
        "[--decisions <json> | --decisions-file <path>] [--tag-prefix <prefix>]",
        file=sys.stderr,
    )
    return APPLY_EXIT_TRANSPORT_FAIL


def main_apply(argv: list[str]) -> int:
    session_id: Optional[str] = None
    decisions: Optional[dict[str, Any]] = None
    force = False
    tag_prefix = "v"
    conflict = detect_conflicting_payload_channels(argv)
    if conflict is not None:
        print(f"merge-assemble apply: {conflict}", file=sys.stderr)
        return _usage("merge-assemble")
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--session-id":
            if i + 1 >= len(argv):
                return _usage("merge-assemble")
            session_id = argv[i + 1]
            i += 2
        elif tok == "--force":
            force = True
            i += 1
        elif tok == "--tag-prefix":
            if i + 1 >= len(argv):
                return _usage("merge-assemble")
            tag_prefix = argv[i + 1]
            i += 2
        elif (payload := resolve_json_payload_flag(argv, i)).consumed:
            if payload.error is not None:
                print(f"merge-assemble apply: {payload.error}", file=sys.stderr)
                return _usage("merge-assemble")
            decisions = payload.value
            i += payload.consumed
        else:
            print(f"merge-assemble apply: unrecognized argument {tok!r}", file=sys.stderr)
            return _usage("merge-assemble")

    exit_code, report = apply(session_id=session_id, decisions=decisions, force=force, tag_prefix=tag_prefix)
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code

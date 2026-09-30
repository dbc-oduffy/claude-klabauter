"""
coordinator_core.workweek_complete.apply — the `workweek-complete` computed-
skill engine's MUTATING half, standalone-conformant per coordinator-content-repo
`coordinator/docs/wiki/computed-skills.md` § The compute/apply split and
§ What bounds a mutating apply half. Mirrors
`coordinator_core.workday_complete.apply` (C2)'s shape — see that module for
the shared design rationale (closed dispatch, halt contract, in-process
invocation).

Purpose: recomputes `brief.brief()` in-process (never trusts a caller-
supplied decision object), applies the HALT CONTRACT (per-directive,
disposition-value-aware — see `_execute_directives` below), and executes
every execution-ready `directives[]` entry through a CLOSED, literal
dispatch table naming the C4 consumes-manifest CLIs/scripts. Deliberately
does NOT build or depend on `coordinator_core.contract.apply_base` (the D1
shared mutating-apply runner) — that module is a different baton's
anti-scope surface (plan § Anti-scope).

Contract (frozen, reviewed): coordinator-content-repo coordinator/docs/wiki/computed-skills.md
Spec backlink: coordinator-content-repo:pln-b1-ceremony-complete-computed--9ffa54, chunk C5

HALT CONTRACT (2026-07-24 premise-check reconciliation; mirrors C2's own
apply.py and the pickup exemplar's AC10 design): a directive `d` gated on
judgment_point `j` (`d["depends_on"] == j["id"]`) is resolved-to-fire iff
`decisions[j_id]["disposition"]` is set AND that CHOSEN disposition's own
`resolves` list (looked up in `j["dispositions"]`) includes `d["id"]` —
never merely "some disposition was picked." A directive whose `depends_on`
is `None` always fires. This chunk's Tier-1 directives are all ungated
(`depends_on=None`) — see `brief.py`'s `_build_directives` docstring.

Security-load-bearing (mirrors `workday_complete.apply`'s own shape): the
executable universe this module can reach is a CLOSED CONSTRUCTION.
`directives[].cli` resolves through `_CLI_DISPATCH` — a literal, hardcoded
`dict[str, Path]` naming exactly the C4 consumes-manifest — never `getattr`,
never `importlib.import_module` on a brief-derived string, never a
subprocess/shell invocation built from `directives[].args`. Every named
script is loaded ONCE, by a fixed literal path resolved from THIS module's
own location (`Path(__file__)`), and invoked IN-PROCESS via its own
`main(argv)`/`main()` entrypoint — never spawned as a child process. An
unrecognized `cli` raises before that directive dispatches.

Negative-spec:
    - Do NOT add a dispatch entry resolved via `getattr`/`importlib.
      import_module`/any brief-derived string — every `_CLI_DISPATCH` key is
      written by hand in `brief.CONSUMES_MANIFEST`, and every value is a
      fixed `Path(__file__)`-relative script path (some consumes-manifest
      scripts under `coordinator/bin/` carry a `.py` suffix, some are
      bareword-named launcher shims with no extension — `_resolve_cli`
      checks both fixed candidate paths at load time, never a glob/search).
    - Do NOT call `subprocess.run`/`Popen`/`os.system` anywhere in this
      module — every consumes-manifest CLI is loaded and invoked in-process
      via `importlib.util.spec_from_file_location` + its own `main`
      entrypoint, never spawned.
    - Do NOT import or compose `coordinator_core.contract.apply_base`'s
      directive-execution engine (`execute_directives`/`resolve_cli`/
      `resolve_op`) — that module (D1) is a separate baton's anti-scope
      surface. The ONE named exception (C7,
      `docs/plans/2026-08-19-directives-name-an-op-not-a-cli.md`) is
      `apply_base.assert_dispatchable` — the single shared admission check
      against `authz.dispatchable.ASSEMBLER_DISPATCHABLE`, called from
      `_resolve_cli` below so the membership check is never hand-copied
      into three private bodies. It does not resolve, dispatch, or execute
      anything; it only raises.
    - Do NOT auto-resolve a `judgment_points[]` entry — a directive only
      ever fires off an EXPLICIT `decisions[jp_id]["disposition"]` whose OWN
      `resolves` list names it.
    - Do NOT treat `recommendation` on a judgment point as a control-flow
      input anywhere in `_execute_directives` — it is offer-only.
    - Do NOT stage the whole tree in the commit tail: only paths dirtied
      during this pass (after minus before, less live peers' paths) are
      committed, never `-A` or a directory pathspec.
    - Do NOT release the session's full claim surface from the tail: only the
      committed paths' claims are released, and only after a sha lands.
    - Do NOT push from the tail; publication is the push checkpoint's job.

Commit tail: after the directive loop, `_run_workweek_commit_tail` commits the
paths this pass dirtied through `directives_commit_tail.run_close_commit` and
folds its outcome into `report["commit"]`. Fail-closed skips are reported as
`attempted: False` with a `workweek-commit:*` reason and never stop apply.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Optional

from coordinator_core.ceremony_common.apply_halt import (
    UnrecognizedDirective,
    _directive_gate_open,
    budget_advisory_mid_directive,
    budget_check_post_mutation,
    budget_check_pre_mutation,
    build_ceremony_halt_exit_codes,
    exit_code_label,
)
from coordinator_core.ceremony_common.json_payload_flag import (
    detect_conflicting_payload_channels,
    resolve_json_payload_flag,
)
from coordinator_core.ceremony_common.cli_dispatch import (
    resolve_cli_script_root,
    invoke_cli_main as _shared_invoke_cli_main,
    load_cli_module as _shared_load_cli_module,
)
from coordinator_core.ceremony_common.cli_rejection import (
    CliExitClass,
    describe_exit_class,
)
from coordinator_core.telemetry.composition_record import (
    flush_composition_record,
    make_fleet_budget,
)
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.dirty_tree_gate import parse_porcelain_paths
from coordinator_core.session import scope as session_scope
from coordinator_core.session.core import resolve_session_id
from coordinator_core.workstream_complete import directives_commit_tail
from coordinator_core.workweek_complete.brief import (
    CONSUMES_MANIFEST,
    _resolve_repo_root_for_doc_staleness,
    brief,
)
from coordinator_core.contract.apply_base import assert_dispatchable

if TYPE_CHECKING:
    from coordinator_core.composition_budget import CompositionBudget

# ---------------------------------------------------------------------------
# Exit-code contract (apply-side, 0-4) — SEPARATE from `brief.WorkweekExitCode`
# (0-3). computed-skills.md § Exit-code contract for a mutating half requires
# each half to pin its own enumeration; this one is never reused by brief().
# Built from the shared `ceremony_common.apply_halt` ladder (C2h) so this
# module's numbering can never independently drift from
# `workday_complete.apply`'s own.
# ---------------------------------------------------------------------------
WorkweekApplyExitCode = build_ceremony_halt_exit_codes("WorkweekApplyExitCode")

_WORKWEEK_COMMIT_SUBJECT = "chore(workweek-complete): commit ceremony outputs"


#: THE closed dispatch table (security-load-bearing — see module docstring).
#: Every key is a literal member of `brief.CONSUMES_MANIFEST`; every value is
#: this module's own fixed, `Path(__file__)`-relative script location under
#: `coordinator/bin/`. Resolved once at import time — never mutated at
#: runtime, never resolved via glob/search.
_CLI_SCRIPT_ROOT = resolve_cli_script_root()


def _resolve_script_path(name: str) -> Path:
    py_path = _CLI_SCRIPT_ROOT / f"{name}.py"
    if py_path.exists():
        return py_path
    return _CLI_SCRIPT_ROOT / name


_CLI_DISPATCH: dict[str, Path] = {
    name: _resolve_script_path(name) for name in CONSUMES_MANIFEST
}

_LOADED_MODULES: dict[str, ModuleType] = {}


def _resolve_cli(cli_name: str) -> Path:
    """The one seam `directives[].cli` ever passes through. Closed over a
    literal dict — an unrecognized name raises before any directive in the
    run dispatches. Additionally gated (C7) by the shared
    `apply_base.assert_dispatchable` admission check against
    `authz.dispatchable.ASSEMBLER_DISPATCHABLE["workweek_complete"]` — no
    table dispatching a registered op is left with review-only admission."""
    if cli_name not in _CLI_DISPATCH:
        raise UnrecognizedDirective(
            f"workweek_complete.apply: unrecognized cli {cli_name!r} — not a "
            f"member of the consumes-manifest {sorted(_CLI_DISPATCH)!r}"
        )
    assert_dispatchable("workweek_complete", cli_name)
    return _CLI_DISPATCH[cli_name]


def _load_cli_module(cli_name: str) -> ModuleType:
    """Loads (once, cached) the script named by `cli_name` via a fixed
    literal path — never a brief-derived import target. Never spawns a
    subprocess. `_resolve_cli` (admission) runs BEFORE the cache check
    (F6, cold review 2026-08-19) so the control is a per-dispatch check,
    never merely a first-load one.

    Routes through the shared `ceremony_common.cli_dispatch.load_cli_module`
    primitive (C1/C5) rather than a private `importlib` copy — this
    module's own cache (`_LOADED_MODULES`) and `UnrecognizedDirective`-on-
    failure behaviour are unchanged; only the load mechanics themselves are
    now the ONE shared implementation. `_resolve_cli` (admission) still runs
    BEFORE either cache is touched, so a denied `cli` never reaches the
    shared primitive at all — `ValueError` from a genuinely unloadable spec
    is translated to `UnrecognizedDirective` to keep this module's own
    exception contract unchanged for its callers."""
    script_path = _resolve_cli(cli_name)
    if cli_name in _LOADED_MODULES:
        return _LOADED_MODULES[cli_name]
    module_name = f"_workweek_complete_cli_{cli_name.replace('-', '_')}"
    try:
        module = _shared_load_cli_module(module_name, script_path)
    except ValueError as exc:
        raise UnrecognizedDirective(
            f"workweek_complete.apply: could not load {cli_name!r} from {script_path}"
        ) from exc
    _LOADED_MODULES[cli_name] = module
    return module


def _invoke_cli_main(module: ModuleType, args: list[str]) -> tuple[int, str, CliExitClass]:
    try:
        exit_code, _stdout_text, stderr_text, exit_class = _shared_invoke_cli_main(module, args)
    except ValueError as exc:
        raise UnrecognizedDirective(
            f"workweek_complete.apply: {module.__name__} exposes no main() entrypoint"
        ) from exc
    return exit_code, stderr_text, exit_class


def _dispatch_directive(directive: dict[str, Any]) -> dict[str, Any]:
    module = _load_cli_module(directive["cli"])
    exit_code, stderr_text, exit_class = _invoke_cli_main(module, directive.get("args", []))
    if stderr_text:
        sys.stderr.write(stderr_text)
    return {
        "id": directive["id"],
        "cli": directive["cli"],
        "args": list(directive.get("args", [])),
        "exit_code": exit_code,
        "stderr": stderr_text,
        "exit_class": exit_class.value,
    }


def _judgment_points_by_id(judgment_points: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {jp["id"]: jp for jp in judgment_points}


def _execute_directives(
    directives: list[dict[str, Any]],
    judgment_points: list[dict[str, Any]],
    decisions: dict[str, Any],
    composition_budget: "Optional[CompositionBudget]" = None,
) -> tuple[int, dict[str, Any]]:
    """THE directive-execution seam (halt contract). Iterates `directives`
    in list order; each entry whose gate is open (per
    `_directive_gate_open`) dispatches through the closed `_CLI_DISPATCH`
    table, each blocked entry is recorded (never dispatched, never silently
    dropped), and every OTHER ready directive still executes even when one
    entry is blocked or fails — the halt is PER-DIRECTIVE, never whole-run.

    Returns `(exit_code, report)`. `report["landed"]` names directive ids
    that dispatched AND exited 0; `report["blocked"]` names directive ids
    whose judgment-point gate stayed closed this pass; `report["failed"]`
    names directive ids whose dispatch either raised OR returned a non-zero
    `exit_code` (2026-07-26 arg-mismatch audit, systemic finding 1 — mirrors
    `workday_complete.apply`'s identical fix: a directive previously joined
    `landed` after any dispatch that did not *raise*, so an argparse usage
    error or a gate's business-fail read as ceremony success);
    `report["degraded"]` names directive ids whose dispatch returned a
    non-zero `exit_code` but which carry `best_effort: True` — a tolerated
    failure the operator still sees, but which never joins `failed` and
    therefore never moves the exit code below (2026-08-08 fix: cadence
    emission is documented best-effort per AC5 of the plan this fix
    backlinks, but nothing implemented that tolerance — a non-zero exit
    from it read as a `PARTIAL_MUTATION` whose own contract tells the
    operator to stop and reconcile a ceremony that in fact fully
    succeeded). An absent `best_effort` key is `False`, so every directive
    that predates this fix keeps today's behaviour unchanged. Both
    `failed[].error` and `degraded[].error` fold in the dispatch's captured
    stderr (2026-07-27 finding B) when the CLI produced any, appended after
    the existing `"<cli> exited <n> (args=[...])"` prefix. Exit code:
    `HALTED_AT_JUDGMENT` when anything was blocked and nothing failed;
    `PARTIAL_MUTATION` when something failed but something else also
    landed; `DIRECTIVE_FAILED` when something failed and nothing landed at
    all; `SUCCESS` when every directive either landed or merely degraded —
    `degraded` never moves the exit code off `SUCCESS`.
    """
    jp_by_id = _judgment_points_by_id(judgment_points)
    landed: list[str] = []
    blocked: list[str] = []
    failed: list[dict[str, Any]] = []
    degraded: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    pre_mutation_breach = budget_check_pre_mutation(composition_budget)
    if pre_mutation_breach is not None:
        return int(WorkweekApplyExitCode.DIRECTIVE_FAILED), {
            "landed": [],
            "blocked": [],
            "failed": [],
            "degraded": [],
            "results": [],
            "budget_breach": pre_mutation_breach,
        }

    try:
        for directive in directives:
            # An `already_satisfied` directive ran in an earlier pass and hits
            # `continue` below without ever dispatching, so its verb name is
            # never resolved by the main loop either. Admission-checking it here
            # would refuse the WHOLE run over a name that cannot dispatch --
            # a false refusal on a replayed directive whose verb has since left
            # `ASSEMBLER_DISPATCHABLE` (slice-B review finding 1, 2026-08-20).
            # A gate-blocked directive is deliberately NOT skipped: it is still
            # a live member of this run's list and dispatches the moment its
            # gate resolves, so an un-admitted verb there is a structurally
            # invalid list, which is exactly what this pre-pass exists to catch.
            if directive.get("already_satisfied"):
                continue
            _resolve_cli(directive["cli"])
    except UnrecognizedDirective as exc:
        return int(WorkweekApplyExitCode.DIRECTIVE_FAILED), {
            "landed": [],
            "blocked": [],
            "failed": [{"id": None, "error": str(exc)}],
            "degraded": [],
            "results": [],
        }

    for directive in directives:
        if directive.get("already_satisfied"):
            landed.append(directive["id"])
            continue
        try:
            gate_open = _directive_gate_open(directive, jp_by_id, decisions)
        except Exception as exc:  # noqa: BLE001 - malformed envelope is per-directive
            failed.append({"id": directive["id"], "error": f"gate evaluation error: {exc}"})
            continue
        if not gate_open:
            blocked.append(directive["id"])
            continue
        try:
            result = _dispatch_directive(directive)
        except Exception as exc:  # noqa: BLE001 - closed-table dispatch failure
            failed.append({"id": directive["id"], "error": str(exc)})
            budget_advisory_mid_directive(composition_budget, directive["id"])
            continue
        budget_advisory_mid_directive(composition_budget, directive["id"])
        if result.get("exit_code", 0) != 0:
            error = (
                f"{directive['cli']} exited {result['exit_code']} "
                f"(args={result.get('args', [])})"
            )
            exit_class_note = describe_exit_class(
                CliExitClass(result.get("exit_class", CliExitClass.RETURNED.value))
            )
            if exit_class_note:
                error = f"{error} — {exit_class_note}"
            stderr_text = (result.get("stderr") or "").strip()
            if stderr_text:
                error = f"{error} — stderr: {stderr_text}"
            entry = {"id": directive["id"], "error": error}
            if directive.get("best_effort"):
                degraded.append(entry)
            else:
                failed.append(entry)
            results.append(result)
            continue
        results.append(result)
        landed.append(directive["id"])

    report = {
        "landed": landed,
        "blocked": blocked,
        "failed": failed,
        "degraded": degraded,
        "results": results,
    }

    if failed and landed:
        return int(WorkweekApplyExitCode.PARTIAL_MUTATION), report
    if failed:
        return int(WorkweekApplyExitCode.DIRECTIVE_FAILED), report
    if blocked:
        return int(WorkweekApplyExitCode.HALTED_AT_JUDGMENT), report
    post_mutation_breach = budget_check_post_mutation(composition_budget)
    if post_mutation_breach is not None:
        report["budget_breach"] = post_mutation_breach
    return int(WorkweekApplyExitCode.SUCCESS), report


def _dirty_snapshot(root: Any) -> Optional[dict[str, str]]:
    """`{path: xy}` for every dirty path under `root`, or `None` when git could
    not answer — never an empty dict on failure, which would make every
    pre-existing dirty file read as ceremony output."""
    result = git_native.status_porcelain(root, untracked_all=True)
    if not result.ok:
        return None
    return {path: xy for xy, path in parse_porcelain_paths(result.stdout, unquote=True)}


def _resolve_commit_root() -> Optional[str]:
    return _resolve_repo_root_for_doc_staleness()


def _in_linked_worktree(root: Any) -> bool:
    """Filesystem-only, zero spawns. True unless the nearest `.git`-bearing
    ancestor of cwd is `root` itself; a cwd with no `.git` ancestor reads as
    linked (fail closed)."""
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate != Path(root).resolve()
    return True


def _prepare_commit_tail() -> tuple[Optional[str], str, Optional[dict[str, str]], Optional[str]]:
    """Resolves root and session id and takes the pre-pass snapshot. Returns
    `(root, sid, before, skip_reason)`; a non-None reason means no tail."""
    try:
        root = _resolve_commit_root()
    except Exception:  # noqa: BLE001
        root = None
    if not root:
        return None, "", None, "workweek-commit:no-repo-root"
    try:
        sid = resolve_session_id(str(root)) or ""
    except Exception:  # noqa: BLE001
        sid = ""
    if not sid:
        return root, "", None, "workweek-commit:no-session-id"
    if _in_linked_worktree(root):
        return root, sid, None, "workweek-commit:linked-worktree"
    return root, sid, _dirty_snapshot(root), None


def _run_workweek_commit_tail(
    root: Any, before: Optional[dict[str, str]], decisions: dict[str, Any], sid: str
) -> dict[str, Any]:
    """Commits exactly the paths this pass dirtied (after minus before, less
    live peers' paths) through bare `run_close_commit`, then releases only the
    claims of what landed and re-reads status over the committed paths."""
    after = _dirty_snapshot(root)
    if before is None or after is None:
        return {"attempted": False, "skipped": "workweek-commit:snapshot-failed"}
    delta = set(after) - set(before)
    preexisting = sorted(set(after) & set(before))
    try:
        peers = directives_commit_tail.resolve_known_concurrent_paths(Path(root), sid)
    except directives_commit_tail.PeerAttributionUnavailable:
        return {
            "attempted": False,
            "skipped": "workweek-commit:peer-attribution-unavailable",
        }
    stage = sorted(delta - peers)
    withheld = sorted(delta & peers)
    deleted = [p for p in stage if "D" in after[p]]
    stage_paths = [p for p in stage if "D" not in after[p]]
    report: dict[str, Any] = {
        "attempted": True,
        "committed_sha": None,
        "commit_failed": False,
        "staged": stage_paths,
        "deleted": deleted,
        "withheld_peer": withheld,
        "preexisting_dirty": preexisting,
        "post_commit_dirty": [],
        "diagnostics": [],
    }
    if not stage:
        return report
    try:
        outcome = directives_commit_tail.run_close_commit(
            root,
            session_id=sid,
            subject=decisions.get("subject") or _WORKWEEK_COMMIT_SUBJECT,
            stage_paths=stage_paths,
            deleted_paths=deleted,
        )
    except Exception as exc:  # noqa: BLE001
        report["commit_failed"] = True
        report["error"] = str(exc)
        return report
    report["commit_failed"] = bool(outcome.commit_failed)
    report["committed_sha"] = outcome.committed_sha
    report["diagnostics"] = list(outcome.diagnostics)
    if not outcome.committed_sha:
        return report
    committed = stage_paths + deleted
    try:
        session_scope.release_committed_claims(sid, committed, cwd=str(root))
    except Exception:  # noqa: BLE001 - best-effort; a stale claim is the safe residue
        pass
    check = git_native.status_porcelain(root, paths=committed)
    if not check.ok:
        report["post_commit_check"] = "unreadable"
    else:
        report["post_commit_dirty"] = sorted(
            {path for _xy, path in parse_porcelain_paths(check.stdout, unquote=True)}
        )
    return report


def apply(*, decisions: Optional[dict[str, Any]] = None) -> tuple[int, dict[str, Any]]:
    """`apply()` — recomputes the brief in-process (never trusts a
    caller-supplied decision object) and executes every execution-ready
    `directives[]` entry per the halt contract. `decisions` is the EM-
    resolved `{judgment_point_id: {disposition, ...}}` map — omitted
    entries leave their gated directives blocked, not auto-fired.

    Constructs a fresh composition budget (never module-level — see
    `telemetry.composition_record`'s own PER-CALL FACTORY note) and flushes
    exactly one record for it in a `finally`, regardless of outcome
    (§ `flush_composition_record`'s own docstring).
    """
    composition_budget = make_fleet_budget("workweek_complete")
    outcome = "directive_failed"
    exit_label = None
    try:
        brief_exit_code, envelope = brief(decisions=decisions)
        if brief_exit_code != 0:
            transport_fail_report = {
                "error": envelope.get("error", "brief() did not resolve an actionable plan"),
                "landed": [],
            }
            exit_label = exit_code_label(
                int(WorkweekApplyExitCode.TRANSPORT_FAIL), transport_fail_report
            )
            return int(WorkweekApplyExitCode.TRANSPORT_FAIL), transport_fail_report

        directives = envelope.get("directives", [])
        judgment_points = envelope.get("judgment_points", [])
        effective_decisions = decisions if decisions is not None else envelope.get("decisions", {})

        root, sid, before, pre_skip = _prepare_commit_tail()
        exit_code, report = _execute_directives(
            directives, judgment_points, effective_decisions, composition_budget=composition_budget
        )
        if pre_skip is None and not report["results"] and exit_code == int(
            WorkweekApplyExitCode.DIRECTIVE_FAILED
        ):
            pre_skip = "workweek-commit:nothing-dispatched"
        if pre_skip is not None:
            commit: dict[str, Any] = {"attempted": False, "skipped": pre_skip}
        else:
            try:
                commit = _run_workweek_commit_tail(root, before, effective_decisions, sid)
            except Exception as exc:  # noqa: BLE001 - the tail never crashes apply
                commit = {"attempted": True, "commit_failed": True, "error": str(exc)}
        report["commit"] = commit
        if exit_code == int(WorkweekApplyExitCode.SUCCESS) and (
            commit.get("commit_failed") or commit.get("post_commit_dirty")
        ):
            exit_code = int(WorkweekApplyExitCode.PARTIAL_MUTATION)
        exit_label = exit_code_label(exit_code, report)
        if exit_code == int(WorkweekApplyExitCode.SUCCESS):
            outcome = "success"
        elif exit_code == int(WorkweekApplyExitCode.PARTIAL_MUTATION):
            outcome = "partial_mutation"
        return exit_code, report
    finally:
        flush_composition_record(composition_budget, outcome, exit_code_label=exit_label)


def main(argv: list[str]) -> int:
    decisions: Optional[dict[str, Any]] = None
    conflict = detect_conflicting_payload_channels(argv)
    if conflict is not None:
        print(f"workweek-complete-apply: {conflict}", file=sys.stderr)
        return int(WorkweekApplyExitCode.TRANSPORT_FAIL)
    i = 0
    while i < len(argv):
        tok = argv[i]
        if (payload := resolve_json_payload_flag(argv, i)).consumed:
            if payload.error is not None:
                print(f"workweek-complete-apply: {payload.error}", file=sys.stderr)
                return int(WorkweekApplyExitCode.TRANSPORT_FAIL)
            decisions = payload.value
            i += payload.consumed
        else:
            print(f"workweek-complete-apply: unrecognized argument {tok!r}", file=sys.stderr)
            return int(WorkweekApplyExitCode.TRANSPORT_FAIL)

    exit_code, report = apply(decisions=decisions)
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

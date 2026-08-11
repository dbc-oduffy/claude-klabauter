"""
coordinator_core.workweek_complete.apply — the `workweek-complete` computed-
skill engine's MUTATING half, standalone-conformant per example-doctrine-repo
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

Contract (frozen, reviewed): example-doctrine-repo coordinator/docs/wiki/computed-skills.md
Spec backlink: docs/plans/2026-07-24-b1-ceremony-complete-computed-conversion.md, chunk C5

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
    - Do NOT import or compose `coordinator_core.contract.apply_base` — that
      module (D1) is a separate baton's anti-scope surface.
    - Do NOT auto-resolve a `judgment_points[]` entry — a directive only
      ever fires off an EXPLICIT `decisions[jp_id]["disposition"]` whose OWN
      `resolves` list names it.
    - Do NOT treat `recommendation` on a judgment point as a control-flow
      input anywhere in `_execute_directives` — it is offer-only.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import inspect
import io
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Optional

from coordinator_core.ceremony_common.apply_halt import (
    UnrecognizedDirective,
    _directive_gate_open,
    build_ceremony_halt_exit_codes,
)
from coordinator_core.workweek_complete.brief import CONSUMES_MANIFEST, brief

# ---------------------------------------------------------------------------
# Exit-code contract (apply-side, 0-4) — SEPARATE from `brief.WorkweekExitCode`
# (0-3). computed-skills.md § Exit-code contract for a mutating half requires
# each half to pin its own enumeration; this one is never reused by brief().
# Built from the shared `ceremony_common.apply_halt` ladder (C2h) so this
# module's numbering can never independently drift from
# `workday_complete.apply`'s own.
# ---------------------------------------------------------------------------
WorkweekApplyExitCode = build_ceremony_halt_exit_codes("WorkweekApplyExitCode")


#: THE closed dispatch table (security-load-bearing — see module docstring).
#: Every key is a literal member of `brief.CONSUMES_MANIFEST`; every value is
#: this module's own fixed, `Path(__file__)`-relative script location under
#: `coordinator/bin/`. Resolved once at import time — never mutated at
#: runtime, never resolved via glob/search.
_CLI_SCRIPT_ROOT = Path(__file__).resolve().parents[2] / "coordinator" / "bin"


def _resolve_script_path(name: str) -> Path:
    """A consumes-manifest CLI ships as either `<name>.py` or a bareword
    launcher shim `<name>` (no extension) under `coordinator/bin/` — both
    shapes are fixed, literal candidates checked in that order; this is not
    a glob/search, just a two-candidate literal lookup."""
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
    run dispatches."""
    if cli_name not in _CLI_DISPATCH:
        raise UnrecognizedDirective(
            f"workweek_complete.apply: unrecognized cli {cli_name!r} — not a "
            f"member of the consumes-manifest {sorted(_CLI_DISPATCH)!r}"
        )
    return _CLI_DISPATCH[cli_name]


def _load_cli_module(cli_name: str) -> ModuleType:
    """Loads (once, cached) the script named by `cli_name` via a fixed
    literal path — never a brief-derived import target. Never spawns a
    subprocess."""
    if cli_name in _LOADED_MODULES:
        return _LOADED_MODULES[cli_name]
    script_path = _resolve_cli(cli_name)
    module_name = f"_workweek_complete_cli_{cli_name.replace('-', '_')}"
    # Some consumes-manifest scripts are bareword launcher shims with no
    # `.py` suffix — `spec_from_file_location` cannot infer a source loader
    # from an extensionless path, so pass `SourceFileLoader` explicitly
    # (mirrors the loader Python itself would pick for a `.py` file).
    loader = importlib.machinery.SourceFileLoader(module_name, str(script_path))
    spec = importlib.util.spec_from_file_location(module_name, script_path, loader=loader)
    if spec is None or spec.loader is None:
        raise UnrecognizedDirective(
            f"workweek_complete.apply: could not load {cli_name!r} from {script_path}"
        )
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec — some consumes-manifest scripts
    # define `@dataclass`-decorated classes whose class-body execution
    # resolves `sys.modules[cls.__module__]` (dataclasses' own
    # forward-ref-eval path); an unregistered module makes that lookup
    # return `None` and crash the load with an unrelated AttributeError.
    # `workday_complete.apply._load_cli_module` carries the identical fix
    # (Review: code-reviewer — Finding 2).
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    _LOADED_MODULES[cli_name] = module
    return module


def _invoke_cli_main(module: ModuleType, args: list[str]) -> tuple[int, str]:
    """Invokes `module.main` in-process (never a subprocess) — accepts
    either an `argv`-taking `main(argv)` or a zero-arg `main()`. Returns
    `(exit_code, stderr)`: the resolved integer exit code (`main`'s own
    return value when it returns one, else the code a `SystemExit` it
    raises carries, else `0` on a clean fallthrough) paired with everything
    the call printed to stderr.

    Zero-arg `main()` trampolines (per the 2026-07-26 arg-mismatch audit,
    systemic finding 2 — mirrors `workday_complete.apply`'s identical fix)
    are `def main() -> None` wrappers whose body still calls
    `sys.exit(op_main(sys.argv[1:]))` — they parse `sys.argv` themselves.
    Calling `main_fn()` bare hands them the APPLY PROCESS's own `sys.argv`,
    silently discarding `args`. This splices `args` into `sys.argv` (dummy
    `argv[0]`, restored in `finally`) for the duration of the call so a
    directive's emitted args reach the op the same way an `argv`-taking
    `main(argv)` would receive them.

    Stderr capture (2026-07-27 finding, mirrors `workstream_complete.apply`
    and `workday_complete.apply`'s identical fix): `module`'s own stderr is
    captured via `contextlib.redirect_stderr` — a non-zero directive's
    diagnostic text (e.g. `wsc-tail.py`'s exit-2 diagnostics block, printed
    unconditionally to `sys.stderr`) was previously neither captured nor
    threaded into `_dispatch_directive`'s result dict at all, so it never
    reached `report["results"]`/`report["failed"]` — the one place a caller
    (the skill, the EM) could read it back. `_dispatch_directive` re-emits
    it onto apply's own stderr afterward so live console visibility is
    unchanged. This module does not capture stdout (unlike its two
    siblings, which thread captured stdout into inter-directive value
    substitution) — this chunk's directives never consume another
    directive's stdout, so that capture was never added here; this fix
    does not change that."""
    main_fn: Optional[Callable[..., Any]] = getattr(module, "main", None)
    if main_fn is None:
        raise UnrecognizedDirective(
            f"workweek_complete.apply: {module.__name__} exposes no main() entrypoint"
        )
    try:
        params = inspect.signature(main_fn).parameters
    except (TypeError, ValueError):
        params = {}
    stderr_buf = io.StringIO()
    with contextlib.redirect_stderr(stderr_buf):
        try:
            if params:
                result = main_fn(list(args))
            else:
                saved_argv = sys.argv
                sys.argv = [module.__name__, *args]
                try:
                    result = main_fn()
                finally:
                    sys.argv = saved_argv
        except SystemExit as exc:
            code = exc.code
            exit_code = int(code) if isinstance(code, int) else (0 if code is None else 1)
            return exit_code, stderr_buf.getvalue()
    exit_code = int(result) if isinstance(result, int) else 0
    return exit_code, stderr_buf.getvalue()


def _dispatch_directive(directive: dict[str, Any]) -> dict[str, Any]:
    """Loads and invokes the one CLI a single `directives[]` entry names,
    returning a small result record. Raises `UnrecognizedDirective` before
    any dispatch on an unrecognized `cli`. The invoked CLI's own captured
    stderr is re-emitted onto apply's own stderr here (see
    `_invoke_cli_main`'s docstring) so nothing that used to print to the
    ceremony run's console goes silent."""
    module = _load_cli_module(directive["cli"])
    exit_code, stderr_text = _invoke_cli_main(module, directive.get("args", []))
    if stderr_text:
        sys.stderr.write(stderr_text)
    return {
        "id": directive["id"],
        "cli": directive["cli"],
        "args": list(directive.get("args", [])),
        "exit_code": exit_code,
        "stderr": stderr_text,
    }


# ---------------------------------------------------------------------------
# Halt contract — per-directive, disposition-value-aware (module docstring).
# ---------------------------------------------------------------------------


def _judgment_points_by_id(judgment_points: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {jp["id"]: jp for jp in judgment_points}


def _execute_directives(
    directives: list[dict[str, Any]],
    judgment_points: list[dict[str, Any]],
    decisions: dict[str, Any],
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
            continue
        if result.get("exit_code", 0) != 0:
            error = (
                f"{directive['cli']} exited {result['exit_code']} "
                f"(args={result.get('args', [])})"
            )
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
    return int(WorkweekApplyExitCode.SUCCESS), report


def apply(*, decisions: Optional[dict[str, Any]] = None) -> tuple[int, dict[str, Any]]:
    """`apply()` — recomputes the brief in-process (never trusts a
    caller-supplied decision object) and executes every execution-ready
    `directives[]` entry per the halt contract. `decisions` is the EM-
    resolved `{judgment_point_id: {disposition, ...}}` map — omitted
    entries leave their gated directives blocked, not auto-fired.
    """
    brief_exit_code, envelope = brief(decisions=decisions)
    if brief_exit_code != 0:
        return int(WorkweekApplyExitCode.TRANSPORT_FAIL), {
            "error": envelope.get("error", "brief() did not resolve an actionable plan"),
            "landed": [],
        }

    directives = envelope.get("directives", [])
    judgment_points = envelope.get("judgment_points", [])
    effective_decisions = decisions if decisions is not None else envelope.get("decisions", {})

    return _execute_directives(directives, judgment_points, effective_decisions)


def main(argv: list[str]) -> int:
    """`main()`'s `apply` dispatch arm — `--decisions <json>` is the one
    supported flag, mirroring `workday_complete.apply`'s CLI shape."""
    decisions: Optional[dict[str, Any]] = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--decisions":
            if i + 1 >= len(argv):
                print("workweek-complete-apply: --decisions requires a value", file=sys.stderr)
                return int(WorkweekApplyExitCode.TRANSPORT_FAIL)
            try:
                decisions = json.loads(argv[i + 1])
            except json.JSONDecodeError as exc:
                print(f"workweek-complete-apply: malformed --decisions JSON: {exc}", file=sys.stderr)
                return int(WorkweekApplyExitCode.TRANSPORT_FAIL)
            i += 2
        else:
            print(f"workweek-complete-apply: unrecognized argument {tok!r}", file=sys.stderr)
            return int(WorkweekApplyExitCode.TRANSPORT_FAIL)

    exit_code, report = apply(decisions=decisions)
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

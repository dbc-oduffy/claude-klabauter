"""
coordinator_core.workday_complete.apply — the `workday-complete` computed-
skill engine's MUTATING half, standalone-conformant per DoE-claude
`coordinator/docs/wiki/computed-skills.md` § The compute/apply split and
§ What bounds a mutating apply half.

Purpose: recomputes `brief.brief()` in-process (never trusts a caller-
supplied decision object), applies the HALT CONTRACT (per-directive,
disposition-value-aware — see `_execute_directives` below), and executes
every execution-ready `directives[]` entry through a CLOSED, literal
dispatch table naming the C1 consumes-manifest CLIs. `apply()`'s own
`for_date`/`only_mode` kwargs (mirroring `brief.brief()`'s same-named
kwargs byte-for-byte — see that function's docstring) are threaded
straight into the recompute call so a targeted `--for-date <d> --only`
ceremony run date-scopes `d_step3_5_backfill_phase_b` in the MUTATING half
too, not only the compute half brief() already covered
(`f1ced234febc8e2e09ebe8e7f3f75cdac7151300`) — without this, the recompute
here silently rebuilt the directive with neither flag and Phase B wrapped
every gap row regardless of what the ceremony run asked for. Deliberately does NOT
build or depend on `coordinator_core.contract.apply_base` (the D1 shared
mutating-apply runner) — that module is a different baton's anti-scope
surface (plan § Anti-scope); this module's directive-execution engine,
closed dispatch, and halt contract are authored locally instead (plan §
Tasks C2 body, "Apply half (the Staff Engineer F1)" paragraph).

Contract (frozen, reviewed): DoE-claude coordinator/docs/wiki/computed-skills.md
Spec backlink: DoE-claude:pln-b1-ceremony-complete-computed--9ffa54, chunk C2

HALT CONTRACT (2026-07-24 premise-check reconciliation; mirrors the pickup
exemplar's AC10 design, `claude-klabauter/docs/plans/2026-07-24-pickup-as-a-
fully-assembled-decision-sur.md`, the Director of Engineering v2 finding 1): a directive `d` gated
on judgment_point `j` (`d["depends_on"] == j["id"]`) is resolved-to-fire iff
`decisions[j_id]["disposition"]` is set AND that CHOSEN disposition's own
`resolves` list (looked up in `j["dispositions"]`) includes `d["id"]` —
never merely "some disposition was picked." A directive whose `depends_on`
is `None` always fires, regardless of any OTHER judgment point's resolution
state — this is what makes a mechanical directive (e.g. the improvement-
queue nudge) immune to an unrelated open ambiguous-dirty-tree ask blocking
it. This halts THAT ONE directive, never the whole run.

Security-load-bearing (mirrors `pickup_assemble.apply`'s own the Director of Engineering F1 /
The Staff Engineer second-pass finding #1 shape): the executable universe this module
can reach is a CLOSED CONSTRUCTION. `directives[].cli` resolves through
`_CLI_DISPATCH` — a literal, hardcoded `dict[str, Path]` naming exactly the
C1 consumes-manifest — never `getattr`, never `importlib.import_module` on
a brief-derived string, never a subprocess/shell invocation built from
`directives[].args`. Every named script is loaded ONCE, by a fixed literal
path resolved from THIS module's own location (`Path(__file__)`), and
invoked IN-PROCESS via its own `main(argv)`/`main()` entrypoint — never
spawned as a child process. An unrecognized `cli` raises before that
directive dispatches (the other ready directives this pass are unaffected —
see `_execute_directives`).

Negative-spec:
    - Do NOT add a dispatch entry resolved via `getattr`/`importlib.
      import_module`/any brief-derived string — every `_CLI_DISPATCH` key is
      written by hand in this file, and every value is a fixed
      `Path(__file__)`-relative script path.
    - Do NOT call `subprocess.run`/`Popen`/`os.system` anywhere in this
      module — every consumes-manifest CLI is loaded and invoked in-process
      via `importlib.util.spec_from_file_location` + its own `main`
      entrypoint (`_load_cli_module`/`_invoke_cli_main`), never spawned.
    - Do NOT import or compose `coordinator_core.contract.apply_base`'s
      directive-execution engine (`execute_directives`/`resolve_cli`/
      `resolve_op`) — that module (D1) is a separate baton's anti-scope
      surface; this module's own execution engine is authored locally (see
      module docstring). The ONE named exception (C7,
      `docs/plans/2026-08-19-directives-name-an-op-not-a-cli.md`) is
      `apply_base.assert_dispatchable` — the single shared admission check
      against `authz.dispatchable.ASSEMBLER_DISPATCHABLE`, called from
      `_resolve_cli` below so the membership check is never hand-copied
      into three private bodies. It does not resolve, dispatch, or execute
      anything; it only raises.
    - Do NOT auto-resolve a `judgment_points[]` entry — a directive only
      ever fires off an EXPLICIT `decisions[jp_id]["disposition"]` whose OWN
      `resolves` list names it; an unresolved (or non-terminally-resolved)
      judgment point blocks every directive naming it in `depends_on`, never
      auto-fires one.
    - Do NOT treat `recommendation` on a judgment point as a control-flow
      input anywhere in `_execute_directives` — it is offer-only, per the
      shipped `build_judgment_point` contract's own docstring.
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
from coordinator_core.workday_complete.brief import CONSUMES_MANIFEST, brief
from coordinator_core.contract.apply_base import assert_dispatchable

if TYPE_CHECKING:
    from coordinator_core.composition_budget import CompositionBudget

# ---------------------------------------------------------------------------
# Exit-code contract (apply-side, 0-4) — SEPARATE from `brief.WorkdayExitCode`
# (0-3). computed-skills.md § Exit-code contract for a mutating half requires
# each half to pin its own enumeration; this one is never reused by brief().
# Built from the shared `ceremony_common.apply_halt` ladder (C2h) so this
# module's numbering can never independently drift from
# `workweek_complete.apply`'s own.
# ---------------------------------------------------------------------------
WorkdayApplyExitCode = build_ceremony_halt_exit_codes("WorkdayApplyExitCode")


#: THE closed dispatch table (security-load-bearing — see module docstring).
#: Every key is a literal member of `brief.CONSUMES_MANIFEST`, written here
#: by hand; every value is this module's own fixed, `Path(__file__)`-relative
#: script location under `coordinator/bin/`. Never mutated at runtime.
_CLI_SCRIPT_ROOT = resolve_cli_script_root()

_CLI_DISPATCH: dict[str, Path] = {
    name: _CLI_SCRIPT_ROOT / f"{name}.py" for name in CONSUMES_MANIFEST
}

_LOADED_MODULES: dict[str, ModuleType] = {}


def _resolve_cli(cli_name: str) -> Path:
    """The one seam `directives[].cli` ever passes through. Closed over a
    literal dict — an unrecognized name raises before any directive in the
    run dispatches. Additionally gated (C7) by the shared
    `apply_base.assert_dispatchable` admission check against
    `authz.dispatchable.ASSEMBLER_DISPATCHABLE["workday_complete"]` — no
    table dispatching a registered op is left with review-only admission."""
    if cli_name not in _CLI_DISPATCH:
        raise UnrecognizedDirective(
            f"workday_complete.apply: unrecognized cli {cli_name!r} — not a "
            f"member of the consumes-manifest {sorted(_CLI_DISPATCH)!r}"
        )
    assert_dispatchable("workday_complete", cli_name)
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
    exception contract unchanged for its callers. The shared primitive
    always passes an explicit `SourceFileLoader` (superset behaviour — see
    `cli_dispatch`'s own module docstring, Contested behaviour 3), covering
    the extensionless bareword launcher shims this module's own prior
    no-explicit-loader call to `spec_from_file_location` could not load."""
    script_path = _resolve_cli(cli_name)
    if cli_name in _LOADED_MODULES:
        return _LOADED_MODULES[cli_name]
    module_name = f"_workday_complete_cli_{cli_name.replace('-', '_')}"
    try:
        module = _shared_load_cli_module(module_name, script_path)
    except ValueError as exc:
        raise UnrecognizedDirective(
            f"workday_complete.apply: could not load {cli_name!r} from {script_path}"
        ) from exc
    _LOADED_MODULES[cli_name] = module
    return module


def _invoke_cli_main(
    module: ModuleType, args: list[str], *, stdin_text: Optional[str] = None
) -> tuple[int, str, str, CliExitClass]:
    """Invokes `module.main` in-process (never a subprocess) — accepts
    either an `argv`-taking `main(argv)` or a zero-arg `main()` (the C1
    manifest's `workday-complete-step2_5-dirty-tree` script exposes the
    latter, calling `sys.exit` internally). Returns `(exit_code, stdout,
    stderr, exit_class)`: the resolved integer exit code (`main`'s own return value when
    it returns one, else the code a `SystemExit` it raises carries, else `0`
    on a clean fallthrough) paired with everything the call printed to
    stdout and, separately, to stderr.

    The fourth return value is `ceremony_common.cli_rejection.classify_cli_exit`'s
    verdict over this invocation: `CliExitClass.ARGV_REJECTED` when the
    callee raised `SystemExit(2)` with argparse-shaped stderr (the argv
    itself was rejected before any op-level code ran), else
    `CliExitClass.RETURNED` — including every zero-arg trampoline's own
    raised, semantic exit. This does not change `exit_code` itself or what
    a ceremony reports upward; it only names, for the caller, whether the
    exit code above actually means something the callee decided.

    Zero-arg `main()` trampolines (~16 consumes-manifest scripts, per the
    2026-07-26 arg-mismatch audit) are `def main() -> None` wrappers whose
    BODY still calls `sys.exit(op_main(sys.argv[1:]))` — they parse
    `sys.argv` themselves rather than accepting a caller-supplied argv. Left
    alone, calling `main_fn()` bare hands them the APPLY PROCESS's own
    `sys.argv` (e.g. `--decisions <json>`), silently discarding `args` and
    leaking unrelated tokens into the wrapped op. This splices `args` into
    `sys.argv` (with a dummy `argv[0]`, restored in `finally`) for the
    duration of the call so a directive's emitted args reach the op the
    same way they would for an `argv`-taking `main(argv)` — one fix here
    covers every zero-arg trampoline in the manifest, rather than patching
    each script's body individually.

    Stdin wiring (2026-07-26 backfill-leg fix, general to ANY directive that
    declares `stdin_from` — see `brief._directive`'s docstring): when
    `stdin_text` is not `None`, `sys.stdin` is swapped for a fresh
    `io.StringIO(stdin_text)` for the duration of the call and restored in
    `finally`, mirroring the `sys.argv` splice above. When `stdin_text` IS
    `None` (the overwhelming majority of directives — none of them declare
    `stdin_from`), `sys.stdin` is left completely untouched: this module
    never opens or redirects a stdin stream for a directive that didn't ask
    for one, so a directive with no producer can never block reading a real
    stdin. `module`'s own stdout is captured via `contextlib.redirect_stdout`
    rather than left to print straight through, so a downstream directive
    naming this one in its own `stdin_from` can consume the text; the caller
    (`_dispatch_directive`) re-emits it onto apply's own stdout afterward so
    ceremony-run visibility is unchanged.

    Stderr capture (2026-07-27 finding, mirrors `workstream_complete.apply`'s
    identical fix): `module`'s own stderr is captured the same way, via
    `contextlib.redirect_stderr` — a non-zero directive's diagnostic text
    (e.g. `wsc-tail.py`'s exit-2 diagnostics block, printed unconditionally
    to `sys.stderr`) was previously neither captured nor threaded into
    `_dispatch_directive`'s result dict at all, so it never reached
    `report["results"]`/`report["failed"]` — the one place a caller (the
    skill, the EM) could read it back. `_dispatch_directive` re-emits it
    onto apply's own stderr afterward, mirroring the stdout re-emission, so
    live console visibility is unchanged.

    Routes through the shared `ceremony_common.cli_dispatch.invoke_cli_main`
    primitive (C1/C5) — this module's own 4-tuple return shape IS the
    primitive's superset shape byte-for-byte (this module was the source of
    truth C1 built the primitive's signature from), so no narrowing or
    translation is needed here beyond the `ValueError`-on-no-`main`
    ->`UnrecognizedDirective` mapping this module's own exception contract
    already required."""
    try:
        return _shared_invoke_cli_main(module, args, stdin_text=stdin_text)
    except ValueError as exc:
        raise UnrecognizedDirective(
            f"workday_complete.apply: {module.__name__} exposes no main() entrypoint"
        ) from exc


def _dispatch_directive(
    directive: dict[str, Any], *, stdin_text: Optional[str] = None
) -> dict[str, Any]:
    """Loads and invokes the one CLI a single `directives[]` entry names,
    returning a small result record. Raises `UnrecognizedDirective` before
    any dispatch on an unrecognized `cli` (never a partial/silent skip).
    `stdin_text` (see `_invoke_cli_main`) is `apply._execute_directives`'s
    resolved stdin for this directive — `None` unless the directive declared
    `stdin_from` AND its producer already landed this pass. The producing
    CLI's own captured stdout and stderr are each re-emitted onto apply's
    own stdout/stderr here (see `_invoke_cli_main`'s docstring) so nothing
    that used to print to the ceremony run's console goes silent."""
    module = _load_cli_module(directive["cli"])
    exit_code, stdout_text, stderr_text, exit_class = _invoke_cli_main(
        module, directive.get("args", []), stdin_text=stdin_text
    )
    if stdout_text:
        sys.stdout.write(stdout_text)
    if stderr_text:
        sys.stderr.write(stderr_text)
    return {
        "id": directive["id"],
        "cli": directive["cli"],
        "args": list(directive.get("args", [])),
        "exit_code": exit_code,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "exit_class": exit_class.value,
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
    `exit_code` (2026-07-26 arg-mismatch audit, systemic finding 1: a
    directive previously joined `landed` after any dispatch that did not
    *raise* a Python exception, so an argparse usage error, a gate's
    business-fail, or any other non-zero exit read as ceremony success —
    the report was structurally incapable of showing the failure it was
    supposed to record). `report["degraded"]` names directive ids whose
    dispatch returned a non-zero `exit_code` while the directive itself
    carries `best_effort: True` (2026-08-08 "a best-effort directive cannot
    fail a ceremony" — emit-cadence's pre-conversion bash `|| echo ...
    skipped` fail-open shape had no directive-runner equivalent, so a
    transport hiccup on that one step read as a ceremony that needed
    reconciling). A `degraded` entry is NOT silence — it carries the same
    `{"id", "error"}` shape as `failed`, the operator still sees it, it
    simply never joins `failed` and therefore never moves the exit code.
    Exit code: `HALTED_AT_JUDGMENT` when anything was blocked and nothing
    failed; `PARTIAL_MUTATION` when something failed but something else
    also landed; `DIRECTIVE_FAILED` when something failed and nothing
    landed at all; `SUCCESS` when every directive either fired clean or
    only degraded (a `best_effort` non-zero exit alone does not prevent
    `SUCCESS`).

    Stdin wiring (2026-07-26 backfill-leg fix): `stdout_by_id` accumulates
    the captured stdout of every directive that LANDED this pass (exit 0),
    keyed by directive id. A directive naming a producer in `stdin_from`
    (see `brief._directive`'s docstring) is fed that producer's captured
    text via `_dispatch_directive`'s `stdin_text`. If the named producer
    never lands this pass — it failed, stayed blocked, or (a brief-authoring
    bug) is sequenced AFTER the consumer in `directives` — the consumer is
    recorded in `failed` WITHOUT dispatching: piping empty/absent stdin into
    a directive that expects real input is exactly the silent-Phase-B-no-op
    shape the memo reported, so refusing to dispatch (rather than feeding an
    empty stream and letting the CLI's own exit code decide) keeps the
    honesty property `e2f3a25f` established — the failure is visible on the
    id whose input was actually missing, not laundered through a downstream
    CLI's own no-input-is-success shortcut. An `already_satisfied` producer
    is a THIRD case, distinct from failed/blocked: it is genuinely in
    `landed` (see below) but produced no stdout this pass, so a `stdin_from`
    consumer naming it still refuses to dispatch — the refusal is correct,
    only the message differs, naming the id as already-satisfied-with-no-
    output rather than as never-landed (2026-08-08 defect C)."""
    jp_by_id = _judgment_points_by_id(judgment_points)
    landed: list[str] = []
    blocked: list[str] = []
    failed: list[dict[str, Any]] = []
    degraded: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    stdout_by_id: dict[str, str] = {}
    already_satisfied_ids: set[str] = set()

    pre_mutation_breach = budget_check_pre_mutation(composition_budget)
    if pre_mutation_breach is not None:
        return int(WorkdayApplyExitCode.DIRECTIVE_FAILED), {
            "landed": [],
            "blocked": [],
            "failed": [],
            "degraded": [],
            "results": [],
            "budget_breach": pre_mutation_breach,
        }

    # Whole-run admission pre-pass (F1, cold review 2026-08-19): an
    # un-admitted `cli` must fail the WHOLE run before any directive
    # dispatches, mirroring `apply_base.execute_directives`'s own
    # whole-list pre-validation — never degrade to a per-directive
    # skip-and-continue on an admission/manifest-membership refusal. This
    # is admission-only (`_resolve_cli` raising `UnrecognizedDirective`);
    # the per-directive halt contract below (gates, non-zero exits,
    # `best_effort`) is untouched. Scope, stated because the first cut of
    # this pre-pass widened it silently: every directive that CAN dispatch in
    # this run is checked, including a gate-blocked one; an `already_satisfied`
    # directive is skipped, because it cannot.
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
        return int(WorkdayApplyExitCode.DIRECTIVE_FAILED), {
            "landed": [],
            "blocked": [],
            "failed": [{"id": None, "error": str(exc)}],
            "degraded": [],
            "results": [],
        }

    for directive in directives:
        if directive.get("already_satisfied"):
            landed.append(directive["id"])
            already_satisfied_ids.add(directive["id"])
            continue
        try:
            gate_open = _directive_gate_open(directive, jp_by_id, decisions)
        except Exception as exc:  # noqa: BLE001 - malformed envelope is per-directive
            failed.append({
                "id": directive["id"],
                "error": f"gate evaluation error: {type(exc).__name__}: {exc}",
            })
            continue
        if not gate_open:
            blocked.append(directive["id"])
            continue

        stdin_from = directive.get("stdin_from")
        stdin_text: Optional[str] = None
        if stdin_from is not None:
            if stdin_from not in stdout_by_id:
                if stdin_from in already_satisfied_ids:
                    reason = (
                        f"stdin producer {stdin_from!r} was already-satisfied "
                        f"before this pass and produced no stdout to feed "
                        f"{directive['id']!r} — refusing to dispatch with "
                        "missing stdin rather than feeding an empty stream"
                    )
                else:
                    reason = (
                        f"stdin producer {stdin_from!r} did not land before "
                        f"{directive['id']!r} this pass (failed, blocked, or "
                        "not yet dispatched) — refusing to dispatch with "
                        "missing stdin rather than feeding an empty stream"
                    )
                failed.append({"id": directive["id"], "error": reason})
                continue
            stdin_text = stdout_by_id[stdin_from]

        try:
            result = _dispatch_directive(directive, stdin_text=stdin_text)
        except Exception as exc:  # noqa: BLE001 - closed-table dispatch failure
            # type(exc).__name__ prefix, not bare str(exc): an
            # io.UnsupportedOperation("fileno") formats as the single word
            # "fileno" under str() alone, with no type and no traceback --
            # exactly what hid the subprocess-under-capture-buffer defect
            # run_forwarding (coordinator_core.win_portability) fixes.
            failed.append({
                "id": directive["id"],
                "error": f"{type(exc).__name__}: {exc}",
            })
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
        stdout_by_id[directive["id"]] = result.get("stdout", "")

    report = {
        "landed": landed,
        "blocked": blocked,
        "failed": failed,
        "degraded": degraded,
        "results": results,
    }

    if failed and landed:
        return int(WorkdayApplyExitCode.PARTIAL_MUTATION), report
    if failed:
        return int(WorkdayApplyExitCode.DIRECTIVE_FAILED), report
    if blocked:
        return int(WorkdayApplyExitCode.HALTED_AT_JUDGMENT), report
    post_mutation_breach = budget_check_post_mutation(composition_budget)
    if post_mutation_breach is not None:
        report["budget_breach"] = post_mutation_breach
    return int(WorkdayApplyExitCode.SUCCESS), report


def apply(
    *,
    decisions: Optional[dict[str, Any]] = None,
    for_date: Optional[str] = None,
    only_mode: bool = False,
) -> tuple[int, dict[str, Any]]:
    """`apply()` — recomputes the brief in-process (never trusts a
    caller-supplied decision object) and executes every execution-ready
    `directives[]` entry per the halt contract. `decisions` is the EM-
    resolved `{judgment_point_id: {disposition, ...}}` map — omitted
    entries leave their gated directives blocked, not auto-fired.

    `for_date`/`only_mode` (default `None`/`False` — reproduces prior
    behavior byte-for-byte when omitted) are threaded straight into the
    `brief(decisions=..., for_date=..., only_mode=...)` recompute call
    below, unvalidated here: `brief()` already regex-gates and
    `date.fromisoformat`-validates `for_date`, returning
    `WorkdayExitCode.USAGE` with an `error` key on a malformed value, and
    the non-zero-`brief_exit_code` branch immediately below already maps
    ANY non-zero brief exit (USAGE included) to
    `WorkdayApplyExitCode.TRANSPORT_FAIL` carrying that same `error`
    message — so a bad `--for-date` surfaces here exactly as it does
    through `brief.main`, with no separate validation needed in this
    module. They date-scope `d_step3_5_backfill_phase_b` ONLY — see
    `brief._build_directives`'s docstring for the exact args shape; every
    other directive in the recomputed envelope is unaffected.

    Constructs a fresh composition budget (never module-level — see
    `telemetry.composition_record`'s own PER-CALL FACTORY note) and flushes
    exactly one record for it in a `finally`, regardless of outcome
    (§ `flush_composition_record`'s own docstring).
    """
    composition_budget = make_fleet_budget("workday_complete")
    outcome = "directive_failed"
    try:
        brief_exit_code, envelope = brief(
            decisions=decisions, for_date=for_date, only_mode=only_mode
        )
        if brief_exit_code != 0:
            return int(WorkdayApplyExitCode.TRANSPORT_FAIL), {
                "error": envelope.get("error", "brief() did not resolve an actionable plan"),
                "landed": [],
            }

        directives = envelope.get("directives", [])
        judgment_points = envelope.get("judgment_points", [])
        effective_decisions = decisions if decisions is not None else envelope.get("decisions", {})

        exit_code, report = _execute_directives(
            directives, judgment_points, effective_decisions, composition_budget=composition_budget
        )
        if exit_code == int(WorkdayApplyExitCode.SUCCESS):
            outcome = "success"
        elif exit_code == int(WorkdayApplyExitCode.PARTIAL_MUTATION):
            outcome = "partial_mutation"
        return exit_code, report
    finally:
        flush_composition_record(composition_budget, outcome)


def main(argv: list[str]) -> int:
    """`main()`'s `apply` dispatch arm — `--decisions <json>` (or its
    file-borne sibling `--decisions-file <path>`), `--for-date <date>`, and
    `--only` are the supported flags, mirroring
    `brief.main`'s own `--for-date`/`--only` spelling (this module keeps
    its existing hand-rolled token loop rather than switching to
    `brief.main`'s argparse, matching this file's established style).
    `--for-date`/`--only` are forwarded verbatim to `apply()`'s same-named
    kwargs, which thread them into the recomputed brief — see `apply()`'s
    docstring for the date-scope contract and the brief-USAGE ->
    TRANSPORT_FAIL propagation. A `--for-date` with a missing value fails
    the same way a `--decisions` with a missing value already does."""
    decisions: Optional[dict[str, Any]] = None
    for_date: Optional[str] = None
    only_mode = False
    conflict = detect_conflicting_payload_channels(argv)
    if conflict is not None:
        print(f"workday-complete-apply: {conflict}", file=sys.stderr)
        return int(WorkdayApplyExitCode.TRANSPORT_FAIL)
    i = 0
    while i < len(argv):
        tok = argv[i]
        if (payload := resolve_json_payload_flag(argv, i)).consumed:
            if payload.error is not None:
                print(f"workday-complete-apply: {payload.error}", file=sys.stderr)
                return int(WorkdayApplyExitCode.TRANSPORT_FAIL)
            decisions = payload.value
            i += payload.consumed
        elif tok == "--for-date":
            if i + 1 >= len(argv):
                print("workday-complete-apply: --for-date requires a value", file=sys.stderr)
                return int(WorkdayApplyExitCode.TRANSPORT_FAIL)
            for_date = argv[i + 1]
            i += 2
        elif tok == "--only":
            only_mode = True
            i += 1
        else:
            print(f"workday-complete-apply: unrecognized argument {tok!r}", file=sys.stderr)
            return int(WorkdayApplyExitCode.TRANSPORT_FAIL)

    exit_code, report = apply(decisions=decisions, for_date=for_date, only_mode=only_mode)
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

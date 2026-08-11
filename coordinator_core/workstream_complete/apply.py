"""
coordinator_core.workstream_complete.apply — the `workstream-complete`
computed-skill engine's MUTATING half, standalone-conformant per
Example-doctrine-repo `coordinator/docs/wiki/computed-skills.md` § The compute/apply
split and § What bounds a mutating apply half. Mirrors
`coordinator_core.workday_complete.apply` and
`coordinator_core.workweek_complete.apply`'s shape (Lineage 2, D-1 of the
spec-backlinked plan) — see those modules for the shared design rationale
(closed dispatch, halt contract, in-process invocation); this module's own
deviations from them are named below.

Purpose: recomputes `workstream_complete.brief()` in-process (never trusts
a caller-supplied decision object), applies the HALT CONTRACT (per-
directive, disposition-value-aware — see `_execute_directives` below), and
executes every execution-ready `directives[]` entry through a CLOSED,
literal dispatch table naming every `CONSUMES_MANIFEST` member (C3).
Deliberately does NOT build or depend on `coordinator_core.contract.
apply_base` (the D1 shared mutating-apply runner) — see Negative-spec.

Best-effort directives (docs/plans/2026-08-08-a-best-effort-directive-
cannot-fail-a-ce.md, chunk C1): a directive carrying `best_effort: True`
(absent key means `False` — every pre-existing directive keeps today's
behaviour) that dispatches and exits non-zero lands in `report["degraded"]`
instead of `report["failed"]` — recorded, never silent, but never able to
move the exit code off `SUCCESS` the way a `failed` entry does. The
captured stderr (when the CLI produced any) is folded into the `error`
string on BOTH the `failed` and `degraded` paths, after the existing
`"<cli> exited <n> (args=[...])"` prefix — the field an operator actually
reads back previously named only the args, which is why every one of the
three cross-repo reporters this plan cites suspected the argument instead
of the real cause.

Contract (frozen, reviewed): example-doctrine-repo coordinator/docs/wiki/computed-skills.md
Spec backlink: docs/plans/2026-07-26-workstream-complete-computed-frontage.md, chunk C4
Spec backlink (no-commit row guard): example-doctrine-repo docs/plans/2026-07-29-pm-approved-
provenance-write-time-closure-gate.md, chunk C13

Deviation from the workday/workweek exemplars (both noted, both forced by
`workstream_complete`'s own landed shape, not authored fresh here):

    1. `workstream_complete.brief()` does not return a `(exit_code,
       envelope)` tuple the way `workday_complete.brief`/`workweek_complete.
       brief` do — it returns the envelope `Mapping` directly and raises
       `TransportFailure` on a repo-root/bin-module resolution failure
       (`__init__.py:683-692`). `apply()` below adapts to that shape by
       catching `TransportFailure` itself rather than checking a returned
       code, everything downstream (directive extraction, the halt
       contract) is unchanged.
    2. The C4 plan body's own text says to pin and assert-equal a 4-member
       CLI tuple (`wsc-coverage-gate-runner.py`,
       `check-workstream-complete-deletion-blocks.py`, `wsc-close.py`,
       `wsc-tail.py`) against `CONSUMES_MANIFEST`. That text predates C3
       landing the full 7-submodule wiring (C2a-C2i): C3's actual
       `CONSUMES_MANIFEST` carries 20 members, and
       `test_workstream_complete_contract.py`'s phantom-verb guard
       (`test_no_directive_names_a_cli_outside_the_manifest`) already
       enforces that every directive's `cli` is a `CONSUMES_MANIFEST`
       member — a hand-typed 4-tuple asserted equal to a 20-member manifest
       would fail on import against current disk-truth, not protect
       anything. This module instead dispatches over the FULL
       `CONSUMES_MANIFEST` (mirroring `workday_complete.apply`/
       `workweek_complete.apply`'s own `{name: path for name in
       CONSUMES_MANIFEST}` construction) so `apply()` can execute every
       directive `brief()` is capable of emitting, not only the four
       "legacy" Convert #2 ones. The four names above remain individually
       correct and are still `CONSUMES_MANIFEST` members; they are simply
       not the whole manifest any more. Flagged to the EM for plan-text
       reconciliation — this is a disk-truth-over-stale-plan-text call, not
       a silent scope cut.
    3. `directives_completion.build_reconcile_completion_commits_directive`
       emits `d-reconcile-completion-commits` with a LITERAL, unresolved
       inter-directive token in its args (`RECONCILE_ENTRY_PATH_TOKEN`,
       `"{d-complete-entry.entry_path}"`) in place of the real completion-
       entry path, which `d-complete-entry` (its `depends_on` producer)
       only knows at RUN time (that CLI's own idempotency guard, LoE
       computation, and today's-date filename derivation all happen
       in-process — see `directives_completion.py`'s module docstring,
       Design note 3). `workday_complete.apply`/`workweek_complete.apply`
       already solve the general "thread a producer's runtime output into
       a later directive" problem via `stdin_from` (pipe a producer's
       captured stdout into a consumer's stdin) — but
       `reconcile-completion-commits.py` takes its entry-path as a
       POSITIONAL ARGUMENT, not stdin content (confirmed by reading that
       CLI: it never touches `sys.stdin`), so `stdin_from` cannot express
       this directive's actual need. This module extends the family's own
       idiom rather than inventing a new templating language: `_invoke_cli_
       main` captures stdout exactly as the stdin_from family already does
       (mirrors `workday_complete.apply._invoke_cli_main`'s `stdout_buf`
       capture), and `_resolve_arg_tokens` (below) substitutes a
       `{<producer-id>.entry_path}` token in a directive's `args` with the
       FIRST LINE of that producer's captured stdout — matching
       `coordinator-complete-entry.py`'s own single-line `print(entry_path)`
       contract (`coordinator_core/ops/coordinator_complete_entry.py:726`).
       An unresolvable token (producer never landed this pass, landed with
       no stdout, or any `{...}` token this function doesn't recognize
       survives substitution) fails the directive loud into `report["failed"]`
       — see `_resolve_arg_tokens` — it is NEVER dispatched with the literal
       token string as a live argument.

       Defect C fix (same plan, chunk C1): an `already_satisfied` directive
       is registered in `stdout_by_id` with `""` (empty string) the moment
       `_execute_directives` appends it to `landed`, rather than being
       skipped entirely. A `{<id>.landed}` token naming such a producer now
       resolves (it substitutes the empty string, same as any other landed
       producer's ordering-only use) instead of failing with "did not land
       before this directive this pass" — a false statement about a
       directive that IS in `landed`. A `{<id>.entry_path}` token naming the
       same producer still fails, correctly: the empty-stdout guard in
       `_resolve_arg_tokens` fires on the registered `""`, producing the
       honest "landed but captured no stdout to resolve its {entry_path}
       token from" rather than the dishonest "did not land" message.
    4. `directives_commit_tail.build_release_plan_claim_directive`/
       `build_emit_cadence_directive` set `depends_on="d-run-wsc-tail"` —
       a sibling-directive id, which `_directive_gate_open` deliberately
       does not gate on (see that function's docstring: gating a
       directive-id dependency there would route a failed producer into
       `HALTED_AT_JUDGMENT`, a state no disposition can ever clear).
       Neither directive's CLI needs a VALUE from `d-run-wsc-tail`'s
       output, so the `.entry_path` token shape doesn't fit — but each
       still needs to refuse to dispatch when the commit-tail producer
       never landed this pass (blocked at a judgment point, or failed).
       `_resolve_arg_tokens`'s `.landed` field (below) covers exactly this
       case: an ordering-only token that substitutes to the empty string
       once its producer is confirmed landed, otherwise fails the
       directive loud the same way `.entry_path` does.

No-commit row guard (example-doctrine-repo docs/plans/2026-07-29-pm-approved-
provenance-write-time-closure-gate.md, chunk C13): before dispatching any
directive, `apply()` checks whether the governing plan carries a
commit-required task-spine row (disposition `open`/`coded`) that this
session's commit-coverage oracle (`close_out_and_stamp._determine_shipped`,
reused verbatim, never re-derived) found no covering commit for. Verified
against the running code before this chunk landed: NO such guard existed
anywhere in this module or `judgments.py` — every prior `commit`-adjacent
gate here (`jp-commit-subject-missing`, `jp-completion-entry-scaffold`)
concerns THIS session's own commit, never a plan-spine row's. A no-commit
row must resolve to one of five named exits (`shipped`/`spun-off`/
`backlogged`/`wont-do`/`carried-forward`, `judgments.
build_no_commit_row_disposition_judgment_point` — per the five-exit ruling,
cross-repo/inbox/2026-08-05-example-doctrine-repo-em-plan-tasks-five-exits-ruling.md)
— "deferred, ignore the guard" is deliberately not a sixth option. This
check gates the WHOLE apply, not one directive's `depends_on`: unlike
every judgment point
`brief()` itself emits (which gate individual directives via the halt
contract below), this judgment is computed independently, in `apply()`
itself rather than in `__init__.py`'s `brief()`/`_build_preserved_
judgment_points` — this chunk's own file scope is `apply.py`+`judgments.py`
only, so governing-plan resolution is recomputed here via the same
`directives_lessons_plan.resolve_governing_plan_with_source` call `brief()`
itself makes (not a second implementation), narrowed to the
decisions-slug/decisions-path/fixed-fallback legs (the chain-terminal
consumed-handoff `governing_plan:` frontmatter leg lives behind
`__init__.py`'s own session-shape gate machinery, out of this chunk's
scope — see `_no_commit_row_judgment`'s own docstring for the exact
narrowing and why "resolve to None, check nothing" is the safe direction
there). Anti-scope, restated from the governing plan: this is NOT a hard
block on finishing a workstream with rows genuinely `open` and carried
forward (AC22) — only the SILENT sixth exit (an unclassified no-commit
row) halts.

HALT CONTRACT: identical to `workday_complete.apply`/`workweek_complete.
apply` — composed, not re-derived, from `coordinator_core.ceremony_common.
apply_halt` (C2g/C2h): `_directive_gate_open`, `UnrecognizedDirective`, and
the shared `build_ceremony_halt_exit_codes` ladder. A directive `d` gated
on judgment_point `j` fires iff `decisions[j_id]["disposition"]` names a
disposition whose OWN `resolves` list includes `d["id"]` — never merely
"some disposition was picked." A directive whose `depends_on` is `None`
always fires.

Security-load-bearing (mirrors the two exemplars' own shape): the
executable universe this module can reach is a CLOSED CONSTRUCTION.
`directives[].cli` resolves through `_CLI_DISPATCH` — a literal, hardcoded
`dict[str, Path]` naming exactly `CONSUMES_MANIFEST` — never `getattr`,
never `importlib.import_module` on a brief-derived string, never a
subprocess/shell invocation built from `directives[].args`. Every named
script is loaded ONCE, by a fixed literal path resolved from THIS module's
own location (`Path(__file__)`), and invoked IN-PROCESS via its own
`main(argv)`/`main()` entrypoint — never spawned as a child process. An
unrecognized `cli` raises before that directive dispatches (the other
ready directives this pass are unaffected — see `_execute_directives`).

Known dispatch-table gap (documented, not a defect this chunk introduces):
`scan_unresolved_ubt_records.py` (`d-run-ubt-pending-check`'s cli, per
`directives_review.py`) has no `coordinator/bin/` CLI wrapper on disk —
the only real module is `coordinator_core/ops/scan_unresolved_ubt_records.
py`, which exposes a bare `scan_unresolved_ubt_records(caller_worktree:
Path) -> list[str]` function, not an argv-taking `main`. `__init__.py`'s
own module docstring already names this exact class of gap as legitimate,
expected residual (several `CONSUMES_MANIFEST` members "genuinely cannot
fire under the sweep"). This module does not special-case it: the entry
still occupies `_CLI_DISPATCH` (so an unrecognized-cli check never fires
for it) and resolves to a literal, non-existent `coordinator/bin/
scan_unresolved_ubt_records.py` path; if `d-run-ubt-pending-check` is ever
 ready and dispatched, `_load_cli_module` raises `FileNotFoundError`,
which `_execute_directives` catches and records in `report["failed"]` —
the same per-directive-halt path any other dispatch failure takes, never a
whole-run crash. Building a real `coordinator/bin/` wrapper for this
script is out of this chunk's file scope (apply.py only).

Negative-spec:
    - Do NOT add a dispatch entry resolved via `getattr`/`importlib.
      import_module`/any brief-derived string — every `_CLI_DISPATCH` key
      is a literal `CONSUMES_MANIFEST` member, and every value is a fixed
      `Path(__file__)`-relative script path.
    - Do NOT call `subprocess.run`/`Popen`/`os.system` anywhere in this
      module — every consumes-manifest CLI is loaded and invoked in-process
      via `importlib.util.spec_from_file_location` + its own `main`
      entrypoint, never spawned.
    - Do NOT import or compose `coordinator_core.contract.apply_base` —
      that module (D1) is a separate baton's anti-scope surface (plan §
      Anti-scope); this module's directive-execution engine, closed
      dispatch, and halt-contract composition are authored/assembled
      locally instead (this module composes the trio from
      `ceremony_common.apply_halt` — see module docstring above — it does
      not hand-copy a third instance of it, and it does not reach for
      `apply_base`).
    - Do NOT auto-resolve a `judgment_points[]` entry — a directive only
      ever fires off an EXPLICIT `decisions[jp_id]["disposition"]` whose
      OWN `resolves` list names it; an unresolved (or non-terminally-
      resolved) judgment point blocks every directive naming it in
      `depends_on`, never auto-fires one.
    - Do NOT treat `recommendation` on a judgment point as a control-flow
      input anywhere in `_execute_directives` — it is offer-only, per the
      shipped `build_judgment_point` contract's own docstring.
    - Do NOT hand-copy `_directive_gate_open`/`_disposition_resolves_
      directive`/the exit-code ladder locally — compose them from
      `coordinator_core.ceremony_common.apply_halt` (this is the exact
      duplication that module's own extraction context paragraph exists to
      prevent; see that module's docstring).
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import inspect
import io
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Optional

from coordinator_core.ceremony_common.apply_halt import (
    UnrecognizedDirective,
    _directive_gate_open,
    _disposition_resolves_directive,
    _normalize_depends_on,
    assert_disjoint_dependency_namespaces,
    build_ceremony_halt_exit_codes,
)
from coordinator_core.execute_plan_assemble.close_out_and_stamp import _determine_shipped
from coordinator_core.pickup_assemble import resolve_repo_root  # zero-spawn `.git` read-model
from coordinator_core.workstream_complete import CONSUMES_MANIFEST, TransportFailure, brief
from coordinator_core.workstream_complete import directives_lessons_plan
from coordinator_core.workstream_complete import directives_review
from coordinator_core.workstream_complete import judgments as _judgments

# ---------------------------------------------------------------------------
# Exit-code contract (apply-side) — SEPARATE from `__init__.py`'s own
# EXIT_OK/EXIT_BUSINESS_FAIL/EXIT_USAGE/EXIT_TRANSPORT_FAIL (the `brief`
# subcommand's ladder). Built from the shared `ceremony_common.apply_halt`
# ladder (C2g/C2h) so this module's numbering can never independently drift
# from `workday_complete.apply.WorkdayApplyExitCode`/`workweek_complete.
# apply.WorkweekApplyExitCode`'s own.
# ---------------------------------------------------------------------------
WorkstreamApplyExitCode = build_ceremony_halt_exit_codes("WorkstreamApplyExitCode")

#: The four "legacy" Convert #2 CLI names the C4 plan body names by hand —
#: still individually correct, still `CONSUMES_MANIFEST` members, but no
#: longer the whole manifest (see module docstring, deviation 2). Kept as a
#: named constant (not inlined) purely so a future reader can grep for
#: exactly the four the plan text called out, without this module treating
#: them as an exhaustive dispatch boundary.
_LEGACY_CONVERT2_CLI_NAMES: tuple[str, ...] = (
    "wsc-coverage-gate-runner",
    "check-workstream-complete-deletion-blocks",
    "wsc-close",
    "wsc-tail",
)
assert set(_LEGACY_CONVERT2_CLI_NAMES) <= set(CONSUMES_MANIFEST), (
    "workstream_complete.apply: the four legacy Convert #2 CLI names must "
    "remain a subset of CONSUMES_MANIFEST (C1's single oracle for the "
    "tuple) — if this fails, either a name was renamed on the manifest "
    "side or this module's own pinned constant drifted from it"
)

#: THE closed dispatch table (security-load-bearing — see module docstring).
#: Every key is a literal member of `CONSUMES_MANIFEST` (imported from
#: `workstream_complete.__init__`, C3 — the single oracle for the tuple);
#: every value is this module's own fixed, `Path(__file__)`-relative script
#: location under `coordinator/bin/`. Resolved once at import time — never
#: mutated at runtime, never resolved via glob/search.
_CLI_SCRIPT_ROOT = Path(__file__).resolve().parents[2] / "coordinator" / "bin"


def _resolve_script_path(name: str) -> Path:
    """Every `CONSUMES_MANIFEST` member is a bareword (no `.py` suffix) —
    the shape the installed `$COORDINATOR_SETTINGS_HOME/bin/` forwarders
    expose to a human/agent reader of `directives[].cli`. On disk under
    `coordinator/bin/`, a CLI ships as either `<name>.py` or an
    extensionless launcher shim `<name>` — both shapes are fixed, literal
    candidates checked in that order (mirrors `workweek_complete.apply.
    _resolve_script_path`); this is not a glob/search, just a two-candidate
    literal lookup. A CLI backed by a real `.py` script (e.g. `wsc-tail`)
    resolves on the first candidate; a CLI that is ALREADY a bareword shim
    on disk (e.g. `session-claim-cli`) resolves on the second. Neither
    candidate existing on disk (see module docstring's "Known
    dispatch-table gap" paragraph) is tolerated here — resolution still
    returns a path, and any failure to load it surfaces per-directive at
    dispatch time, never at import time."""
    py_path = _CLI_SCRIPT_ROOT / f"{name}.py"
    if py_path.exists():
        return py_path
    return _CLI_SCRIPT_ROOT / name


_CLI_DISPATCH: dict[str, Path] = {name: _resolve_script_path(name) for name in CONSUMES_MANIFEST}

_LOADED_MODULES: dict[str, ModuleType] = {}


def _resolve_cli(cli_name: str) -> Path:
    """The one seam `directives[].cli` ever passes through. Closed over a
    literal dict — an unrecognized name raises before any directive in the
    run dispatches."""
    if cli_name not in _CLI_DISPATCH:
        raise UnrecognizedDirective(
            f"workstream_complete.apply: unrecognized cli {cli_name!r} — not a "
            f"member of the consumes-manifest {sorted(_CLI_DISPATCH)!r}"
        )
    return _CLI_DISPATCH[cli_name]


def _load_cli_module(cli_name: str) -> ModuleType:
    """Loads (once, cached) the script named by `cli_name` via a fixed
    literal path — never a brief-derived import target. Never spawns a
    subprocess. Raises whatever `importlib`/the filesystem raises (e.g.
    `FileNotFoundError` for the documented `scan_unresolved_ubt_records.py`
    gap) — the caller (`_dispatch_directive`, via `_execute_directives`)
    catches that as an ordinary per-directive dispatch failure."""
    if cli_name in _LOADED_MODULES:
        return _LOADED_MODULES[cli_name]
    script_path = _resolve_cli(cli_name)
    module_name = f"_workstream_complete_cli_{cli_name.replace('-', '_').replace('.', '_')}"
    # Some consumes-manifest scripts are bareword launcher shims with no
    # `.py` suffix — `spec_from_file_location` cannot infer a source loader
    # from an extensionless path, so pass `SourceFileLoader` explicitly
    # (mirrors `workweek_complete.apply._load_cli_module`'s identical fix).
    loader = importlib.machinery.SourceFileLoader(module_name, str(script_path))
    spec = importlib.util.spec_from_file_location(module_name, script_path, loader=loader)
    if spec is None or spec.loader is None:
        raise UnrecognizedDirective(
            f"workstream_complete.apply: could not load {cli_name!r} from {script_path}"
        )
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec — some consumes-manifest scripts
    # define `@dataclass`-decorated classes whose class-body execution
    # resolves `sys.modules[cls.__module__]` (dataclasses' own
    # forward-ref-eval path); an unregistered module makes that lookup
    # return `None` and crash the load with an unrelated AttributeError.
    # `workday_complete.apply`/`workweek_complete.apply`'s `_load_cli_module`
    # carry the identical fix (Review: code-reviewer — Finding 2).
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    _LOADED_MODULES[cli_name] = module
    return module


def _invoke_cli_main(module: ModuleType, args: list[str]) -> tuple[int, str, str]:
    """Invokes `module.main` in-process (never a subprocess) — accepts
    either an `argv`-taking `main(argv)` or a zero-arg `main()`. Returns
    `(exit_code, stdout, stderr)`: the resolved integer exit code (`main`'s
    own return value when it returns one, else the code a `SystemExit` it
    raises carries, else `0` on a clean fallthrough) paired with everything
    the call printed to stdout and, separately, to stderr.

    Zero-arg `main()` trampolines (per the 2026-07-26 arg-mismatch audit;
    mirrors `workday_complete.apply`/`workweek_complete.apply`'s identical
    fix) are `def main() -> None` wrappers whose body still calls
    `sys.exit(op_main(sys.argv[1:]))` — they parse `sys.argv` themselves
    rather than accepting a caller-supplied argv. Calling `main_fn()` bare
    hands them the APPLY PROCESS's own `sys.argv`, silently discarding
    `args`. This splices `args` into `sys.argv` (dummy `argv[0]`, restored
    in `finally`) for the duration of the call so a directive's emitted
    args reach the op the same way an `argv`-taking `main(argv)` would
    receive them — checked at every dispatch (not just once), since the
    closed table spans 20 heterogeneous CLIs and either shape may appear.

    Stdout capture (module docstring, deviation 3): `module`'s own stdout
    is captured via `contextlib.redirect_stdout` rather than left to print
    straight through, so `_execute_directives` can thread a producer
    directive's captured text into a later directive's `args` via
    `_resolve_arg_tokens`'s `{<id>.entry_path}` / `{<id>.landed}` substitution — mirrors
    `workday_complete.apply._invoke_cli_main`'s identical capture (there,
    used for `stdin_from` piping rather than arg-token substitution; the
    capture mechanism itself is the same). `_dispatch_directive` re-emits
    the captured text onto apply's own stdout afterward so ceremony-run
    console visibility is unchanged for every directive, producer or not.

    Stderr capture (2026-07-27 finding): `module`'s own stderr is captured
    the same way, via `contextlib.redirect_stderr` — a non-zero directive's
    diagnostic text (e.g. `wsc-tail.py`'s exit-2 diagnostics block, printed
    unconditionally to `sys.stderr`) was previously neither captured nor
    threaded into `_dispatch_directive`'s result dict at all, so it never
    reached `report["results"]`/`report["failed"]` — the one place a
    caller (the skill, the EM) could read it back. `_dispatch_directive`
    re-emits it onto apply's own stderr afterward, mirroring the stdout
    re-emission, so live console visibility is unchanged."""
    main_fn: Optional[Callable[..., Any]] = getattr(module, "main", None)
    if main_fn is None:
        raise UnrecognizedDirective(
            f"workstream_complete.apply: {module.__name__} exposes no main() entrypoint"
        )
    try:
        params = inspect.signature(main_fn).parameters
    except (TypeError, ValueError):
        params = {}

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
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
            return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()

    exit_code = int(result) if isinstance(result, int) else 0
    return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()


#: Matches the three inter-directive token shapes this manifest emits, all
#: generalized over the producer id so any directive naming any field
#: resolves through the same seam without a code change:
#:   - `.entry_path` — `directives_completion.RECONCILE_ENTRY_PATH_TOKEN`'s
#:     own literal, `"{d-complete-entry.entry_path}"` — a VALUE-threading
#:     token, substituted with the producer's captured stdout (see
#:     `_resolve_arg_tokens`).
#:   - `.landed` — an ORDERING-only token (no value threaded; substitutes
#:     to the empty string) for a directive whose CLI needs no data from
#:     its producer but must still refuse to dispatch when that producer
#:     never landed this pass. `depends_on` naming a sibling directive id
#:     is deliberately NOT gated by `ceremony_common.apply_halt.
#:     _directive_gate_open` (see that function's docstring) — producer
#:     readiness for a directive-id dependency is this module's own
#:     concern, and a directive with nothing to thread still needs a way
#:     to express it.
#:   - `.argv` — a WHOLE-ARG, list-expanding token (`directives_commit_
#:     tail.build_wsc_tail_directive`'s `"{d-close-tail-args.argv}"`):
#:     every non-blank line of the producer's captured stdout becomes its
#:     own element of the consumer's resolved argv, in order. Only legal
#:     when it is the ENTIRE arg string — splicing a multi-token list into
#:     the middle of a larger string is meaningless, so an `.argv` token
#:     embedded in a bigger arg fails loud rather than partially
#:     resolving (see `_resolve_arg_tokens`).
#: Deliberately closed to exactly these three fields: a future producer
#: field beyond `entry_path`/`landed`/`argv` is a deliberate, reviewed
#: addition to this regex, never a silently-broadened catch-all.
_ARG_TOKEN_RE = re.compile(r"\{([A-Za-z0-9_-]+)\.(entry_path|landed|argv)\}")

#: The whole-arg shape `.argv` requires — anchored so an `.argv` token
#: embedded in a larger string never matches here (it still matches
#: `_ARG_TOKEN_RE` above, which is how `_resolve_arg_tokens` detects and
#: rejects that embedded case).
_ARGV_TOKEN_RE = re.compile(r"^\{([A-Za-z0-9_-]+)\.argv\}$")

#: Fail-loud backstop: ANY `{...}` still present in an arg after
#: `_ARG_TOKEN_RE` substitution is a bug — either a token this module
#: doesn't (yet) recognize, or a genuine unresolved placeholder. Never
#: passed through to a live dispatch (see `_resolve_arg_tokens`).
_RESIDUAL_TOKEN_RE = re.compile(r"\{[^{}]+\}")


def _resolve_arg_tokens(
    args: list[str], stdout_by_id: dict[str, str]
) -> tuple[Optional[list[str]], Optional[str]]:
    """Substitutes every `{<producer-id>.entry_path}`/`{<producer-id>.
    landed}`/`{<producer-id>.argv}` token in `args`. All three fields
    share ONE precondition — the named producer must be a key of
    `stdout_by_id`, i.e. a directive that already landed (exit 0) earlier
    THIS pass (see `_execute_directives`) — then diverge on what
    "resolved" produces:

    - `.entry_path` substitutes the FIRST LINE (stripped) of the
      producer's captured stdout, failing if that stdout is empty/
      whitespace-only. Matches `coordinator-complete-entry.py`'s own
      single-line `print(entry_path)` contract
      (`coordinator_core/ops/coordinator_complete_entry.py:726`) —
      trailing residue (e.g. a second `stand-down:` stderr-only line
      never reaches stdout, so "first line" and "the whole payload"
      coincide here).
    - `.landed` substitutes the EMPTY STRING unconditionally once the
      producer precondition holds — it threads no value, only proves the
      producer ran and exited 0 before this directive dispatches. Exists
      for a directive whose CLI needs nothing from its producer's output
      (a pure ordering dependency) but still must not dispatch silently
      when the producer never landed.
    - `.argv` is a WHOLE-ARG, list-expanding token: legal only when the
      arg string is EXACTLY `{<producer-id>.argv}` (an `.argv` token
      embedded in a larger string fails loud — splicing a multi-token
      list into the middle of a string is meaningless). Every non-blank
      line of the producer's captured stdout, stripped, becomes its own
      element of the resolved arg list, in order. Unlike `.entry_path`,
      EMPTY producer stdout is legal here and resolves to ZERO args —
      `wsc-close tail-args` prints nothing when none of its optional
      flag groups were supplied, and that is the ordinary case, not a
      failure.

    Returns `(resolved_args, None)` on success, or `(None, error_message)`
    on ANY of: a named producer that never landed this pass (failed,
    blocked, or sequenced after this directive in `directives[]`); an
    `.entry_path` producer that landed but captured empty/whitespace-only
    stdout; an `.argv` token embedded in a larger arg string; or a
    `{...}` token surviving substitution (an unrecognized token shape) —
    never a silent literal-token passthrough to dispatch, this is exactly
    the break-class defect this function exists to close."""
    resolved: list[str] = []
    for arg in args:
        argv_match = _ARGV_TOKEN_RE.match(arg)
        if argv_match:
            producer_id = argv_match.group(1)
            if producer_id not in stdout_by_id:
                return None, (
                    f"token producer {producer_id!r} did not land before this "
                    "directive this pass (failed, blocked, or not yet dispatched) "
                    "— refusing to dispatch with an unresolved token"
                )
            producer_stdout = stdout_by_id[producer_id]
            resolved.extend(line.strip() for line in producer_stdout.splitlines() if line.strip())
            continue

        resolved_arg = arg
        for producer_id, field in _ARG_TOKEN_RE.findall(arg):
            if field == "argv":
                return None, (
                    f"'{{{producer_id}.argv}}' must be the entire arg string, not "
                    f"embedded within {arg!r} — .argv is a whole-arg, "
                    "list-expanding token and cannot be spliced into a larger string"
                )
            if producer_id not in stdout_by_id:
                return None, (
                    f"token producer {producer_id!r} did not land before this "
                    "directive this pass (failed, blocked, or not yet dispatched) "
                    "— refusing to dispatch with an unresolved token"
                )
            if field == "landed":
                resolved_arg = resolved_arg.replace(f"{{{producer_id}.landed}}", "")
                continue
            producer_stdout = stdout_by_id[producer_id]
            first_line = producer_stdout.splitlines()[0].strip() if producer_stdout.strip() else ""
            if not first_line:
                return None, (
                    f"token producer {producer_id!r} landed but captured no stdout "
                    "to resolve its {entry_path} token from"
                )
            resolved_arg = resolved_arg.replace(f"{{{producer_id}.entry_path}}", first_line)
        resolved.append(resolved_arg)

    for resolved_arg in resolved:
        residual = _RESIDUAL_TOKEN_RE.search(resolved_arg)
        if residual:
            return None, (
                f"unresolved inter-directive token {residual.group(0)!r} remains "
                f"in arg {resolved_arg!r} after substitution"
            )
    return resolved, None


def _dispatch_directive(
    directive: dict[str, Any], *, args: Optional[list[str]] = None
) -> dict[str, Any]:
    """Loads and invokes the one CLI a single `directives[]` entry names,
    returning a small result record. Raises `UnrecognizedDirective`/
    `FileNotFoundError`/any other load-time exception before any dispatch
    on an unrecognized or unresolvable `cli` — `_execute_directives` is the
    one place that catches it. `args` is `_execute_directives`'s already
    token-resolved argument list (see `_resolve_arg_tokens`) when supplied
    — `None` falls back to the directive's own literal `args` unchanged,
    which is correct for the overwhelming majority of directives that
    carry no inter-directive token at all. The producing CLI's own captured
    stdout and stderr are each re-emitted onto apply's own stdout/stderr
    here (see `_invoke_cli_main`'s docstring) so nothing that used to
    print to the ceremony run's console goes silent."""
    module = _load_cli_module(directive["cli"])
    effective_args = directive.get("args", []) if args is None else args
    exit_code, stdout_text, stderr_text = _invoke_cli_main(module, effective_args)
    if stdout_text:
        sys.stdout.write(stdout_text)
    if stderr_text:
        sys.stderr.write(stderr_text)
    return {
        "id": directive["id"],
        "cli": directive["cli"],
        "args": list(effective_args),
        "exit_code": exit_code,
        "stdout": stdout_text,
        "stderr": stderr_text,
    }


# ---------------------------------------------------------------------------
# Halt contract — per-directive, disposition-value-aware (module docstring).
# Composed from `ceremony_common.apply_halt` (`_directive_gate_open`), never
# re-derived locally.
# ---------------------------------------------------------------------------


def _judgment_points_by_id(judgment_points: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {jp["id"]: jp for jp in judgment_points}


def _first_blocking_judgment_point_id(
    directive: dict[str, Any],
    jp_by_id: dict[str, dict[str, Any]],
    decisions: dict[str, Any],
) -> Optional[str]:
    """AC4's remedy-naming half of the halt contract: WHICH `depends_on`
    member is actually holding a blocked directive's gate shut.
    `_directive_gate_open` (`ceremony_common.apply_halt`) only returns a
    bool — this walks the SAME `depends_on` list, in the same order, using
    the same two primitives (`_normalize_depends_on`,
    `_disposition_resolves_directive`) it does, stopping at the first
    judgment-point member whose gate is not yet open (no disposition
    chosen, or a chosen disposition that does not name this directive in
    its own `resolves`). This is never called for a directive whose gate
    IS open (only `_execute_directives`'s `blocked` branch calls it), so
    a `depends_on` member has already been proven to exist and to fail.

    Returns `None` when every `depends_on` member is a directive-id
    ordering dependency (never gates) or an id in neither namespace (fails
    closed with nothing to name a remedy against) — a directive can be
    blocked with no judgment-point remedy to report; the caller degrades
    to an empty `blocked_remedy` entry for that id rather than fabricating
    one.
    """
    for dep in _normalize_depends_on(directive.get("depends_on")):
        judgment_point = jp_by_id.get(dep)
        if judgment_point is None:
            continue
        decision = decisions.get(dep)
        chosen_value = decision.get("disposition") if isinstance(decision, dict) else None
        if not chosen_value or not _disposition_resolves_directive(judgment_point, chosen_value, directive["id"]):
            return dep
    return None


def _build_blocked_remedy_entry(
    directive: dict[str, Any],
    jp_by_id: dict[str, dict[str, Any]],
    decisions: dict[str, Any],
) -> dict[str, Any]:
    """AC4: `{"judgment_point_id": <gating jp id>, "dispositions": [<jp's
    disposition values whose OWN resolves list names this directive>]}`
    for one blocked directive — every field read straight off the SAME
    `judgment_points[]` this pass already has in hand (via `jp_by_id`), no
    new computation. `judgment_point_id` is `None` (and `dispositions` is
    `[]`) in the rare case `_first_blocking_judgment_point_id` finds no
    judgment-point member to name (see that function's docstring)."""
    jp_id = _first_blocking_judgment_point_id(directive, jp_by_id, decisions)
    if jp_id is None:
        return {"judgment_point_id": None, "dispositions": []}
    judgment_point = jp_by_id[jp_id]
    dispositions = [
        entry["value"]
        for entry in judgment_point.get("dispositions", [])
        if directive["id"] in entry.get("resolves", [])
    ]
    return {"judgment_point_id": jp_id, "dispositions": dispositions}


def render_blocked_remedy_lines(blocked_remedy: dict[str, Any]) -> list[str]:
    """AC5: the human-readable apply-tail line(s), one per blocked
    directive — `BLOCKED <directive-id> — set decisions["<jp-id>"].
    disposition to one of: <values>`. Reads only `report["blocked_remedy"]`
    (AC4) already computed by `_execute_directives`; no new computation.
    A blocked directive with no nameable judgment point (`judgment_point_id`
    is `None` — see `_first_blocking_judgment_point_id`) is skipped: there
    is no `decisions[...]` key to name a remedy against."""
    lines: list[str] = []
    for directive_id, remedy in blocked_remedy.items():
        jp_id = remedy.get("judgment_point_id")
        if jp_id is None:
            continue
        dispositions = remedy.get("dispositions") or []
        values = ", ".join(dispositions) if dispositions else "(no disposition resolves this directive)"
        lines.append(f'BLOCKED {directive_id} — set decisions["{jp_id}"].disposition to one of: {values}')
    return lines


def _execute_directives(
    directives: list[dict[str, Any]],
    judgment_points: list[dict[str, Any]],
    decisions: dict[str, Any],
    repo_root: Optional[Path] = None,
) -> tuple[int, dict[str, Any]]:
    """THE directive-execution seam (halt contract). Iterates `directives`
    in list order; each entry whose gate is open (per
    `_directive_gate_open`) dispatches through the closed `_CLI_DISPATCH`
    table, each blocked entry is recorded (never dispatched, never silently
    dropped), and every OTHER ready directive still executes even when one
    entry is blocked or fails — the halt is PER-DIRECTIVE, never whole-run.

    Returns `(exit_code, report)`. `report["landed"]` names directive ids
    that dispatched AND exited 0; `report["blocked"]` names directive ids
    whose judgment-point gate stayed closed this pass — paired 1:1 with
    `report["blocked_remedy"]` (AC4,
    docs/plans/2026-07-29-workstream-complete-the-envelope-names-t.md): for
    each blocked directive id, the gating judgment-point id and the
    disposition values whose OWN `resolves` list names that directive —
    see `_build_blocked_remedy_entry`. `report["failed"]`
    names directive ids whose dispatch either raised (including the
    documented `scan_unresolved_ubt_records.py` `FileNotFoundError` gap) OR
    returned a non-zero `exit_code` — a directive never joins `landed`
    merely because dispatch didn't raise. `report["degraded"]` is `failed`'s
    best-effort sibling (docs/plans/2026-08-08-a-best-effort-directive-
    cannot-fail-a-ce.md, chunk C1): a directive carrying `best_effort: True`
    whose dispatch returns a non-zero `exit_code` lands its `{"id",
    "error"}` record here INSTEAD of in `failed` — still visible to an
    operator, never silent, but never able to move the exit code. Both
    `failed` and `degraded` entries fold the directive's captured stderr
    into `error` (after the `"<cli> exited <n> (args=[...])"` prefix) when
    the CLI produced any. Exit code: `HALTED_AT_JUDGMENT`
    when anything was blocked and nothing failed; `PARTIAL_MUTATION` when
    something failed but something else also landed; `DIRECTIVE_FAILED`
    when something failed and nothing landed at all; `SUCCESS` when every
    directive fired clean OR the only non-zero exits were `degraded` ones —
    the exit-code ladder reads `failed` only, never `degraded`, by
    construction.

    Inter-directive arg-token threading (module docstring, deviation 3):
    `stdout_by_id` accumulates the captured stdout of every directive that
    LANDED this pass (exit 0), keyed by directive id — mirrors `workday_
    complete.apply`'s identical `stdout_by_id` accumulator (there consumed
    by `stdin_from`; here consumed by `_resolve_arg_tokens`). Before
    dispatch, each directive's `args` is passed through `_resolve_arg_
    tokens` against the accumulator so far; a directive naming a producer
    in a `{<id>.entry_path}` token (threads the producer's captured stdout)
    or a `{<id>.landed}` token (ordering-only, substitutes to the empty
    string) that never landed THIS pass — failed,
    blocked, or (a brief-authoring bug) sequenced AFTER the consumer in
    `directives` — is recorded in `failed` WITHOUT dispatching, the same
    honesty property `_dispatch_directive`'s `stdin_from` sibling
    established: refusing to dispatch with an unresolved token, rather
    than letting the literal token string reach the CLI as a live
    argument.

    Gate verdict memo (C4, AC6, docs/plans/2026-08-10-commit-event-5s-cap-
    and-the-silent-tail.md, retry #3): EXECUTION-TIME is the only place this
    module records the gate verdict memo `directives_review.
    build_review_brightline_gate_directive`/`__init__.py`'s `d-coverage-gate`
    builder consult (read-only) at build time. After a directive lands this
    pass (dispatched, exit 0), `directives_review.
    record_gate_verdict_if_passed(repo_root, directive, exit_code, stdout)`
    is called — a no-op for every directive id other than the two live
    gates it knows about, and itself verdict-aware for the coverage gate
    (a `VERDICT=WARN` exit-0 is not a confirmed pass). `repo_root` is
    resolved once, lazily, via `resolve_repo_root()` (the same zero-spawn
    `.git` read-model `brief()` itself uses) the first time a landed
    directive actually needs it, when the caller did not supply one — never
    up front, so a caller exercising directives that never touch a gate
    pays no resolution cost. The memo write is wrapped in a bare
    `try/except OSError` here (never inside `directives_review.
    record_gate_verdict_if_passed`, which stays fail-loud per `record_gate_
    memo`'s own contract): a memo I/O failure degrades this pass to "the
    next pass re-walks the gate," never to a reported apply failure —
    memoisation is a performance optimization, and a miss is always the
    safe direction (see the module-level "Gate verdict memo" docstring in
    `directives_review.py`)."""
    jp_by_id = _judgment_points_by_id(judgment_points)
    directive_ids = {d["id"] for d in directives}
    # `depends_on` is a UNION namespace (judgment-point id OR sibling
    # directive id — `ceremony_common.apply_halt._directive_gate_open`
    # docstring). `directives_completion.build_reconcile_completion_
    # commits_directive` sets `depends_on` to a DIRECTIVE id
    # (`"d-complete-entry"`, its arg-token producer), never a judgment
    # point (module docstring, deviation 3); the gate resolves that case
    # itself now rather than this loop special-casing it (former
    # `is_producer_dependency` local, retired — commit `82f8fa4f`'s
    # workaround is now the shared, tested behavior). Assert the two
    # namespaces are disjoint once per envelope so an id collision is a
    # loud producer bug, not a silent precedence accident.
    assert_disjoint_dependency_namespaces(jp_by_id, directive_ids)
    landed: list[str] = []
    blocked: list[str] = []
    blocked_remedy: dict[str, Any] = {}
    failed: list[dict[str, Any]] = []
    degraded: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    stdout_by_id: dict[str, str] = {}
    _resolved_repo_root: list[Optional[Path]] = [repo_root]
    _repo_root_resolved: list[bool] = [repo_root is not None]

    def _lazy_repo_root() -> Optional[Path]:
        # Resolved at most once per `_execute_directives` call, only when a
        # directive that actually landed needs it for the gate verdict
        # memo — see this function's own "Gate verdict memo" docstring
        # paragraph. A `list` cell (not a plain local) so this nested
        # function can both read and write it under Python's closure rules.
        if not _repo_root_resolved[0]:
            _resolved_repo_root[0] = resolve_repo_root()
            _repo_root_resolved[0] = True
        return _resolved_repo_root[0]

    for directive in directives:
        if directive.get("already_satisfied"):
            landed.append(directive["id"])
            # Defect C (docs/plans/2026-08-08-a-best-effort-directive-
            # cannot-fail-a-ce.md, chunk C1): register the producer with an
            # empty captured-stdout value even though it never dispatched
            # this pass, so a `{<id>.landed}` token naming it resolves
            # instead of falsely reporting "did not land" — see module
            # docstring, deviation 3.
            stdout_by_id[directive["id"]] = ""
            continue

        try:
            gate_open = _directive_gate_open(directive, jp_by_id, decisions, directive_ids)
        except Exception as exc:  # noqa: BLE001 - malformed envelope is per-directive
            failed.append({"id": directive["id"], "error": f"gate evaluation error: {exc}"})
            continue
        if not gate_open:
            blocked.append(directive["id"])
            blocked_remedy[directive["id"]] = _build_blocked_remedy_entry(directive, jp_by_id, decisions)
            continue

        resolved_args, token_error = _resolve_arg_tokens(directive.get("args", []), stdout_by_id)
        if token_error is not None:
            failed.append(
                {
                    "id": directive["id"],
                    "error": f"unresolved inter-directive token: {token_error}",
                }
            )
            continue

        try:
            result = _dispatch_directive(directive, args=resolved_args)
        except Exception as exc:  # noqa: BLE001 - closed-table dispatch failure
            entry = {"id": directive["id"], "error": str(exc)}
            if directive.get("best_effort"):
                degraded.append(entry)
            else:
                failed.append(entry)
            continue
        if result.get("exit_code", 0) != 0:
            error = f"{directive['cli']} exited {result['exit_code']} (args={result.get('args', [])})"
            captured_stderr = (result.get("stderr") or "").strip()
            if captured_stderr:
                error = f"{error} — stderr: {captured_stderr}"
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

        gate_root = _lazy_repo_root()
        if gate_root is not None:
            try:
                directives_review.record_gate_verdict_if_passed(
                    gate_root, directive, result.get("exit_code", 0), result.get("stdout", "")
                )
            except OSError:
                # Memo write is best-effort — see this function's own "Gate
                # verdict memo" docstring paragraph. A failure here degrades
                # to "the next pass re-walks," never to a reported failure.
                pass

    report = {
        "landed": landed,
        "blocked": blocked,
        "blocked_remedy": blocked_remedy,
        "failed": failed,
        "degraded": degraded,
        "results": results,
    }

    if failed and landed:
        return int(WorkstreamApplyExitCode.PARTIAL_MUTATION), report
    if failed:
        return int(WorkstreamApplyExitCode.DIRECTIVE_FAILED), report
    if blocked:
        return int(WorkstreamApplyExitCode.HALTED_AT_JUDGMENT), report
    return int(WorkstreamApplyExitCode.SUCCESS), report


# ---------------------------------------------------------------------------
# No-commit row guard (example-doctrine-repo docs/plans/2026-07-29-pm-approved-
# provenance-write-time-closure-gate.md, chunk C13) -- see module docstring
# § No-commit row guard for the design rationale and why this lives in
# `apply()` rather than `__init__.py`'s `brief()`/`_build_preserved_
# judgment_points`.
# ---------------------------------------------------------------------------


def _no_commit_row_judgment(
    decisions: dict[str, Any], root: Path
) -> Optional[dict[str, Any]]:
    """Surfaces `jp-no-commit-row-disposition` (`judgments.
    build_no_commit_row_disposition_judgment_point`) when the governing
    plan has a commit-required task-spine row (disposition `open`/`coded`)
    this session's commit-coverage oracle found no covering commit for,
    AND that row is not already named in `decisions["no_commit_row_
    dispositions"]` (the EM's already-recorded classification -- a mapping
    of row-id -> one of `shipped`/`spun-off`/`backlogged`/`wont-do`/
    `carried-forward`, supplied back by the caller once the judgment is
    answered).

    Reuses `coordinator_core.execute_plan_assemble.close_out_and_stamp.
    _determine_shipped` verbatim (the SAME oracle `/execute-plan` Phase 4's
    close-out already trusts for "did every commit-required chunk-id land a
    commit") rather than re-deriving a second git-log scan -- per this
    chunk's own governing instruction: duplicating commit-scanning logic is
    the desync hazard this plan keeps hitting. `_determine_shipped` already
    handles the absent-spine (nothing to check), malformed-spine (fails
    loud via its own `error` return, treated here as "cannot judge, do not
    guess" and skipped rather than guessed past), and cross-repo `scope:`
    sibling-scanning cases -- none of that is reimplemented here.

    False-positive-stamp incident fix: a plan reporting
    `close_out_and_stamp.JOIN_PROVENANCE_NO_EVIDENCE_SOURCE` always returns
    an empty `missing_chunk_ids` too (see that constant's own docstring --
    it is the no-spine/no-ledger case, so there is nothing to name as
    missing), so this guard already returns `None` for it via the
    `not missing_chunk_ids` check below without any special-casing --
    named here so a reader does not need to re-derive that from scratch.
    `close_out_and_stamp`'s own stamping path is what refuses to stamp a
    plan carrying that value; this guard's job (no-commit-row disposition)
    was never in the stamp-decision path to begin with.

    Governing-plan resolution reuses `directives_lessons_plan.
    resolve_governing_plan_with_source` (the SAME function `__init__.py`'s
    own `brief()` calls) rather than a second plan-locate implementation.
    The one case this narrows relative to `brief()`'s own resolution: the
    chain-terminal consumed-handoff `governing_plan:` frontmatter leg is
    not threaded through here (that leg lives behind `__init__.py`'s own
    session-shape gate machinery, out of this chunk's file scope -- see
    module docstring), so an explicit `decisions["governing_plan_slug"]`/
    `["governing_plan_path"]` (or the fixed `tasks/todo.md`/`tasks/plan.md`
    fallback) is what this guard can see; a plan resolved ONLY via the
    consumed-handoff leg is not checked by this guard. Returns `None`
    (nothing to check) rather than guessing in that case -- the safe
    failure direction for a guard, per this same fix's own posture on
    every other conservative choice.

    Returns `None` when: no governing plan resolves, the plan text cannot
    be read, `_determine_shipped` reports an error (malformed spine or a
    broken git-log query -- fails loud elsewhere, never guessed past
    here), every commit-required row already has a covering commit, or
    every row missing one is already named in `decisions["no_commit_row_
    dispositions"]`. Otherwise returns the built judgment point for the
    still-unresolved row-id subset.

    `join_provenance` (cross-repo memo fix -- see `_determine_shipped`'s own
    widened docstring) is threaded straight through to `judgments.
    build_no_commit_row_disposition_judgment_point` unchanged: this guard
    still fires in every case (an unjoinable key never suppresses the
    judgment -- see that builder's own docstring for why a silent sixth
    exit is deliberately not introduced here), it only changes how the
    judgment's OWN evidence text frames a non-`"joined"` result -- as an
    unattributable key, not as unshipped work."""
    governing_plan, _source = directives_lessons_plan.resolve_governing_plan_with_source(
        root, decisions
    )
    if governing_plan is None:
        return None
    try:
        plan_text = governing_plan.path.read_text(encoding="utf-8")
    except OSError:
        return None

    _is_shipped, missing_chunk_ids, join_provenance, error = _determine_shipped(
        plan_text, str(governing_plan.path), root
    )
    if error is not None or not missing_chunk_ids:
        return None

    already_resolved = decisions.get("no_commit_row_dispositions") or {}
    unresolved = [cid for cid in missing_chunk_ids if cid not in already_resolved]
    if not unresolved:
        return None
    return _judgments.build_no_commit_row_disposition_judgment_point(
        unresolved, join_provenance
    )


def apply(*, decisions: Optional[dict[str, Any]] = None) -> tuple[int, dict[str, Any]]:
    """`apply()` — recomputes the brief in-process (never trusts a
    caller-supplied decision object) and executes every execution-ready
    `directives[]` entry per the halt contract. `decisions` is the EM-
    resolved `{judgment_point_id: {disposition, ...}}` map — omitted
    entries leave their gated directives blocked, not auto-fired.

    Unlike `workday_complete.apply`/`workweek_complete.apply` (whose
    `brief()` returns `(exit_code, envelope)`), `workstream_complete.
    brief()` returns the envelope `Mapping` directly and raises
    `TransportFailure` on a resolution failure (see module docstring,
    deviation 1) — that exception is the one this function's `try` guards.
    """
    try:
        envelope = brief(decisions=decisions)
    except TransportFailure as exc:
        return int(WorkstreamApplyExitCode.TRANSPORT_FAIL), {
            "error": str(exc),
            "landed": [],
        }

    directives = envelope.get("directives", [])
    judgment_points = envelope.get("judgment_points", [])
    effective_decisions = decisions if decisions is not None else envelope.get("decisions", {})

    # No-commit row guard (C13) — checked BEFORE dispatch, not woven into
    # the ordinary directive halt contract above: this judgment gates the
    # WHOLE apply (no directive names it in `depends_on`), because a
    # no-commit row's disposition is a scope question, not a step any
    # single directive's execution depends on. See `_no_commit_row_
    # judgment`'s own docstring for why this recomputes governing-plan
    # resolution here rather than reading it back out of `envelope` (that
    # value never made it into the envelope's own shape — see module
    # docstring § No-commit row guard).
    artifact_path = envelope.get("artifact", {}).get("path")
    if artifact_path:
        pending_jp = _no_commit_row_judgment(effective_decisions, Path(artifact_path))
        if pending_jp is not None:
            return int(WorkstreamApplyExitCode.HALTED_AT_JUDGMENT), {
                "error": (
                    "task-spine row(s) with no covering commit require an explicit "
                    "disposition (shipped/spun-off/backlogged/wont-do/carried-forward) "
                    "in decisions['no_commit_row_dispositions'] before this workstream "
                    "can close — see judgment_points[0]"
                ),
                "judgment_points": [pending_jp],
                "landed": [],
                "blocked": [pending_jp["id"]],
                "blocked_remedy": {},
                "failed": [],
                "results": [],
            }

    return _execute_directives(directives, judgment_points, effective_decisions)


def main(argv: list[str]) -> int:
    """`main()`'s `apply` dispatch arm — `--decisions <json>` is the one
    supported flag, mirroring `workday_complete.apply`/`workweek_complete.
    apply`'s CLI shape. Invoked via `workstream_complete.__init__._main_apply`
    (C3's `apply` subcommand wiring), never directly by an operator."""
    decisions: Optional[dict[str, Any]] = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--decisions":
            if i + 1 >= len(argv):
                print("workstream-complete-apply: --decisions requires a value", file=sys.stderr)
                return int(WorkstreamApplyExitCode.TRANSPORT_FAIL)
            try:
                decisions = json.loads(argv[i + 1])
            except json.JSONDecodeError as exc:
                print(f"workstream-complete-apply: malformed --decisions JSON: {exc}", file=sys.stderr)
                return int(WorkstreamApplyExitCode.TRANSPORT_FAIL)
            i += 2
        else:
            print(f"workstream-complete-apply: unrecognized argument {tok!r}", file=sys.stderr)
            return int(WorkstreamApplyExitCode.TRANSPORT_FAIL)

    exit_code, report = apply(decisions=decisions)
    print(json.dumps(report, indent=2, sort_keys=True))
    for line in render_blocked_remedy_lines(report.get("blocked_remedy") or {}):
        print(line)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

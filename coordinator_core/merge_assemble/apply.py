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

POST-C2 SPLIT (chunk C2 of docs/plans/2026-08-26-merges-directives-stop-
starting-interpreters.md): this table is no longer uniform. Three verbs
(`check-no-illegal-paths`, `merge-recovery-and-tag-cut`, `portability-sweep`)
dispatch IN-PROCESS through `coordinator_core.ceremony_common.cli_dispatch`
— module load + cached reuse + `main()` invocation, no subprocess, ever.
Three verbs (`merge-gate-and-pr`, `merge-release-notes-derive`,
`orphan-branch-sweep`) still shell out to an EXISTING atomic `coordinator/
bin/*.py` script via `_run_py_script`, because none of the three has an
in-scope argument path for its own repo root (see each handler's own
docstring for the specific gap, and C2's "record why it does not converge"
rule this exclusion discharges). `node-ceremony-gate` spawns `node --test`
— a genuinely external program with no import path, and is never converged.
`tier-u-grant` was already in-process before this chunk (`_dispatch_tier_u_grant`'s
own docstring). Every SPAWNING handler still resolves its script from an
explicit argv list built from this module's own file location — never a
brief-derived import, never a shell string built from `directives[].args`.

Contract (frozen, reviewed): DoE-claude coordinator/docs/wiki/computed-skills.md
Spec backlink: DoE-claude:pln-b4-baton-branch-lifecycle-comp-780d48, chunk C6
Spec backlink (C2): docs/plans/2026-08-26-merges-directives-stop-starting-interpreters.md, chunk C2

Negative-spec:
    - Do NOT add a dispatch entry that resolves `cli` via `getattr`,
      `importlib`, or any brief-derived string — every entry in
      `_CLI_DISPATCH` is a literal key written by hand in this file.
    - Do NOT build a subprocess argv via string interpolation/shell=True —
      every SPAWNING handler below passes a literal list to `subprocess.run`.
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

from coordinator_core.ceremony_common.cli_dispatch import (
    invoke_cli_main,
    load_cli_module,
    resolve_cli_script_root,
)
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
    with a literal argv list and `cwd=repo_root`.

    NARROWED, not deleted, by C2 (docs/plans/2026-08-26-merges-directives-
    stop-starting-interpreters.md): after that chunk this is called only by
    the three verbs EXCLUDED from in-process conversion — `merge-gate-and-pr`,
    `merge-release-notes-derive`, `orphan-branch-sweep` — each of which has
    no in-scope argument path for its own repo root (see each handler's own
    docstring). The other three (`merge-recovery-and-tag-cut`,
    `portability-sweep`, `check-no-illegal-paths`) dispatch through
    `_dispatch_in_process`/`ceremony_common.cli_dispatch` instead and never
    reach this function. Do not reach for this helper for a new handler
    without first checking whether the script it targets has a repo-root
    argument path — if it does, `_dispatch_in_process` is the correct home."""
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


def _dispatch_in_process(cli: str, script_name: str, args: list[str], repo_root: Path) -> dict[str, Any]:
    """Shared body for merge_assemble's three IN-PROCESS verbs (C2 AC3):
    resolves `<repo_root>/coordinator/bin/<script_name>.py` via
    `ceremony_common.cli_dispatch.resolve_cli_script_root` — `repo_root`
    always explicit, never `Path(__file__)`/`Path.cwd()` (that module's own
    docstring, "CWD IS A LOAD-BEARING GAP") — loads it once per process via
    `load_cli_module` (cached by module name across calls in this same
    engine process), and invokes its `main()` via `invoke_cli_main`. No
    subprocess, ever, for the three callers of this function.

    Absent-producer mapping (C2 AC, named explicitly per the chunk body):
    a missing script raises `UnrecognizedDirective` here (`load_cli_module`
    cannot build a spec for a path that does not exist) — a DIFFERENT
    exception type than today's nonzero-interpreter-exit `RuntimeError`
    from `_dispatch_result`, carrying the SAME "this verb did not run" fact.
    In the one caller where this actually matters — `portability-sweep`'s
    `d5` — `merge_assemble.build_directives` already marks `d5`
    `already_satisfied` with a `skipped_reason` when the script is absent,
    so `apply_base.execute_directives` never dispatches this handler for it
    in the normal case; this function's own `UnrecognizedDirective` is the
    handler's fallback contract, not the path a healthy run takes, and the
    gate still reports `"unavailable"`, never `"passed"`, either way
    (`_fill_gate_verdicts` reads `already_satisfied`/`skipped_reason`
    upstream of this handler, unaffected by this chunk).

    `_dispatch_result`'s raise-on-nonzero contract is preserved for a
    resolved nonzero exit: still `RuntimeError`, mapped from the
    primitive's returned integer exit code exactly as it was from a
    `subprocess.CompletedProcess.returncode`."""
    script_path = resolve_cli_script_root(repo_root) / f"{script_name}.py"
    if not script_path.is_file():
        raise UnrecognizedDirective(
            f"{cli}: no producer at {script_path} — cannot dispatch in-process"
        )
    module_name = f"_merge_assemble_cli_{script_name.replace('-', '_')}"
    module = load_cli_module(module_name, script_path)
    exit_code, stdout, stderr, _exit_class = invoke_cli_main(module, args)
    if exit_code != 0:
        raise RuntimeError(
            f"{cli}: exited {exit_code}: {stderr.strip() or stdout.strip() or '<no output>'}"
        )
    return {"cli": cli, "returncode": exit_code, "stdout": stdout.strip()}


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


def _anchor_merge_recovery_config_path(args: list[str], repo_root: Path) -> list[str]:
    """`d1` (`resolve-tag-prefix`) has NO `--repo-root` flag at all — verified
    against `merge-recovery-and-tag-cut.py`'s own argparse setup, only
    `recovery-branch` and `cut-tag` declare one. The one path-shaped
    argument `resolve-tag-prefix` DOES take is `--config PATH`, which the
    script resolves against whatever cwd it happens to run in when `PATH`
    is relative — `d1`'s own `args` carries the relative
    `coordinator.local.md`. Rather than inject a flag this subcommand's
    parser does not accept, this anchors that relative `--config` value
    onto `repo_root` before dispatch — the argument path this verb actually
    has is `--config`, not `--repo-root`, and anchoring it discharges the
    same "explicit repo root, never ambient cwd" requirement through it."""
    out = list(args)
    for i, token in enumerate(out):
        if token == "--config" and i + 1 < len(out):
            value = Path(out[i + 1])
            if not value.is_absolute():
                out[i + 1] = str(repo_root / value)
    return out


def _dispatch_merge_recovery_and_tag_cut(args: list[str], repo_root: Path) -> dict[str, Any]:
    """`d1`/`d2` — IN-PROCESS (C2 AC3). The two directives that share this
    `cli` name take DIFFERENT argument paths for their repo root, verified
    against `merge-recovery-and-tag-cut.py`'s own argparse setup rather than
    assumed uniform:

    - `d2` (`cut-tag`) takes an explicit `--repo-root PATH` flag;
      `build_directives`'s own `args` omit it (verified: it falls back to
      `Path.cwd()` today, masked only because `_run_py_script(cwd=repo_root)`
      ran the whole interpreter with `repo_root` as its cwd). This handler
      appends `--repo-root` explicitly so the in-process call — which never
      chdirs — targets `repo_root`, not the serving engine's own cwd. `d2`
      is `cut-tag`, a real branch/tag mutation; this is the verb AC3 names
      by example for exactly this reason.
    - `d1` (`resolve-tag-prefix`) has NO `--repo-root` flag on its own
      subparser — see `_anchor_merge_recovery_config_path`'s docstring for
      the argument path it actually has and how this handler uses it.

    `require_engine_on_path(__file__)` runs at this script's MODULE level on
    import. Confirmed live (C2): `resolve_engine_root` walks up from this
    script's own file location and resolves to the running engine's own
    checkout root — so front-inserting it onto `sys.path` in THIS engine's
    current layout is a no-op, not a live shadow defect;
    `load_cli_module` does not catch the `RuntimeError` this call can raise
    if that ever stops being true (module docstring's own negative-spec:
    a non-`SystemExit` import-time exception propagates uncaught)."""
    subcommand = args[0] if args else None
    if subcommand == "resolve-tag-prefix":
        resolved_args = _anchor_merge_recovery_config_path(args, repo_root)
    else:
        resolved_args = [*args, "--repo-root", str(repo_root)]
    return _dispatch_in_process(
        "merge-recovery-and-tag-cut", "merge-recovery-and-tag-cut", resolved_args, repo_root
    )


def _dispatch_merge_gate_and_pr(args: list[str], repo_root: Path) -> dict[str, Any]:
    """`d4` — STILL SPAWNS. EXCLUDED from C2's conversion (recorded here per
    this chunk's own "record why it does not converge" rule): neither
    `merge-gate-and-pr.py` subcommand (`pr-body`, `active-branch-guard`)
    declares a `--repo-root` flag, and its bare `subprocess.run` calls for
    `git log`/`gh pr view` pass no `cwd=` at all — verified, they inherit
    whichever cwd the calling process happens to have. Today that is masked
    because `_run_py_script(cwd=repo_root)` runs the whole interpreter with
    `repo_root` as its cwd; `ceremony_common.cli_dispatch.invoke_cli_main`
    never chdirs (that module's own docstring, "CWD IS A LOAD-BEARING GAP"),
    and `coordinator/bin/merge-gate-and-pr.py` is in neither this chunk's
    `writes:` nor the parent plan's frontmatter `scope:` — there is no
    in-scope way to give this script an argument path for its repo root.
    Keeps spawning via `_run_py_script` until a future chunk gives it one."""
    proc = _run_py_script("merge-gate-and-pr", args, repo_root)
    return _dispatch_result("merge-gate-and-pr", proc)


def _dispatch_portability_sweep(args: list[str], repo_root: Path) -> dict[str, Any]:
    """`d5` — IN-PROCESS (C2 AC3). Unlike the other converged verbs,
    `build_directives` already threads `str(repo_root)` as `args[0]` for
    this one, so no injection is needed here — passed straight through.
    `portability-sweep.py` has no producer in `coordinator/bin/` on this
    box; `build_directives` already marks `d5` `already_satisfied` with a
    `skipped_reason` when absent, so `apply_base.execute_directives` never
    reaches this handler in that case (see `_dispatch_in_process`'s own
    docstring for the `UnrecognizedDirective` fallback this handler inherits
    if it is ever invoked anyway). The gate still reports `"unavailable"`,
    never `"passed"`, either way — converging this verb does not change
    that."""
    return _dispatch_in_process("portability-sweep", "portability-sweep", args, repo_root)


def _dispatch_check_no_illegal_paths(args: list[str], repo_root: Path) -> dict[str, Any]:
    """`d6` — IN-PROCESS (C2 AC3). `check-no-illegal-paths.py main(argv)`
    reads its repo root as the sole POSITIONAL `argv[0]` (`explicit_root =
    argv[0] if argv else None`); `d6`'s own `args` is `[]` today, relying on
    `_run_py_script(cwd=repo_root)`'s subprocess working directory. This
    handler injects `str(repo_root)` as that positional so the in-process
    call — which never chdirs — targets `repo_root`."""
    return _dispatch_in_process(
        "check-no-illegal-paths", "check-no-illegal-paths", [str(repo_root), *args], repo_root
    )


def _dispatch_merge_release_notes_derive(args: list[str], repo_root: Path) -> dict[str, Any]:
    """`d7` — STILL SPAWNS. EXCLUDED from C2's conversion (recorded here per
    this chunk's own "record why it does not converge" rule): the
    `flip-tags` subcommand's parser declares only positional
    `release_tag_cut`/`merge_sha`/`merge_date`/`entry_paths` — no
    `--repo-root` flag anywhere. Its own `_git()` helper accepts an optional
    `cwd` keyword, but `cmd_flip_tags` never populates it from an argv value
    because none exists to populate it from. `coordinator/bin/
    merge-release-notes-derive.py` is in neither this chunk's `writes:` nor
    the parent plan's frontmatter `scope:`, so there is no in-scope way to
    add one. `require_engine_on_path(__file__)` also runs at this script's
    module level (same "resolves to this engine's own root today" finding
    as `merge-recovery-and-tag-cut`), but that is moot while this verb keeps
    spawning. Keeps spawning via `_run_py_script`."""
    proc = _run_py_script("merge-release-notes-derive", args, repo_root)
    return _dispatch_result("merge-release-notes-derive", proc)


def _dispatch_orphan_branch_sweep(args: list[str], repo_root: Path) -> dict[str, Any]:
    """`d8` — STILL SPAWNS. EXCLUDED from C2's conversion (recorded here per
    this chunk's own "record why it does not converge" rule): this script's
    `main()` is a ZERO-ARG trampoline (`def main() -> None`) that reads
    `sys.argv` itself and forwards to `coordinator_core.ops.
    orphan_branch_sweep.main(argv)`, whose own argv parser recognises only
    `--format`/`--severity-min`/`--include-remote`/`--no-include-remote`/
    `--max-age-days` — no `--repo-root` option exists to inject one into.
    Neither `coordinator/bin/orphan-branch-sweep.py` nor `coordinator_core/
    ops/orphan_branch_sweep.py` is in this chunk's `writes:` or the parent
    plan's frontmatter `scope:`. Keeps spawning via `_run_py_script`."""
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
    is a no-op, not a second effect.

    Returns `None` on success, and RAISES on a non-zero exit rather than
    returning the raw `(code, message)` tuple. `apply_base._run_compensators`
    reads the return value against a bool contract: only a literal `False`
    is a non-success, and every other value — a 2-tuple included — records
    `succeeded: True`. Handing it the tuple therefore reported the grant as
    handed back on the abort path even when `revoke_tier_u_grant` had not
    handed it back, which is the one lie this compensator exists to prevent.
    A raise (recorded as `succeeded: False` with `error`) is the honest
    signal over an explicit `False`: neither an unresolvable session id nor
    a malformed argv is the "I ran and deliberately chose not to act"
    that `declined: True` asserts."""
    from coordinator_core.session.grant_directive import run_grant_directive

    code, message = run_grant_directive(["revoke", "--only-ceremony", _CEREMONY_NAME])
    if code != 0:
        raise RuntimeError(
            f"grant handback compensation did not complete (exit {code}): {message}"
        )
    return None


#: Per-directive-id compensators, fired in reverse landing order by
#: `apply_base.execute_directives` when a handler raises. Only the grant
#: write registers one: it is the single directive here whose effect
#: OUTLIVES the run (a token in `.git/coordinator-sessions/<sid>/` that a
#: later Tier-U consumer reads), so it is the only one an aborted run can
#: strand.
_COMPENSATORS: dict[str, Any] = {
    "d_grant_write": _compensate_grant_write,
}


#: C6 discriminator decision (docs/plans/2026-08-19-directives-name-an-op-not-
#: a-cli.md § C6 / § The discriminator for the mixed end state) — measured
#: live against `coordinator_core.authz.registration_quad._live_registry()`
#: this chunk: NONE of merge's eight verbs (`node-ceremony-gate`,
#: `merge-recovery-and-tag-cut`, `merge-gate-and-pr`, `portability-sweep`,
#: `check-no-illegal-paths`, `merge-release-notes-derive`,
#: `orphan-branch-sweep`, `tier-u-grant`) resolve to a registered op, so ALL
#: EIGHT stay `cli`-named — none migrate to `op`. No new op is minted to
#: force a migration (out of scope by name). `orphan-branch-sweep` is the
#: one name that LOOKS closest to a registered surface — its own bin script
#: composes four registered `git_branch.*` ops internally
#: (`coordinator_core/ops/orphan_branch_sweep.py`), but the DIRECTIVE this
#: table dispatches names the SCRIPT, never one of those four op keys
#: directly, so the discriminator's answer is unchanged: not a registered
#: op under this literal name. `node-ceremony-gate` spawns `node --test`
#: (a genuinely external program with no import path — never converged).
#: POST-C2: `merge-recovery-and-tag-cut`, `portability-sweep`, and
#: `check-no-illegal-paths` dispatch IN-PROCESS via `ceremony_common.
#: cli_dispatch` (no subprocess, ever); `merge-gate-and-pr`,
#: `merge-release-notes-derive`, and `orphan-branch-sweep` still spawn an
#: existing `coordinator/bin/*.py` script via `sys.executable` — each
#: EXCLUDED from C2's conversion because it has no in-scope argument path
#: for its own repo root (see each handler's own docstring for the specific
#: gap). Neither population is `bash`/`sh`, so `docs/reference/
#: shell-out-carve-outs.md` (scoped to interpreter/shell spawns) does not
#: apply to any of the eight, and none is a `CONSUMES_MANIFEST`-driven
#: script module in the completion-family sense, so no `CONSUMES_MANIFEST`
#: entry applies either.
#:
#: AC5 (verified live this chunk, C2 — the reasoning is settled at plan
#: time, this is verification, not a decision point): `apply_base.
#: execute_directives`'s admission keys on `directives[].op` via
#: `resolve_op`, which calls `assert_dispatchable`; `directives[].cli`
#: routes through `resolve_cli`, which never calls it. C2 moves `cli` keys
#: between execution models (in-process vs. spawned) — it introduces no
#: `op` directive — so the premise `assert_dispatchable` gates is
#: untouched, and `ASSEMBLER_DISPATCHABLE` (coordinator_core/authz/
#: dispatchable.py) still carries NO `"merge_assemble"` entry (confirmed:
#: `"merge_assemble" not in ASSEMBLER_DISPATCHABLE` at execution time).
#:
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

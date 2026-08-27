"""coordinator_core.merge_assemble.tests.test_warm_ceremony_brightline --
C7 (docs/plans/2026-08-26-merges-directives-stop-starting-interpreters.md):
re-measures the converged merge ceremony against DR-344's 500ms brightline
on the WARM-SERVED path C6 registered (`merge_assemble.brief` /
`merge_assemble.apply` in `coordinator_core.merge_assemble.ops`), and
proves which path a real ceremony dispatch actually takes.

AC11 (brightline, on the correct path). C3 measured the converged handlers'
COLD-forwarder delta pre-C6 — necessary evidence, not this AC's claim:
measuring before C6 registered the ops would have measured the still-cold
forwarder path, a guaranteed miss unrelated to this plan's work (this
module's own dispatch brief). Every number below is labelled with the path
it was taken on; an unlabelled number is not evidence for this AC.

AC12 ("REACHED"). `TestNoInTreeCallerDispatchesTheRegisteredOp` is the
structural half: post-C6, `coordinator/bin/merge-assemble.py` is STILL a
thin shim over `entry_point_shim.py :: run_target`, which resolves
`merge-assemble` via `_ENGINE_ENTRIES` (`_simple_entry("merge-assemble",
"coordinator_core.merge_assemble")`) and runs the entry point IN-PROCESS,
in a COLD interpreter, per invocation — never through the warm engine's
UDS transport (`coordinator_core.merge_assemble.ops` module docstring).
Grepped at build time: no in-tree module outside `ops.py` itself (its
registration side-effect), `authz/classification.py` / `op_scopes.py` /
`ops/_registry_map.py` (registration bookkeeping, not call sites), and
this test module dispatches `"merge_assemble.brief"`/`"merge_assemble.
apply"` as an op id to any warm-dispatch mechanism. C6 registered two ops
nobody dispatches; the 137.5ms/5-process cold-forwarder leg of the C3
baseline is UNTOUCHED, exactly as this plan's own C6 chunk predicted.

SUPERSEDED BY A FOLLOW-ON PLAN (docs/plans/2026-08-26-merge-assembles-
entry-point-reaches-the-warm-engine.md, chunk C3, its own AC8): that plan's
C2 wires `coordinator/bin/lib/entry_point_shim.py :: _merge_assemble_entry`
to actually dispatch both op ids through `cc_invoke.route`, which the
paragraph above no longer describes accurately for that ONE call site.
`TestNoInTreeCallerDispatchesTheRegisteredOp` below is inverted
accordingly: it now asserts the entry DOES dispatch (AST-based, keyed on
the op id reaching an actual `route(...)` call in `entry_point_shim.py`,
not a bare substring/docstring hit) rather than that nothing does. See that
class's own docstring for the inversion's shape and why a bare `== []` ->
`!= []` flip was rejected as vacuous.

METHODOLOGY. `apply()` MUTATES (cuts tags, runs `gh`, writes grant
records) so it cannot be safely re-run `k` times against a shared repo the
way `brief()` (COMPUTE_ONLY) can — this module never runs it against this
tree's own working copy. Instead:

  - `test_warm_op_path_brief_process_time` batches the WARM OP PATH's
    registered `merge_assemble.brief` handler (real git calls, read-only)
    against a disposable throwaway git repo built fresh per test.
  - `test_warm_op_path_ceremony_process_time` batches "brief plus apply"
    through the SAME warm op path, against the SAME disposable repo, with
    every `_CLI_DISPATCH` handler monkeypatched to a uniform stub. This
    isolates the OP PATH's own reach and directive-loop overhead (real git
    reads for brief's branch/tag state, the async dispatch adapters in
    `ops.py`, `apply_base.execute_directives`'s ordering/gating) — each
    individual directive handler's OWN execution cost (real tag cuts, PR
    creation, portability sweeps) is a separate, per-handler question
    C2/C3 and each handler's own module already own; duplicating it here
    would conflate two different things under one number. Verified by
    hand against this fixture before landing this file: with no judgment
    decisions supplied, `apply()` halts at `ship_verdict`/
    `version_bump_final` (exit_code 1, "directive_failed") without landing
    `d2`/`d4` — i.e. the stub table is exercised but the repo is never
    mutated, so re-running it `k` times measures the same job every time.
  - `test_cold_forwarder_brief_process_time` re-measures the COLD path
    (`coordinator/bin/merge-assemble.py brief`, C3's own instrument) on
    the SAME fixture, so the two paths are compared like-for-like rather
    than against C3's original (differently-provisioned) baseline.

NEGATIVE-SPEC:
    - Do NOT run `apply()` (real dispatch table, no stub) against this
      repo's own working tree — a prior hand-verification of this file's
      approach did exactly that from the wrong cwd and minted a real
      `tier-u-grant` write against this tree before the throwaway-repo
      fixture was added; every test in this module passes an explicit
      `repo_root` AND spawns with `cwd` pointed at the throwaway fixture,
      never this tree.
    - Do NOT roll a new timer — `benchhmarks/process_time.py ::
      batched_process_time_ms` is the one instrument, per that module's
      own docstring and every prior chunk's usage of it.
    - Do NOT re-assert C2/C3's static AST spawn-freedom guard here
      (`test_no_interpreter_spawn.py` already owns it) — this module's
      job is process TIME on the real dispatch path, not source shape.

Spec backlink: docs/plans/2026-08-26-merges-directives-stop-starting-interpreters.md, chunk C7
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    batched_process_time_ms,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 6
"""Matches the sizing/C3/C4 methodology (k>=6) already established for this
instrument elsewhere in this repo — not re-derived here."""

BRIGHTLINE_MS = 500.0
"""DR-344's own brightline (CLAUDE.md § The brightline) — the number every
assertion in this module is read against, never `SUSPENSION_BAR_MS`."""

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FORWARDER = _REPO_ROOT / "coordinator" / "bin" / "merge-assemble.py"
_ENTRY_POINT_SHIM = _REPO_ROOT / "coordinator" / "bin" / "lib" / "entry_point_shim.py"
_LAUNCHER_CMD = _REPO_ROOT / "coordinator" / "bin" / "merge-assemble.cmd"

_OP_PATH_SWEEP_EXEMPT_FILES = (
    Path(__file__).resolve(),
    (_REPO_ROOT / "coordinator_core" / "merge_assemble" / "ops.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "merge_assemble" / "apply.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "merge_assemble" / "cli.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "merge_assemble" / "__init__.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "authz" / "classification.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "op_scopes.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "ops" / "_registry_map.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "ops" / "__init__.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "directive_cli_arity.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "session" / "grant_directive.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "workweek_complete" / "brief.py").resolve(),
)
"""Files that legitimately NAME the two op ids without DISPATCHING them,
verified individually at pin time (each occurrence read at its cited line
before exemption — no blanket "docstrings are fine" rule):

    - `ops.py` — defines the two handlers (`register_op` decorator).
    - `apply.py`, `merge_assemble/__init__.py`, `cli.py` (the last added by
      this plan's C1, this exemption row added by C3 after the sweep fired
      on it — verified at source: two docstring mentions of the module
      path `merge_assemble.apply.main_apply`/`coordinator_core.
      merge_assemble.apply`, neither inside a call expression) — a bare
      PYTHON MODULE PATH (`coordinator_core.merge_assemble.apply`), not the
      op-id STRING `"merge_assemble.apply"` a dispatch call would pass —
      the substring match is incidental to Python's own dotted-path
      spelling, confirmed by reading each cited line (module docstrings, an
      import statement, inline comments), none inside a call expression.
    - `authz/classification.py`, `op_scopes.py`, `ops/_registry_map.py`,
      `ops/__init__.py` — registration/classification bookkeeping (op-class
      table, scope table, module-path map, this package's own doc comment
      about C6) — none calls a warm-dispatch mechanism with either op id.
    - `directive_cli_arity.py`, `session/grant_directive.py`,
      `workweek_complete/brief.py` — prose citing the op/module by name in
      a docstring or comment, not a call site.

This module itself names both ids to assert their absence elsewhere, so it
is exempt from its own sweep too — matching the idiom
`test_assembler_brightline_conformance.py` already established for this
exact shape of check."""

_OP_IDS = ("merge_assemble.brief", "merge_assemble.apply")

_WARM_OP_PROBE_SOURCE = textwrap.dedent(
    '''\
    """Standalone probe (written to disk by test_warm_ceremony_brightline.py,
    never committed): calls the WARM OP PATH's own two registered handlers
    in-process, in a single spawned interpreter, so
    `batched_process_time_ms` can measure that interpreter's process time.

    argv[1] is the throwaway repo root; argv[2] ("stub" | "real") selects
    whether `_CLI_DISPATCH` is monkeypatched to a uniform no-op stub before
    calling `merge_assemble.apply` — "stub" isolates the op path's own
    reach/dispatch-loop overhead (this module's ceremony test); "real"
    never actually lands here (apply mutates and this probe is reused
    read-only-only by design), kept as an explicit branch so a future
    caller cannot silently drop the stub without noticing the branch.
    """
    import asyncio
    import sys
    from pathlib import Path

    from coordinator_core.merge_assemble import apply as _ma_apply
    from coordinator_core.merge_assemble import ops as _ma_ops


    def _stub_handler(args, repo_root):
        return {"ok": True, "args": args, "repo_root": str(repo_root)}


    def main() -> int:
        repo_root = Path(sys.argv[1])
        mode = sys.argv[2] if len(sys.argv) > 2 else "brief-only"

        brief_result = asyncio.run(_ma_ops._merge_assemble_brief({}, repo_root))
        if brief_result["exit_code"] != 0:
            print(f"brief failed: {brief_result}", file=sys.stderr)
            return 1
        if mode == "brief-only":
            return 0

        if mode == "stub":
            for cli_name in list(_ma_apply._CLI_DISPATCH):
                _ma_apply._CLI_DISPATCH[cli_name] = _stub_handler
        elif mode != "real":
            print(f"unrecognized mode {mode!r}", file=sys.stderr)
            return 2

        apply_result = asyncio.run(
            _ma_ops._merge_assemble_apply(
                {"session_id": "c7-warm-op-probe", "force": True}, repo_root
            )
        )
        # exit_code 1 ("directive_failed"/halted-at-judgment) is EXPECTED
        # and fine here — no decisions are supplied, so ship_verdict/
        # version_bump_final stay unresolved and d2/d4 never land (verified
        # by hand: the repo's own tag set is unchanged after this call).
        # Only a transport-level failure (exit_code not in {0, 1}) is an
        # error this probe should surface.
        if apply_result["exit_code"] not in (0, 1):
            print(f"apply transport-failed: {apply_result}", file=sys.stderr)
            return 1
        return 0


    if __name__ == "__main__":
        sys.exit(main())
    '''
)


def _require_windows_or_darwin() -> None:
    if not (IS_WINDOWS or IS_DARWIN):
        pytest.skip(
            "batched_process_time_ms's spawn-count primitive is Windows/Darwin-only "
            "(coordinator_core.benchmarks.process_time module docstring)"
        )


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(root),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_throwaway_repo(root: Path) -> None:
    """A minimal, disposable git repo with one commit and one tag — enough
    for `brief()`'s real (non-mocked) `rev-list`/`tag` calls to succeed,
    entirely isolated from this tree's own working copy."""
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "c7-brightline@example.invalid")
    _git(root, "config", "user.name", "c7-brightline")
    (root / "README.md").write_text("throwaway C7 brightline fixture\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "tag", "v0.0.1")


@pytest.fixture()
def _throwaway_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "c7-throwaway-repo"
    repo.mkdir()
    _init_throwaway_repo(repo)
    return repo


@pytest.fixture()
def _warm_op_probe(tmp_path: Path) -> Path:
    probe = tmp_path / "warm_op_probe.py"
    probe.write_text(_WARM_OP_PROBE_SOURCE, encoding="utf-8")
    return probe


def _spawn_env() -> dict:
    """The probe imports `coordinator_core` — it is spawned with `cwd` set
    to the throwaway repo (never this tree), so `PYTHONPATH` is how it
    finds this repo's own package rather than relying on an accidental
    cwd-based `sys.path` entry."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(_REPO_ROOT) + (os.pathsep + existing if existing else "")
    return env


def _call_callee_name(call: ast.Call) -> Optional[str]:
    """The bare name a `Call` node's callee resolves to: `foo(...)` ->
    "foo", `mod.foo(...)` -> "foo" (attribute access, module-qualification
    dropped — this module only ever needs to distinguish the LEAF name,
    `route`/`cc_invoke.route` and `_merge_assemble_dispatch` alike)."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _literal_arg_call_sites(tree: ast.AST, literal_value: str) -> list:
    """`(enclosing_call_callee_name, positional_arg_index)` for every `Call`
    node in `tree` that passes `literal_value` as a positional string
    constant — i.e. an actual CALL ARGUMENT, never a module docstring or a
    bare `Expr` statement (AST has no comment nodes at all, and a docstring
    is a statement, not a call argument, so both are structurally excluded
    by construction, not by a separate filter)."""
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee_name = _call_callee_name(node)
        if callee_name is None:
            continue
        for idx, arg in enumerate(node.args):
            if isinstance(arg, ast.Constant) and arg.value == literal_value:
                sites.append((callee_name, idx))
    return sites


def _function_param_name_at(tree: ast.AST, func_name: str, idx: int) -> Optional[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            args = node.args.args
            if idx < len(args):
                return args[idx].arg
    return None


def _function_calls_route_with_param(tree: ast.AST, func_name: str, param_name: str) -> bool:
    """True if the named function's body contains a call whose leaf callee
    name is `route` (matches both a bare `route(...)` and `cc_invoke.
    route(...)`) with `param_name` passed as its FIRST positional argument —
    i.e. the function's own `op` parameter is threaded straight into the
    dispatch call, not merely present somewhere in the function body."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == func_name):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            if _call_callee_name(sub) != "route":
                continue
            if not sub.args:
                continue
            first = sub.args[0]
            if isinstance(first, ast.Name) and first.id == param_name:
                return True
    return False


def _op_id_reaches_a_route_call(source: str, op_id: str) -> bool:
    """AC8 (this plan): True iff `op_id` is passed as a literal positional
    argument to some function `F` in `source`, AND `F`'s body itself calls
    `route(...)`/`cc_invoke.route(...)` passing the SAME parameter (by
    position) as `route`'s first argument.

    Two hops by construction, matching the real call shape this plan's C2
    wrote: `entry_point_shim._merge_assemble_entry` calls
    `_merge_assemble_dispatch("merge_assemble.apply", ...)` (the literal),
    and `_merge_assemble_dispatch(op, ...)` itself calls
    `cc_invoke.route(op, params, repo_root, _legacy_fn)` (the same `op`
    parameter, threaded through). A one-hop check (bare substring/literal
    presence) cannot tell that shape apart from a docstring mentioning the
    op id in prose — the exact vacuous-inversion failure this check exists
    to avoid (see this class's own docstring and the module's SUPERSEDED
    note above)."""
    tree = ast.parse(source)
    for callee_name, idx in _literal_arg_call_sites(tree, op_id):
        param_name = _function_param_name_at(tree, callee_name, idx)
        if param_name is None:
            continue
        if _function_calls_route_with_param(tree, callee_name, param_name):
            return True
    return False


class TestNoInTreeCallerDispatchesTheRegisteredOp:
    """AC12's structural half, INVERTED by a follow-on plan (module
    docstring's SUPERSEDED note above, docs/plans/2026-08-26-merge-
    assembles-entry-point-reaches-the-warm-engine.md, chunk C3/AC8): C2
    wired `entry_point_shim.py :: _merge_assemble_entry` to actually
    dispatch both op ids through `cc_invoke.route`, falsifying the original
    "nothing in this tree dispatches these ops" claim on purpose.

    `test_no_in_tree_caller_dispatches_either_op_id` below does NOT reuse
    the retired substring sweep with `== []` flipped to `!= []` — that
    would pass identically whether or not the warm route was ever wired,
    since a bare docstring/module-path mention of the op id (which this
    file's own `_OP_PATH_SWEEP_EXEMPT_FILES` list proves exist, e.g. C1's
    `cli.py`) satisfies "not empty" without ever calling `route(...)`. The
    replacement is `_op_id_reaches_a_route_call` above: AST-based, keyed on
    the op id reaching an actual `route(...)`/`cc_invoke.route(...)` call
    site specifically inside `coordinator/bin/lib/entry_point_shim.py`, and
    covered by its own red/green pin (`test_goes_red_if_the_route_call_is_
    removed`) so a future edit that quietly drops the dispatch call fails
    this test rather than a differently-shaped one."""

    def test_the_sweep_still_catches_a_production_caller(self) -> None:
        """Retained: the retired substring-sweep helper's own sanity check
        (a planted reference in ordinary prose must still be caught by a
        plain substring scan) — orthogonal to the AST-based replacement
        below and still a fair characterization of why a bare substring
        flip is not enough on its own."""
        content = 'client.dispatch("merge_assemble.apply", params={})\n'
        offenders = [op_id for op_id in _OP_IDS if op_id in content]
        assert offenders == ["merge_assemble.apply"]

    def test_entry_point_shim_dispatches_both_op_ids(self) -> None:
        """AC8: the entry DOES dispatch — both `merge_assemble.brief` and
        `merge_assemble.apply` reach an actual `route(...)` call inside
        `entry_point_shim.py`, keyed by AST, not by a substring hit."""
        source = _ENTRY_POINT_SHIM.read_text(encoding="utf-8")
        for op_id in _OP_IDS:
            assert _op_id_reaches_a_route_call(source, op_id), (
                f"{op_id} is not reached by a route(...)/cc_invoke.route(...) "
                f"call in {_ENTRY_POINT_SHIM} — AC8 requires an actual "
                "dispatch call site, not a docstring/module-path mention"
            )

    def test_goes_red_if_the_route_call_is_removed(self) -> None:
        """AC8's (c): the check must actually depend on C2's `route(...)`
        call being present — proved by mutating it out of the REAL source
        text (never a hand-written fixture) and asserting the check flips
        to False for both op ids."""
        source = _ENTRY_POINT_SHIM.read_text(encoding="utf-8")
        target = "result = cc_invoke.route(op, params, repo_root, _legacy_fn)"
        assert target in source, (
            "mutation target line not found in entry_point_shim.py — this "
            "test is stale against the real C2 call site and needs updating"
        )
        mutated = source.replace(target, "result = _legacy_fn()")
        assert mutated != source
        for op_id in _OP_IDS:
            assert not _op_id_reaches_a_route_call(mutated, op_id), (
                f"{op_id} still reads as dispatched after removing the "
                "route(...) call — the check is not actually keyed on that "
                "call site"
            )

    def test_a_bare_docstring_mention_does_not_satisfy_the_check(self) -> None:
        """Guards against the exact vacuous-inversion failure mode the EM
        addendum named: a bare `== []` -> `!= []` substring flip would pass
        on a docstring/module-path mention alone. A fake module that only
        ever mentions the op id in prose must NOT satisfy the AST check."""
        fake_source = (
            '"""Mentions merge_assemble.apply only in prose, e.g. the '
            'module path coordinator_core.merge_assemble.apply — never as '
            'a call argument."""\n'
            "def _entry(argv):\n"
            "    return 0\n"
        )
        assert not _op_id_reaches_a_route_call(fake_source, "merge_assemble.apply")

    def test_no_in_tree_caller_dispatches_either_op_id(self) -> None:
        """Narrowed scope note (unchanged from before this plan): this
        substring sweep only ever walked `coordinator_core/`, never
        `coordinator/bin/lib/` — so it was never the mechanism that would
        have caught `entry_point_shim.py`'s new dispatch call site either
        way, and stays a true statement about `coordinator_core` itself:
        no module INSIDE `coordinator_core` (as opposed to the `coordinator/
        bin/lib` entry point above) dispatches either op id directly."""
        offenders: list[tuple[str, str]] = []
        for root, dirs, files in os.walk(_REPO_ROOT / "coordinator_core"):
            if "__pycache__" in root:
                continue
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                path = Path(root) / fname
                if path.resolve() in _OP_PATH_SWEEP_EXEMPT_FILES:
                    continue
                if fname.startswith("test_") or fname.endswith("_test.py"):
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for op_id in _OP_IDS:
                    if op_id in content:
                        offenders.append((str(path.relative_to(_REPO_ROOT)), op_id))
        assert offenders == [], (
            "an in-tree caller under coordinator_core/ now references a "
            "merge_assemble op id outside ops.py's own registration/"
            "classification bookkeeping — if this is a new dispatch call "
            f"site, re-derive this test's scope: {offenders!r}"
        )

    def test_forwarder_still_resolves_in_process_via_entry_point_shim(self) -> None:
        """`coordinator/bin/merge-assemble.py` is still the routing half:
        a thin shim importing `entry_point_shim.run_target`, never a warm
        client. This is the file every real `/merge-to-main` invocation
        actually runs."""
        content = _FORWARDER.read_text(encoding="utf-8")
        assert "run_target" in content
        assert "entry_point_shim" in content
        assert "try_warm_dispatch" not in content

        shim_content = (
            _REPO_ROOT / "coordinator" / "bin" / "lib" / "entry_point_shim.py"
        ).read_text(encoding="utf-8")
        assert '"merge-assemble"' in shim_content
        assert "_simple_entry" in shim_content


class TestWarmCeremonyBrightline:
    """AC11: the converged ceremony's process-time cost on the WARM OP
    PATH (C6's registered handlers, called in-process), against DR-344's
    500ms brightline. Every assertion below is labelled with which path
    produced its number."""

    def test_warm_op_path_brief_process_time(
        self, _throwaway_repo: Path, _warm_op_probe: Path
    ) -> None:
        """WARM OP PATH, `merge_assemble.brief` only — COMPUTE_ONLY, real
        (non-stubbed) git reads against the throwaway fixture, safely
        batched k times."""
        _require_windows_or_darwin()

        result = batched_process_time_ms(
            [sys.executable, str(_warm_op_probe), str(_throwaway_repo), "brief-only"],
            k=K_INVOCATIONS,
            cwd=str(_throwaway_repo),
            env=_spawn_env(),
        )
        assert result["rc"] == 0, result
        assert result["process_time_ms"] <= BRIGHTLINE_MS, (
            f"WARM OP PATH brief() missed the 500ms brightline: {result!r}"
        )

    def test_warm_op_path_ceremony_process_time(
        self, _throwaway_repo: Path, _warm_op_probe: Path
    ) -> None:
        """WARM OP PATH, brief() + apply() end to end, `_CLI_DISPATCH`
        stubbed (module docstring § METHODOLOGY) — the op path's own
        reach and directive-loop overhead, real git reads included, no
        per-handler execution cost folded in."""
        _require_windows_or_darwin()

        result = batched_process_time_ms(
            [sys.executable, str(_warm_op_probe), str(_throwaway_repo), "stub"],
            k=K_INVOCATIONS,
            cwd=str(_throwaway_repo),
            env=_spawn_env(),
        )
        assert result["rc"] == 0, result
        # Reported findings from this chunk's own verification run (this
        # box, 2026-08-26, k=6, Windows): 182.292ms process time / 9.0
        # procs per call (1 interpreter + 8 real `git` subprocesses — two
        # brief() computations, one standalone and one inside apply()'s
        # own re-brief, each issuing ~4 real git calls). Both figures are
        # reported, not just gated, so a reader does not have to re-derive
        # the spawn/interpreter split from a bare pass/fail.
        assert result["process_time_ms"] <= BRIGHTLINE_MS, (
            "WARM OP PATH brief()+apply() (dispatch table stubbed) missed "
            f"the 500ms brightline: {result!r} — dominant cost is real git "
            "subprocesses (brief() runs twice: once standalone, once "
            "inside apply()'s own re-brief), not the op-path adapter itself"
        )

    def test_warm_op_probe_rejects_unrecognized_mode(
        self, _throwaway_repo: Path, _warm_op_probe: Path
    ) -> None:
        """Not a brightline measurement — covers the probe's own
        `elif mode != "real":` guard (code-reviewer finding: that branch
        was unreachable from this module, since every other test here only
        ever passes "brief-only" or "stub"). A single plain subprocess
        call, no batching: asserts the probe's documented `argv[2]`
        contract ("stub" | "real", never anything else) is actually
        enforced, not merely asserted in its own docstring."""
        _require_windows_or_darwin()

        completed = subprocess.run(
            [sys.executable, str(_warm_op_probe), str(_throwaway_repo), "bogus-mode"],
            cwd=str(_throwaway_repo),
            env=_spawn_env(),
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert completed.returncode == 2, completed
        assert "unrecognized mode" in completed.stderr, completed

    @pytest.mark.real_home
    def test_cold_forwarder_brief_process_time(self, _throwaway_repo: Path) -> None:
        """COLD FORWARDER PATH (`coordinator/bin/merge-assemble.py brief`,
        C3's own instrument), re-measured on this SAME throwaway fixture
        so the warm/cold comparison above is like-for-like rather than
        against C3's differently-provisioned baseline. Reported findings
        from this chunk's own verification run (this box, 2026-08-26,
        k=6, Windows): 114.583ms process time / 5.0 procs per call —
        consistent with C3's own pre-C6 baseline (137.5ms/5 procs), i.e.
        this leg is genuinely untouched by C6/C7 (AC12's own finding: no
        caller was repointed at the registered op).

        `@pytest.mark.real_home`: the forwarder resolves `CLAUDE_KLABAUTER_ROOT`
        through the machine-local registry — under this suite's own
        HOME-quarantine autouse fixture (`coordinator_core/conftest.py ::
        _quarantine_real_home`) that resolution fails outright
        ("CLAUDE_KLABAUTER_ROOT resolution failed... set it via 'machine-local set
        repos.claude_klabauter'"), which is a fixture-environment artifact,
        not a property of the path itself. This is exactly the documented
        opt-out case (a read-only oracle against the live tree's own
        registry), verified against this file's own throwaway repo before
        landing this test."""
        _require_windows_or_darwin()

        result = batched_process_time_ms(
            [sys.executable, str(_FORWARDER), "brief"],
            k=K_INVOCATIONS,
            cwd=str(_throwaway_repo),
        )
        assert result["rc"] == 0, result
        assert result["procs_per_call"] >= 2.0, (
            "the cold forwarder path is expected to still spawn multiple "
            f"processes (interpreter + real git calls) — got {result!r}, "
            "which would mean this leg silently changed shape"
        )


def _ps_single_quote(value: str) -> str:
    """PowerShell single-quoted literal, embedded-`'` doubled per PS's own
    quoting rule — no other escaping applies inside a single-quoted PS
    string, unlike a double-quoted one."""
    return "'" + value.replace("'", "''") + "'"


def _powershell_exe() -> str:
    import shutil

    return shutil.which("powershell.exe") or shutil.which("pwsh") or "powershell.exe"


def _run_via_powershell_call_operator(cmd_path: Path, args: list, cwd: Path):
    """Invokes `cmd_path` (a `.cmd` launcher) through PowerShell's `&` call
    operator — the actual Shape W rung this module's docstring names, and
    the one measured (this chunk's own verification, 2026-08-27) to produce
    a `%CMDCMDLINE%` capture classified SOUND by `raw_cmdline_recovery.
    _classify_raw_cmdline_transport`: PowerShell always wraps a `.cmd`
    invocation as `cmd.exe /c ""<path>" <args>""`, regardless of how the
    argument itself was quoted on the PowerShell side. This is the ONLY
    invocation shape verified (by hand, against this exact launcher, before
    writing this helper) to reproduce cmd.exe's real `%*` quote-stripping —
    calling `coordinator/bin/merge-assemble.py` directly with
    `sys.executable`, or spawning `cmd.exe` in list-form, does not reliably
    reproduce it (measured: a list-form `subprocess.run([...])` spawn of
    this exact launcher did NOT corrupt a compact, whitespace-free JSON
    payload on this box, so it is not usable as a negative control either)."""
    quoted_args = " ".join(_ps_single_quote(a) for a in args)
    ps_command = f"& {_ps_single_quote(str(cmd_path))} {quoted_args}"
    return subprocess.run(
        [_powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", ps_command],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class TestDecisionsJsonSurvivesTheCmdLauncher:
    """AC5 (this plan): `--decisions <json>` through the REAL `.cmd`
    launcher, invoked via PowerShell's call operator, with a quote-bearing
    payload — proof only a genuine launcher spawn can provide (calling the
    Python entry directly, or `coordinator/bin/merge-assemble.py` with
    `sys.executable`, never exercises `cmd.exe`'s own argv mangling).

    KNOWN GAP, found while writing this test (not fixed here — the fix
    lives in `coordinator/bin/lib/raw_cmdline_recovery.py`, outside this
    chunk's declared write scope, and is shared by five OTHER CLIs besides
    merge-assemble): `recover_json_flag_argv` recovers a JSON payload's
    value by patching ONE argv slot with the extracted, well-formed JSON
    text, but does not remove the EXTRA slots cmd.exe's `%*` re-tokenizes a
    payload into when the JSON contains internal whitespace (e.g. `{"note":
    "a quoted probe"}` mangles into `['{note:', 'a', 'quoted', 'probe...`,
    and only the first of those four tokens gets replaced) — the
    parse loop then chokes on the leftover stray tokens
    (`merge-assemble apply: unrecognized argument 'quoted'`). Verified by
    hand against this exact launcher before landing this test: a
    WHITESPACE-FREE JSON payload (still quote-bearing — this is the actual
    defect the recovery mechanism targets, cmd.exe stripping the payload's
    double quotes) round-trips correctly; a payload with internal
    whitespace does not. This test asserts the case that is actually
    fixed today; the whitespace gap is reported to the dispatching EM as a
    found defect, not silently worked around or asserted against here."""

    @pytest.mark.real_home
    def test_a_whitespace_free_quote_bearing_payload_survives(
        self, _throwaway_repo: Path
    ) -> None:
        _require_windows_or_darwin()
        if not IS_WINDOWS:
            pytest.skip("cmd.exe argv mangling is Windows-only")

        payload = json.dumps({"note": "a-quoted-probe", "n": 1}, separators=(",", ":"))
        completed = _run_via_powershell_call_operator(
            _LAUNCHER_CMD,
            ["apply", "--decisions", payload],
            cwd=_throwaway_repo,
        )

        assert "unrecognized argument" not in completed.stderr, completed
        assert "malformed" not in completed.stderr.lower(), completed
        # A usage/parse failure (exit 2/3) means the payload arrived
        # corrupted; every other documented apply exit code (0 ok, 1
        # halted-at-judgment, 4 partial-mutation) means parse_apply_argv
        # accepted the recovered JSON and dispatch actually ran on it —
        # which is what this AC is evidence for, not a specific outcome.
        assert completed.returncode not in (2, 3), completed
        report = json.loads(completed.stdout)
        assert isinstance(report, dict), completed


class TestInvokeByNameFromTheLauncher:
    """AC7 (this plan, = parent plan's AC8's two-consumer rule): `merge-
    assemble brief` and `merge-assemble apply --help` both still work when
    run from a prompt through the real `.cmd` launcher — not merely
    through `sys.executable coordinator/bin/merge-assemble.py`."""

    @pytest.mark.real_home
    def test_brief_runs_from_the_launcher(self, _throwaway_repo: Path) -> None:
        _require_windows_or_darwin()
        if not IS_WINDOWS:
            pytest.skip(".cmd launcher invocation is Windows-only")

        completed = subprocess.run(
            [str(_LAUNCHER_CMD), "brief"],
            cwd=str(_throwaway_repo),
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert completed.returncode == 0, completed
        decision_object = json.loads(completed.stdout)
        assert isinstance(decision_object, dict), completed

    @pytest.mark.real_home
    def test_apply_help_runs_from_the_launcher(self, _throwaway_repo: Path) -> None:
        """`apply --help` is NOT recognized by `main_apply`'s own hand-rolled
        argv loop (pre-existing, unrelated to this plan's routing work —
        only bare top-level `--help`/`-h` short-circuits before subcommand
        dispatch); it falls through to `unrecognized argument '--help'` and
        the CLI's own usage text at exit 3 — verified by hand against this
        exact launcher before landing this test. AC7's claim is narrower
        than "succeeds": invoke-by-name through the launcher reaches the
        real CLI and prints its own documented usage diagnostic, rather
        than an import/transport failure or a corrupted-argv failure."""
        _require_windows_or_darwin()
        if not IS_WINDOWS:
            pytest.skip(".cmd launcher invocation is Windows-only")

        completed = subprocess.run(
            [str(_LAUNCHER_CMD), "apply", "--help"],
            cwd=str(_throwaway_repo),
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert completed.returncode == 3, completed
        assert "usage" in completed.stderr.lower(), completed
        assert "not importable" not in completed.stderr, completed
        assert "CLAUDE_KLABAUTER_ROOT resolution failed" not in completed.stderr, completed

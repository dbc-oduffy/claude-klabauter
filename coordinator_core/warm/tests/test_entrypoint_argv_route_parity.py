"""coordinator_core.warm.tests.test_entrypoint_argv_route_parity -- the
route-parity gate DoE-claude asked for: for EVERY allowlisted CLI, the
arguments the warm door route hands `main` are the arguments that CLI's own
cold route hands `main`, given the same typed argv.

Why this suite exists at all. `coordinator/bin` carries three argv
conventions and, until 7cdd815b0/this chunk, the warm route imposed one on
all of them: it relayed the door's `argv[1:]` (`door.c` -- argv[0] never
crosses the wire) straight into `main(argv)` and never set `sys.argv`. The
231 entrypoints written `sys.exit(main())` therefore read the warm SERVER's
command line instead of the caller's, and the 36 written
`sys.exit(main(sys.argv))` re-sliced `[1:]` themselves and ate their own
first real argument -- which, on subcommand CLIs, reads as
`pickup-assemble: unknown subcommand '<path>'`. Both are silent argv
corruption at the highest-traffic surface in the fleet.
→ cross-repo/inbox/2026-08-29-doe-claude-em-exe-forwarder-argv-mangling.md
→ state/bug-backlog/2026-08-29-the-warm-route-hands-clis-the-server-s-s-6f56b9b28c79.yaml

TWO REQUIREMENTS, BOTH FROM HOW THE ORIGINAL INVESTIGATION NEARLY WENT
WRONG, and neither of them watered down here:

  PER CLI, NEVER ON A SAMPLE. DoE's two probes disagreed with each other --
  `pickup-assemble` lost one position, `coordinator-queue-append` lost
  everything -- because they are different shapes. A sample of one would have
  certified whichever probe happened to be picked. `test_route_parity_per_
  allowlisted_cli` therefore runs the whole allowlist, and
  `test_the_population_is_the_live_allowlist` is what stops that population
  quietly shrinking to a sample later.

  A FAILING LEG PINNED IN THE TEST, not performed once at authoring time. A
  parity check written against an already-green pair proves the harness runs,
  never that it discriminates. The pre-fix door's own verification did
  exactly that and passed on patched and unpatched alike (its probe carried
  `--help`, which `coordinator-queue-append` answers from its `argv`
  parameter before ever reaching the `parse_args()` line that held the bug).
  `test_the_parity_check_discriminates` re-runs the parity predicate against
  the PRE-FIX behaviour -- one shape for everyone -- and requires it to go
  red, per shape, with the mismatch named.

HOW THE COLD SIDE IS OBTAINED, and why it is not circular. The cold argv is
not modelled and not hardcoded per name: `_cold_call_args` reads the file's
OWN `if __name__ == "__main__":` guard and EVALUATES the expression that
guard passes to `main`, against a `sys.argv` set exactly as a real cold
invocation would set it. The warm side comes from the production decision
function (`invoke_from_argv.entrypoint_call_args`) run on the shape label
`serve_classifier.classify_main_argv_shape` assigns. So the two sides are
derived by different means from the same source line -- an expression
evaluation versus a shape classification -- and a misclassified file fails
here rather than agreeing with itself.

Negative-spec (RAG-bait):
    This suite does NOT load, import, or exec any `coordinator/bin/*.py`
    module body for its per-CLI leg -- 365 module executions inside one test
    process is both a spawn-class cost and an arbitrary-side-effect hazard
    (`serve_classifier`'s own negative-spec makes the same commitment for the
    same reason). The single end-to-end leg that DOES run a real module body
    is one named CLI, and is there to prove the wiring the static legs
    cannot: that `_run_entrypoint` actually calls what
    `entrypoint_call_args` returns.

    It does NOT spawn a subprocess for the cold side. A cold `python
    coordinator/bin/<name>.py` per allowlisted CLI is ~365 process starts to
    re-measure something the file's own guard already states, against a
    500ms brightline (CLAUDE.md § The brightline) -- and it would measure the
    CLI's whole behaviour, where the defect is entirely in which argv reaches
    `main`.

    It does NOT assert anything about the six entrypoints whose guard
    expression cannot be evaluated (`_ARGV_UNEVALUABLE`) beyond their shape
    and the set staying closed -- an unevaluable guard is exactly the case a
    static reader cannot certify, and claiming parity for it would be the
    vacuous green this module exists to refuse.

Spec backlink: cross-repo/inbox/2026-08-29-doe-claude-em-exe-forwarder-argv-mangling.md
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from coordinator_core.ops import invoke_from_argv
from coordinator_core.ops.invoke_from_argv import (
    _entrypoint_argv_shape,
    _run_entrypoint,
    entrypoint_call_args,
)
from coordinator_core.warm import serve_classifier

_ENGINE_ROOT = Path(__file__).resolve().parents[3]
_BIN_DIR = _ENGINE_ROOT / "coordinator" / "bin"

#: A probe argv shaped like the reported failure: a subcommand followed by an
#: operand. One position is what the 36-shape loses, so a single-token probe
#: would pass on a broken door for half the population -- the first token has
#: to be distinguishable from the second for the loss to be observable.
_PROBE = ["brief", "some/artifact/path.md"]

#: Guards whose `main(...)` argument cannot be evaluated from source, with the
#: reason. Five compute `_argv` through `raw_cmdline_recovery.recover_windows_
#: argv` (the caret-eating .cmd defect's recovery hook -- see gen-launcher-
#: shim.py § RAW-CMDLINE-PRESERVATION ENTRYPOINTS); one calls a crash-guard
#: wrapper rather than `main` directly. All six are TAIL-shaped in fact, and
#: `test_unevaluable_guards_are_tail_shaped` asserts the door treats them so,
#: but this module will not claim measured parity for a guard it could not
#: evaluate.
_ARGV_UNEVALUABLE = {
    "cross-repo-memo": "_argv = recover_windows_argv(sys.argv[1:], ...)",
    "freeze-review-diff": "_argv = recover_windows_argv(sys.argv[1:], ...)",
    "parallel-review-gate-decision": "_argv = recover_windows_argv(sys.argv[1:], ...)",
    "parallel-review-orthogonality-guard": "_argv = recover_windows_argv(sys.argv[1:], ...)",
    "wsc-coverage-gate-runner": "_argv = recover_windows_argv(sys.argv[1:], ...)",
    "workday-start-step0": "guard calls _main_with_crash_guard(sys.argv[1:]), not main",
}

_NO_ARGS = ()


class _UnevaluableGuard(Exception):
    """The guard's `main(...)` argument is not a pure `sys.argv` expression."""


def _eval_argv_expr(node: ast.expr, cold_argv: list[str]) -> list[str]:
    """Evaluate a guard's `main(...)` argument against `cold_argv`.

    Deliberately a hand-written walk over three node types rather than
    `eval()`: this runs over 365 files of source that no reviewer has vetted
    for this purpose, and the whole point is to read what the guard says
    without executing anything it brought with it.
    """
    if isinstance(node, ast.Attribute) and node.attr == "argv":
        if isinstance(node.value, ast.Name) and node.value.id == "sys":
            return list(cold_argv)
        raise _UnevaluableGuard(ast.unparse(node))
    if isinstance(node, ast.Subscript):
        base = _eval_argv_expr(node.value, cold_argv)
        index = node.slice
        if isinstance(index, ast.Slice):
            if index.step is not None or index.upper is not None:
                raise _UnevaluableGuard(ast.unparse(node))
            if index.lower is None:
                return base
            if isinstance(index.lower, ast.Constant) and isinstance(index.lower.value, int):
                return base[index.lower.value :]
        raise _UnevaluableGuard(ast.unparse(node))
    raise _UnevaluableGuard(ast.unparse(node))


def _cold_call_args(script: Path, probe: list[str]) -> tuple:
    """The positional arguments `script`'s OWN `__main__` guard hands `main`
    when the file is run cold as `python <script> *probe`.

    Returns the same `tuple` shape `entrypoint_call_args` returns, so the two
    are directly comparable: `()` for a guard that calls `main()`, a
    one-tuple otherwise.
    """
    tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    calls = serve_classifier._guard_main_calls(tree)
    if not calls:
        raise _UnevaluableGuard("<no main(...) call in the __main__ guard>")
    cold_argv = [str(script)] + list(probe)
    resolved = set()
    for call in calls:
        if call.keywords or len(call.args) > 1:
            raise _UnevaluableGuard(ast.unparse(call))
        if not call.args:
            resolved.add(_NO_ARGS)
        else:
            resolved.add((tuple(_eval_argv_expr(call.args[0], cold_argv)),))
    if len(resolved) != 1:
        raise _UnevaluableGuard("guard calls main with more than one argv shape")
    return resolved.pop()


def _warm_call_args(script: Path, probe: list[str]) -> tuple:
    """The positional arguments the WARM route hands `main` -- the production
    decision, not a restatement of it. `probe` is passed as the door passes
    it: bare, argv[0] already stripped on the wire."""
    args = entrypoint_call_args(_entrypoint_argv_shape(script), script, list(probe))
    return tuple(tuple(a) if isinstance(a, list) else a for a in args)


def _allowlisted_scripts() -> list[tuple[str, Path]]:
    """Every allowlisted name with a script on disk, read from the LIVE
    allowlist. Names with no script are excluded here rather than skipped
    per-test: their absence is `test_every_allowlisted_name_warm_serves.py`'s
    finding to make, and duplicating it would put a second owner on it."""
    pairs = []
    for name in serve_classifier.load_allowlist_names():
        script = _BIN_DIR / f"{name}.py"
        if script.is_file():
            pairs.append((name, script))
    return pairs


def test_route_parity_per_allowlisted_cli():
    """THE gate: same typed argv, same arguments into `main`, warm or cold --
    asserted for every allowlisted CLI, never for a sample of them."""
    mismatches = []
    for name, script in _allowlisted_scripts():
        if name in _ARGV_UNEVALUABLE:
            continue
        cold = _cold_call_args(script, _PROBE)
        warm = _warm_call_args(script, _PROBE)
        if warm != cold:
            mismatches.append(
                f"{name}: cold hands main {cold!r}, warm route hands it {warm!r} "
                f"(shape={_entrypoint_argv_shape(script)})"
            )
    assert not mismatches, "\n".join(mismatches)


def test_the_parity_check_discriminates():
    """The pinned failing leg: the SAME predicate, run against the pre-fix
    door -- one shape for everyone, bare argv -- must go red, and must go red
    on both families that were broken.

    Without this, a green `test_route_parity_per_allowlisted_cli` is
    consistent with a harness that compares nothing. This is the test that
    would have caught the earlier `--help` probe passing on patched and
    unpatched alike.
    """
    def pre_fix_call_args(probe: list[str]) -> tuple:
        return (tuple(probe),)

    broken_full, broken_none = [], []
    for name, script in _allowlisted_scripts():
        if name in _ARGV_UNEVALUABLE:
            continue
        shape = _entrypoint_argv_shape(script)
        if pre_fix_call_args(_PROBE) == _cold_call_args(script, _PROBE):
            continue
        if shape == serve_classifier.ARGV_SHAPE_FULL:
            broken_full.append(name)
        elif shape == serve_classifier.ARGV_SHAPE_NONE:
            broken_none.append(name)

    assert broken_full, (
        "the parity predicate did not fail for a single main(sys.argv) CLI under the "
        "pre-fix door -- it is not discriminating, and a green parity run means nothing"
    )
    assert broken_none, (
        "the parity predicate did not fail for a single main() CLI under the pre-fix "
        "door -- it is not discriminating for the shape that lost ALL arguments"
    )
    assert "pickup-assemble" in broken_full, (
        "pickup-assemble is DoE's reported reproduction and the canonical "
        "main(sys.argv) CLI; a discrimination check that no longer covers it has "
        "drifted off the reported defect"
    )


def test_unevaluable_guards_are_a_closed_set():
    """`_ARGV_UNEVALUABLE` is a list of things this module CANNOT certify, so
    it may not grow silently. A new guard shape lands here, visibly, and gets
    a decision -- rather than being skipped into a green run."""
    unevaluable = set()
    for name, script in _allowlisted_scripts():
        try:
            _cold_call_args(script, _PROBE)
        except _UnevaluableGuard:
            unevaluable.add(name)
    assert unevaluable == set(_ARGV_UNEVALUABLE), (
        f"unevaluable guards changed: newly unevaluable={sorted(unevaluable - set(_ARGV_UNEVALUABLE))}, "
        f"no longer unevaluable={sorted(set(_ARGV_UNEVALUABLE) - unevaluable)}"
    )


def test_unevaluable_guards_are_tail_shaped():
    """All six are bare-args CLIs in fact; the door must treat them as such.
    This is the weaker claim the module is entitled to make about them -- it
    asserts the door's choice, never that parity was measured."""
    for name in _ARGV_UNEVALUABLE:
        script = _BIN_DIR / f"{name}.py"
        assert _entrypoint_argv_shape(script) == serve_classifier.ARGV_SHAPE_TAIL, name


def test_the_population_is_the_live_allowlist():
    """The per-CLI leg is only "per CLI" while its population is the live
    allowlist. Pinned so a later edit cannot narrow the gate to a sample
    without this failing."""
    names = {name for name, _ in _allowlisted_scripts()}
    allowlisted = set(serve_classifier.load_allowlist_names())
    assert names <= allowlisted
    assert len(names) > 300, f"population collapsed to {len(names)} CLIs"


def test_end_to_end_pickup_assemble_keeps_its_subcommand(monkeypatch):
    """The one leg that runs a real module body: proof that `_run_entrypoint`
    actually calls what `entrypoint_call_args` returns.

    The static legs above compare two derivations of the same source line and
    would both stay green if the call site ignored them entirely.

    Carries its own failing leg rather than asserting on output text: forcing
    the pre-fix shape (bare argv for everyone) must reproduce DoE's exact
    reported symptom through the same call, and the fixed route must not. A
    text assertion on the SUCCESS path would be environment-coupled -- under
    this suite's home quarantine `pickup-assemble` resolves its subcommand and
    then fails at CLAUDE_KLABAUTER_ROOT resolution, which is a pass for the property
    under test and looks like a failure to a naive output match.

    The engine root is handed over by env var rather than by opting out of
    the suite's home quarantine (`real_home` is refused outright for warm
    tests -- `test_warm_suite_does_not_litter_the_real_runtime_base.py`).
    It has to be handed over somehow: quarantined, `pickup-assemble` fails at
    root resolution BEFORE parsing, both legs return the same resolution
    error, and the check goes green while measuring nothing. Read-only, on a
    path deliberately not present.
    """
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(_ENGINE_ROOT))
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(_ENGINE_ROOT))
    argv = ["brief", "docs/plans/__parity_probe__.md"]

    monkeypatch.setattr(
        invoke_from_argv,
        "_entrypoint_argv_shape",
        lambda script: serve_classifier.ARGV_SHAPE_TAIL,
    )
    pre_fix = _run_entrypoint("pickup-assemble", list(argv), str(_ENGINE_ROOT))
    pre_fix_out = pre_fix["stdout"] + pre_fix["stderr"]
    assert "unknown subcommand" in pre_fix_out, (
        "forcing the pre-fix shape did not reproduce the reported symptom, so this "
        f"leg is not discriminating: {pre_fix_out[:400]}"
    )

    monkeypatch.undo()
    fixed = _run_entrypoint("pickup-assemble", list(argv), str(_ENGINE_ROOT))
    fixed_out = fixed["stdout"] + fixed["stderr"]
    assert "unknown subcommand" not in fixed_out, (
        f"pickup-assemble lost its subcommand through the warm route: {fixed_out[:400]}"
    )


def test_shape_cache_reresolves_when_the_file_changes(tmp_path):
    """A warm server outliving a publish must not keep serving the pre-publish
    shape. The cache is keyed on the stat pair for exactly that reason, and
    this is what holds it there."""
    script = tmp_path / "probe-cli.py"
    script.write_text(
        "import sys\ndef main(argv):\n    return 0\nif __name__ == '__main__':\n"
        "    sys.exit(main(sys.argv))\n",
        encoding="utf-8",
    )
    assert _entrypoint_argv_shape(script) == serve_classifier.ARGV_SHAPE_FULL

    script.write_text(
        "import sys\ndef main(argv=None):\n    return 0\nif __name__ == '__main__':\n"
        "    sys.exit(main(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    os.utime(script, (0, 0))
    assert _entrypoint_argv_shape(script) == serve_classifier.ARGV_SHAPE_TAIL


def test_unreadable_script_degrades_to_tail(tmp_path):
    """The default is behaviour-preserving, including on the paths that never
    reach a parse: an unreadable or unparseable file gets today's behaviour,
    never a guess. `_load_entrypoint_main` is about to fail on it anyway, with
    a better message than this function could give."""
    missing = tmp_path / "not-there.py"
    assert _entrypoint_argv_shape(missing) == serve_classifier.ARGV_SHAPE_TAIL

    broken = tmp_path / "broken.py"
    broken.write_text("def main(argv:\n", encoding="utf-8")
    assert _entrypoint_argv_shape(broken) == serve_classifier.ARGV_SHAPE_TAIL


def test_entrypoint_call_args_is_shape_keyed_not_name_keyed():
    """Negative-spec, asserted: the decision is a function of the SHAPE, and
    nothing in it may key on a CLI's identity -- that would be the per-name
    translation table `invoke.from_argv`'s own docstring forbids (DR-347
    Ruling 2)."""
    script = _BIN_DIR / "pickup-assemble.py"
    other = _BIN_DIR / "baton-assemble.py"
    probe = ["brief", "x.md"]
    full = serve_classifier.ARGV_SHAPE_FULL
    assert entrypoint_call_args(full, script, probe) == ([str(script)] + probe,)
    assert entrypoint_call_args(full, other, probe) == ([str(other)] + probe,)
    assert entrypoint_call_args(serve_classifier.ARGV_SHAPE_NONE, script, probe) == ()
    assert entrypoint_call_args(serve_classifier.ARGV_SHAPE_TAIL, script, probe) == (probe,)


def test_module_under_test_is_the_engine_tree_not_the_published_mirror():
    """Guards the trap that made two sessions read a post-commit reproduction
    as a failed fix: the warm server serves the PUBLISHED engine, so a green
    run here certifies the claude-klabauter tree and says nothing about what is running
    on the box until a publish lands."""
    assert Path(invoke_from_argv.__file__).resolve().is_relative_to(_ENGINE_ROOT)

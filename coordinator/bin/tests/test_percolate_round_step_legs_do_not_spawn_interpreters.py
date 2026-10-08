"""test_percolate_round_step_legs_do_not_spawn_interpreters -- a ratchet on
the one number the PM named: how many Python interpreters one percolate round
starts.

A round used to start eight, plus a ninth for the state-root resolver and a
tenth for the ladder probe that only existed to pick the ninth's interpreter.
That was an artifact of `percolate-round.py`'s origin -- it ports nine steps
the EM used to type one CLI invocation at a time, and the subprocess boundary
came along with them rather than being required by any of them. Measured on
the four resolution legs alone, best of 3: 411.9 ms wall spawned against
3.8 ms in-process.

The measurement is not the guard. Nothing stops a later edit from adding
`_run([sys.executable, str(_PERCOLATE_GATE), ...])` back one step at a time,
each one locally reasonable, and the count is back at eight with no single
commit to blame. This file is the guard.

Two legs are ALLOWED to spawn and are named here rather than pattern-matched,
so adding a third is a deliberate act that edits this list and says why:

  `_PUBLISH`   -- the real run. Its `_run` bound guards actual work rather
                  than spawn scheduling (`_PUBLISH_LEG_TIMEOUT_SECS`, 3600s),
                  it needs a distinct child environment, and an in-process
                  call cannot be timed out because there is no killable unit.
  `ci_script`  -- `<dest>/.github/scripts/run-all-checks.py`, which is FOREIGN
                  code living in the publish mirror. Running another repo's
                  script inside the round driver is a different objection
                  entirely, and process isolation is the whole point.

Negative-spec: this file asserts nothing about how many processes those two
legs or `git` cost, and nothing about wall-clock (§ CLAUDE.md -- process time
and spawn count, never wall clock). It reads source, never runs a round.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_step_legs_do_not_spawn_interpreters.py -q
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROUND_PY = Path(__file__).resolve().parent.parent / "percolate-round.py"

# The step CLIs a round consults. Each is called through `_run_step`, which
# calls its `main(argv)` in this interpreter.
_MUST_NOT_BE_SPAWNED = {"_PERCOLATE_GATE", "_PARSE_DRYRUN", "_STATE_ROOT_RESOLVER"}

# `_run_step` builds this for `_print_step_failure` to print -- the command a
# reader would run by hand to reproduce a step. It is a display string that
# never reaches `subprocess`, so it is not a spawn.
_DISPLAY_ONLY_TARGET = "equivalent_spawn"


def _interpreter_spawn_argvs(tree: ast.AST) -> list:
    """Every list literal that starts with an interpreter and is not the
    display-only reproduction line."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == _DISPLAY_ONLY_TARGET for t in node.targets
        ):
            continue
        if not isinstance(node, ast.List) or not node.elts:
            continue
        head = node.elts[0]
        starts_with_interpreter = (
            isinstance(head, ast.Attribute)
            and isinstance(head.value, ast.Name)
            and head.value.id == "sys"
            and head.attr == "executable"
        ) or (isinstance(head, ast.Name) and head.id == "python")
        if starts_with_interpreter:
            found.append(node)
    return found


def _referenced_names(node: ast.AST) -> set:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def test_no_step_cli_is_reached_by_starting_an_interpreter():
    """The eight-to-one collapse, pinned by name rather than by count."""
    tree = ast.parse(_ROUND_PY.read_text(encoding="utf-8"))
    offenders = []
    for argv in _interpreter_spawn_argvs(tree):
        spawned = _referenced_names(argv) & _MUST_NOT_BE_SPAWNED
        if spawned:
            offenders.append((argv.lineno, sorted(spawned)))
    assert not offenders, (
        "percolate-round.py starts a Python interpreter to reach a step CLI it "
        "can call in-process via `_run_step`: "
        + "; ".join(f"line {line}: {names}" for line, names in offenders)
        + ". Each of these CLIs is `main(argv) -> int` over `args.func(args)` with "
        "no `sys.exit` outside its `__main__` guard, so `_run_step(<SCRIPT>, [...])` "
        "returns the same `CompletedProcess` shape the call site already handles."
    )


def test_the_display_only_reproduction_line_is_still_display_only():
    """Guards this file's own blind spot.

    `_run_step` assembles an argv that LOOKS exactly like a spawn so a failure
    can print a reproducible command. The test above skips it by name. If that
    assignment is ever renamed or handed to `subprocess`, the skip silently
    starts excusing a real spawn -- so pin that it exists, and that it is only
    ever assigned, never called.
    """
    source = _ROUND_PY.read_text(encoding="utf-8")
    assert f"{_DISPLAY_ONLY_TARGET} = [sys.executable" in source, (
        f"`{_DISPLAY_ONLY_TARGET}` no longer names the display-only reproduction "
        "line in `_run_step`; the exclusion in this file's spawn scan is now "
        "either dead or excusing something else. Re-read `_run_step` before "
        "renaming it here."
    )
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
        # `subprocess.CompletedProcess(equivalent_spawn, ...)` is the intended
        # use -- it parks the argv on `.args` for `_print_step_failure` to
        # print. Only an EXECUTING callee is the failure.
        if name not in ("run", "Popen", "_run", "_run_step", "call", "check_output"):
            continue
        for arg in node.args:
            assert not (
                isinstance(arg, ast.Name) and arg.id == _DISPLAY_ONLY_TARGET
            ), (
                f"`{_DISPLAY_ONLY_TARGET}` (line {node.lineno}) is handed to "
                f"`{name}`. It is an argv built for `_print_step_failure` to "
                "PRINT; executing it reintroduces the spawn this module removed, "
                "and this file's scan skips it by name."
            )

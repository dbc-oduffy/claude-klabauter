"""test_no_client_timeout_dial_raises.py — no CLI trampoline under
coordinator/bin/ may set a client-side timeout dial for the op it invokes.

PM ruling 2026-08-21: nobody raises a timeout dial, us included. The habit it
bans is re-running a failing op with more time instead of fixing it, on a box
running 50-70 concurrent sessions where every widened wait is held against a
shared budget.

`reassess-goal-krs.py` shipped exactly that habit — an
`os.environ.setdefault("CC_INVOKE_TIMEOUT_SECS", "70")` in `main()`, sized
against a rationale that had already expired. Two things make it worth a
standing guard rather than a one-line deletion:

  - It bought nothing even when written. `CC_INVOKE_TIMEOUT_SECS` is a FLOOR
    on the client's wait (`cc_invoke.py::_op_timeout_ceiling`: the ceiling is
    `max(FLOOR, engine_budget(op) + MARGIN)`), not a raise on the op's own
    engine-side budget. `goals.reassess_krs` has no `ipc._OP_TIMEOUT_OVERRIDES`
    row, so the engine still gives up at `ipc.DISPATCH_TIMEOUT_SECS`; the 70
    only made the CLIENT keep waiting on a dispatch that had already died.
    `cc_invoke.py::_timeout_exceeded_message` documents an operator who set
    this to 300 and watched the same 30s-derived timeout recur.
  - `setdefault` inverted the precedence it claimed to preserve. Its comment
    said it avoided clobbering an operator override, but the trampoline runs
    before the op and an operator export sets the var FIRST, so the 70 lost to
    any ambient value and won against none.

The env-var name is not the invariant — the DIRECTION is. Pinning it by name
keeps the guard readable while the peer work retiring that var's read lands.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent

#: Client-side dials a trampoline could reach for. Every one governs how long
#: a CALLER waits, never how long the engine is allowed to take, so setting
#: one from a wrapper can only ever delay an honest failure.
_TIMEOUT_DIALS = frozenset(
    {
        "CC_INVOKE_TIMEOUT_SECS",
        "CC_INVOKE_CLIENT_MARGIN_SECS",
        "COORDINATOR_DISPATCH_TIMEOUT_SECS",
    }
)

#: `os.environ` methods that WRITE. `setdefault` is in the corpus on purpose:
#: it reads as deferential and is not — see this module's docstring.
_ENV_WRITE_METHODS = frozenset({"setdefault", "__setitem__", "update"})


def _dial_writes(path: Path):
    """Every `(lineno, dial)` where *path* writes one of `_TIMEOUT_DIALS` into
    the process environment.

    AST-based rather than a text grep so a mention in a docstring or comment
    (this file's own, for one) is not a finding, and so `os.environ[X] = ...`
    is caught alongside the method-call spellings.

    Sorted by line: `ast.walk` is breadth-first, so its raw order reflects
    node depth, not source order — an offender list a reader is meant to walk
    top-down must not be ordered by how deeply each poke happens to nest."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []

    def _dial_of(node) -> str:
        if isinstance(node, ast.Constant) and node.value in _TIMEOUT_DIALS:
            return str(node.value)
        return ""

    def _is_environ(node) -> bool:
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            return True
        return isinstance(node, ast.Name) and node.id == "environ"

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _ENV_WRITE_METHODS and _is_environ(node.func.value):
                for arg in node.args:
                    dial = _dial_of(arg)
                    if dial:
                        found.append((node.lineno, dial))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and _is_environ(target.value):
                    dial = _dial_of(target.slice)
                    if dial:
                        found.append((node.lineno, dial))
    return sorted(found)


class TestNoClientTimeoutDialRaises(unittest.TestCase):
    def test_reassess_goal_krs_sets_no_timeout_dial(self):
        """The named regression: the script this guard was written for."""
        script = _BIN_DIR / "reassess-goal-krs.py"
        self.assertTrue(script.is_file(), f"missing: {script}")
        self.assertEqual([], _dial_writes(script))

    def test_no_bin_trampoline_sets_a_timeout_dial(self):
        """The ruling is fleet-wide, so the guard is too — one script losing
        its poke means nothing if the next wrapper re-adds one."""
        offenders = []
        for script in sorted(_BIN_DIR.glob("*.py")):
            for lineno, dial in _dial_writes(script):
                offenders.append(f"{script.name}:{lineno} sets {dial}")
        self.assertEqual(
            [],
            offenders,
            "a client-side timeout dial set from a CLI wrapper delays an honest "
            "failure; size the op's own engine budget instead:\n  "
            + "\n  ".join(offenders),
        )

    def test_the_guard_can_see_the_shape_it_forbids(self):
        """Mechanism check — without it the two asserts above pass on a broken
        detector just as happily as on a clean tree."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.py"
            probe.write_text(
                "import os\n"
                'os.environ.setdefault("CC_INVOKE_TIMEOUT_SECS", "70")\n'
                'os.environ["COORDINATOR_DISPATCH_TIMEOUT_SECS"] = "300"\n',
                encoding="utf-8",
            )
            self.assertEqual(
                [(2, "CC_INVOKE_TIMEOUT_SECS"), (3, "COORDINATOR_DISPATCH_TIMEOUT_SECS")],
                _dial_writes(probe),
            )


if __name__ == "__main__":
    unittest.main()

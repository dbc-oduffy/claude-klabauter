"""Zero-spawn, fast-tier growth ratchet over the Bash-guard hot-path surface.

Spec backlink: docs/plans/2026-09-11-every-bash-call-pays-23k-lines-of-guard.md
(chunk C4 / AC7-AC9).

Three independent pins, each a per-commit OBSERVER over the hot-path
surface `dispatch.py`/`dispatch_checks.py` -- both imported per-invocation
by every peer session's Bash/PowerShell fire:

1. Eager-import count -- module-scope `import`/`from ... import`
   statements in `dispatch.py`.
2. `dispatch_checks.py` line count.
3. Registered-entry count -- `len(guard_roster())`.

Discriminator against
`coordinator_core/tests/test_hot_path_hook_import_budget.py`: that file's
`bash_guards_dispatch` row (`module_count_ceiling`) measures the real COLD
CLOSURE via a subprocess-spawning `len(sys.modules)` delta and is the
AUTHORITATIVE BAR over that closure -- it stays cadence-tier because a
fresh interpreter is not free. THIS file answers a cheaper structural
question (does the eager-import COUNT in `dispatch.py`'s own source, the
line count of `dispatch_checks.py`, or the registered-entry count grow)
with pure `ast`/text parsing -- zero subprocesses, fast tier -- so it
observes every commit instead of every cadence run. A legitimate growth
updates BOTH pins together; a green leg here is never sufficient on its own
to justify raising `module_count_ceiling`, and vice versa.

No leg of this module mutates a tracked file. The three measured inputs
are read through module-level indirections -- `_DISPATCH_PY_PATH`,
`_DISPATCH_CHECKS_PY_PATH`, and `_guard_roster_provider` (defaults to
`guard_roster`) -- so the red legs monkeypatch the path constants to a
`tmp_path` copy and the provider to a synthetic roster, while the green
legs read the real tree through the module defaults. `dispatch.py` and
`dispatch_checks.py` are imported per-invocation by every peer session's
Bash/PowerShell fire on a tree CLAUDE.md sizes at ~50 concurrent sessions;
a literal inject-and-revert into either live file is FORBIDDEN here
(CLAUDE.md's load norm) and is not how any leg of this module operates.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Callable, Sequence

import pytest

from coordinator_core.bash_guards.roster import guard_roster as _live_guard_roster

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Path indirection for leg 1 -- monkeypatched to a tmp_path copy by the red
#: leg, never mutated in place.
_DISPATCH_PY_PATH = _REPO_ROOT / "coordinator_core" / "bash_guards" / "dispatch.py"

#: Path indirection for leg 2 -- monkeypatched to a tmp_path copy by the red
#: leg, never mutated in place.
_DISPATCH_CHECKS_PY_PATH = (
    _REPO_ROOT / "coordinator_core" / "bash_guards" / "dispatch_checks.py"
)

#: Roster-provider indirection for leg 3 -- monkeypatched to a synthetic
#: callable by the red leg, defaults to the live registration.
GuardRosterProvider = Callable[[], Sequence[object]]
_guard_roster_provider: GuardRosterProvider = _live_guard_roster

# Pins below are independent second copies of the measured size, per AC7 --
# never `len()` of the thing they guard, never raised to make a red pass.
# Measured 2026-09-24, Linux-6.18.44-fc-v37-x86_64-with-glibc2.39,
# CPython 3.11.15, against dispatch.py post-P070-C3's eager-import shed.
_EAGER_IMPORT_CEILING = 21
_DISPATCH_CHECKS_LINE_CEILING = 12131
_REGISTERED_ENTRY_CEILING = 53


def _count_module_scope_imports(source: str) -> int:
    """Count `import`/`from ... import` statements at true
    module-execution scope (the AST's top-level `Module.body`) -- an import
    deferred into a function body (P070-C3's lever (a)) does not count."""
    tree = ast.parse(source)
    return sum(
        1 for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    )


def _count_lines(source: str) -> int:
    return len(source.splitlines())


def _assert_eager_import_ratchet() -> None:
    count = _count_module_scope_imports(
        _DISPATCH_PY_PATH.read_text(encoding="utf-8")
    )
    assert count <= _EAGER_IMPORT_CEILING, (
        f"dispatch.py's module-scope import count ({count}) exceeds its "
        f"per-commit pin ({_EAGER_IMPORT_CEILING}). Defer the new import "
        "into the function that references it, per P070-C3's lever (a) -- "
        "do not raise this pin."
    )


def _assert_dispatch_checks_line_ratchet() -> None:
    count = _count_lines(_DISPATCH_CHECKS_PY_PATH.read_text(encoding="utf-8"))
    assert count <= _DISPATCH_CHECKS_LINE_CEILING, (
        f"dispatch_checks.py's line count ({count}) exceeds its per-commit "
        f"pin ({_DISPATCH_CHECKS_LINE_CEILING}). Move the new guard to its "
        "own module, or raise this pin in a commit citing the EM/PM ruling "
        "that sanctioned it."
    )


def _assert_registered_entry_ratchet() -> None:
    count = len(_guard_roster_provider())
    assert count <= _REGISTERED_ENTRY_CEILING, (
        f"dispatch.py's registered guard-entry count ({count}) exceeds its "
        f"per-commit pin ({_REGISTERED_ENTRY_CEILING}). This fast-tier pin "
        "is the per-commit observer; test_hot_path_hook_import_budget.py's "
        "bash_guards_dispatch row is the authoritative cold-closure bar -- "
        "a legitimate growth updates both together."
    )


def test_eager_import_ratchet_green_at_head() -> None:
    """AC7 leg 1, green: dispatch.py's real, tracked module-scope import
    count sits at or under the pin."""
    _assert_eager_import_ratchet()


def test_eager_import_ratchet_reds_on_synthetic_growth(tmp_path, monkeypatch) -> None:
    """AC7 leg 1, red: a synthetic copy of dispatch.py with one extra
    module-scope import trips the pin. Never touches the tracked file."""
    synthetic = tmp_path / "dispatch.py"
    original = _DISPATCH_PY_PATH.read_text(encoding="utf-8")
    synthetic.write_text(
        original
        + "\nimport coordinator_core.bash_guards._advisory_value as "
        "_synthetic_growth_marker\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "_DISPATCH_PY_PATH", synthetic)
    with pytest.raises(AssertionError):
        _assert_eager_import_ratchet()


def test_dispatch_checks_line_ratchet_green_at_head() -> None:
    """AC7 leg 2, green."""
    _assert_dispatch_checks_line_ratchet()


def test_dispatch_checks_line_ratchet_reds_on_synthetic_growth(
    tmp_path, monkeypatch
) -> None:
    """AC7 leg 2, red: a synthetic copy of dispatch_checks.py, ten lines
    over the pin. Never touches the tracked file."""
    synthetic = tmp_path / "dispatch_checks.py"
    original = _DISPATCH_CHECKS_PY_PATH.read_text(encoding="utf-8")
    extra_lines = "\n".join(f"# synthetic-growth-{i}" for i in range(10)) + "\n"
    synthetic.write_text(original + extra_lines, encoding="utf-8")
    monkeypatch.setattr(
        sys.modules[__name__], "_DISPATCH_CHECKS_PY_PATH", synthetic
    )
    with pytest.raises(AssertionError):
        _assert_dispatch_checks_line_ratchet()


def test_registered_entry_ratchet_green_at_head() -> None:
    """AC7 leg 3, green."""
    _assert_registered_entry_ratchet()


def test_registered_entry_ratchet_reds_on_synthetic_growth(monkeypatch) -> None:
    """AC7 leg 3, red: a synthetic roster one entry past the pin. Never
    invokes the live `guard_roster()` or touches `dispatch.py`."""
    synthetic_roster = tuple(range(_REGISTERED_ENTRY_CEILING + 1))
    monkeypatch.setattr(
        sys.modules[__name__], "_guard_roster_provider", lambda: synthetic_roster
    )
    with pytest.raises(AssertionError):
        _assert_registered_entry_ratchet()

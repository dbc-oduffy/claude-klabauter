"""The kill-ledger guards resolve their subject through `machinery_paths`, not
a hand-rolled path.

`kill_ledger_path()` is the single owner of where the ledger lives on disk —
`.coordinator-local/kill-ledger.md`, moved out of `state/` by PM Adjudication 4
(C7). A guard that instead rebuilds `state/kill-ledger.md` from its own
`__file__` is checking a path nothing has written since the move: its
module-level `if not LEDGER.is_file(): pytest.skip(...)` fires on every
checkout, corpus present or not, so the guard is permanently dark rather than
conditionally skipped.

This test proves the guards read through the canonical resolver by running
each guard module's real source under a spoofed `__file__` that points at a
temp repo carrying only the relocated path — a `state/kill-ledger.md`-rooted
guard has nothing there and stays skipped; a `kill_ledger_path()`-rooted one
finds it and proceeds to collection.

Negative-spec: does not exercise `fate_entries()` or the `Returns-when`
parser's correctness — both are covered elsewhere. This is a path-resolution
check only.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_GUARD_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _GUARD_DIR.parents[1]


def _run_guard_module_under_fake_repo(real_source_path: Path, tmp_path: Path) -> None:
    """Exec `real_source_path`'s current text with `__file__` rebased under
    `tmp_path`, so `Path(__file__).resolve().parents[2]` resolves to
    `tmp_path` the same way it resolves to the real repo root in-place.

    Raises whatever the module itself raises, including a `pytest.skip` --
    the caller decides what that means.
    """
    fake_file = tmp_path / "coordinator_core" / "tests" / real_source_path.name
    source = real_source_path.read_text(encoding="utf-8")
    code = compile(source, str(fake_file), "exec")
    module_name = f"_fake_{real_source_path.stem}"
    module = types.ModuleType(module_name)
    module.__file__ = str(fake_file)
    sys.modules[module_name] = module
    try:
        exec(code, module.__dict__)
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.parametrize(
    "guard_filename",
    ["test_kill_ledger_fate_is_current.py", "test_gravestoned_ops_never_return.py"],
)
def test_guard_finds_ledger_at_relocated_path(tmp_path, guard_filename):
    machinery = tmp_path / ".coordinator-local"
    machinery.mkdir()
    (machinery / "kill-ledger.md").write_text("", encoding="utf-8")

    real_source_path = _GUARD_DIR / guard_filename
    try:
        _run_guard_module_under_fake_repo(real_source_path, tmp_path)
    except pytest.skip.Exception as exc:  # pragma: no cover - failure path
        pytest.fail(
            f"{guard_filename} skipped against a repo carrying "
            f".coordinator-local/kill-ledger.md: {exc}. It is still resolving "
            "the retired state/kill-ledger.md path instead of "
            "machinery_paths.kill_ledger_path()."
        )


def test_guards_resolve_through_the_canonical_helper():
    from coordinator_core.session.machinery_paths import kill_ledger_path

    import coordinator_core.tests.test_kill_ledger_fate_is_current as fate_guard
    import coordinator_core.tests.test_gravestoned_ops_never_return as revival_guard

    expected = Path(kill_ledger_path(str(_REPO_ROOT)))
    assert fate_guard.LEDGER == expected
    assert revival_guard._ledger_path() == expected

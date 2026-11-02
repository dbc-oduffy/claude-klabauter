"""Only rea_ledger_write.py and coordinator_core/rea_ledger/ may name the rea-ledger root."""

from __future__ import annotations

from pathlib import Path

_PKG = Path(__file__).resolve().parents[1]
_ROOT = _PKG.parent
_NEEDLE = "rea-ledger"

_ALLOWED = {
    "coordinator_core/ops/rea_ledger_write.py",
    "coordinator_core/ops/tests/test_rea_ledger_write.py",
    "coordinator_core/tests/test_rea_ledger_sole_writer.py",
    "coordinator_core/ipc.py",
    "coordinator_core/tests/test_raw_writes_have_a_disposition.py",
}
_ALLOWED_PREFIXES = ("coordinator_core/rea_ledger/",)
_SKIP_DIRS = {"__pycache__"}


def _naming_modules() -> set:
    hits = set()
    for path in _PKG.rglob("*.py"):
        if _SKIP_DIRS & set(path.parts):
            continue
        if _NEEDLE in path.read_text(encoding="utf-8", errors="replace"):
            hits.add(path.relative_to(_ROOT).as_posix())
    return hits


def _strays(hits: set) -> set:
    return {rel for rel in hits if rel not in _ALLOWED and not rel.startswith(_ALLOWED_PREFIXES)}


def test_no_module_outside_the_writer_names_the_ledger_root():
    stray = _strays(_naming_modules())
    assert not stray, f"modules naming {_NEEDLE!r} outside the sole writer: {sorted(stray)}"


def test_the_guard_refuses_a_second_writer_and_the_scan_sees_the_real_writer():
    assert _strays({"coordinator_core/ops/other_writer.py"}) == {"coordinator_core/ops/other_writer.py"}
    assert _strays({"coordinator_core/rea_ledger/canonical.py", "coordinator_core/ops/rea_ledger_write.py"}) == set()
    assert "coordinator_core/rea_ledger/canonical.py" in _naming_modules()

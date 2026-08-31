"""Characterization tests for coordinator_core.ops.verify_no_powershell_flash.

The bash oracle was a thin `exec` shim over the canonical
`verify-no-console-flash.sh` guard. That guard's own port (C6, the POSIX-exec
drain) renamed the canonical guard to `verify-no-console-flash.py` and this
shim's own `_CANONICAL_GUARD_NAME` was corrected to match
(cross-repo/archive/2026-08-28-doe-claude-em-verify-no-powershell-flash-
trampolines-to-a-file-that-no-longer-exists.md) — the module's own docstring
already named the `.py`, only the constant had not caught up. These tests
exercise the shim's delegation contract in isolation, using a fake
canonical-guard fixture (now a `.py`) so they don't depend on the real
guard's detection rules (which are a separate port item).

Port of: verify-no-powershell-flash.sh (DoE b5a4192c, 2026-07-20)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.verify_no_powershell_flash import main


def _write_fake_guard(bin_dir: Path, body: str) -> Path:
    guard = bin_dir / "verify-no-console-flash.py"
    guard.write_text(f"import sys\n{body}\n", encoding="utf-8")
    return guard


def test_delegates_clean_exit_zero(tmp_path: Path) -> None:
    _write_fake_guard(tmp_path, "sys.exit(0)")
    rc = main([str(tmp_path)])
    assert rc == 0


def test_delegates_violation_exit_one(tmp_path: Path) -> None:
    _write_fake_guard(tmp_path, "sys.exit(1)")
    rc = main([str(tmp_path)])
    assert rc == 1


def test_forwards_argv_to_canonical_guard(tmp_path: Path, capfd: pytest.CaptureFixture) -> None:
    _write_fake_guard(tmp_path, 'print("got: " + sys.argv[1])')
    rc = main([str(tmp_path), "/some/root"])
    assert rc == 0
    out = capfd.readouterr().out
    assert "got: /some/root" in out


def test_missing_bin_dir_arg_returns_two() -> None:
    rc = main([])
    assert rc == 2


def test_missing_canonical_guard_returns_two(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    rc = main([str(empty_dir)])
    assert rc == 2


def test_canonical_guard_never_returns_shim_internal_code(tmp_path: Path) -> None:
    # The canonical guard's own exit-code contract is 0/1 only (never 2) — a
    # shim-level 2 is therefore unambiguous evidence of a shim failure, not a
    # guard verdict. Exercise a guard that (mis)behaves and returns 2 anyway;
    # the shim must still pass it through verbatim (no swallowing/mangling).
    _write_fake_guard(tmp_path, "sys.exit(2)")
    rc = main([str(tmp_path)])
    assert rc == 2

"""Characterization tests for coordinator_core.ops.verify_no_powershell_flash.

The bash oracle was a thin `exec` shim over the canonical
`verify-no-console-flash.sh` guard, itself covered by its own pre-existing
bats test (`test-verify-no-console-flash.sh` Test 4). These tests exercise
the shim's delegation contract in isolation, using a fake canonical-guard
fixture so they don't depend on the real guard's detection rules (which are
a separate port item).

Port of: verify-no-powershell-flash.sh (coordinator-claude b5a4192c, 2026-07-20)
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from coordinator_core.ops.verify_no_powershell_flash import main


def _write_fake_guard(bin_dir: Path, body: str) -> Path:
    guard = bin_dir / "verify-no-console-flash.sh"
    guard.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    guard.chmod(guard.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return guard


def test_delegates_clean_exit_zero(tmp_path: Path) -> None:
    _write_fake_guard(tmp_path, "exit 0")
    rc = main([str(tmp_path)])
    assert rc == 0


def test_delegates_violation_exit_one(tmp_path: Path) -> None:
    _write_fake_guard(tmp_path, "exit 1")
    rc = main([str(tmp_path)])
    assert rc == 1


def test_forwards_argv_to_canonical_guard(tmp_path: Path, capfd: pytest.CaptureFixture) -> None:
    _write_fake_guard(tmp_path, 'echo "got: $1"')
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
    _write_fake_guard(tmp_path, "exit 2")
    rc = main([str(tmp_path)])
    assert rc == 2

"""Pins spawn_policy.windowless: Popen without creationflags gets CREATE_NO_WINDOW; explicit flags win."""

import sys

import pytest

from coordinator_core.spawn_policy import windowless

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="console windows are a Windows concern")


def test_both_console_less_roots_install_it():
    from pathlib import Path

    root = Path(windowless.__file__).resolve().parents[1]
    for rel in ("warm/server.py", "ops/workflow_fire/headless.py"):
        assert "windowless.install()" in (root / rel).read_text(encoding="utf-8"), rel

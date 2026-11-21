"""Engine and hook entry cap BLAS pools in-process via setdefault, never box-wide."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core.testing import spawn_depth

ROOT = Path(__file__).resolve().parents[2]
ENTRY_FILES = ("coordinator_core/ipc.py", "coordinator_core/hooks/_envelope.py")
ENTRY_MODULES = ("coordinator_core.ipc", "coordinator_core.hooks._envelope")


@pytest.mark.parametrize("rel", ENTRY_FILES)
def test_entry_keys_mirror_spawn_depth_and_only_setdefault(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    for key in spawn_depth.BLAS_THREAD_KEYS:
        assert '"%s"' % key in text
    assert not re.search(r"os\.environ\[[^\]]*\]\s*=", text)
    assert not re.search(r"\b(setx|putenv|HKCU|HKLM)\b", text)

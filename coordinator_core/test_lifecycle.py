
from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path

import pytest

from coordinator_core.lifecycle import _compute_core_version


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_subtree_warns_and_is_excluded_from_hash(tmp_path, monkeypatch, caplog):
    pkg_dir = tmp_path / "coordinator_core"
    pkg_dir.mkdir()
    (pkg_dir / "visible.py").write_text("x = 1\n", encoding="utf-8")

    blocked_dir = pkg_dir / "blocked_subpkg"
    blocked_dir.mkdir()
    (blocked_dir / "hidden.py").write_text("y = 2\n", encoding="utf-8")

    monkeypatch.setattr(
        "coordinator_core.lifecycle.Path",
        Path,
    )
    monkeypatch.setattr(
        "coordinator_core.lifecycle.__file__",
        str(pkg_dir / "lifecycle.py"),
    )

    original_mode = blocked_dir.stat().st_mode
    os.chmod(blocked_dir, 0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="coordinator_core.lifecycle"):
            digest = _compute_core_version()
    finally:
        os.chmod(blocked_dir, original_mode)

    expected = hashlib.sha256((pkg_dir / "visible.py").read_bytes()).hexdigest()
    assert digest == expected, (
        "unreadable subtree must be excluded from the hash exactly like an absent "
        "subtree would be — divergence here means the walk didn't skip it cleanly"
    )

    dir_warnings = [
        r
        for r in caplog.records
        if str(blocked_dir) in r.message and r.levelno == logging.WARNING
    ]
    assert dir_warnings, (
        "expected a logged WARNING naming the unreadable subtree; "
        f"none found in: {[r.message for r in caplog.records]}"
    )

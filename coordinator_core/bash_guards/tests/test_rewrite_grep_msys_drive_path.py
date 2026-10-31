"""A grep rewritten to the Windows-Python walker finds matches under a Git Bash `/c/...` drive path.

Live 2026-10-30: `grep -rn "def stub_root" /e/.../door-go/` came back empty on Windows -- the
walker called `os.walk("/e/...")`, which names nothing there, so every MSYS-path grep on the box
answered with silence instead of its matches.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from coordinator_core.bash_guards import _rewrite_support as rs

pytestmark = pytest.mark.skipif(os.name != "nt", reason="MSYS drive paths exist only on Windows")


def _msys(path) -> str:
    drive, rest = os.path.splitdrive(str(path))
    return "/%s%s" % (drive[0].lower(), rest.replace("\\", "/"))


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_msys_drive_target_is_translated_and_the_walker_prints_the_match(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\ndef stub_root():\n    pass\n", encoding="utf-8")
    parsed = rs._bt_grep_flags_and_operands(["grep", "-rn", "def stub_root", _msys(tmp_path) + "/"])

    assert parsed is not None and parsed["targets"][0][1] == ":"
    body = rs._bt_grep_python_rewrite(parsed).split(" -c ", 1)[1].strip()[1:-1]
    out = subprocess.run(
        [sys.executable, "-c", body], capture_output=True, text=True, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert "def stub_root" in out.stdout, (out.stdout, out.stderr)


def test_a_posix_path_that_is_not_a_drive_prefix_is_left_alone():
    parsed = rs._bt_grep_flags_and_operands(["grep", "-rn", "x", "/etc/hosts"])

    assert parsed is not None and parsed["targets"] == ["/etc/hosts"]

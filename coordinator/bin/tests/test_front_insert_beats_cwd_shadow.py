"""The dispatch-engine seam must import the stamped engine, never the cwd's.

Under `python -c` sys.path[0] is '' (the cwd); a root already on sys.path at
the tail (PYTHONPATH / editable .pth) used to skip the insert, so a
`coordinator_core` in the cwd shadowed the real engine and the write-claim
op was never found.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_LIB = _REPO / "coordinator" / "bin" / "lib"

_PROBE = (
    "import sys; sys.path.insert(0, sys.argv[1]);"
    "import cc_invoke; root = cc_invoke.require_dispatch_engine_on_path();"
    "import coordinator_core.hooks.postuse_advisory_dispatch as pad;"
    "print(root); print(pad.__file__); print(hasattr(pad, '_record_write_touch'))"
)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_cwd_decoy_engine_cannot_shadow_the_stamped_engine(tmp_path):
    decoy = tmp_path / "cwd" / "coordinator_core"
    (decoy / "hooks").mkdir(parents=True)
    (decoy / "__init__.py").write_text("DECOY = True\n", encoding="utf-8")
    (decoy / "hooks" / "__init__.py").write_text("", encoding="utf-8")
    (decoy / "hooks" / "postuse_advisory_dispatch.py").write_text("DECOY = True\n", encoding="utf-8")

    env = {
        **os.environ,
        "COORDINATOR_ENGINE_ROOT": str(_REPO),
        "PYTHONPATH": str(_REPO),
    }
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(_LIB)],
        cwd=str(tmp_path / "cwd"),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    root, imported, has_claim_writer = proc.stdout.split("\n")[:3]
    assert Path(root).resolve() == _REPO.resolve()
    assert Path(imported).resolve().is_relative_to(_REPO.resolve())
    assert has_claim_writer == "True"


def test_front_insert_moves_an_already_present_root_ahead_of_the_cwd_entry(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(_LIB))
    import cc_invoke

    root = str(tmp_path / "engine")
    monkeypatch.setattr(sys, "path", ["", "x", root, "y"])
    assert cc_invoke._front_insert_on_path(root) == root
    assert sys.path == [root, "", "x", "y"]

"""`coordinator-doc-new --type sizing-object` names every missing required flag in one refusal."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "coordinator-doc-new.py"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_one_refusal_lists_title_premise_and_evidence(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(_BIN), "--type", "sizing-object", "--out", str(tmp_path / "x.yaml")],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 1
    for flag in ("--title", "--premise ", "--premise-evidence"):
        assert flag in proc.stderr
    assert not (tmp_path / "x.yaml").exists()

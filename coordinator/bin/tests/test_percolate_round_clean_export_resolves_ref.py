"""The dirty-publisher retry hands baseline-export a hex commit id, never a symbolic ref.

baseline-export refuses anything but 4-40 hex characters, so a retry built from the default
`HEAD` could never run.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BIN_DIR.parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("percolate_round_clean_export", _BIN_DIR / "percolate-round.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sha_arg(cmd):
    return cmd[cmd.index("--sha") + 1]


@pytest.mark.parametrize("source_ref", [None, "HEAD"])
def test_a_symbolic_ref_reaches_baseline_export_as_the_full_commit_id(source_ref):
    rnd = _load()
    head = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    cmd = rnd._clean_export_publish_cmd([sys.executable, "publish.py", "klabauter"], source_ref)
    assert cmd is not None
    assert _sha_arg(cmd) == head
    assert cmd[-1] == "klabauter"


def test_a_ref_naming_no_commit_yields_no_retry():
    rnd = _load()
    assert rnd._clean_export_publish_cmd([sys.executable, "publish.py", "klabauter"], "no-such-ref-xyz") is None

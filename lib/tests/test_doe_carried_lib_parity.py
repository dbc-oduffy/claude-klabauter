"""Byte parity of the DoE-only lib modules claude-klabauter carries in coordinator/lib with coordinator-content-repo's copies.

The published payload imports these as `lib/<name>`; the engine ports under coordinator_core serve
the engine and are a separate contract.
"""
import os
from pathlib import Path

import pytest

CARRIED = (
    "output_tree_layout.py",
    "research_layout.py",
    "plugin_review_signals.py",
    "ue_review_signal.py",
)
CLAUDE_KLABAUTER_LIB = Path(__file__).resolve().parent.parent


def _doe_lib() -> Path:
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not root:
        pytest.skip("CLAUDE_PLUGIN_ROOT unset: DoE coordinator root not resolvable")
    lib = Path(root) / "lib"
    if not lib.is_dir():
        pytest.skip(f"DoE lib dir absent under CLAUDE_PLUGIN_ROOT: {lib}")
    return lib


@pytest.mark.parametrize("name", CARRIED)
def test_carried_module_exists_in_claude_klabauter(name):
    assert (CLAUDE_KLABAUTER_LIB / name).is_file()


@pytest.mark.parametrize("name", CARRIED)
def test_carried_module_bytes_equal_doe(name):
    doe = _doe_lib() / name
    if not doe.is_file():
        pytest.skip(f"DoE {name} absent")
    assert (CLAUDE_KLABAUTER_LIB / name).read_bytes() == doe.read_bytes()

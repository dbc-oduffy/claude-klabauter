"""Ratchet: every `<module>.<attr>` read of an imported coordinator_core module
resolves in the committed tree at HEAD, never the live worktree, so a caller
shipped without its callee's defining hunk fails here."""

import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_LIB = _REPO / "coordinator" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from percolate.import_closure import find_commit_module_attr_violations  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]


def test_every_module_attribute_read_resolves_in_the_committed_tree():
    head = subprocess.run(
        ["git", "-C", str(_REPO), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    examined, violations = find_commit_module_attr_violations(_REPO, head)
    assert examined > 0
    assert violations == [], "module attribute absent from the committed module: " + "; ".join(
        f"{path}: {dotted}" for path, dotted in violations
    )

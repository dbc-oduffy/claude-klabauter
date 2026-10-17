"""Pin memo.list_outbox's import closure: it answers "which outbox drafts exist" and nothing heavier."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.spawns_process

_REPO = Path(__file__).resolve().parents[4]

_PROBE = """
import json, sys
import coordinator_core.ipc, coordinator_core.invoke.__main__
before = set(sys.modules)
import coordinator_core.ops.fleet.memo_list_outbox as m
added = sorted(set(sys.modules) - before)
print(json.dumps({"added": added, "has_op": hasattr(m, "_memo_list_outbox")}))
"""

_REMOVED = (
    "coordinator_core.ops.fleet._common",
    "coordinator_core.ops.fleet.memo_draft",
    "coordinator_core.ops.fleet._memo_compose",
    "coordinator_core.ops.fleet._memo_resolver",
    "coordinator_core.ops.ceremony.git_native",
    "coordinator_core.dag",
    "coordinator_core.session.core",
    "coordinator_core.git.commit_trailers",
    "coordinator_core.machine_profile",
    "yaml",
    "subprocess",
    "tempfile",
)


@pytest.fixture(scope="module")
def added_modules() -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(_REPO), capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["has_op"]
    return payload["added"]


@pytest.mark.parametrize("module", _REMOVED)
def test_importing_the_op_does_not_load(added_modules, module):
    assert module not in added_modules


def test_reexports_keep_their_old_homes():
    from coordinator_core.ops.fleet import _common, _outbox_enum, memo_draft

    for name in ("build_dry_run_result", "build_setup_error_result"):
        assert getattr(_common, name) is getattr(_outbox_enum, name)
    for name in ("outbox_dir", "legacy_outbox_dir", "merged_outbox_drafts"):
        assert getattr(memo_draft, name) is getattr(_outbox_enum, name)

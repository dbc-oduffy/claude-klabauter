"""Pin memo.list_outbox's child-side cost: frontmatter parsing loads only when a draft exists."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.spawns_process

_REPO = Path(__file__).resolve().parents[3]

_PROBE = """
import json, sys
import coordinator_core.ipc, coordinator_core.invoke.__main__
from pathlib import Path
import coordinator_core.ops.fleet.memo_list_outbox as m
root = Path(sys.argv[1])
loaded_at_import = "coordinator_core.frontmatter.primitives" in sys.modules
result = m._memo_list_outbox({"dry_run": True}, root)
print(json.dumps({
    "after_import": loaded_at_import,
    "after_empty_call": "coordinator_core.frontmatter.primitives" in sys.modules,
    "hashlib_after_empty_call": "hashlib" in sys.modules,
    "candidates": len(result["candidates"]),
}))
"""


def _run(root: Path) -> dict:
    (root / ".git").mkdir(exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(root)],
        cwd=str(_REPO), capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_empty_outbox_never_loads_frontmatter_primitives(tmp_path):
    payload = _run(tmp_path)
    assert payload["candidates"] == 0
    assert payload["after_import"] is False
    assert payload["after_empty_call"] is False
    assert payload["hashlib_after_empty_call"] is False


def test_a_draft_still_loads_primitives_and_lists(tmp_path):
    outbox = tmp_path / ".coordinator-local" / "memo-outbox"
    outbox.mkdir(parents=True)
    (outbox / "topic.md").write_text("---\ntitle: T\nto: x\n---\nbody\n", encoding="utf-8")
    payload = _run(tmp_path)
    assert payload["candidates"] == 1
    assert payload["after_empty_call"] is True

"""A row reply with no contract status line gets one bounded re-ask for just that line (DoE/example-game-repo f49580:
"C2 -- nothing written, no files changed." held C6 unstarted). Still missing: the halt quotes the reply."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.win_portability import no_console_creationflags

from .test_halt_on_unstarted_dependents import _HARNESS

_NO_STATUS = "C2 \u2014 nothing written, no files changed."


def _go(reask: str, rows: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = _HARNESS.replace(
        "const agent = () => { throw new Error('no agent'); };",
        "let _asks = 0; const agent = async (p) => { _asks++; return %s; };" % json.dumps(reask),
    ).replace("notStarted: _notStarted }", "notStarted: _notStarted, asks: _asks, held: Object.keys(_heldBy) }")
    proc = subprocess.run(
        [node, "-"], input=harness % {"helper": emit._run_row_helper_js(), "rows": rows},
        capture_output=True, text=True, encoding="utf-8", timeout=60, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


_ROWS = (
    f"_rows.C2 = _runRow('C2', [], null, async () => {json.dumps(_NO_STATUS)});"
    "_rows.C6 = _runRow('C6', [_rows.C2], null, run('C6'));"
)


def test_reask_status_line_releases_the_dependent():
    out = _go("DONE: no change needed", _ROWS)
    assert out["asks"] == 1
    assert out["reason"] is None and out["notStarted"] == []


def test_still_no_status_line_halts_quoting_the_reply():
    out = _go("sorry, nothing more", _ROWS)
    assert out["asks"] == 1
    assert out["reason"].startswith("dependents unstarted: C6 (held by C2)")
    assert _NO_STATUS in out["reason"]

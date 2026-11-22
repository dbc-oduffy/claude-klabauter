"""A row reply with no contract status line gets one bounded re-ask for just that line (DoE/example-game-repo f49580:
"C2 -- nothing written, no files changed." held C6 unstarted). The re-ask agent did no work, so its answer
never grades the row as BLOCKED: the prior turn's report stays the graded reply (ec5ddc: finished C1 held
dependents on 'BLOCKED: no C1 work was done in this turn')."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.win_portability import no_console_creationflags

from .test_halt_on_unstarted_dependents import _HARNESS

_NO_STATUS = "C2 — nothing written, no files changed."
_FINISHED = "disposition: proceed\n72 tests passing, GPU rerun done."


def _go(reask: str, rows: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = _HARNESS.replace(
        "const agent = () => { throw new Error('no agent'); };",
        "let _asks = 0, _prompt = ''; const agent = async (p) => { _asks++; _prompt = p; return %s; };" % json.dumps(reask),
    ).replace(
        "notStarted: _notStarted }",
        "notStarted: _notStarted, asks: _asks, prompt: _prompt, blocked: _blockedChunks, "
        "incomplete: _incompleteChunks, held: Object.keys(_heldBy) }",
    )
    proc = subprocess.run(
        [node, "-"], input=harness % {"helper": emit._run_row_helper_js(), "rows": rows},
        capture_output=True, text=True, encoding="utf-8", timeout=60, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _rows(reply: str) -> str:
    return (
        f"_rows.C2 = _runRow('C2', [], null, async () => {json.dumps(reply)});"
        "_rows.C6 = _runRow('C6', [_rows.C2], null, run('C6'));"
    )


def test_reask_status_line_releases_the_dependent():
    out = _go("DONE: no change needed", _rows(_NO_STATUS))
    assert out["asks"] == 1
    assert out["reason"] is None and out["notStarted"] == []


def test_still_no_status_line_halts_quoting_the_reply():
    out = _go("sorry, nothing more", _rows(_NO_STATUS))
    assert out["asks"] == 1
    assert out["reason"].startswith("dependents unstarted: C6 (held by C2)")
    assert _NO_STATUS in out["reason"]


@pytest.mark.parametrize("reask", [
    "",
    "BLOCKED: no C2 work was done in this turn. The only task was to supply the contract status line",
])
def test_empty_or_no_work_reask_is_never_graded_blocked(reask):
    out = _go(reask, _rows(_FINISHED))
    assert out["asks"] == 1
    assert "C2" not in out["blocked"]
    assert "C2" not in out["incomplete"]
    assert out["held"] == [] and out["notStarted"] == []
    assert out["reason"] is None
    assert "C6" not in out["incomplete"]


def test_empty_prior_reply_and_empty_reask_stays_incomplete():
    out = _go("", _rows(""))
    assert "C2" in out["incomplete"]
    assert "C2" not in out["blocked"]


def test_reask_prior_reply_is_capped():
    long_reply = "disposition: proceed\n" + "x" * (emit.STATUS_REASK_PRIOR_CAP + 500) + "TAILMARK"
    out = _go("DONE: finished", _rows(long_reply))
    assert "disposition: proceed" in out["prompt"]
    assert "TAILMARK" not in out["prompt"]
    assert "x" * (emit.STATUS_REASK_PRIOR_CAP - 50) in out["prompt"]
    assert "x" * (emit.STATUS_REASK_PRIOR_CAP + 1) not in out["prompt"]


def test_reask_prompt_carries_the_prior_report_and_says_it_counts():
    out = _go("DONE: finished", _rows(_FINISHED))
    assert "72 tests passing" in out["prompt"]
    assert "finished work and it stands" in out["prompt"]
    assert out["reason"] is None

"""A memo-close-only manifest joins the verify-and-commit tail instead of the zero-row refusal halt."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.memo_rows import MEMO_CLOSE_KEY
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_MEMO_PREFIX = "  if (!_manifest.error && !(_manifest.rows ?? []).length && !(_manifest.owed ?? []).length"
_HALT_PREFIX = "  else if (!_parked && (_manifest.error || (!(_manifest.rows ?? []).length && !_allLanded && !_resumeLedger))) {"
_CLAUSE = "MEMO-CLOSE: no executor ran"

_HARNESS = """
let _halted = null, _allLanded = false, _memoCloseOnly = false, _xsTail = false, _resumeLedger = false, _parked = null, _planRel = 'p.md', _sizingRel = 's.yaml', _runId = 'r';
let _gate = {arm: 's'}, _manifest = %s;
%s
console.log(JSON.stringify({ halted: _halted, memoCloseOnly: _memoCloseOnly, xsTail: _xsTail }));
"""

_MEMO_ROW = {"memo": "state/cross-repo/inbox/m.md", "id": "X1"}


def _drive(script: str, manifest: dict) -> dict:
    lines = [ln for ln in script.split("\n") if ln.startswith((_MEMO_PREFIX, _HALT_PREFIX))]
    body = "\n".join(ln.replace("  else if (", "  if (", 1) for ln in lines)
    out = subprocess.run(
        ["node", "-"], input=_HARNESS % (json.dumps(manifest), body),
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout
    return json.loads(out)


def test_the_memo_close_branch_precedes_the_refusal_halt_and_the_script_checks_clean():
    script = _compose()
    assert script.index(_MEMO_PREFIX) < script.index(_HALT_PREFIX) < script.index("label: 'verify'")
    assert [f for f in run_checks(script) if f.severity is Severity.ERROR] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_a_memo_close_only_manifest_takes_the_xs_tail_without_halting():
    got = _drive(_compose(), {"rows": [], "owed": [], "gated": [], MEMO_CLOSE_KEY: [_MEMO_ROW]})
    assert got == {"halted": None, "memoCloseOnly": True, "xsTail": True}


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.parametrize(
    "manifest",
    [
        {"rows": [], "owed": [], "gated": [], MEMO_CLOSE_KEY: []},
        {"rows": [], "owed": [], "gated": []},
        {"rows": [], "owed": [{"id": "M1"}], "gated": [], MEMO_CLOSE_KEY: [_MEMO_ROW]},
        {"rows": [], "owed": [], "gated": [{"id": "G"}], MEMO_CLOSE_KEY: [_MEMO_ROW]},
        {"error": "stage failed", "rows": [], MEMO_CLOSE_KEY: [_MEMO_ROW]},
    ],
    ids=["empty-memo-close", "no-key", "owed", "gated", "error"],
)
def test_every_other_zero_row_manifest_still_halts_refusal(manifest):
    got = _drive(_compose(), manifest)
    assert got["halted"]["halted"] == "refusal" and got["memoCloseOnly"] is False


def test_the_verify_prompt_clause_is_gated_on_memo_close_only():
    script = _compose()
    assert script.count(_CLAUSE) == 1
    at = script.index(_CLAUSE)
    assert "(_memoCloseOnly ? '" in script[at - 40 : at]


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.parametrize(
    "item, blocking",
    [
        ({"id": "X1", "status": "not_met", "remaining": ["sha_reply"]}, False),
        ({"id": "X1", "status": "not_met", "remaining": ["sha_reply", "commit"]}, False),
        ({"id": "X1", "status": "not_met", "remaining": ["write the fix"]}, True),
        ({"id": "X1", "status": "not_met"}, True),
    ],
    ids=["sha_reply", "sha_reply+commit", "real-work", "no-remaining"],
)
def test_a_not_met_row_remaining_only_post_commit_acts_does_not_block_the_commit(item, blocking):
    script = _compose()
    start = script.index("    const _postCommitActs = ")
    end = script.index("\n    });", script.index("const _ledgerBlocking", start)) + len("\n    });")
    program = (
        "const _manifest = { gated: [] };\n" + script[start:end]
        + f"\nconsole.log(JSON.stringify(_ledgerBlocking({{ items: [{json.dumps(item)}] }}).length));"
    )
    out = subprocess.run(
        ["node", "-"], input=program, capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout
    assert (json.loads(out) == 1) is blocking

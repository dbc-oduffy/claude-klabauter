"""A multi-ask child whose first leg ran under another id chains into its own minted run id.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_chain_emit_accepts_child_minted_run_id.py -q
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import OUTPUT_PATH_REDIRECT_KEY
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY
from coordinator_core.ops.dispatch_emit.tests.test_fan_launcher import _write

pytestmark = pytest.mark.spawns_process

_MINTED = "ask-20261115T233333-1e5331"
_FIRST = "warp-sizing-20261115T233400-abcdef"


def _hop(tmp_path, first_run: str, chain_run: str):
    """Run `_runChild` in node: the first leg starts `first_run` and hands off; the chain emit answers `chain_run`."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    _, script = _write(tmp_path, ids=(_MINTED,))
    redirect = ""
    if first_run != _MINTED:
        marker = {"halted_run": _MINTED, "to": f"scratch/warp/{first_run}.workflow.mjs"}
        redirect = f", {OUTPUT_PATH_REDIRECT_KEY}: {json.dumps(marker)}"
    body = script.split("\n", 1)[1].split("const _settled", 1)[0].replace("export const meta", "const meta")
    root = re.search(r"const REPO_ROOT = (\".*?\");", body).group(1)
    code = (
        "const calls = [];\n"
        f"const FIRST = {{ path: {root} + '/scratch/warp/{first_run}.workflow.mjs', run_id: '{first_run}', fire_args: {{}}"
        f"{redirect} }};\n"
        f"const CHAINED = {{ path: {root} + '/scratch/warp/{chain_run}.workflow.mjs', run_id: '{chain_run}', fire_args: {{}} }};\n"
        "const agent = async () => CHAINED;\n"
        f"const workflow = async (o) => {{ calls.push(o.scriptPath); return calls.length === 1 ? {{ {CHAIN_HANDOFF_KEY}: "
        f"{json.dumps({'sizing_path': 'state/sizings/x.yaml', 'ask': True})} }} : {{ done: true }}; }};\n"
        "const parallel = async () => [];\n"
        + body
        + "\n_runChild(CHILDREN[0], FIRST).then((r) => console.log(JSON.stringify([r, calls])));\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_chain_emit_naming_the_childs_unstarted_minted_id_starts_that_run(tmp_path):
    result, calls = _hop(tmp_path, _FIRST, _MINTED)
    assert result["status"] == "ok" and result["result"]["chained_from"] == _MINTED
    assert len(calls) == 2 and calls[1].endswith(f"/{_MINTED}.workflow.mjs")


def test_a_chain_emit_naming_the_run_the_first_leg_started_is_still_refused(tmp_path):
    result, calls = _hop(tmp_path, _FIRST, _FIRST)
    assert result["status"] == "failed" and "is not a fresh run id" in result["error"]
    assert len(calls) == 1


def test_a_chain_emit_naming_the_minted_id_the_first_leg_started_is_still_refused(tmp_path):
    result, calls = _hop(tmp_path, _MINTED, _MINTED)
    assert result["status"] == "failed" and "is not a fresh run id" in result["error"]
    assert len(calls) == 1

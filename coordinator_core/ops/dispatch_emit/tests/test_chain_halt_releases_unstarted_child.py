"""A halted parent releases its never-started chained child: the ask chain block and the launcher hop send abandon_run with chained_from."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, halted_run
from coordinator_core.ops.dispatch_emit.tests.test_abandon_stalled_run import (
    PARENT, RUN, SIZING, _abandon, _chained, _closed,
)
from coordinator_core.ops.dispatch_emit.tests.test_fan_launcher import _write


def _hop(tmp_path, workflow_js: str):
    """Run the launcher's _chainHop in node with a stubbed workflow(); returns [result, abandon prompts]."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    _, script = _write(tmp_path, ids=("ask-r1",))
    body = script.split("\n", 1)[1].split("const _settled", 1)[0].replace("export const meta", "const meta")
    import re
    root = re.search(r"const REPO_ROOT = (\".*?\");", body).group(1)
    code = (
        "const prompts = [];\n"
        "const agent = async (p, o) => { prompts.push(o.label + ' ' + p); return { replies: [] }; };\n"
        f"let n = 0; const workflow = async (o) => {{ n++; return {workflow_js}; }};\n"
        "const parallel = async () => [];\n" + body
        + f"\nconst H = {{ path: {root} + '/scratch/warp/warp-new.workflow.mjs', run_id: 'warp-new' }};\n"
        "_chainHop(CHILDREN[0], H).then(async (r) => { await _abandonAll(); console.log(JSON.stringify([r, prompts])); });\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.spawns_process
def test_hop_whose_first_start_threw_abandons_the_hop_run_with_chained_from(tmp_path):
    result, prompts = _hop(tmp_path, "(() => { throw new Error('boom'); })()")
    assert result["status"] == "failed"
    leg = [p for p in prompts if p.startswith("abandon-batch")]
    assert len(leg) == 1
    assert '"abandon_run":"warp-new","chained_from":"ask-r1"' in leg[0]


@pytest.mark.spawns_process
def test_hop_that_started_is_never_abandoned(tmp_path):
    result, prompts = _hop(tmp_path, "n === 1 ? { stage_pending: { handle: { sha256: 'zz' } } } : { done: true }")
    assert result["status"] == "failed"
    assert not any("abandon_run" in p for p in prompts)


def test_ask_chain_block_releases_the_child_only_when_the_first_start_threw():
    script = "\n".join(ask_compose._chain_emit_start_lines(sizing_js="SZ", head="H", anchor="A", run_id="r1"))
    start = script.index("_why = 'start threw: '")
    abandon = script.index("chain-abandon")
    halt_fallthrough = script.index("if (_why === null) { return")
    assert start < abandon < halt_fallthrough
    assert "if (_chainRunId !== '')" in script[start:abandon]
    assert "abandon_run: _chainRunId, chained_from: _runId" in script
    assert script.count("abandon_run: _chainRunId") == 1


def test_released_child_no_longer_resolves_as_the_halted_run_to_resume(tmp_path):
    rdir = _chained(tmp_path)
    assert not _closed(rdir)
    out = _abandon(tmp_path, session_id="s1", chained_from=PARENT)
    assert out["abandoned"] is True
    assert _closed(rdir)
    assert halted_run.halted_run_to_resume(tmp_path, SIZING) != RUN


def test_child_past_minted_stays_resolvable(tmp_path):
    rdir = _chained(tmp_path, child_stage="staged")
    assert _abandon(tmp_path, session_id="s1", chained_from=PARENT)["abandoned"] is False
    assert not _closed(rdir)

"""Every stage-read agent keys its sidecar dir per read, so no read can return another read's reply.

The composed `_stageRecover` / `_stageHold` run under node with an `agent()` stub that behaves like the sidecar
reply file: a read whose dir already holds a reply returns it unchanged, so a shared dir shows up as a stale reply.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, ask_roadmap
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_plan_blitz_ready_restage import _PLAN, _script
from coordinator_core.win_portability import no_console_creationflags

_DIR_RE = re.compile(r"sidecar_dir: (\S+)")
_SCHEMA = {"type": "object", "properties": {}}


def _recover_block(script: str) -> str:
    start = script.index("  const _stageRecoverParams")
    tail = "  _stageHold = (w) => _stageRecover(w);"
    return script[start : script.index(tail, start) + len(tail)]


def _run_reads(script: str, calls: str) -> list[dict]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    program = (
        "const _files = {};\n"
        "const _seen = [];\n"
        "const _agentDefsClause = () => '';\n"
        "const agent = async (p, opts) => {\n"
        "  const dir = p.match(/sidecar_dir: (\\S+)/)[1];\n"
        "  if (!(dir in _files)) _files[dir] = { stage_reply: { n: Object.keys(_files).length + 1 } };\n"
        "  _seen.push({ dir, label: opts.label, reply: _files[dir] });\n"
        "  return _files[dir];\n"
        "};\n"
        "(async () => {\n"
        "let _planRel = 'p.md', _sizingRel = null, _runId = 'run-1', _crossRepoApproved = false, _SESSION_ID = 's';\n"
        "let _writes = ['a.py'], _gated = [], _stageRecover = null, _stageHold = null, _stageReadSeq = 0;\n"
        + _recover_block(script)
        + "\n"
        + calls
        + "\nconsole.log(JSON.stringify(_seen));\n"
        "})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });\n"
    )
    proc = subprocess.run(
        [node, "-"], input=program, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env=os.environ.copy(), **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.spawns_process
def test_a_recover_then_two_stage_wait_holds_each_poll_their_own_reply(monkeypatch):
    seen = _run_reads(
        _script(monkeypatch, {"planPath": _PLAN}),
        "await _stageRecover();\n"
        "await _stageHold({ blockers: ['r1'], deadline: 1 });\n"
        "await _stageHold({ blockers: ['r1'], deadline: 1 });\n",
    )
    assert len(seen) == 3
    assert [s["label"] for s in seen] == ["blitz-stage-read"] * 3
    assert len({s["dir"] for s in seen}) == 3, seen
    assert [s["reply"]["stage_reply"]["n"] for s in seen] == [1, 2, 3]
    assert "blitz-stage-read-recover-1/" in seen[0]["dir"] and "blitz-stage-read-hold-2/" in seen[1]["dir"]


def test_the_readyop_miss_read_and_the_roadmap_read_own_dirs_apart_from_every_other_read(monkeypatch):
    script = _script(monkeypatch, {"planPath": _PLAN})
    roadmap = "\n".join(ask_roadmap._staged_from_disk_lines("REPO", "run-1", "scratch/warp/r1/roadmap-blitz"))
    ready = re.search(r"const _blitzStageRead = async.*?\n  };", script, re.S).group(0)
    dirs = [
        ".coordinator-local/workflow-sidecars/ask/run-1/blitz-stage-read-ready/",
        ".coordinator-local/workflow-sidecars/ask/run-1/blitz-stage-read-disk/",
    ]
    assert dirs[0] in script and dirs[1] in roadmap
    assert dirs[1] not in script and dirs[0] not in roadmap
    assert ".coordinator-local/workflow-sidecars/ask/run-1/blitz-stage-read/" not in script + roadmap
    assert "_stageReadSeq" not in ready


def test_a_stage_read_names_exactly_one_sidecar_key():
    for kw in ({}, {"arm": "a", "label_js": "'x'"}):
        with pytest.raises(ValueError):
            ask_compose._stage_read_agent("p", schema=_SCHEMA, phase="plan", run_id="run-1", **kw)

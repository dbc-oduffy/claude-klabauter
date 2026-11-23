"""A deferred launcher whose dispatch.emit times out (-32004) after minting re-sends the emit and starts the one run."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import deferred_emit_script as des
from coordinator_core.warm import client

_MINTED = "ask-20261123T011621-f90d25"
_SCRIPT = f"/r/scratch/warp/{_MINTED}.workflow.mjs"


def _indeterminate_error() -> str:
    msg = {"jsonrpc": "2.0", "id": 1, "method": "dispatch.emit", "params": {}}
    return client._indeterminate_envelope(msg, "no response within 30.0s")["error"]["message"]


def _run_launcher(tmp_path: Path, replies: list[dict]) -> dict:
    """Render the real launcher, run it in node with `engineOp` answering `replies` in order (the last repeats);
    returns {result, emits, started}."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = des.write_deferred_script(tmp_path, {"ask_brief": "x"})
    body = script.read_text(encoding="utf-8").replace("export const meta", "const meta", 1)
    code = (
        f"const REPLIES = {json.dumps(replies)};\n"
        "let emits = 0;\n"
        "const started = [];\n"
        "const engineOp = async () => REPLIES[Math.min(emits++, REPLIES.length - 1)];\n"
        "const workflow = async (o) => { started.push(o.scriptPath); return { done: true }; };\n"
        "(async () => {\n" + body + "\n})().then((result) => console.log(JSON.stringify({ result, emits, started })));\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_TIMED_OUT = {"path": "", "run_id": "", "fire_args": {}, "error": _indeterminate_error()}
_MINTED_REPLY = {"path": _SCRIPT, "run_id": _MINTED, "fire_args": {}, "deduped": True}


def test_a_slow_emit_that_already_minted_still_starts_its_workflow_once(tmp_path: Path) -> None:
    out = _run_launcher(tmp_path, [_TIMED_OUT, _MINTED_REPLY])

    assert out["started"] == [_SCRIPT]
    assert out["emits"] == 2
    assert out["result"] == {"done": True}


def test_an_emit_that_never_answers_still_halts_refused_without_starting(tmp_path: Path) -> None:
    out = _run_launcher(tmp_path, [_TIMED_OUT])

    assert out["started"] == []
    assert out["emits"] == 1 + 2
    assert out["result"]["status"] == "emit_refused"
    assert "halted_by" in out["result"]


def test_a_plain_emit_error_is_not_re_sent(tmp_path: Path) -> None:
    out = _run_launcher(tmp_path, [{"path": "", "run_id": "", "fire_args": {}, "error": "emit refused: no sizing"}])

    assert out["started"] == []
    assert out["emits"] == 1
    assert out["result"]["status"] == "emit_refused"

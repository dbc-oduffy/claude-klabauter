"""Incident pin: a launcher child's stage hand-back needs no twin-token agent leg.

The engine escrows the run token at `dispatch.emit` for a launcher child, leaves it out of the reply, and the
stage-time twin write inlines it; the rendered launcher then starts the twin directly on `stage_pending`."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, fan_launcher, op, reply_sidecar, run_token
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (
    _BLITZ_FN,
    _LANDED,
    _SIZING,
    big_manifest,
)
from coordinator_core.ops.dispatch_emit.tests.test_sizing_fire_wiring import env  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_INLINE_TOKEN = re.compile(r"const _STAGED_INLINE = (\{.*?\});\n", re.S)

_PROGRAM = r"""
const fs = require('fs');
const cfg = JSON.parse(process.env.CFG);
const launcherSrc = fs.readFileSync(process.env.LAUNCHER_FILE, 'utf8');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const strip = (s) => s.replace(/^export const meta/m, 'const meta');
const labels = [];
const scriptPaths = [];
const agent = async (prompt, opts) => {
  const label = opts && opts.label;
  labels.push(label);
  if (label === 'emit-' + cfg.run_id) return cfg.emit;
  return {};
};
const parallel = async (xs) => Promise.all(xs.map((f) => f()));
const noop = async () => ({});
const workflow = async (spec) => {
  scriptPaths.push(String(spec.scriptPath));
  return scriptPaths.length === 1 ? { stage_pending: { handle: cfg.handle } } : { committed: true };
};
const launcher = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'workflow', strip(launcherSrc));
launcher(undefined, agent, parallel, noop, () => {}, workflow)
  .then((result) => console.log(JSON.stringify({ result, labels, scriptPaths })))
  .catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""


def _emit_child(env, monkeypatch) -> dict:  # noqa: F811
    """Emit one launcher child through the real handler, with the real child script as the composed body."""
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")

    def compose(**kw):
        return compose_ask_script(
            repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id=kw["run_id"], session_id=None,
            wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
            script_path=kw["script_path"], in_run_commit=True, launcher_child=True, **REVIEW_KW,
        )

    monkeypatch.setattr(ask_compose, "compose_ask_script", compose)
    return op._dispatch_emit({"ask": "add a widget", "launcher_child": True}, repo_root=env.root)


def _twin_inline(twin: Path) -> dict:
    return json.loads(_INLINE_TOKEN.search(twin.read_text(encoding="utf-8")).group(1))


def test_a_launcher_child_stages_and_resumes_with_only_its_emit_leg(env, monkeypatch):  # noqa: F811
    root = env.root
    reply = _emit_child(env, monkeypatch)
    run_id = reply["run_id"]
    assert "runToken" not in reply["fire_args"]
    token = run_token.escrowed(root, run_id)
    assert token is not None and run_token.TOKEN_RE.match(token)
    assert list(root.rglob(f"escrow/{run_id}.json"))

    handle = reply_sidecar.write_reply(root, root / f"state/warp/{run_id}/manifest.json", big_manifest(3))
    twin = root / fan_launcher.staged_twin_rel(run_id, handle["sha256"])
    assert _twin_inline(twin)["runToken"] == token

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    child = fan_launcher.LaunchChild(
        run_id=run_id, sizing_rel=_SIZING, emit_params={"ask": True, "sizing_path": _SIZING, "reserved_run_id": run_id},
    )
    launcher = fan_launcher.write_launcher(root, "ask-parent", [child], "sess-1")
    cfg = {"run_id": run_id, "emit": reply, "handle": handle, "commit": _LANDED}
    proc = subprocess.run(
        [node, "-"], input=_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "LAUNCHER_FILE": payload_file(launcher.read_text(encoding="utf-8")), "CFG": json.dumps(cfg)},
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["labels"] == [f"emit-{run_id}"]
    assert out["scriptPaths"][1] == twin.as_posix()
    assert out["result"]["runs"][0]["status"] == "ok", out["result"]

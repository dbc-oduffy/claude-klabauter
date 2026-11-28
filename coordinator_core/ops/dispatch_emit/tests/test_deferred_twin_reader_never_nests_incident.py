"""Incident pin: a run started by a deferred start script never puts a stage-sidecar reader below depth 1.

The runtime refuses `workflow()` from a child workflow. A deferred start script (the deferred-emit script and the
minted-start script) starts the run script, and its `stage_pending` staged twin, with `workflow()`, so both run at
depth 1; any `workflow()` reader they hold (`reply_sidecar.READ_STAGE_SIDECAR_JS`) is refused with the nesting text and
the run halts. The harness executes the rendered start scripts for real, the scripts they start for real, and throws
the refusal from every `workflow()` call made below the top level."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real
from coordinator_core.hooks.tests.test_submit_engine_ask_mint_or_refuse import RUN_ID, _mint
from coordinator_core.ops.dispatch_emit import deferred_emit_script, minted_run
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.nested_run_contract import RUN_SCRIPT_SUFFIX, staged_twin_rel
from coordinator_core.ops.dispatch_emit.reply_sidecar import READ_STAGE_SIDECAR_JS, VERIFY_REPLY_JS

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

repo = real.repo
NESTING = "workflow() cannot be called from within a child workflow — nesting is limited to one level"
RUN_REL = f"{RUN_DIR_ROOT}/{RUN_ID}{RUN_SCRIPT_SUFFIX}"
MANIFEST_TEXT = json.dumps({"rows": [{"id": "C0"}, {"id": "C1"}]}, separators=(",", ":"))
HANDLE = {
    "path": "state/warp/manifest.json",
    "sha256": hashlib.sha256(MANIFEST_TEXT.encode("ascii")).hexdigest(),
    "bytes": len(MANIFEST_TEXT),
}

_HARNESS = r"""
const fs = require('fs');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const cfg = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const calls = [];
const strip = (s) => s.replace(/^export const meta/m, 'const meta');
async function runScript(path, args, depth, engineOp) {
  const workflow = async (spec) => {
    calls.push({ depth, scriptPath: String(spec.scriptPath) });
    if (depth >= 1) throw new Error(cfg.nesting);
    return runScript(spec.scriptPath, spec.args, depth + 1, undefined);
  };
  const agent = async (_prompt, opts) => {
    const queue = (cfg.agentReplyQueue || {})[opts && opts.label];
    if (queue && queue.length) return queue.shift();
    return (cfg.agentReplies || {})[opts && opts.label] || {};
  };
  const src = strip(fs.readFileSync(path, 'utf8'));
  return new AsyncFunction('workflow', 'args', 'agent', 'engineOp', src)(workflow, args, agent, engineOp);
}
const engineOp = cfg.emitReply ? async () => cfg.emitReply : undefined;
runScript(process.argv[2], undefined, 0, engineOp).then(
  (out) => console.log(JSON.stringify({ out, calls })),
  (err) => console.log(JSON.stringify({ error: String((err && err.message) || err), calls })),
);
"""

_READ_STAGE_JS = (
    "export const meta = {}\n"
    + VERIFY_REPLY_JS
    + READ_STAGE_SIDECAR_JS
    + "const _read = await _readStageSidecar(__HANDLE__, __ROOT__);\n"
    "if (_read.fail) return { halted_by: { halted: 'stage-reply-handle', reason: 'stage reply sidecar does not match its handle: ' + _read.fail } };\n"
    "return { stage: 'done', rows: _read.manifest.rows.map((r) => r.id) };\n"
)


def _reader_holding_script(root: Path) -> str:
    """A run script that reads its stage reply through the real `_readStageSidecar`, as an emit without `launcher_child` leaves it."""
    reader = root / "scratch" / "stage-readers" / f"{HANDLE['sha256']}.js"
    reader.parent.mkdir(parents=True, exist_ok=True)
    reader.write_text(f"return {json.dumps(MANIFEST_TEXT)};\n", encoding="utf-8", newline="")
    return _READ_STAGE_JS.replace("__HANDLE__", json.dumps(HANDLE)).replace("__ROOT__", json.dumps(root.as_posix()))


def _put(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _stage_pending_script() -> str:
    return "export const meta = {}\nreturn { stage_pending: { handle: " + json.dumps(HANDLE) + " } };\n"


def _node(script: Path, tmp_path: Path, cfg: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = tmp_path / "defer-nesting-harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    cfg_file = tmp_path / "defer-nesting-cfg.json"
    cfg_file.write_text(json.dumps({**cfg, "nesting": NESTING}), encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), str(script), str(cfg_file)],
        capture_output=True, text=True, timeout=60, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _deferred(root: Path, tmp_path: Path) -> dict:
    """The rendered deferred-emit script run under node; its emit relay answers with the run script already on disk."""
    script = deferred_emit_script.write_deferred_script(root, {"ask": "build the thing", "preminted_run_id": RUN_ID})
    emit = {"path": (root / RUN_REL).as_posix(), "run_id": RUN_ID, "fire_args": {}}
    return _node(script, tmp_path, {"emitReply": emit})


def _minted_start(root: Path, tmp_path: Path) -> dict:
    """The rendered minted-start script run under node; its op relay answers with the real `dispatch.minted_run` start reply."""
    reply = minted_run._handler({"run_id": RUN_ID, "action": "start"}, root)
    assert reply["ok"] is True, reply
    script = submit_engine_ask._write_minted_start_script(root, [RUN_ID])
    relay = {k: reply[k] for k in ("ok", "script_path", "run_script", "fire_args") if reply.get(k) is not None}
    return _node(script, tmp_path, {"agentReplies": {"minted-start": relay}})


def _no_nesting(got: dict) -> None:
    assert NESTING not in json.dumps(got, ensure_ascii=False), got
    assert "error" not in got, got
    assert not [c for c in got["calls"] if c["depth"] >= 1], got["calls"]


def _completed_read(got: dict) -> None:
    _no_nesting(got)
    assert got["out"] == {"stage": "done", "rows": ["C0", "C1"]}, got


def test_a_deferred_start_over_a_run_with_no_reader_completes(repo, tmp_path):
    _mint(repo, script=False)
    _put(repo, RUN_REL, "export const meta = {}\nreturn { stage: 'done', rows: ['C0', 'C1'] };\n")
    _completed_read(_deferred(repo, tmp_path))


def _handed_off(got: dict, rel: str, root: Path) -> None:
    _no_nesting(got)
    assert got["out"]["status"] == "start_handoff" and got["out"]["run_id"] == RUN_ID, got
    assert got["out"]["script_path"] == (root / rel).as_posix(), got


def test_a_deferred_start_over_a_run_holding_its_own_reader_never_nests(repo, tmp_path):
    _mint(repo, script=False)
    _put(repo, RUN_REL, _reader_holding_script(repo))
    _handed_off(_deferred(repo, tmp_path), RUN_REL, repo)


def test_a_deferred_start_over_a_stage_pending_twin_holding_the_reader_never_nests(repo, tmp_path):
    _mint(repo, script=False)
    _put(repo, RUN_REL, _stage_pending_script())
    twin = staged_twin_rel(RUN_ID, HANDLE["sha256"])
    _put(repo, twin, _reader_holding_script(repo))
    _handed_off(_deferred(repo, tmp_path), twin, repo)


def test_a_minted_start_over_a_run_holding_its_own_reader_never_nests(repo, tmp_path):
    _mint(repo, script=False)
    _put(repo, RUN_REL, _reader_holding_script(repo))
    got = _minted_start(repo, tmp_path)
    _no_nesting(got)
    assert got["out"]["status"] == "start_handoff" and got["out"]["run_id"] == RUN_ID, got


def test_a_minted_start_over_a_stage_pending_twin_holding_the_reader_never_nests(repo, tmp_path):
    _mint(repo, script=False)
    _put(repo, RUN_REL, _stage_pending_script())
    twin = staged_twin_rel(RUN_ID, HANDLE["sha256"])
    _put(repo, twin, _reader_holding_script(repo))
    got = _minted_start(repo, tmp_path)
    _no_nesting(got)
    assert got["out"]["status"] == "start_handoff" and got["out"]["script_path"] == (repo / twin).as_posix(), got


# A twin staged at runtime, after the start script was rendered and after the op reported the run reader-free, is never
# seen by a render-time or start-time hand-back, so the loop itself starts it. The twin holds the real adopt tail:
# a reader it does not run itself is answered with `stage_pending` carrying the handle, exactly as `ask_compose` leaves it.
_ADOPTING_TWIN_JS = (
    "export const meta = {}\n"
    + VERIFY_REPLY_JS
    + READ_STAGE_SIDECAR_JS
    + "const _read = await _readStageSidecar(__HANDLE__, __ROOT__);\n"
    "if (_read.fail) return { halted_by: { halted: 'stage-reply-handle', reason: 'stage reply sidecar does not match its handle: ' + _read.fail } };\n"
    "if (!_read.manifest) return { stage_pending: { handle: __HANDLE__ } };\n"
    "return { stage: 'first', rows: _read.manifest.rows.map((r) => r.id) };\n"
)
FIRST_STAGE = {"stage": "first", "rows": ["C0", "C1"]}


def _adopting_twin(root: Path, *, reader: bool = True, reader_text: str = MANIFEST_TEXT) -> str:
    """The twin script holding the adopt tail; `reader` writes the reader workflow the loop runs at its own depth."""
    if reader:
        path = root / "scratch" / "stage-readers" / f"{HANDLE['sha256']}.js"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"return {json.dumps(reader_text)};\n", encoding="utf-8", newline="")
    return _ADOPTING_TWIN_JS.replace("__HANDLE__", json.dumps(HANDLE)).replace("__ROOT__", json.dumps(root.as_posix()))


def _stage_twin_at_runtime(root: Path, **twin_kw) -> str:
    """The run script answers `stage_pending`; its twin lands on disk only now."""
    _put(root, RUN_REL, _stage_pending_script())
    twin = staged_twin_rel(RUN_ID, HANDLE["sha256"])
    _put(root, twin, _adopting_twin(root, **twin_kw))
    return twin


def _deferred_staged_late(root: Path, tmp_path: Path, **twin_kw) -> dict:
    """The deferred script is rendered with nothing on disk (no render-time reader hand-back), then the twin is staged."""
    _mint(root, script=False)
    script = deferred_emit_script.write_deferred_script(root, {"ask": "build the thing", "preminted_run_id": RUN_ID})
    _stage_twin_at_runtime(root, **twin_kw)
    emit = {"path": (root / RUN_REL).as_posix(), "run_id": RUN_ID, "fire_args": {}}
    return _node(script, tmp_path, {"emitReply": emit})


def _minted_staged_late(root: Path, tmp_path: Path, **twin_kw) -> dict:
    """The op reports the run reader-free (a wrapper, no `fire_args`), then the twin is staged before the loop starts the run."""
    _mint(root, script=False)
    _put(root, RUN_REL, _stage_pending_script())
    reply = minted_run._handler({"run_id": RUN_ID, "action": "start"}, root)
    assert reply["ok"] is True and reply["fire_args"] is None, reply
    script = submit_engine_ask._write_minted_start_script(root, [RUN_ID])
    _stage_twin_at_runtime(root, **twin_kw)
    relay = {k: reply[k] for k in ("ok", "script_path", "run_script", "fire_args") if reply.get(k) is not None}
    return _node(script, tmp_path, {"agentReplies": {"minted-start": relay}})


def _first_stage_reached(got: dict) -> None:
    _no_nesting(got)
    assert got["out"] == FIRST_STAGE, got
    assert any(c["scriptPath"].endswith(f"{HANDLE['sha256']}.js") for c in got["calls"]), "the stage reply was never read"


def test_a_deferred_start_over_a_twin_staged_at_runtime_reads_its_reply_and_reaches_the_first_stage(repo, tmp_path):
    _first_stage_reached(_deferred_staged_late(repo, tmp_path))


def test_a_minted_start_over_a_twin_staged_at_runtime_reads_its_reply_and_reaches_the_first_stage(repo, tmp_path):
    _first_stage_reached(_minted_staged_late(repo, tmp_path))


def test_a_twin_fired_top_level_still_reads_its_reply_through_the_reader(repo, tmp_path):
    """The other side of the decision: a script nothing started as a child keeps its own reader call, at depth 0."""
    _mint(repo, script=False)
    twin = _put(repo, staged_twin_rel(RUN_ID, HANDLE["sha256"]), _adopting_twin(repo))
    got = _node(twin, tmp_path, {})
    _no_nesting(got)
    assert got["out"] == FIRST_STAGE, got
    assert [c["depth"] for c in got["calls"]] == [0], got["calls"]


@pytest.mark.parametrize("start", [_deferred_staged_late, _minted_staged_late])
def test_a_reply_the_reader_returns_tampered_is_never_adopted(repo, tmp_path, start):
    got = start(repo, tmp_path, reader_text=MANIFEST_TEXT.replace("C1", "C9"))
    assert NESTING not in json.dumps(got, ensure_ascii=False), got
    assert got["out"] != FIRST_STAGE and "C9" not in json.dumps(got["out"]), got

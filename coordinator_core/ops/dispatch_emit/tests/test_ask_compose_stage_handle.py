"""The composed ask script takes a stage reply from every stage leg and assembles the manifest from the sidecar
the engine's reader script returns through `workflow({scriptPath})`, never from bytes an agent relayed. The whole
composed script runs under node with every agent leg stubbed by label and `workflow` evaluating the reader file
`reply_sidecar.write_reply` really wrote.

The helpers below are the shared fake the other composed-script tests bind their stage replies with."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, reply_sidecar
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.nested_run_contract import STAGED_INLINE_SLOT
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_STAGED: dict[str, tuple[str, dict]] = {}
_ROOT: list[Path] = []

#: Prepended to a node program: `makeWorkflow(cfg)` is a `workflow` global that reads the reader at
#: `cfg.root` + the script's `REPO/`-relative scriptPath, drops a leading `export const meta` line (the
#: runner strips it) and evaluates the rest as an AsyncFunction body. `workflowCalls` records each scriptPath.
WORKFLOW_FAKE_JS = r"""
const fs = require('fs');
const workflowCalls = [];
const makeWorkflow = (cfg) => async (spec) => {
  const p = String(spec && spec.scriptPath);
  workflowCalls.push(p);
  if (!p.startsWith('REPO/')) throw new Error('Workflow script file not found: ' + p);
  let body;
  try { body = fs.readFileSync(cfg.root + '/' + p.slice(5), 'utf8'); }
  catch (_e) { throw new Error('Workflow script file not found: ' + p); }
  body = body.replace(/^export const meta[^\n]*\n/, '');
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  return await new AsyncFunction(body)();
};
"""


@pytest.fixture(autouse=True)
def stage_root(tmp_path):
    """The directory `readers_cfg()` writes every staged sidecar and its reader under."""
    _ROOT[:] = [tmp_path]
    yield tmp_path
    _ROOT.clear()


def sidecar_text(manifest: dict) -> str:
    return reply_sidecar.canonical_json(manifest)


def stage_reply(manifest: dict | None) -> dict | None:
    """The stage op's reply for `manifest`: a handle over its sidecar text, or the refusal `{error}`.

    The sidecar and its reader reach disk when `readers_cfg()` runs, through the real `write_reply`.
    """
    if manifest is None:
        return None
    if manifest.get("error"):
        return {k: manifest[k] for k in ("error", "owed_em_ids", "owed_plan") if k in manifest}
    raw = sidecar_text(manifest).encode("ascii")
    sha = hashlib.sha256(raw).hexdigest()
    rel = f"state/warp/fixture/{sha[:16]}.json"
    _STAGED[sha] = (rel, manifest)
    return {"manifest_handle": {"path": rel, "sha256": sha, "bytes": len(raw)}}


def agent_gate_reply(root, reply: dict) -> dict:
    """What the gate agent returns for a real `dispatch.ask_gate` reply: its stage as `stage_reply`."""
    out = {k: v for k, v in reply.items() if k not in ("manifest", "manifest_handle")}
    if "manifest_handle" in reply:
        out["stage_reply"] = {"manifest_handle": reply["manifest_handle"]}
    elif "manifest" in reply:
        out["stage_reply"] = reply["manifest"]
    return out


def readers_cfg(root: Path | None = None) -> dict:
    """The node config naming the root the reader scripts live under.

    With no `root`, every `stage_reply` manifest is written under the test's tmp_path first.
    """
    if root is None:
        root = _ROOT[0]
        for sha, (rel, manifest) in _STAGED.items():
            assert reply_sidecar.write_reply(root, root / rel, manifest)["sha256"] == sha
    return {"root": str(root)}


def reader_file(handle: dict) -> Path:
    return _ROOT[0] / reply_sidecar.reader_rel(handle["sha256"])


_ROW = {
    "id": "C1", "brief_path": "b/C1.md", "agent_type": "coordinator:executor", "model": "opus",
    "writes": ["a.py"], "wave": 0, "deps": [],
}
_SHA = "c" * 40
_TARGET = "pkg/tail_test_target.py"
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_PLAN = "docs/plans/2026-10-16-resumed.md"
_SIZING = "state/sizings/x.yaml"
_TOKEN = "0123456789abcdef0123456789abcdef"
_RESUME = {"run_id": "ask-prior", "plan": _PLAN, "blocking": ["T6"], "em_discharged": {}}
_LANDED = {"committed": True, "sha": "abc1234", "error": None, "refused": None}
_MET = {"items": [{"id": "T6", "status": "met", "wired": True}]}


def big_manifest(rows: int = 40) -> dict:
    return {
        "run_dir": "d", "plan_id": "p", "marker_path": "m", "head_sha": _SHA,
        "review_declared_paths": [f"pkg/f{n}.py" for n in range(rows)], "test_targets": [_TARGET],
        "rows": [
            {**_ROW, "id": f"C{n}", "brief_path": f"briefs/C{n}.md", "writes": [f"pkg/f{n}.py"]} for n in range(rows)
        ],
    }


_NODE_PROGRAM = (
    WORKFLOW_FAKE_JS
    + r"""
const src = require('fs').readFileSync(process.env.SRC_FILE, 'utf8').replace(/^export const meta/m, 'const meta');
const cfg = JSON.parse(process.env.CFG);
const workflow = makeWorkflow(cfg);
const calls = [];
const agent = async (prompt, opts) => {
  const label = opts && opts.label;
  calls.push({ label, prompt: String(prompt) });
  if (label === 'halt-commit' || label === 'commit') return cfg.commit;
  if (label === 'verify') return cfg.verify;
  if (cfg.replies[label] !== undefined) return cfg.replies[label];
  return {};
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'workflow', src);
run(cfg.args ?? { runToken: cfg.token }, agent, async (xs) => Promise.all(xs.map((f) => f())), async () => ({}), () => {}, workflow)
  .then((result) => console.log(JSON.stringify({ calls, result, workflowCalls })))
  .catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""
)


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )


def _run(script: str, reply: dict | None, *, relay=None, tamper=None, args: dict | None = None) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    gate = {
        "arm": "m_plus", "halt": None, "baton": None, "resume_ledger": _RESUME, "tshirt": "M",
        "route": "spec-dispatch", "stage_reply": reply, **({"manifest": relay} if relay is not None else {}),
    }
    cfg = {
        "token": _TOKEN, "verify": {**{"items": _MET["items"]}}, "commit": _LANDED,
        "replies": {"gate": gate, "gate-stage": gate}, **readers_cfg(),
        **({"args": {"runToken": _TOKEN, **args}} if args is not None else {}),
    }
    if tamper is not None:
        tamper()
    proc = subprocess.run(
        [node, "-"], input=_NODE_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC_FILE": payload_file(script), "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _labels(out: dict) -> list[str]:
    return [c["label"] for c in out["calls"]]


def _halt_reason(out: dict) -> str:
    return out["result"]["halted_by"]["reason"]


def _no_executor(out: dict) -> bool:
    labels = _labels(out)
    return "verify" not in labels and not [lab for lab in labels if lab and lab.startswith(("work:", "C"))]


def _edit_reader(handle: dict, old: str, new: str) -> None:
    path = reader_file(handle)
    body = path.read_text(encoding="ascii")
    assert old in body
    path.write_text(body.replace(old, new, 1), encoding="ascii")


def test_a_forty_row_manifest_is_assembled_whole_from_the_reader_with_no_agent_leg(monkeypatch):
    manifest = big_manifest(120)
    reply = stage_reply(manifest)
    sha = reply["manifest_handle"]["sha256"]
    assert len(sidecar_text(manifest)) > 3 * 4497
    out = _run(_script(monkeypatch), reply)
    res = out["result"]
    assert res["halted_by"] is None, res["halted_by"]
    assert res["rows"] == [r["id"] for r in manifest["rows"]]
    assert out["workflowCalls"] == [f"REPO/{reply_sidecar.reader_rel(sha)}"]
    assert not [lab for lab in _labels(out) if lab and "reply" in lab and lab != "gate-stage"], _labels(out)
    # The ledger-remaining prompt names `resume_ledger.leg.writes`, not manifest rows; _RESUME has no leg, so only the leg's presence is pinned.
    assert "ledger-remaining" in _labels(out)


def test_a_one_row_manifest_is_read_by_one_workflow_call(monkeypatch):
    reply = stage_reply(big_manifest(2))
    out = _run(_script(monkeypatch), reply)
    assert out["result"]["halted_by"] is None
    assert len(out["workflowCalls"]) == 1


def test_a_reader_one_byte_off_the_handle_halts_and_dispatches_no_executor(monkeypatch):
    reply = stage_reply(big_manifest(40))
    handle = reply["manifest_handle"]
    out = _run(_script(monkeypatch), reply, tamper=lambda: _edit_reader(handle, '\\"C1\\"', '\\"C9\\"'))
    assert _halt_reason(out).startswith("stage reply sidecar does not match its handle: ")
    assert _no_executor(out), _labels(out)


def test_a_reader_returning_a_shorter_text_halts(monkeypatch):
    reply = stage_reply(big_manifest(40))
    handle = reply["manifest_handle"]
    out = _run(_script(monkeypatch), reply, tamper=lambda: _edit_reader(handle, '\\"marker_path\\"', '\\"marker\\"'))
    assert _halt_reason(out).startswith("stage reply sidecar does not match its handle: ")
    assert _no_executor(out)


def test_a_missing_reader_halts_naming_the_reader(monkeypatch):
    reply = stage_reply(big_manifest(40))
    handle = reply["manifest_handle"]
    out = _run(_script(monkeypatch), reply, tamper=lambda: reader_file(handle).unlink())
    reason = _halt_reason(out)
    assert reason.startswith("stage reply sidecar does not match its handle: ")
    assert reply_sidecar.reader_rel(handle["sha256"]) in reason
    assert _no_executor(out)


def test_a_manifest_an_agent_relays_under_manifest_is_ignored(monkeypatch):
    out = _run(_script(monkeypatch), None, relay=big_manifest(3))
    assert _halt_reason(out).startswith("no folded stage manifest")
    assert out["workflowCalls"] == []


def test_a_stage_refusal_still_halts_by_name_with_no_reader_read(monkeypatch):
    out = _run(_script(monkeypatch), {"error": "boom"})
    assert _halt_reason(out) == "stage refused: boom"
    assert out["workflowCalls"] == []


def test_a_reply_carrying_both_an_error_and_a_handle_is_not_adopted(monkeypatch):
    reply = stage_reply(big_manifest(2))
    out = _run(_script(monkeypatch), {**reply, "error": "boom"})
    assert _halt_reason(out) == "stage manifest relay is not whole: stage reply carries both error and manifest_handle"


def test_a_reply_carrying_neither_is_not_whole(monkeypatch):
    out = _run(_script(monkeypatch), {})
    assert _halt_reason(out) == "stage manifest relay is not whole: manifest_handle"


def _slotted(script: str, handle: dict, text: str) -> str:
    """The staged twin: the inline slot line replaced by `{handle, text}`."""
    assert script.count(STAGED_INLINE_SLOT) == 1
    inline = json.dumps({"handle": handle, "text": text})
    return script.replace(STAGED_INLINE_SLOT, "const _STAGED_INLINE = " + inline + ";")


def _launcher_script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, launcher_child=True, **REVIEW_KW,
    )


def test_a_launcher_child_script_holds_no_reader_no_workflow_call_and_one_slot_line(monkeypatch):
    script = _launcher_script(monkeypatch)
    assert "workflow(" not in script
    assert "_readStageSidecar" not in script
    assert "_LAUNCHER_ARGS" not in script and "_adoptStagedArg" not in script
    assert script.count(STAGED_INLINE_SLOT + "\n") == 1
    assert script.index(STAGED_INLINE_SLOT) < script.index("function _adoptStagedInline(")


def test_a_launcher_child_with_an_empty_slot_is_pending_and_never_calls_workflow(monkeypatch):
    reply = stage_reply(big_manifest(3))
    out = _run(_launcher_script(monkeypatch), reply)
    assert out["result"] == {"stage_pending": {"handle": reply["manifest_handle"]}}
    assert out["workflowCalls"] == []
    assert _no_executor(out)


def test_a_launcher_child_adopts_the_matching_inline_manifest_with_no_workflow_call(monkeypatch):
    manifest = big_manifest(3)
    reply = stage_reply(manifest)
    script = _slotted(_launcher_script(monkeypatch), reply["manifest_handle"], sidecar_text(manifest))
    out = _run(script, reply)
    assert out["result"]["halted_by"] is None, out["result"]["halted_by"]
    assert out["result"]["rows"] == [r["id"] for r in manifest["rows"]]
    assert out["workflowCalls"] == []


def test_a_launcher_child_refuses_inline_text_that_does_not_match_its_handle(monkeypatch):
    manifest = big_manifest(3)
    reply = stage_reply(manifest)
    bad = sidecar_text(manifest).replace('"C1"', '"C9"', 1)
    out = _run(_slotted(_launcher_script(monkeypatch), reply["manifest_handle"], bad), reply)
    assert _halt_reason(out).startswith("stage reply sidecar does not match its handle: ")
    assert _no_executor(out)
    assert out["workflowCalls"] == []


def test_an_inline_manifest_for_another_handle_is_pending_not_adopted(monkeypatch):
    other_manifest = big_manifest(2)
    other = stage_reply(other_manifest)["manifest_handle"]
    reply = stage_reply(big_manifest(3))
    out = _run(_slotted(_launcher_script(monkeypatch), other, sidecar_text(other_manifest)), reply)
    assert "stage_pending" in out["result"]


def test_a_standalone_script_has_no_slot_no_args_branch_and_reads_the_reader_unconditionally(monkeypatch):
    script = _script(monkeypatch)
    assert STAGED_INLINE_SLOT not in script
    for gone in ("_LAUNCHER_CHILD", "_LAUNCHER_ARGS", "_adoptStagedArg", "_adoptStagedInline"):
        assert gone not in script
    assert ask_compose._stage_reply_js() == ask_compose._stage_reply_js(False)
    assert ask_compose._ADOPT_STAGE_REPLY_JS in script


def test_a_standalone_run_ignores_launcher_args_and_reads_the_reader(monkeypatch):
    manifest = big_manifest(3)
    reply = stage_reply(manifest)
    args = {
        "launcherChild": True,
        "stagedManifest": {"handle": reply["manifest_handle"], "text": sidecar_text(manifest)},
    }
    out = _run(_script(monkeypatch), reply, args=args)
    assert out["result"]["halted_by"] is None
    assert len(out["workflowCalls"]) == 1


def test_the_script_reads_the_reader_through_workflow_and_composes_no_page_leg(monkeypatch):
    script = _script(monkeypatch)
    assert "workflow({ scriptPath: reader })" in script
    assert "await _readStageSidecar(r.manifest_handle, REPO_ROOT)" in script
    assert "stage-reply-page" not in script and "reply_page" not in script
    assert "_MANIFEST_READ_SCHEMA" not in script and "_adoptManifest" not in script


def test_a_launcher_child_run_token_reads_the_inline_slot_and_standalone_bytes_are_unchanged(monkeypatch):
    script = _launcher_script(monkeypatch)
    assert "_STAGED_INLINE.runToken" in script
    assert script.index(STAGED_INLINE_SLOT) < script.index("const _RUN_TOKEN")
    standalone = _script(monkeypatch)
    assert ask_compose._RUN_TOKEN_DECL_JS in standalone
    assert "_STAGED_INLINE" not in standalone

"""The composed ask script takes a stage reply from every stage leg and assembles the manifest from
verified `dispatch.reply_page` pages, never from a manifest an agent relayed. The whole composed script
runs under node with every agent leg stubbed by label; the page leg answers with
`reply_sidecar.page_reply`, the function the engine op returns.

The helpers below are the shared fake the other composed-script tests bind their stage replies with."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, reply_sidecar
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PAGE_LABEL = "stage-reply-page"
_PAGES: dict[str, list[dict]] = {}

#: Prepended to a node program that defines `agent`; its stub returns `fakePage(cfg, prompt)` for the page label.
PAGE_FAKE_JS = r"""
const fakePage = (cfg, prompt) => {
  const m = /dispatch\.reply_page '([^']*)'/.exec(prompt);
  if (!m) return { error: 'page leg prompt names no reply_page call' };
  const want = JSON.parse(m[1]);
  const pages = (cfg.pages || {})[want.handle.sha256];
  if (!pages || pages[want.page] === undefined) return { error: 'no such page' };
  return pages[want.page];
};
"""


def sidecar_text(manifest: dict) -> str:
    return json.dumps(manifest, indent=2) + "\n"


def register(handle: dict, text: str) -> None:
    """Record every page the engine op would return for `handle`, under the handle's sha256."""
    pages = reply_sidecar.page(text, 0)[1]
    _PAGES[handle["sha256"]] = [reply_sidecar.page_reply(handle, text, i) for i in range(pages)]


def stage_reply(manifest: dict | None) -> dict | None:
    """The stage op's reply for `manifest`: a handle over its sidecar text, or the refusal `{error}`."""
    if manifest is None:
        return None
    if manifest.get("error"):
        return {k: manifest[k] for k in ("error", "owed_em_ids", "owed_plan") if k in manifest}
    text = sidecar_text(manifest)
    raw = text.encode("ascii")
    handle = {"path": "state/warp/fixture/manifest.json", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    register(handle, text)
    return {"manifest_handle": handle}


def agent_gate_reply(root, reply: dict) -> dict:
    """What the gate agent returns for a real `dispatch.ask_gate` reply: its stage as `stage_reply`."""
    out = {k: v for k, v in reply.items() if k not in ("manifest", "manifest_handle")}
    if "manifest_handle" in reply:
        handle = reply["manifest_handle"]
        register(handle, reply_sidecar.read_text(root, handle))
        out["stage_reply"] = {"manifest_handle": handle}
    elif "manifest" in reply:
        out["stage_reply"] = reply["manifest"]
    return out


def pages_cfg() -> dict[str, list[dict]]:
    return copy.deepcopy(_PAGES)


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
    PAGE_FAKE_JS
    + r"""
const src = process.env.SRC.replace(/^export const meta/m, 'const meta');
const cfg = JSON.parse(process.env.CFG);
const calls = [];
const agent = async (prompt, opts) => {
  const label = opts && opts.label;
  calls.push({ label, prompt: String(prompt) });
  if (label === 'stage-reply-page') return fakePage(cfg, String(prompt));
  if (label === 'halt-commit' || label === 'commit') return cfg.commit;
  if (label === 'verify') return cfg.verify;
  if (cfg.replies[label] !== undefined) return cfg.replies[label];
  return {};
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', src);
run({ runToken: cfg.token }, agent, async (xs) => Promise.all(xs.map((f) => f())), async () => ({}), () => {})
  .then((result) => console.log(JSON.stringify({ calls, result })))
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


def _run(script: str, reply: dict | None, *, pages=None, relay=None) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    gate = {
        "arm": "m_plus", "halt": None, "baton": None, "resume_ledger": _RESUME, "tshirt": "M",
        "route": "spec-dispatch", "stage_reply": reply, **({"manifest": relay} if relay is not None else {}),
    }
    cfg = {
        "token": _TOKEN, "verify": {**{"items": _MET["items"]}}, "commit": _LANDED,
        "replies": {"gate": gate, "gate-stage": gate}, "pages": pages if pages is not None else pages_cfg(),
    }
    proc = subprocess.run(
        [node, "-e", _NODE_PROGRAM], capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC": script, "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _labels(out: dict) -> list[str]:
    return [c["label"] for c in out["calls"]]


def test_a_forty_row_manifest_is_assembled_whole_from_more_than_one_verified_page(monkeypatch):
    manifest = big_manifest(40)
    reply = stage_reply(manifest)
    pages = len(_PAGES[reply["manifest_handle"]["sha256"]])
    assert pages > 1
    out = _run(_script(monkeypatch), reply)
    res = out["result"]
    assert res["halted_by"] is None, res["halted_by"]
    assert res["rows"] == [r["id"] for r in manifest["rows"]]
    assert _labels(out).count(PAGE_LABEL) == pages
    tail = next(c for c in out["calls"] if c["label"] == "ledger-remaining")
    assert "pkg/f39.py" in tail["prompt"]


def test_a_one_page_manifest_costs_one_page_leg(monkeypatch):
    reply = stage_reply(big_manifest(2))
    out = _run(_script(monkeypatch), reply)
    assert out["result"]["halted_by"] is None
    assert _labels(out).count(PAGE_LABEL) == 1


def _flip(pages: dict, reply: dict, page: int) -> dict:
    sha = reply["manifest_handle"]["sha256"]
    chunk = pages[sha][page]["chunk"]
    pages[sha][page]["chunk"] = chunk[:10] + ("X" if chunk[10] != "X" else "Y") + chunk[11:]
    return pages


def _halt_reason(out: dict) -> str:
    return out["result"]["halted_by"]["reason"]


def test_a_tampered_sidecar_halts_and_dispatches_no_executor(monkeypatch):
    reply = stage_reply(big_manifest(40))
    out = _run(_script(monkeypatch), reply, pages=_flip(pages_cfg(), reply, 1))
    assert _halt_reason(out).startswith("stage reply sidecar does not match its handle:")
    labels = _labels(out)
    assert "verify" not in labels and not [lab for lab in labels if lab and lab.startswith(("work:", "C"))], labels


def test_a_dropped_page_halts(monkeypatch):
    reply = stage_reply(big_manifest(40))
    pages = pages_cfg()
    del pages[reply["manifest_handle"]["sha256"]][1]
    out = _run(_script(monkeypatch), reply, pages=pages)
    assert _halt_reason(out).startswith("stage reply sidecar does not match its handle:")
    assert "verify" not in _labels(out)


def test_a_page_whose_sha256_echo_differs_halts(monkeypatch):
    reply = stage_reply(big_manifest(40))
    pages = pages_cfg()
    pages[reply["manifest_handle"]["sha256"]][1]["sha256"] = "0" * 64
    out = _run(_script(monkeypatch), reply, pages=pages)
    assert _halt_reason(out).startswith("stage reply sidecar does not match its handle:")
    assert "verify" not in _labels(out)


def test_a_page_the_op_refuses_halts_with_the_ops_reason(monkeypatch):
    reply = stage_reply(big_manifest(40))
    out = _run(_script(monkeypatch), reply, pages={})
    assert _halt_reason(out).startswith("stage reply sidecar does not match its handle:")


def test_a_manifest_an_agent_relays_under_manifest_is_ignored(monkeypatch):
    out = _run(_script(monkeypatch), None, relay=big_manifest(3))
    assert _halt_reason(out).startswith("no folded stage manifest")
    assert PAGE_LABEL not in _labels(out)


def test_a_stage_refusal_still_halts_by_name_with_no_page_leg(monkeypatch):
    out = _run(_script(monkeypatch), {"error": "boom"})
    assert _halt_reason(out) == "stage refused: boom"
    assert PAGE_LABEL not in _labels(out)


def test_a_reply_carrying_both_an_error_and_a_handle_is_not_adopted(monkeypatch):
    reply = stage_reply(big_manifest(2))
    out = _run(_script(monkeypatch), {**reply, "error": "boom"})
    assert _halt_reason(out) == "stage manifest relay is not whole: stage reply carries both error and manifest_handle"


def test_a_reply_carrying_neither_is_not_whole(monkeypatch):
    out = _run(_script(monkeypatch), {})
    assert _halt_reason(out) == "stage manifest relay is not whole: manifest_handle"


def test_the_page_leg_is_the_mechanical_agent_and_makes_one_engine_call(monkeypatch):
    script = _script(monkeypatch)
    opts = script[script.index("label: 'stage-reply-page'"):].split("schema:", 1)[0]
    assert "agentType: 'general-purpose'" in opts and "model: 'haiku'" in opts
    assert script.count(reply_sidecar.OP_REPLY_PAGE) == 1
    assert "_MANIFEST_READ_SCHEMA" not in script and "_adoptManifest" not in script

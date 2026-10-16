"""A 3-source research emit whose notebooklm gate fires still runs web and repo, names the halted segments, and closes."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.pipeline_contract import WORKFLOW_SCRIPT_CAP_BYTES
from coordinator_core.ops.dispatch_emit.tests.test_research_emit_script_cap import _emit
from coordinator_core.ops.dispatch_emit.tests.test_research_gate_end_to_end import close_repo  # noqa: F401
from coordinator_core.ops.research_close import _handler

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.skipif(shutil.which("node") is None, reason="node absent"),
]

VENDORED = Path(__file__).parent / "fixtures" / "pipeline_research"
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SOURCES = ["web", "notebooklm", "repo"]

_REPO_MANIFEST = """schema_version: 1
pipeline: repo
description: Fixture repo pipeline shaped like web; declares the flags the depth binds.
inputs:
  brief: required
  scratch_dir: optional
  flags:
    sonnet_scouts: ["true", "false"]
    deepest: ["true", "false"]
stages:
  - id: scope
    phase: RepoScope
    agent_type: general-purpose
    model: sonnet
    template: web-scope-template.md
    fan_out: none
    produces_brief: true
    output: "{{scratch_dir}}/scope.md"
  - id: sweep
    phase: RepoSweep
    agent_type: general-purpose
    model: sonnet
    template: web-sweep-template.md
    fan_out: none
    depends_on: [scope]
    output: "{{scratch_dir}}/sweep.md"
"""

_SOURCE_GATE = """  - id: source-gate
    phase: SourceGate
    agent_type: general-purpose
    model: haiku
    template: notebooklm-ask-template.md
    fan_out: none
    depends_on: [scout]
    halts_unless: ok
    output: "{{scratch_dir}}/source-gate.md"
    schema:
      type: object
      required: [ok, remedy]
      properties:
        ok: { type: boolean }
        remedy: { type: array, items: { type: string } }
"""

# Runs the emitted script with stubbed host globals; agent answers by label and prompt, and the
# run prints its return value and every agent label called, in order, as JSON.
_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8').replace(/^export const meta/m, 'const meta');
const mode = process.argv[3];
const labels = [];
const answer = (label, prompt) => {
  const nlm = /NotebookLM/.test(prompt) && !/signed in/.test(prompt);
  if (label === 'check') return { ready: mode !== 'preflight-down', remedy: mode === 'preflight-down' ? ['sign in'] : [] };
  if (label === 'source-gate') return { ok: mode === 'clean', remedy: ['r'] };
  if (label === 'scope' && nlm) return { worker_count: 1, notebooks: ['T0'] };
  if (label === 'scout' && nlm) return { usable_sources: [{ notebook: 'T0', reason: 'x' }] };
  return 'done';
};
const agent = async (prompt, opts) => {
  const label = (opts && opts.label) || '';
  labels.push(label);
  return answer(label, prompt);
};
const parallel = async (fns) => Promise.all(fns.map((f) => (typeof f === 'function' ? f() : f)));
const run = new Function('agent', 'parallel', 'phase', 'log', 'return (async () => {' + src + '\n})();');
run(agent, parallel, () => {}, () => {}).then((ret) => {
  console.log(JSON.stringify({ ret, labels }));
}, (e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""


@pytest.fixture
def content_root(tmp_path):
    """The vendored pipeline tree plus a repo manifest and a `source-gate` (halts_unless ok) stage in notebooklm."""
    root = tmp_path / "content"
    shutil.copytree(VENDORED, root)
    base = root / "pipelines" / "deep-research"
    (base / "repo.manifest.yaml").write_text(_REPO_MANIFEST, encoding="utf-8")
    nlm = base / "notebooklm.manifest.yaml"
    text = nlm.read_text(encoding="utf-8")
    marker = "  - id: workers\n"
    assert marker in text
    text = text.replace(marker, _SOURCE_GATE + marker, 1)
    workers_dep = "    depends_on: [scout]\n    output: \"{{scratch_dir}}/{{item}}"
    assert workers_dep in text
    nlm.write_text(text.replace(workers_dep, workers_dep.replace("[scout]", "[source-gate]"), 1), encoding="utf-8")
    return root


@pytest.fixture
def emitted(monkeypatch, tmp_path, capsys, content_root):
    """(script, reply) of the three-source emit over one notebooklm target."""
    return _emit(monkeypatch, tmp_path / "run", capsys, content_root, _SOURCES, 1)


def _drive(script: str, mode: str, tmp_path: Path) -> dict:
    (tmp_path / "driver.js").write_text(_DRIVER, encoding="utf-8")
    (tmp_path / "script.js").write_text(script, encoding="utf-8")
    out = subprocess.run(
        ["node", str(tmp_path / "driver.js"), str(tmp_path / "script.js"), mode],
        capture_output=True, text=True, creationflags=_NOWIN,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _pipelines(ret: dict) -> list[str]:
    return [r["pipeline"] for r in ret["results"]]


def _close(repo: Path, params: dict) -> dict:
    scratch = Path(params["scratch_dir"])
    for seg in ("web", "repo"):
        (scratch / seg).mkdir(parents=True, exist_ok=True)
        (scratch / seg / "sweep.md").write_text("sweep\n", encoding="utf-8")
    return asyncio.run(_handler(params, repo / ".git"))


def test_the_three_source_script_fits_the_cap(emitted):
    script, _ = emitted
    assert len(script.encode("utf-8")) <= WORKFLOW_SCRIPT_CAP_BYTES


def test_a_source_gate_halt_runs_web_and_repo_and_names_the_halted_segments(emitted, tmp_path):
    script, _ = emitted
    got = _drive(script, "source-gate-halt", tmp_path)
    ret = got["ret"]
    assert {"web", "repo"} <= set(_pipelines(ret))
    halts = {h["segment"]: h for h in ret["segment_halts"]}
    assert halts["notebooklm"]["halted"] == "notebooklm.source-gate"
    assert halts["notebooklm"]["decision_required"] == ["r"]
    assert "workers:1" not in got["labels"]
    assert ret["next_action"]["params"]["halted_segments"] == ["nlm-preflight", "notebooklm"]


def test_close_after_a_source_gate_halt_commits_deliverable_and_names_the_halted_segments(emitted, close_repo, tmp_path):
    script, _ = emitted
    params = _drive(script, "source-gate-halt", tmp_path)["ret"]["next_action"]["params"]
    out = _close(close_repo, params)
    assert out["committed"] is True and out["deliverable"] is True, out
    assert out["halted_segments"] == ["nlm-preflight", "notebooklm"]


def test_a_preflight_halt_skips_notebooklm_while_web_and_repo_run(emitted, tmp_path):
    script, _ = emitted
    got = _drive(script, "preflight-down", tmp_path)
    ret = got["ret"]
    assert {"web", "repo"} <= set(_pipelines(ret))
    halts = {h["segment"]: h for h in ret["segment_halts"]}
    assert halts["nlm-preflight"]["halted"] == "nlm-preflight.check"
    assert halts["notebooklm"] == {"segment": "notebooklm", "leg": "notebooklm", "skipped": True}
    assert "scout" not in got["labels"]
    assert ret["next_action"]["params"]["halted_segments"] == ["nlm-preflight", "notebooklm"]


def test_a_run_with_no_halt_returns_the_emit_replys_static_next_action(emitted, tmp_path):
    script, reply = emitted
    ret = _drive(script, "clean", tmp_path)["ret"]
    assert ret["segment_halts"] == []
    assert {"web", "repo", "notebooklm"} <= set(_pipelines(ret))
    assert ret["next_action"] == reply["next_action"]

"""Exit-criterion falsifier: research, pipeline, grind and ask emit fewer agents than units, with cost in meta.

Each case builds six small units under tmp_path, counts the agents its script will dispatch, and reads
the meta `fanout` object. The grind already batched triage before the grouping work, so its agent
inequality alone discharges nothing there; its case rests on the meta and queue-reply `fanout`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.dispatch_emit as _pkg
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.fanout_cost import STARTUP_TOKENS_PER_AGENT, read_fanout
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

_FIXTURES = Path(__file__).parent / "fixtures"
_META_KEYS = {"expectedAgents", "basis", "units", "startupTokensPerAgent", "startupTokensTotal"}
_SIX = [f"Question number {w} about the thing?" for w in ("one", "two", "three", "four", "five", "six")]


def _assert_cost_fields(fanout: dict, *, units: int) -> None:
    assert fanout is not None
    assert _META_KEYS <= set(fanout)
    assert isinstance(fanout["expectedAgents"], int) and not isinstance(fanout["expectedAgents"], bool)
    assert fanout["basis"] in ("exact", "lower-bound")
    assert fanout["units"] == units
    assert fanout["startupTokensPerAgent"] == STARTUP_TOKENS_PER_AGENT > 0
    assert fanout["startupTokensTotal"] == fanout["expectedAgents"] * fanout["startupTokensPerAgent"]


class _Admitted:
    @staticmethod
    def await_admission(start_dir, *, hold_allowed):
        return {"verdict": "disabled"}


@pytest.fixture
def repo(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-exit-criterion")
    return tmp_path


# --- research ---------------------------------------------------------------------------------


def test_research_six_questions_compose_fewer_scouts_and_the_reply_carries_the_proposal(repo, monkeypatch, capsys):
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: _FIXTURES / "pipeline_research")
    argv = ["--research", "--ask", "What is X?", "--list", "questions=" + ",".join(_SIX)]

    assert cli_module.main(argv) == cli_module.EXIT_OK
    reply = json.loads(capsys.readouterr().out)
    script = Path(reply["path"]).read_text(encoding="utf-8")

    meta = read_fanout(script)
    _assert_cost_fields(meta, units=6)
    assert meta["expectedAgents"] < 6
    assert meta["expectedAgents"] <= 3
    assert meta["groups"] and sum(len(g) for g in meta["groups"]) == 6
    assert reply["fanout"] == meta

    assert cli_module.main([*argv, "--fanout", "split"]) == cli_module.EXIT_OK
    split = json.loads(capsys.readouterr().out)["fanout"]
    assert split["expectedAgents"] == 6 and split["expectedAgents"] > meta["expectedAgents"]


# --- pipeline ---------------------------------------------------------------------------------

_PIPELINE_MANIFEST = """schema_version: 1
pipeline: sweep
description: One agent per item, six items.
inputs:
  brief: required
  scratch_dir: optional
  lists:
    items:
      kind: strings
stages:
  - id: work
    phase: Work
    agent_type: general-purpose
    model: sonnet
    template: work-template.md
    fan_out:
      over: inputs.items
    output: "{{scratch_dir}}/out-{{item}}.md"
"""


def test_pipeline_six_item_fan_out_composes_fewer_agents_and_the_reply_carries_the_proposal(repo, monkeypatch):
    content = repo / "content"
    pdir = content / "pipelines" / "sweep"
    pdir.mkdir(parents=True)
    (pdir / "sweep.manifest.yaml").write_text(_PIPELINE_MANIFEST, encoding="utf-8")
    (pdir / "work-template.md").write_text("Process {{item}} and write {{scratch_dir}}.\n", encoding="utf-8")
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: content)
    (repo / "brief.md").write_text("sweep the items", encoding="utf-8")

    reply = _dispatch_emit({
        "pipeline": "sweep", "brief": "brief.md", "target_root": str(repo), "session_id": "sess-1",
        "lists": {"items": ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]},
    })
    meta = read_fanout(Path(reply["path"]).read_text(encoding="utf-8"))

    _assert_cost_fields(meta, units=6)
    assert meta["expectedAgents"] < 6 and meta["expectedAgents"] <= 3
    assert meta["basis"] == "exact"
    assert [len(g) for g in meta["groups"]] == [2, 2, 2]
    assert reply["fanout"] == meta

    split = _dispatch_emit({
        "pipeline": "sweep", "brief": "brief.md", "target_root": str(repo), "session_id": "sess-1",
        "lists": {"items": ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]}, "fanout_group": "split",
    })
    assert split["fanout"]["expectedAgents"] == 6


# --- grind ------------------------------------------------------------------------------------

_PROFILE_DIR = _FIXTURES / "queue-profiles"


def _queue_reply(repo: Path) -> dict:
    queue_dir = repo / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    for i in range(6):
        row = {"created": "2026-09-21", "title": "a bug row", "body": "a body", "status": "open",
               "surface": "coordinator_core/x", "severity": "P0"}
        text = "".join(f"{k}: {json.dumps(v)}\n" for k, v in row.items())
        (queue_dir / f"row{i}.yaml").write_text(text, encoding="utf-8")
    (repo / "state" / "queue-grind" / "fixture" / "run-1").mkdir(parents=True)
    out = repo / "queue-grind" / "emitted.mjs"
    out.parent.mkdir(parents=True)
    return _dispatch_emit(
        {"queue": [str(queue_dir)], "profile": "fixture", "profile_dir": str(_PROFILE_DIR), "output_path": str(out)},
        repo_root=repo,
    )


def test_grind_six_entries_batch_and_meta_and_queue_reply_carry_the_cost(repo):
    reply = _queue_reply(repo)
    script = Path(reply["path"]).read_text(encoding="utf-8")

    meta = read_fanout(script)
    _assert_cost_fields(meta, units=6)
    assert meta["basis"] == "lower-bound"
    assert len(meta["groups"]) < 6
    assert sum(len(g) for g in meta["groups"]) == 6
    assert meta["expectedAgents"] > len(meta["groups"])
    assert reply["fanout"] == meta


# --- ask --------------------------------------------------------------------------------------

_PLAN_REL = "docs/plans/2026-10-01-fanout.md"
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_ROADMAP_TEXT = (
    "export const meta = { name: 'rb', phases: [{ title: 'Synthesize', detail: 'x' }] };\n"
    "return { gate_report_path: 'g.json' };"
)


def _ask_manifest(repo: Path):
    rows = "".join(
        f"- id: C{i}\n  title: row C{i}\n  change_kind: code-edit\n  surface: pkg/C{i}.py\n  writes:\n    - pkg/C{i}.py\n"
        for i in range(1, 7)
    )
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / _PLAN_REL).write_text(
        "---\ndeliverable_id: dlv-fan-abc123\n---\n# Fan plan\n\n## Tasks\n\n```yaml plan-tasks\n" + rows + "```\n",
        encoding="utf-8", newline="\n",
    )
    return stage(repo, run_id="r1", plan_rel=_PLAN_REL)


def _ask_script() -> str:
    return ask_compose.compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        roadmap_blitz_text=_ROADMAP_TEXT, **REVIEW_KW,
    )


# `_runRow` is stubbed: group-member line classification by the real helper is not exercised here.
_HARNESS = """
const calls = [];
const log = () => {};
const REPO_ROOT = 'REPO';
const _manifest = %(manifest)s;
const _agentDefsClause = () => '';
const _rowSidecar = () => '';
const _runBase = 'a'.repeat(40);
const _rowPlan = {};
const _verdictReports = [];
const agent = async (prompt, opts) => { calls.push(opts.label); return %(reply)s; };
const _runRow = (id, deps, scope, run, commit) => (async () => { const r = await run(); return r == null ? 'INCOMPLETE ' + id : r; })();
const _commitWave = async () => {};
const _waves = [...new Set(_manifest.rows.map((r) => r.wave))];
const _rows = {};
async function main() {
%(block)s
  for (const v of Object.values(_rows)) await v;
  console.log(JSON.stringify(calls));
}
main();
"""


@pytest.mark.spawns_process
def test_ask_six_same_wave_rows_stage_into_fewer_groups_and_the_script_runs_one_agent_per_group(repo):
    manifest = _ask_manifest(repo)
    rows = manifest.rows
    groups = {r.group for r in rows if r.group}
    solo = [r for r in rows if not r.group]
    agents = len(groups) + len(solo)
    assert agents < len(rows) == 6

    staged = manifest.fanout
    _assert_cost_fields(staged, units=6)
    assert staged["expectedAgents"] == agents

    script = _ask_script()
    meta = read_fanout(script)
    assert meta is not None and _META_KEYS <= set(meta)
    assert meta["startupTokensTotal"] == meta["expectedAgents"] * meta["startupTokensPerAgent"]

    if shutil.which("node") is None:
        pytest.skip("node absent: the dispatch count of the execute loop is not run")
    start = script.index("if (_manifest.fanout) log(")
    marker = "await _commitWave(w, _waveIds);"
    block = script[start:script.index(marker, start) + len(marker)] + "\n  }"
    reply = "\n".join(f"{r.id}: DONE ok" for r in rows)
    program = _HARNESS % {
        "manifest": json.dumps({**manifest.to_json(), "error": None}), "reply": json.dumps(reply), "block": block,
    }
    out = subprocess.run(
        [shutil.which("node"), "-"], input=program, capture_output=True, text=True, timeout=60,
        **no_console_creationflags(),
    )
    assert out.returncode == 0, out.stderr
    labels = json.loads(out.stdout.strip().splitlines()[-1])
    assert len(labels) == agents < 6

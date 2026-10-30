"""Two separable asks in one engine-ask submission become sibling runs, through the hook's `evaluate`
with the real fan-out and the real emit on a tmp repo: the reply, the `after` sequencing, the exact
partition of the parent register, the budget queue, and the release on a predecessor's close.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core import warp_fire, warp_live
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import ask_fanout as fanout_mod
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import requirements_ledger as ledger
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.session_baton import store

SID = "sess-sibling-e2e"
VERBATIM = "fix the parser and also rename the exporter, they are unrelated jobs"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


EMIT_CALLS: list[dict] = []


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DELIVERABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    (root / "state" / "sizings").mkdir(parents=True)
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    hub = tmp_path / "hub" / SID
    hub.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn(VERBATIM, session_id=SID)["ok"]
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    EMIT_CALLS.clear()
    real_emit = mod._default_emit

    def recording_emit(params: dict, repo_root: Path) -> dict:
        EMIT_CALLS.append(params)
        return real_emit(params, repo_root)

    monkeypatch.setattr(mod, "_default_emit", recording_emit)
    return root


def _submit(root: Path, args: dict) -> dict:
    return mod.evaluate({
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": args},
        "session_id": SID,
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    })


def _hso(env: dict) -> dict:
    return env["hookSpecificOutput"]


def _all_text(env: dict) -> str:
    return json.dumps(env)


def _reply_json(env: dict) -> dict:
    blob = json.loads(_all_text(env))
    flat = json.dumps(blob).replace('\\"', '"')
    start = flat.index('Reply: ') + len("Reply: ")
    depth, end = 0, start
    for i in range(start, len(flat)):
        depth += flat[i] == "{"
        depth -= flat[i] == "}"
        if depth == 0:
            end = i + 1
            break
    return json.loads(flat[start:end])


def _parent(root: Path, parent_id: str) -> c.ParentRecord:
    return c.ParentRecord.from_json(json.loads((root / c.parent_record_rel(parent_id)).read_text(encoding="utf-8")))


def _sizing(root: Path, rel: str) -> dict:
    return yaml.safe_load((root / rel).read_text(encoding="utf-8"))


def _fire_slice(root: Path, sizing_rel: str) -> dict:
    return _submit(root, {"sizing": sizing_rel})


def _status(root: Path, run_id: str) -> str:
    return json.loads((root / c.status_rel(run_id)).read_text(encoding="utf-8"))["status"]


def _bind(root: Path, run) -> list[str]:
    """Seed one run's ledger through `requirements_ledger.bind`; the texts of its rows."""
    if ledger.read_run(ledger.run_dir(root, run.run_id)) is None:
        ledger.start(root, run.run_id)
    return [item.text for item in ledger.bind(root, run.run_id, run.slice_sizing).items]


def _disjoint(n: int = 2) -> list[dict]:
    return [{"ask": f"job {i}", "source": "pm", "label": f"job-{i}", "writes": [f"pkg/m{i}.py"]} for i in range(n)]


def _launcher_children(env: dict) -> list[dict]:
    """The children literal the hook's launcher script embeds, read back from the written file."""
    text = Path(_hso(env)["updatedInput"]["scriptPath"]).read_text(encoding="utf-8")
    start = text.index("const CHILDREN = ") + len("const CHILDREN = ")
    return json.JSONDecoder().raw_decode(text[start:])[0]


def _submission(root: Path, run_id: str) -> dict | None:
    path = warp_live._submission_path(root, run_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def test_a_disjoint_pair_replies_parent_and_runs_and_starts_both_under_one_launcher(repo):
    env = _submit(repo, {"asks": _disjoint()})

    assert _hso(env)["permissionDecision"] == "allow", env
    reply = _reply_json(env)
    assert set(reply) == {"parent_id", "runs"}
    assert [set(r) for r in reply["runs"]] == [{"run_id", "label"}] * 2
    parent = _parent(repo, reply["parent_id"])
    assert [r.status for r in parent.runs] == ["ready", "ready"]
    assert "Fire the listed runs" not in _all_text(env)
    assert [ch["run_id"] for ch in _launcher_children(env)] == [r.run_id for r in parent.runs]
    for run in parent.runs:
        assert _status(repo, run.run_id) == "running"

    for run in parent.runs:
        doc = _sizing(repo, run.slice_sizing)
        assert doc["pm_verbatims"][0]["text"] == VERBATIM
        assert c.parse_ask_parent(doc["em_analysis"]["ask_parent"]) == (reply["parent_id"], run.run_id)


def test_slice_ledgers_union_to_the_parent_register_and_seed_no_verbatim(repo):
    reply = _reply_json(_submit(repo, {"asks": _disjoint()}))
    parent = _parent(repo, reply["parent_id"])
    seeded: list[str] = []
    for run in parent.runs:
        texts = _bind(repo, run)
        assert all(VERBATIM not in t for t in texts)
        seeded.extend(texts)
    assert sorted(seeded) == ["job 0", "job 1"]
    assert len(seeded) == len(set(seeded))

    bound_run = parent.runs[1].run_id
    assert (repo / c.run_parent_copy_rel(bound_run)).read_text(encoding="utf-8") == (
        repo / c.parent_record_rel(reply["parent_id"])
    ).read_text(encoding="utf-8")
    assert record_dir(repo, bound_run).is_dir()


@pytest.mark.parametrize(
    "asks",
    [[{"ask": "one", "writes": ["pkg"]}, {"ask": "two", "writes": ["pkg/x.py"]}]],
    ids=["overlapping"],
)
def test_a_sequenced_slice_is_denied_until_the_predecessor_close_names_its_fire_call(repo, asks):
    reply = _reply_json(_submit(repo, {"asks": asks}))
    first, second = reply["runs"]
    assert second["after"] == [first["run_id"]] and "after" not in first
    parent = _parent(repo, reply["parent_id"])
    assert [r.status for r in parent.runs] == ["ready", "after"]

    denied = _fire_slice(repo, parent.runs[1].slice_sizing)
    assert _hso(denied)["permissionDecision"] == "deny"
    assert first["run_id"] in _hso(denied)["permissionDecisionReason"]

    close = run_close.sibling_release(repo, first["run_id"], "abc1234")
    expected = warp_fire.fire_command(parent.runs[1].slice_sizing)
    assert close["released"] == [expected] and close["blocked"] == []

    allowed = _fire_slice(repo, parent.runs[1].slice_sizing)
    assert _hso(allowed)["permissionDecision"] == "allow", allowed
    assert _status(repo, parent.runs[1].run_id) == "running"


def test_unhinted_siblings_run_in_parallel_with_no_after(repo):
    env = _submit(repo, {"asks": [{"ask": "one", "writes": ["a.py"]}, {"ask": "two"}]})
    reply = _reply_json(env)
    assert all("after" not in r for r in reply["runs"])
    assert [r.status for r in _parent(repo, reply["parent_id"]).runs] == ["ready", "ready"]


def test_a_halted_predecessor_releases_no_successor(repo):
    reply = _reply_json(_submit(repo, {"asks": [{"ask": "one", "writes": ["a"]}, {"ask": "two", "writes": ["a"]}]}))
    first, second = reply["runs"]
    close = run_close.sibling_release(repo, first["run_id"], None)
    assert close["released"] == [] and [b["run_id"] for b in close["blocked"]] == [second["run_id"]]
    denied = _fire_slice(repo, _parent(repo, reply["parent_id"]).runs[1].slice_sizing)
    assert _hso(denied)["permissionDecision"] == "deny"
    assert "fresh" in _hso(denied)["permissionDecisionReason"]


def test_a_halted_predecessor_with_every_row_committed_admits_the_successor(repo):
    reply = _reply_json(_submit(repo, {"asks": [{"ask": "one", "writes": ["a"]}, {"ask": "two", "writes": ["a"]}]}))
    first, second = reply["runs"]
    parent = _parent(repo, reply["parent_id"])
    close = run_close.sibling_release(repo, first["run_id"], None, rows_committed=True)
    assert close["released"] == [warp_fire.fire_command(parent.runs[1].slice_sizing)]
    assert close["blocked"] == []
    allowed = _fire_slice(repo, parent.runs[1].slice_sizing)
    assert _hso(allowed)["permissionDecision"] == "allow", allowed
    assert _status(repo, second["run_id"]) == "running"


def test_a_three_ask_submission_with_one_after_edge_launches_the_two_ready_runs_only(repo):
    env = _submit(repo, {"asks": [
        {"ask": "one", "writes": ["a"]},
        {"ask": "two", "writes": ["b"]},
        {"ask": "three", "writes": ["a/x.py"]},
    ]})
    assert _hso(env)["permissionDecision"] == "allow", env
    parent = _parent(repo, _reply_json(env)["parent_id"])
    ready = [r for r in parent.runs if r.status == "ready"]
    held = [r for r in parent.runs if r.status == "after"]
    assert len(ready) == 2 and len(held) == 1

    children = _launcher_children(env)
    assert [(ch["run_id"], ch["sizing_rel"]) for ch in children] == [(r.run_id, r.slice_sizing) for r in ready]
    for run in ready:
        record = _submission(repo, run.run_id)
        assert record is not None and record["script_path"] == _hso(env)["updatedInput"]["scriptPath"]
    assert _submission(repo, held[0].run_id) is None
    assert held[0].run_id not in json.dumps(children)
    assert sorted(p.name for p in (repo / "scratch" / "warp").glob("*.workflow.mjs")) == sorted(
        [Path(_hso(env)["updatedInput"]["scriptPath"]).name] + [f"{r.run_id}.workflow.mjs" for r in ready]
    )
    for run in ready:
        assert _status(repo, run.run_id) == "running"
    assert not (repo / c.status_rel(held[0].run_id)).exists()


def test_three_disjoint_asks_all_read_ready_and_the_launcher_leaves_all_three_running(repo):
    reply = _reply_json(_submit(repo, {"asks": _disjoint(3)}))
    parent = _parent(repo, reply["parent_id"])
    assert [r.status for r in parent.runs] == ["ready", "ready", "ready"]
    assert len(reply["runs"]) == 3 and all("after" not in r for r in reply["runs"])

    assert [_status(repo, r.run_id) for r in parent.runs] == ["running"] * 3
    refire = _fire_slice(repo, parent.runs[1].slice_sizing)
    assert _hso(refire)["permissionDecision"] == "deny"
    assert "already working" in _hso(refire)["permissionDecisionReason"]


def _split_parent(repo: Path) -> str:
    rows = [
        {"id": rid, "source_text": text, "anchor": "pm_verbatims", "surface": "internal", "status": "open", "claimed_by": []}
        for rid, text in (("R1", "fix the parser"), ("R2", "rename the exporter"), ("R3", "document both"))
    ]
    doc = {
        "schema": "sizing-object", "name": "bundle", "intent": "bundle", "route": "dispatch",
        "pm_verbatims": [{"turn": 1, "text": VERBATIM}],
        rr.REGISTER_KEY: {rr.SOURCES_KEY: [{"doc": "pm", "ref": "turn 1"}], rr.ROWS_KEY: rows},
    }
    rel = "state/sizings/2026-10-15-bundle.yaml"
    (repo / rel).write_text(yaml.safe_dump(doc), encoding="utf-8")
    return rel


def test_a_duplicated_and_an_unclaimed_row_on_the_slices_arm_are_refused_naming_both(repo):
    parent = _split_parent(repo)
    before = sorted(p.name for p in (repo / "state" / "sizings").iterdir())
    out = fanout_mod.fanout({
        "parent_sizing": parent, "session_id": SID,
        "slices": [
            {"label": "a", "ask": "parser", "rows": ["R1", "R2"], "writes": ["a"]},
            {"label": "b", "ask": "exporter", "rows": ["R2"], "writes": ["b"]},
        ],
    }, repo)

    assert out["exit_code"] == 1 and out["applied"] is False
    assert "R2" in out["error"] and "R3" in out["error"]
    assert "doubly-claimed" in out["error"] and "unclaimed" in out["error"]
    assert sorted(p.name for p in (repo / "state" / "sizings").iterdir()) == before
    assert not (repo / "scratch" / "warp").exists()


def test_slices_arm_ledgers_union_exactly_to_the_parent_register(repo):
    parent = _split_parent(repo)
    out = fanout_mod.fanout({
        "parent_sizing": parent, "session_id": SID,
        "slices": [
            {"label": "a", "ask": "parser", "rows": ["R1", "R3"], "writes": ["a"]},
            {"label": "b", "ask": "exporter", "rows": ["R2"], "writes": ["b"]},
        ],
    }, repo)
    assert out["exit_code"] == 0, out
    parent_rows = _sizing(repo, parent)[rr.REGISTER_KEY][rr.ROWS_KEY]
    seeded: list[str] = []
    for ready in out["ready"]:
        run_id = c.parse_ask_parent(_sizing(repo, ready["sizing"])["em_analysis"]["ask_parent"])[1]
        run = c.SiblingRun(run_id=run_id, label="", slice_sizing=ready["sizing"])
        texts = _bind(repo, run)
        assert all(VERBATIM not in t for t in texts)
        seeded.extend(texts)
    assert sorted(seeded) == sorted(r[rr.ROW_TEXT] for r in parent_rows)


def test_a_mixed_relayed_and_pm_submission_records_each_slice_origin(repo):
    env = _submit(repo, {"asks": [{"ask": "relay job", "source": "relayed", "writes": ["a.py"]},
                                  {"ask": "pm job", "source": "pm", "writes": ["b.py"]}]})
    assert _hso(env)["permissionDecision"] == "allow", env
    parent = _parent(repo, _reply_json(env)["parent_id"])
    relayed_run, pm_run = parent.runs
    assert [ch["run_id"] for ch in _launcher_children(env)] == [relayed_run.run_id, pm_run.run_id]

    relayed_dir, pm_dir = record_dir(repo, relayed_run.run_id), record_dir(repo, pm_run.run_id)
    assert (relayed_dir / "relayed-ask.md").is_file() and not (relayed_dir / "ask.md").exists()
    assert not (pm_dir / "relayed-ask.md").exists()

    _bind(repo, relayed_run)
    _bind(repo, pm_run)
    anchors = {}
    for run, text in ((relayed_run, "relay job"), (pm_run, "pm job")):
        rows = ledger.read_ledger(repo, ledger.run_dir(repo, run.run_id)).items
        anchors[text] = [i.anchor for i in rows if i.text == text]
    assert anchors == {"relay job": [ledger.ORIGIN_RELAYED], "pm job": [ledger.ORIGIN_ASK]}


def test_an_entry_context_reaches_only_its_own_slice_run_record_ledger_and_script(repo):
    ctx = "concrete host bug: X"
    env = _submit(repo, {"asks": [{"ask": "first job", "source": "relayed", "writes": ["a.py"]},
                                  {"ask": "second job", "source": "relayed", "writes": ["b.py"], "context": [ctx]}]})
    assert _hso(env)["permissionDecision"] == "allow", env
    first, second = _parent(repo, _reply_json(env)["parent_id"]).runs

    emit_params = {p["reserved_run_id"]: p for p in EMIT_CALLS}
    assert set(emit_params) == {first.run_id, second.run_id}
    assert ctx not in json.dumps(emit_params[first.run_id])

    second_script = (repo / "scratch" / "warp" / f"{second.run_id}.workflow.mjs").read_text(encoding="utf-8")
    first_script = (repo / "scratch" / "warp" / f"{first.run_id}.workflow.mjs").read_text(encoding="utf-8")

    second_dir, first_dir = record_dir(repo, second.run_id), record_dir(repo, first.run_id)
    second_ask = (second_dir / "relayed-ask.md").read_text(encoding="utf-8")
    assert "second job" in second_ask

    _bind(repo, first)
    _bind(repo, second)
    second_rows = ledger.read_ledger(repo, ledger.run_dir(repo, second.run_id)).items
    first_rows = ledger.read_ledger(repo, ledger.run_dir(repo, first.run_id)).items
    assert [i.anchor for i in second_rows if ctx in i.text] == [ledger.ORIGIN_RELAYED]
    assert not any(ctx in i.text for i in first_rows)
    view = (ledger.run_dir(repo, second.run_id) / ledger.LEDGER_VIEW_FILE).read_text(encoding="utf-8")
    assert ctx in view

    assert ctx in second_script
    assert ctx not in first_script
    assert "first job" in (first_dir / "relayed-ask.md").read_text(encoding="utf-8")
    assert ctx not in (first_dir / "relayed-ask.md").read_text(encoding="utf-8")


def test_an_unknown_asks_entry_key_is_denied_naming_it_and_writes_nothing(repo):
    env = _submit(repo, {"asks": [{"ask": "one"}, {"ask": "x", "contxt": ["a bug"]}]})

    assert _hso(env)["permissionDecision"] == "deny"
    reason = _hso(env)["permissionDecisionReason"]
    assert "contxt" in reason and "context" in reason
    assert not (repo / "scratch" / "warp").exists()
    assert list((repo / "state" / "sizings").iterdir()) == []


def test_a_malformed_asks_element_is_denied_before_anything_is_written(repo):
    env = _submit(repo, {"asks": [{"ask": ""}, {"ask": "two"}]})
    assert _hso(env)["permissionDecision"] == "deny"
    assert not (repo / "scratch" / "warp").exists()


def test_a_default_launch_two_entry_pm_submission_writes_a_launcher_without_a_missing_argument_refusal(repo, monkeypatch):
    def fake_emit(params: dict, repo_root: Path) -> dict:
        rid = params["reserved_run_id"]
        return {"path": (repo_root / "scratch" / "warp" / rid / "s.workflow.mjs").as_posix(), "run_id": rid, "fire_args": {}}

    monkeypatch.setattr(mod, "_default_emit", fake_emit)
    env = _submit(repo, {"asks": _disjoint(), "source": "pm"})

    assert _hso(env)["permissionDecision"] == "allow", env
    assert "missing 1 required" not in _all_text(env)
    assert Path(_hso(env)["updatedInput"]["scriptPath"]).is_file()
    reply = _reply_json(env)
    assert [r["run_id"] for r in reply["runs"]] == [ch["run_id"] for ch in _launcher_children(env)]


def _in_run_m_plus_block() -> str:
    from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

    script = ask_compose.compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda text: ("", []), plan_blitz_text=None, **REVIEW_KW,
    )
    start = script.index("if (!_halted && _gate.arm === 'm_plus') {")
    depth, i = 0, script.index("{", start)
    for j in range(i, len(script)):
        depth += script[j] == "{"
        depth -= script[j] == "}"
        if depth == 0:
            return script[start:j + 1]
    raise AssertionError("unbalanced m_plus block")


def _run_m_plus_block(tmp_path: Path, reply: dict) -> dict:
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    code = (
        f"const REPO_ROOT = {json.dumps(tmp_path.as_posix())}; const _SESSION_ID = 's'; const _crossRepoApproved = false;\n"
        "const _runId = 'run-1'; const _sizingRel = 'state/sizings/s.yaml'; let _halted = null; const _gate = { arm: 'm_plus', chain_emit: " + json.dumps(reply) + " };\n"
        "const _agentDefsClause = () => ''; const _PFC7 = ''; const _PFC5 = ''; const phase = () => {};\n"
        f"const agent = async () => ({json.dumps(reply)});\n"
        "const started = []; const workflow = async (o) => { started.push(o.scriptPath); return { done: true }; };\n"
        "(async () => { const _out = await (async () => {\n" + _in_run_m_plus_block() + "\nreturn { halted: _halted }; })();\n"
        "console.log(JSON.stringify({ out: _out, started })); })();\n"
    )
    done = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.spawns_process
def test_an_in_run_ask_that_sizes_m_plus_chains_to_the_emit_reply_run_id(tmp_path):
    path = f"{tmp_path.as_posix()}/scratch/warp/ask-r2/c.workflow.mjs"
    got = _run_m_plus_block(tmp_path, {"path": path, "run_id": "ask-r2", "fire_args": {}})
    assert got["started"] == [path]
    assert got["out"]["done"] is True and got["out"]["chained_from"] == "run-1"


@pytest.mark.spawns_process
def test_an_in_run_m_plus_chain_halts_by_name_on_a_reply_without_a_run_id(tmp_path):
    path = f"{tmp_path.as_posix()}/scratch/warp/ask-r2/c.workflow.mjs"
    got = _run_m_plus_block(tmp_path, {"path": path, "run_id": "", "fire_args": {}})
    assert got["started"] == []
    assert "emit reply run_id is not a run id" in got["out"]["halted"]["reason"]

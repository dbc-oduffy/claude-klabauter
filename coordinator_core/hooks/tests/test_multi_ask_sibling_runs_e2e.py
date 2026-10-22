"""Two separable asks in one engine-ask submission become sibling runs, through the hook's `evaluate`
with the real fan-out and the real emit on a tmp repo: the reply, the `after` sequencing, the exact
partition of the parent register, the budget queue, and the release on a predecessor's close.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core import warp_fire
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


def test_a_disjoint_pair_replies_parent_and_runs_and_fires_one_with_the_other_listed(repo):
    env = _submit(repo, {"asks": _disjoint()})

    assert _hso(env)["permissionDecision"] == "allow", env
    reply = _reply_json(env)
    assert set(reply) == {"parent_id", "runs"}
    assert [set(r) for r in reply["runs"]] == [{"run_id", "label"}] * 2
    parent = _parent(repo, reply["parent_id"])
    assert [r.status for r in parent.runs] == ["ready", "ready"]
    other = parent.runs[1]
    assert warp_fire.fire_command(other.slice_sizing) in _all_text(env).replace("\\", "")
    assert Path(_hso(env)["updatedInput"]["scriptPath"]).is_file()
    assert (repo / c.status_rel(parent.runs[0].run_id)).is_file()
    assert not (repo / c.status_rel(other.run_id)).exists()

    for run in parent.runs:
        doc = _sizing(repo, run.slice_sizing)
        assert doc["pm_verbatims"][0]["text"] == VERBATIM
        assert c.parse_ask_parent(doc["em_analysis"]["ask_parent"]) == (reply["parent_id"], run.run_id)


def test_slice_ledgers_union_to_the_parent_register_and_seed_no_verbatim(repo):
    reply = _reply_json(_submit(repo, {"asks": _disjoint()}))
    parent = _parent(repo, reply["parent_id"])
    second = _fire_slice(repo, parent.runs[1].slice_sizing)
    assert _hso(second)["permissionDecision"] == "allow", second

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


def test_three_disjoint_asks_all_read_ready_and_each_fires_while_the_others_run(repo):
    reply = _reply_json(_submit(repo, {"asks": _disjoint(3)}))
    parent = _parent(repo, reply["parent_id"])
    assert [r.status for r in parent.runs] == ["ready", "ready", "ready"]
    assert len(reply["runs"]) == 3 and all("after" not in r for r in reply["runs"])

    for run in parent.runs[1:]:
        allowed = _fire_slice(repo, run.slice_sizing)
        assert _hso(allowed)["permissionDecision"] == "allow", allowed
    assert [_status(repo, r.run_id) for r in parent.runs[1:]] == ["running", "running"]


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
    assert warp_fire.fire_command(pm_run.slice_sizing) in _all_text(env).replace("\\", "")

    pm_fire = _fire_slice(repo, pm_run.slice_sizing)
    assert _hso(pm_fire)["permissionDecision"] == "allow", pm_fire

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

    fired = _fire_slice(repo, second.slice_sizing)
    assert _hso(fired)["permissionDecision"] == "allow", fired
    second_script = Path(_hso(fired)["updatedInput"]["scriptPath"]).read_text(encoding="utf-8")
    first_script = Path(_hso(env)["updatedInput"]["scriptPath"]).read_text(encoding="utf-8")

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

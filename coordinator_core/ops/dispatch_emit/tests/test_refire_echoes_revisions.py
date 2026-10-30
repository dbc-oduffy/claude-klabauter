"""A refire's emit reply names the revision rows it inherited from the sizing's register."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

RUN = "ask-20261015T000000-abc123"
REL = "state/sizings/2026-10-15-refire.yaml"


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return tmp_path


def _ctx(root, run_id):
    return {"root": str(root), "run_id": run_id, "sizing_rel": REL, "prompt": "the ask"}


def test_start_requirements_returns_the_bound_ledger_with_inherited_revisions(tmp_path):
    root = _repo(tmp_path)
    rl.start(root, RUN)
    rl.bind(root, RUN, REL, ask_text="the ask")
    rl._handler({"run_id": RUN, "verbatim": "also cover Windows"}, repo_root=root)

    ledger = op._start_requirements(_ctx(root, RUN), all_of_pm_ask=False)

    inherited = [i.id for i in ledger.items if i.origin == "revision"]
    assert len(inherited) == 1
    assert any(i.origin == "ask" for i in ledger.items)


def test_a_first_run_with_no_revisions_inherits_none(tmp_path):
    root = _repo(tmp_path)
    ledger = op._start_requirements(_ctx(root, RUN), all_of_pm_ask=False)
    assert [i for i in ledger.items if i.origin == "revision"] == []


def test_the_emit_reply_names_the_revisions_a_refire_inherited(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_compose

    root = _repo(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n")
    doc = yaml.safe_load((root / REL).read_text(encoding="utf-8"))
    doc["estimate"] = {"tshirt": "XS", "provisional": True}
    doc.update(
        interaction_mode="pm", deliverable_id="dlv-refire-abc123",
        exit_criterion={"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-15", "mode": "pm"}},
    )
    (root / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rl.start(root, RUN)
    rl.bind(root, RUN, REL, ask_text="the ask")
    rl._handler({"run_id": RUN, "verbatim": "also cover Windows"}, repo_root=root)

    reply = op._dispatch_emit({"ask": True, "sizing_path": REL}, repo_root=root)
    assert reply["revisions_inherited"] == [
        {"id": i.id, "first_line": i.text.split("\n", 1)[0].strip(), "kind": "revision"}
        for i in rl.read_ledger(root, record_dir(root, RUN)).items
        if i.origin == "revision"
    ]
    assert reply["revisions_inherited"][0]["first_line"] == "also cover Windows"
    assert reply["revisions_inherited"]


def test_no_sizing_binds_no_ledger(tmp_path):
    root = _repo(tmp_path)
    ctx = {**_ctx(root, RUN), "sizing_rel": None}
    assert op._start_requirements(ctx, all_of_pm_ask=False) is None

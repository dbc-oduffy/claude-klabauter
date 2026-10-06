"""Tests for fleet.prune_emitted_output: classification, fail-closed reads, budget."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import prune_emitted as pe

OLD = time.time() - 2 * 3600


@pytest.fixture
def env(tmp_path, monkeypatch):
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    state = {"tracked": {}, "live": set(), "raise_live": set()}

    def fake_spine(repo, paths):
        if state["tracked"] is None:
            return None
        return {"": {}, "docs/plans": state["tracked"]}

    def fake_live(common_dir, plan_path):
        if plan_path.name in state["raise_live"]:
            return True
        return plan_path.name in state["live"]

    monkeypatch.setattr(pe, "read_tree_spine", fake_spine)
    monkeypatch.setattr(pe, "_is_claim_live", fake_live)
    return plans, state, tmp_path


def plan(plans, name, status="approved", stamp=None):
    fm = f"---\nstatus: {status}\n"
    if stamp:
        fm += f"review_stamp: {stamp}\n"
    (plans / name).write_text(fm + "---\nbody\n", encoding="utf-8")


def emit(plans, stem, variant="", receipt=True, plan_field=None, age=OLD, raw=None):
    script = plans / f"{stem}{variant}.workflow.mjs"
    script.write_text("x")
    files = [script]
    if receipt:
        r = plans / f"{script.name}.emitted.json"
        r.write_text(raw if raw is not None else json.dumps({"plan": plan_field or f"{stem}.md"}))
        files.append(r)
    for f in files:
        os.utime(f, (age, age))
    return files


def run(root, dry=True):
    return pe.prune_emitted_output(root, root / ".git", dry_run=dry)


def reasons(res, key):
    return {x["path"].rsplit("/", 1)[1]: x["reason"] for x in res[key]}


def test_orphan_truncated_stem_resolved_via_receipt(env):
    plans, _, root = env
    plan(plans, "2026-01-01-long-name.md", "implemented")
    emit(plans, "2026-01-01-long-na", ".ac9", plan_field="2026-01-01-long-name.md")
    res = run(root)
    assert {c["reason"] for c in res["candidates"]} == {"plan-not-executing"}
    emit(plans, "2026-02-02-gone")
    res = run(root)
    assert reasons(res, "candidates")["2026-02-02-gone.workflow.mjs"] == "plan-missing"


def test_variant_round2_pruned_and_unit_deleted_together(env):
    plans, _, root = env
    plan(plans, "p.md", "approved")
    files = emit(plans, "p", ".round2", plan_field="p.md")
    res = run(root, dry=False)
    assert len(res["pruned"]) == 2 and res["failed"] == []
    assert not any(f.exists() for f in files)


def test_dry_run_deletes_nothing(env):
    plans, _, root = env
    files = emit(plans, "gone")
    res = run(root)
    assert res["pruned"] == [] and len(res["candidates"]) == 2
    assert all(f.exists() for f in files)


def test_executing_retained(env):
    plans, _, root = env
    plan(plans, "p.md", "executing")
    emit(plans, "p")
    assert reasons(run(root), "retained")["p.workflow.mjs"] == "plan-executing"


def test_live_claim_and_raising_probe_retained(env):
    plans, state, root = env
    plan(plans, "a.md", "implemented")
    plan(plans, "b.md", "implemented")
    emit(plans, "a")
    emit(plans, "b")
    state["live"].add("a.md")
    state["raise_live"].add("b.md")
    got = reasons(run(root), "retained")
    assert got["a.workflow.mjs"] == "live-claim" and got["b.workflow.mjs"] == "live-claim"


def test_stranded_and_stamped(env):
    plans, _, root = env
    plan(plans, "p.md", "approved")
    emit(plans, "p")
    assert reasons(run(root), "retained")["p.workflow.mjs"] == "stranded-run"
    plan(plans, "p.md", "approved", stamp="abc123")
    assert reasons(run(root), "candidates")["p.workflow.mjs"] == "plan-not-executing"


def test_variant_receipt_alone_not_stranded(env):
    plans, _, root = env
    plan(plans, "p.md", "approved")
    emit(plans, "p", ".round2", plan_field="p.md")
    assert reasons(run(root), "candidates")["p.round2.workflow.mjs"] == "plan-not-executing"


def test_fresh_retained(env):
    plans, _, root = env
    plan(plans, "p.md", "implemented")
    emit(plans, "p", age=time.time())
    assert reasons(run(root), "retained")["p.workflow.mjs"] == "fresh-emission"


def test_tracked_retained(env):
    plans, state, root = env
    plan(plans, "p.md", "implemented")
    emit(plans, "p")
    state["tracked"] = {"p.workflow.mjs": (0o100644, "sha")}
    res = run(root, dry=False)
    assert reasons(res, "retained")["p.workflow.mjs"] == "tracked-at-head"
    assert (plans / "p.workflow.mjs").exists() and (plans / "p.workflow.mjs.emitted.json").exists()


def test_head_read_none_deletes_nothing(env):
    plans, state, root = env
    files = emit(plans, "gone")
    state["tracked"] = None
    res = run(root, dry=False)
    assert res["tracked_state_unknown"] is True and res["pruned"] == []
    assert all(f.exists() for f in files)


def test_spine_without_plans_key_deletes_nothing(env, monkeypatch):
    plans, _, root = env
    files = emit(plans, "gone")
    monkeypatch.setattr(pe, "read_tree_spine", lambda r, p: {"": {}})
    res = run(root, dry=False)
    assert res["tracked_state_unknown"] is True and res["pruned"] == []
    assert all(f.exists() for f in files)


@pytest.mark.parametrize(
    "kw",
    [
        {"raw": "{not json"},
        {"raw": json.dumps({"other": 1})},
        {"raw": json.dumps({"plan": 5})},
        {"raw": json.dumps({"plan": "sub/dir/p.md"})},
        {"raw": json.dumps({"plan": "..\\p.md"})},
    ],
)
def test_malformed_receipt_falls_back_to_primary(env, kw):
    plans, _, root = env
    plan(plans, "p.md", "implemented")
    emit(plans, "p", **kw)
    res = run(root)
    assert {c["plan"] for c in res["candidates"]} == {"p.md"}


def test_receipt_plan_unreadable_retained(env, monkeypatch):
    plans, _, root = env
    (plans / "locked.md").mkdir()  # exists but read_text raises OSError
    emit(plans, "p", plan_field="locked.md")
    assert reasons(run(root), "retained")["p.workflow.mjs"] == "plan-unreadable"


def test_no_yaml_parse(env, monkeypatch):
    import yaml

    def boom(*a, **k):
        raise AssertionError("yaml parsed")

    monkeypatch.setattr(yaml, "safe_load", boom)
    plans, _, root = env
    plan(plans, "p.md", "implemented")
    emit(plans, "p")
    assert len(run(root)["candidates"]) == 2


def test_plan_read_once_per_plan(env, monkeypatch):
    plans, _, root = env
    plan(plans, "p.md", "implemented")
    for v in (".a", ".b", ".c"):
        emit(plans, "p", v, plan_field="p.md")
    calls = []
    real = pe._read_plan
    monkeypatch.setattr(pe, "_read_plan", lambda p: (calls.append(p), real(p))[1])
    run(root)
    assert len(calls) == 1


def test_zero_spawns(env, monkeypatch):
    plans, _, root = env
    emit(plans, "gone")

    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    res = run(root, dry=False)
    assert len(res["pruned"]) == 2


def test_single_scandir(env, monkeypatch):
    plans, _, root = env
    emit(plans, "gone")
    calls = []
    real = os.scandir
    monkeypatch.setattr(os, "scandir", lambda p: (calls.append(str(p)), real(p))[1])
    run(root)
    assert sum(1 for c in calls if c.endswith("plans")) == 1


def test_unlink_oserror_goes_to_failed(env, monkeypatch):
    plans, _, root = env
    emit(plans, "gone")
    real = Path.unlink

    def flaky(self, *a, **k):
        if self.name.endswith(".mjs"):
            raise PermissionError("nope")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", flaky)
    res = run(root, dry=False)
    assert len(res["failed"]) == 1 and len(res["pruned"]) == 1


def test_hundred_units_under_budget(env):
    plans, _, root = env
    for i in range(30):
        plan(plans, f"plan{i}.md", "implemented")
    for i in range(100):
        emit(plans, f"plan{i % 30}", f".v{i}", plan_field=f"plan{i % 30}.md")
    t0 = time.process_time()
    res = run(root)
    assert time.process_time() - t0 < 0.5
    assert len(res["candidates"]) == 200

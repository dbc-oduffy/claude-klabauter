"""fleet.prune_emitted_output state/** sweep: fire-*.mjs and *.mjs.emitted.json."""

from __future__ import annotations

import json
import os
import time

import pytest

from coordinator_core.ops.fleet import prune_emitted as pe

OLD = time.time() - 3 * 86400
BLITZ = "state/plan-blitz/20261001T000000Z"


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    state = {"tracked": set(), "live": set(), "spine_calls": 0}

    def fake_spine(repo, paths):
        state["spine_calls"] += 1
        out = {"": {}}
        for p in paths:
            d = p.rsplit("/", 1)[0]
            out[d] = {
                n.rsplit("/", 1)[1]: (0o100644, "sha")
                for n in state["tracked"]
                if n.rsplit("/", 1)[0] == d
            }
        return out

    monkeypatch.setattr(pe, "read_tree_spine", fake_spine)
    monkeypatch.setattr(pe, "_is_claim_live", lambda c, p: p.name in state["live"])
    return tmp_path, state


def plan(root, name, status):
    (root / "docs" / "plans" / name).write_text(f"---\nstatus: {status}\n---\nb\n", encoding="utf-8")


def fire(root, rel_dir, stem, plan_field, age=OLD, script=True):
    d = root / rel_dir
    d.mkdir(parents=True, exist_ok=True)
    files = []
    if script:
        files.append(d / f"{stem}.mjs")
        files[-1].write_text("x")
    receipt = d / f"{stem}.mjs.emitted.json"
    receipt.write_text(json.dumps({"plan": plan_field}))
    files.append(receipt)
    for f in files:
        os.utime(f, (age, age))
    return files


def run(root, dry=False):
    return pe.prune_emitted_output(root, root / ".git", dry_run=dry)


def reasons(res, key):
    return {x["path"].rsplit("/", 1)[1]: x["reason"] for x in res[key]}


# fire-*.mjs: the script and its receipt are one unit.


def test_fire_deleted_when_owner_implemented_and_old(env):
    root, _ = env
    plan(root, "p.md", "implemented")
    files = fire(root, BLITZ, "fire-0-1", "p.md")
    res = run(root)
    assert sorted(res["pruned"]) == sorted(f"{BLITZ}/{f.name}" for f in files)
    assert not any(f.exists() for f in files)


def test_fire_abandoned_owner_deleted(env):
    root, _ = env
    plan(root, "p.md", "abandoned")
    fire(root, BLITZ, "fire-0-1", "p.md")
    assert reasons(run(root, dry=True), "candidates")["fire-0-1.mjs"] == "plan-abandoned"


def test_fire_tracked_kept(env):
    root, state = env
    plan(root, "p.md", "implemented")
    files = fire(root, BLITZ, "fire-0-1", "p.md")
    state["tracked"].add(f"{BLITZ}/fire-0-1.mjs")
    res = run(root)
    assert reasons(res, "retained")["fire-0-1.mjs"] == "tracked-at-head"
    assert all(f.exists() for f in files) and res["pruned"] == []


def test_fire_too_young_kept(env):
    root, _ = env
    plan(root, "p.md", "implemented")
    files = fire(root, BLITZ, "fire-0-1", "p.md", age=time.time() - 3600)
    assert reasons(run(root), "retained")["fire-0-1.mjs"] == "too-young"
    assert all(f.exists() for f in files)


def test_fire_owner_open_kept(env):
    root, _ = env
    plan(root, "p.md", "executing")
    files = fire(root, BLITZ, "fire-0-1", "p.md")
    assert reasons(run(root), "retained")["fire-0-1.mjs"] == "owner-open"
    assert all(f.exists() for f in files)


def test_fire_null_plan_or_missing_plan_or_no_receipt_kept(env):
    root, _ = env
    fire(root, BLITZ, "fire-0-1", None)
    fire(root, BLITZ, "fire-0-2", "gone.md")
    fire(root, BLITZ, "fire-0-3", "p.md", script=True)
    (root / BLITZ / "fire-0-3.mjs.emitted.json").unlink()
    got = reasons(run(root), "retained")
    assert got["fire-0-1.mjs"] == got["fire-0-2.mjs"] == got["fire-0-3.mjs"] == "owner-unresolved"


# *.mjs.emitted.json receipts (non-fire): a receipt alone is the unit.


def test_receipt_deleted_when_owner_closed_and_old(env):
    root, _ = env
    plan(root, "p.md", "implemented")
    files = fire(root, "state/scratch/warp", "c9-m.workflow", "p.md", script=False)
    res = run(root)
    assert res["pruned"] == ["state/scratch/warp/c9-m.workflow.mjs.emitted.json"]
    assert not files[0].exists()


def test_receipt_tracked_kept(env):
    root, state = env
    plan(root, "p.md", "implemented")
    files = fire(root, "state/scratch/warp", "c9-m.workflow", "p.md", script=False)
    state["tracked"].add("state/scratch/warp/c9-m.workflow.mjs.emitted.json")
    res = run(root)
    assert reasons(res, "retained")["c9-m.workflow.mjs.emitted.json"] == "tracked-at-head"
    assert files[0].exists()


def test_receipt_too_young_kept(env):
    root, _ = env
    plan(root, "p.md", "implemented")
    files = fire(root, "state/scratch/warp", "c9-m.workflow", "p.md", age=time.time() - 3600, script=False)
    assert reasons(run(root), "retained")["c9-m.workflow.mjs.emitted.json"] == "too-young"
    assert files[0].exists()


def test_receipt_owner_open_or_live_kept(env):
    root, state = env
    plan(root, "open.md", "approved")
    plan(root, "live.md", "implemented")
    state["live"].add("live.md")
    fire(root, "state/scratch/warp", "a.workflow", "open.md", script=False)
    fire(root, "state/scratch/warp", "b.workflow", "live.md", script=False)
    got = reasons(run(root), "retained")
    assert got["a.workflow.mjs.emitted.json"] == "owner-open"
    assert got["b.workflow.mjs.emitted.json"] == "live-claim"


def test_unreadable_head_keeps_state_files(env, monkeypatch):
    root, _ = env
    plan(root, "p.md", "implemented")
    files = fire(root, BLITZ, "fire-0-1", "p.md")
    monkeypatch.setattr(pe, "read_tree_spine", lambda r, p: None)
    res = run(root)
    assert res["tracked_state_unknown"] is True and res["pruned"] == []
    assert all(f.exists() for f in files)


def test_single_spine_call_covers_plans_and_state(env):
    root, state = env
    plan(root, "p.md", "implemented")
    for i in range(5):
        fire(root, f"{BLITZ}-{i}", "fire-0-1", "p.md")
    (root / "docs" / "plans" / "p.workflow.mjs").write_text("x")
    run(root, dry=True)
    assert state["spine_calls"] == 1


# `<name>.mjs` + `<name>.mjs.emitted.json` (non-fire) pair: both go or both stay.

WARP = "state/scratch/warp"


def test_pair_deleted_together(env):
    root, _ = env
    plan(root, "p.md", "implemented")
    files = fire(root, WARP, "c9-m.workflow", "p.md")
    res = run(root)
    assert sorted(res["pruned"]) == sorted(f"{WARP}/{f.name}" for f in files)
    assert not any(f.exists() for f in files)


def test_pair_tracked_script_keeps_both(env):
    root, state = env
    plan(root, "p.md", "implemented")
    files = fire(root, WARP, "c9-m.workflow", "p.md")
    state["tracked"].add(f"{WARP}/c9-m.workflow.mjs")
    res = run(root)
    assert res["pruned"] == [] and all(f.exists() for f in files)
    assert set(reasons(res, "retained").values()) == {"tracked-at-head"}


def test_pair_young_script_keeps_both(env):
    root, _ = env
    plan(root, "p.md", "implemented")
    files = fire(root, WARP, "c9-m.workflow", "p.md")
    os.utime(files[0], None)
    res = run(root)
    assert res["pruned"] == [] and all(f.exists() for f in files)
    assert set(reasons(res, "retained").values()) == {"too-young"}


def test_bare_non_fire_script_untouched(env):
    root, _ = env
    d = root / WARP
    d.mkdir(parents=True)
    s = d / "lone.workflow.mjs"
    s.write_text("x")
    os.utime(s, (OLD, OLD))
    res = run(root)
    assert s.exists() and res["pruned"] == [] and res["retained"] == []


def _landing(root, rel_dir, name, age):
    f = root / rel_dir / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{}")
    os.utime(f, (age, age))


def _paths(res, key):
    return {r["path"] for r in res[key]}


def test_blitz_fire_closed_by_its_waves_landing(env):
    root, _ = env
    fire(root, BLITZ, "fire-2-1", None)
    fire(root, BLITZ, "fire-3-1", None)
    _landing(root, BLITZ, "wave-2.landing.json", OLD)
    res = run(root)
    assert _paths(res, "candidates") == {f"{BLITZ}/fire-2-1.mjs", f"{BLITZ}/fire-2-1.mjs.emitted.json"}
    assert {r["reason"] for r in res["candidates"]} == {"landed"}
    assert {r["reason"] for r in res["retained"]} == {"owner-unresolved"}
    assert len(res["retained"]) == 2


def test_blitz_closed_fire_without_receipt_is_deleted(env):
    root, _ = env
    d = root / BLITZ
    d.mkdir(parents=True)
    (d / "fire-1-1.mjs").write_text("x")
    os.utime(d / "fire-1-1.mjs", (OLD, OLD))
    _landing(root, BLITZ, "wave-1.landing.json", OLD)
    assert _paths(run(root, dry=True), "candidates") == {f"{BLITZ}/fire-1-1.mjs"}


def test_blitz_young_closed_fire_kept(env):
    root, _ = env
    fire(root, BLITZ, "fire-1-1", None, age=time.time())
    _landing(root, BLITZ, "wave-1.landing.json", OLD)
    res = run(root)
    assert not res["candidates"]
    assert {r["reason"] for r in res["retained"]} == {"too-young"}


def test_blitz_hash_suffixed_landing_does_not_close(env):
    root, _ = env
    fire(root, BLITZ, "fire-1-1", None)
    _landing(root, BLITZ, "wave-1.landing.abc123.json", OLD)
    assert not run(root)["candidates"]


def test_blitz_repair_fire_with_newer_landing_deleted(env):
    root, _ = env
    fire(root, BLITZ, "repair-fire-1", None, age=OLD)
    _landing(root, BLITZ, "wave-4.landing.json", OLD + 3600)
    res = run(root)
    assert len(res["candidates"]) == 2
    assert {r["reason"] for r in res["candidates"]} == {"landed"}


def test_blitz_repair_fire_with_older_landing_kept(env):
    root, _ = env
    fire(root, BLITZ, "repair-fire-1", None, age=OLD)
    _landing(root, BLITZ, "wave-4.landing.json", OLD - 3600)
    res = run(root)
    assert not res["candidates"]
    assert {r["reason"] for r in res["retained"]} == {"owner-unresolved"}


def test_blitz_landing_does_not_override_live_claim(env):
    root, state = env
    plan(root, "p.md", "executing")
    state["live"].add("p.md")
    fire(root, BLITZ, "fire-1-1", "p.md")
    _landing(root, BLITZ, "wave-1.landing.json", OLD)
    res = run(root)
    assert not res["candidates"]
    assert {r["reason"] for r in res["retained"]} == {"live-claim"}

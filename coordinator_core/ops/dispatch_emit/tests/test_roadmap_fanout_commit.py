"""dispatch.terminal_commit's roadmap fan-out record mode: land the planning record, fan plans out."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    FANOUT_RECORD_PARAM,
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
)
from coordinator_core.ops.sizing_intake_contract import intake_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261016T000000-aaaaaa"
PLANS = {"a": "docs/plans/2026-10-16-a.md", "b": "docs/plans/2026-10-16-b.md"}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "concurrent_run_budget", lambda physical_cores=None: 4)
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "run.mjs").write_text("// script\n", encoding="utf-8")
    return root


def _make_plan(root: Path, name: str, with_sizing: bool = True) -> None:
    sizing = f"state/sizings/2026-10-16-{name}.yaml"
    baton = f"state/handoffs/2026-10-16-{name}.md"
    fm = f"---\nsizing_object: {sizing}\n---\n\n" if with_sizing else ""
    plan = root / PLANS[name]
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        fm + "## Tasks\n\n```yaml plan-tasks\n"
        f"- id: {name.upper()}1\n  title: T\n  disposition: open\n  writes:\n    - pkg/{name}.py\n```\n",
        encoding="utf-8", newline="\n",
    )
    if not with_sizing:
        return
    rec = intake_record(name=name, turn={"text": "do the thing", "turn": 1}, today="2026-10-16")
    rec["plan"] = PLANS[name]
    rec["baton"] = baton
    rec.setdefault("interaction_mode", "pm")
    (root / sizing).parent.mkdir(parents=True, exist_ok=True)
    (root / sizing).write_text(yaml.safe_dump(rec, sort_keys=False), encoding="utf-8", newline="\n")
    (root / baton).parent.mkdir(parents=True, exist_ok=True)
    (root / baton).write_text(
        f"---\ntitle: b\nstatus: claimed\nkind: session-handoff\n---\n\n# baton {name}\n", encoding="utf-8"
    )


def _record(root: Path) -> None:
    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True, exist_ok=True)
    for name in ("ask.md", "run.json"):
        (rec / name).write_text(f"{name}\n", encoding="utf-8")


def _call(root: Path, plans, waves=None, **extra) -> dict:
    spec = {"plans": plans, **({"waves": waves} if waves is not None else {})}
    params = {"script_path": "run.mjs", "run_id": RUN, FANOUT_RECORD_PARAM: spec, **extra}
    return terminal_commit._handler(params, repo_root=root / ".git")


def test_lands_record_plans_sizings_batons_then_fans_out(repo):
    _make_plan(repo, "a")
    _make_plan(repo, "b")
    _record(repo)
    token = run_token.mint(repo, RUN, "run.mjs")

    out = _call(repo, list(PLANS.values()), run_token=token)

    assert out["committed"] is True, out
    names = set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split())
    assert names == {
        *PLANS.values(),
        "state/sizings/2026-10-16-a.yaml", "state/sizings/2026-10-16-b.yaml",
        "state/handoffs/2026-10-16-a.md", "state/handoffs/2026-10-16-b.md",
        f"{RECORD_DIR_ROOT}/{RUN}/ask.md", f"{RECORD_DIR_ROOT}/{RUN}/run.json",
    }
    fan = out["fanout"]
    assert fan["exit_code"] == 0, fan
    parent = json.loads((repo / c.parent_record_rel(fan["parent_id"])).read_text(encoding="utf-8"))
    sizings = sorted(r["slice_sizing"] for r in parent["runs"])
    assert sizings == ["state/sizings/2026-10-16-a.yaml", "state/sizings/2026-10-16-b.yaml"]
    assert sorted(r["sizing"] for r in fan["ready"]) == sizings
    assert len(fan["fire"]) == 2
    assert not run_token.is_live(repo, RUN)
    assert not run_token.redeem(repo, RUN, token, "run.mjs")


def _declare_run(repo: Path, run_id: str, session: str, writes: list) -> str:
    from coordinator_core.session import core as session_core

    session_core.init(session, cwd=str(repo))
    rdir = repo / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "manifest.json").write_text(json.dumps({"rows": [{"writes": writes}]}), encoding="utf-8")
    return run_token.mint(repo, run_id, "run.mjs", session_id=session)


def test_a_product_a_live_peer_run_declares_is_withheld(repo):
    _make_plan(repo, "a")
    _record(repo)
    _declare_run(repo, "peer-run-1", "fanout-peer", [PLANS["a"]])

    out = _call(repo, [PLANS["a"]])

    assert out["withheld"] == {PLANS["a"]: "peer-held"}, out


def test_a_product_the_fanout_runs_own_manifest_declares_is_committed(repo):
    _make_plan(repo, "a")
    _record(repo)
    token = _declare_run(repo, RUN, "fanout-self", [PLANS["a"]])

    out = _call(repo, [PLANS["a"]], run_token=token, session_id="not-a-uuid")

    assert out["committed"] is True, out
    assert "withheld" not in out, out
    assert PLANS["a"] in _git(["show", "--name-only", "--format=", "HEAD"], repo).split()


def _parent_runs(repo, out) -> list:
    fan = out["fanout"]
    assert fan["exit_code"] == 0, fan
    parent = json.loads((repo / c.parent_record_rel(fan["parent_id"])).read_text(encoding="utf-8"))
    return parent["runs"]


def test_two_wave_record_orders_the_later_run_after_the_earlier(repo):
    _make_plan(repo, "a")
    _make_plan(repo, "b")
    _record(repo)
    out = _call(repo, [PLANS["a"], PLANS["b"]], waves={PLANS["a"]: 0, PLANS["b"]: 1})
    assert out["committed"] is True, out
    by = {r["slice_sizing"]: r for r in _parent_runs(repo, out)}
    first, second = by["state/sizings/2026-10-16-a.yaml"], by["state/sizings/2026-10-16-b.yaml"]
    assert second["status"] == "after" and first["run_id"] in str(second)
    assert first["status"] != "after"


def test_record_without_waves_fans_out_unordered(repo):
    _make_plan(repo, "a")
    _make_plan(repo, "b")
    _record(repo)
    out = _call(repo, [PLANS["a"], PLANS["b"]])
    assert out["committed"] is True, out
    assert all(r["status"] != "after" for r in _parent_runs(repo, out))


@pytest.mark.parametrize("waves", [{"docs/plans/zzz.md": 0}, {PLANS["a"]: "1"}, {PLANS["a"]: True}, {PLANS["a"]: -1}, ["x"]])
def test_bad_waves_refuse_before_the_commit(repo, waves):
    _make_plan(repo, "a")
    _record(repo)
    head = _git(["rev-parse", "HEAD"], repo)
    out = _call(repo, [PLANS["a"]], waves=waves)
    assert out["committed"] is False and out["refused"] == "fanout-waves"
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_plan_without_sizing_refuses_before_any_write(repo):
    _make_plan(repo, "a")
    _make_plan(repo, "b", with_sizing=False)
    _record(repo)
    head = _git(["rev-parse", "HEAD"], repo)
    token = run_token.mint(repo, RUN, "run.mjs")

    out = _call(repo, list(PLANS.values()), run_token=token)

    assert out["committed"] is False and out["refused"] == "fanout-no-sizing"
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert not (repo / "scratch" / "warp").exists()
    assert run_token.is_live(repo, RUN) or run_token.redeem(repo, RUN, token, "run.mjs")


def test_halted_and_fanout_together_refuse(repo):
    _make_plan(repo, "a")
    out = _call(repo, [PLANS["a"]], **{HALTED_RECORD_PARAM: {"halted": "refusal"}})
    assert out["committed"] is False and out["refused"] == "mode-ambiguous"


def _parent_sizing(root: Path, mode) -> str:
    rel = "state/sizings/2026-10-16-parent.yaml"
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(yaml.safe_dump({"interaction_mode": mode} if mode else {"name": "p"}), encoding="utf-8")
    return rel


def _call_spec(root: Path, plans, **spec_extra) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, FANOUT_RECORD_PARAM: {"plans": plans, **spec_extra}}
    return terminal_commit._handler(params, repo_root=root / ".git")


def test_interaction_mode_of_the_parent_sizing_reaches_the_fanout_params(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_fanout

    seen: dict = {}

    def fake(params, root):
        seen.update(params)
        return {"exit_code": 0, "runs": [], "ready": [], "fire": []}

    monkeypatch.setattr(ask_fanout, "fanout", fake)
    _make_plan(repo, "a")
    _record(repo)
    out = _call_spec(repo, [PLANS["a"]], **{c.PARENT_SIZING_KEY: _parent_sizing(repo, "pm")})
    assert out["committed"] is True, out
    assert seen[c.INTERACTION_MODE_PARAM] == "pm"


def test_interaction_mode_is_not_passed_when_the_parent_records_none(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_fanout

    seen: dict = {}
    monkeypatch.setattr(ask_fanout, "fanout", lambda p, r: seen.update(p) or {"exit_code": 0, "ready": [], "runs": []})
    _make_plan(repo, "a")
    _record(repo)
    _call_spec(repo, [PLANS["a"]], **{c.PARENT_SIZING_KEY: _parent_sizing(repo, None)})
    assert c.INTERACTION_MODE_PARAM not in seen


@pytest.mark.parametrize("parent", ["state/sizings/missing.yaml", "docs/plans/2026-10-16-a.md", "../x.yaml", 5])
def test_bad_parent_sizing_refuses_before_the_commit_interaction_mode(repo, parent):
    _make_plan(repo, "a")
    _record(repo)
    head = _git(["rev-parse", "HEAD"], repo)
    out = _call_spec(repo, [PLANS["a"]], **{c.PARENT_SIZING_KEY: parent})
    assert out["committed"] is False and out["refused"] == "fanout-parent-sizing"
    assert _git(["rev-parse", "HEAD"], repo) == head


@pytest.mark.parametrize("bad", ["x", [{"reason": "nope", "plan": "p"}], [{"reason": "pulled"}], [3]])
def test_malformed_not_fired_refuses_before_the_commit(repo, bad):
    _make_plan(repo, "a")
    _record(repo)
    head = _git(["rev-parse", "HEAD"], repo)
    out = _call_spec(repo, [PLANS["a"]], **{c.NOT_FIRED_KEY: bad})
    assert out["committed"] is False and out["refused"] == "fanout-not-fired"
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_not_fired_echoes_upstream_entries_and_names_after_plans(repo):
    _make_plan(repo, "a")
    _make_plan(repo, "b")
    _record(repo)
    upstream = {"plan": "docs/plans/2026-10-16-hq-01.md", "reason": "pulled", "wave": 0}
    out = _call_spec(
        repo, [PLANS["a"], PLANS["b"]], waves={PLANS["a"]: 0, PLANS["b"]: 1}, **{c.NOT_FIRED_KEY: [upstream]},
    )
    assert out["committed"] is True, out
    nf = out["fanout"][c.NOT_FIRED_KEY]
    assert nf[0] == upstream
    assert [e["plan"] for e in nf[1:]] == [PLANS["b"]]
    assert nf[1]["reason"] == "after" and nf[1]["wave"] == 1 and "ask-" in nf[1]["detail"]
    assert all(e["reason"] in c.NOT_FIRED_REASONS for e in nf)
    fired = {r["sizing"] for r in out["fanout"]["ready"]}
    assert fired == {"state/sizings/2026-10-16-a.yaml"}


def test_not_fired_names_every_plan_when_fanout_refuses(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_fanout

    monkeypatch.setattr(ask_fanout, "fanout", lambda p, r: {"exit_code": 1, "applied": False, "error": "boom"})
    _make_plan(repo, "a")
    _make_plan(repo, "b")
    _record(repo)
    out = _call_spec(repo, list(PLANS.values()))
    assert out["committed"] is True
    nf = out["fanout"][c.NOT_FIRED_KEY]
    assert [(e["plan"], e["reason"], e["detail"]) for e in nf] == [(p, "fanout-refused", "boom") for p in PLANS.values()]


def test_not_fired_names_a_queued_plan(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_fanout

    monkeypatch.setattr(
        ask_fanout, "fanout",
        lambda p, r: {"exit_code": 0, "ready": [], "runs": [{"run_id": "r1", "label": "2026-10-16-a"}]},
    )
    _make_plan(repo, "a")
    _record(repo)
    out = _call_spec(repo, [PLANS["a"]])
    assert [(e["plan"], e["reason"]) for e in out["fanout"][c.NOT_FIRED_KEY]] == [(PLANS["a"], "queued")]


def test_not_fired_names_every_plan_when_nothing_to_commit(repo):
    _make_plan(repo, "a")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "plans"], repo)
    out = _call_spec(repo, [PLANS["a"]])
    assert out["nothing_to_commit"] is True, out
    assert [(e["plan"], e["reason"]) for e in out["fanout"][c.NOT_FIRED_KEY]] == [(PLANS["a"], "nothing-to-commit")]


def test_record_and_plans_already_at_head_fan_out_in_wave_order(repo):
    _make_plan(repo, "a")
    _make_plan(repo, "b")
    _record(repo)
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "record"], repo)
    head = _git(["rev-parse", "HEAD"], repo).strip()
    token = run_token.mint(repo, RUN, "run.mjs")

    out = _call(repo, [PLANS["a"], PLANS["b"]], waves={PLANS["a"]: 0, PLANS["b"]: 1}, run_token=token)

    assert out["committed"] is False and out["sha"] == head, out
    assert out["fanned_out_at_head"] is True, out
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
    by = {r["slice_sizing"]: r for r in _parent_runs(repo, out)}
    first, second = by["state/sizings/2026-10-16-a.yaml"], by["state/sizings/2026-10-16-b.yaml"]
    assert second["status"] == "after" and first["run_id"] in str(second)
    assert first["status"] != "after"
    assert len(out["fanout"]["fire"]) == 1
    assert not run_token.is_live(repo, RUN)


def test_a_withheld_plan_blocks_the_head_fanout(repo):
    _make_plan(repo, "a")
    _record(repo)
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "record"], repo)
    (repo / PLANS["a"]).write_text((repo / PLANS["a"]).read_text(encoding="utf-8") + "\nedit\n", encoding="utf-8")
    _declare_run(repo, "peer-run-1", "fanout-peer", [PLANS["a"]])

    out = _call(repo, [PLANS["a"]])

    assert out["withheld"] == {PLANS["a"]: "peer-held"}, out
    assert "fanned_out_at_head" not in out and "parent_id" not in out["fanout"], out
    assert [(e["plan"], e["reason"]) for e in out["fanout"][c.NOT_FIRED_KEY]] == [(PLANS["a"], "nothing-to-commit")]


def test_a_record_absent_from_head_blocks_the_head_fanout(repo):
    _make_plan(repo, "a")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "plans only"], repo)

    out = _call(repo, [PLANS["a"]])

    assert out["nothing_to_commit"] is True and "fanned_out_at_head" not in out, out
    assert [(e["plan"], e["reason"]) for e in out["fanout"][c.NOT_FIRED_KEY]] == [(PLANS["a"], "nothing-to-commit")]


def test_not_fired_names_every_plan_when_the_commit_does_not_land(repo, monkeypatch):
    from coordinator_core.warm import entry_seam

    monkeypatch.setattr(entry_seam, "reentrant_dispatch", lambda *a, **k: {"committed": False, "error": "gate said no"})
    _make_plan(repo, "a")
    _record(repo)
    out = _call_spec(repo, [PLANS["a"]])
    assert out["committed"] is False
    assert [(e["reason"], e["detail"]) for e in out["fanout"][c.NOT_FIRED_KEY]] == [("commit-not-landed", "gate said no")]


def test_plan_outside_plans_root_refuses(repo):
    out = _call(repo, ["seed.txt"])
    assert out["committed"] is False and out["refused"] == "fanout-plan-path"


def test_run_mode_in_the_spec_is_inherited_when_the_parent_sizing_has_none(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_fanout

    seen: dict = {}
    monkeypatch.setattr(ask_fanout, "fanout", lambda p, r: seen.update(p) or {"exit_code": 0, "ready": [], "runs": []})
    _make_plan(repo, "a")
    _record(repo)
    _call_spec(
        repo, [PLANS["a"]],
        **{c.PARENT_SIZING_KEY: _parent_sizing(repo, None), c.INTERACTION_MODE_PARAM: "ceo"},
    )
    assert seen[c.INTERACTION_MODE_PARAM] == "ceo"


def test_parent_sizing_mode_wins_over_the_spec_run_mode(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_fanout

    seen: dict = {}
    monkeypatch.setattr(ask_fanout, "fanout", lambda p, r: seen.update(p) or {"exit_code": 0, "ready": [], "runs": []})
    _make_plan(repo, "a")
    _record(repo)
    _call_spec(
        repo, [PLANS["a"]],
        **{c.PARENT_SIZING_KEY: _parent_sizing(repo, "pm"), c.INTERACTION_MODE_PARAM: "ceo"},
    )
    assert seen[c.INTERACTION_MODE_PARAM] == "pm"

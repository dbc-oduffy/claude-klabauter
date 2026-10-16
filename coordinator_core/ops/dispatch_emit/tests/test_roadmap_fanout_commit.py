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


def _call(root: Path, plans, **extra) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, FANOUT_RECORD_PARAM: {"plans": plans}, **extra}
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


def test_plan_outside_plans_root_refuses(repo):
    out = _call(repo, ["seed.txt"])
    assert out["committed"] is False and out["refused"] == "fanout-plan-path"

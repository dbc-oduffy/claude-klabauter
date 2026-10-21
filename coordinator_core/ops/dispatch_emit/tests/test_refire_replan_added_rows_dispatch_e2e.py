"""A re-fire whose plan-blitz re-plan adds rows stages them on that same fire: the gate reads C20 as landed,
the re-plan adds C22-C24 under the open slot leg C21, and the stage called with the gate's resume_rows keeps them."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, ask_stage
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PLAN_REL = "docs/plans/2026-10-17-refire-added-rows.md"
RUN_ID = "warp-2026-10-17-refire-added-rows-20261017T050000-a1b2c3"


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())


def _row(rid, disposition="open", deps=(), slot=False):
    dep = "".join(
        f"    - chunk: {d}\n      gate_kind: output-consumption-runtime\n" for d in deps
    )
    return (
        f"- id: {rid}\n  title: T{rid}\n  change_kind: code-edit\n  surface: some/{rid}.py\n"
        f"  disposition: {disposition}\n  writes:\n    - some/{rid}.py\n"
        + ("  needs_slot: true\n" if slot else "")
        + (f"  depends_on:\n{dep}" if deps else "")
    )


def _write_plan(repo, rows):
    path = repo / PLAN_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{''.join(rows)}```\n",
        encoding="utf-8",
        newline="\n",
    )


def test_a_refire_whose_replan_adds_rows_stages_them_on_that_fire(repo):  # noqa: F811
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _write_plan(repo, [_row("C20"), _row("C21", slot=True)])
    _put(repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAN_REL)
    (repo / "some").mkdir(exist_ok=True)
    (repo / "some" / "C20.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "some" / "C20.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "checkpoint(wave 1): 1 row — C20", "-m",
         f"Checkpoint-Plan: {PLAN_REL}\nCheckpoint-Base: abc1234")
    rl.start(repo, RUN_ID)
    rl.revise(repo, RUN_ID, note="the revise adds work the plan lacks")

    reply = ask_gate._handler({"sizing_path": REL, "run_id": RUN_ID}, repo)
    assert reply.get("halt") is None, reply
    assert reply["resume_plan"] == PLAN_REL
    assert reply["resume_rows"] == ["C21"], reply

    _write_plan(
        repo,
        [
            _row("C20"),
            _row("C21", deps=("C22", "C23", "C24"), slot=True),
            _row("C22"),
            _row("C23"),
            _row("C24"),
        ],
    )
    staged = read_stage_reply(
        repo,
        ask_stage._handler(
            {"run_id": RUN_ID, "plan_path": PLAN_REL, "only_rows": reply["resume_rows"]}, repo_root=repo
        ),
    )
    assert "error" not in staged, staged
    rows = {r["id"]: r for r in staged["rows"]}
    assert set(rows) == {"C21", "C22", "C23", "C24"}, f"re-plan-added rows missing from the manifest: {sorted(rows)}"
    assert set(rows["C21"]["deps"]) == {"C22", "C23", "C24"}
    assert all(rows["C21"]["wave"] > rows[d]["wave"] for d in ("C22", "C23", "C24"))

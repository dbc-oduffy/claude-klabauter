"""A refire stages no row already done, and never reports a staged-but-unstarted row complete."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests._git_fixture import _git, seed_repo
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply

pytestmark = [pytest.mark.spawns_process]

PLAN_REL = "docs/plans/p.md"
RUN = "r1"
ALL_ROWS = ["A_CODED", "B_DONE", "C_LANDED", "D_UNSTARTED"]


def _row(rid: str, disposition: str | None = None) -> str:
    tail = f"  disposition: {disposition}\n" if disposition else ""
    if disposition in ("coded", "done"):
        tail += "  disposition_ref: abc1234\n"
    return (
        f"- id: {rid}\n  title: t{rid}\n  change_kind: doc-edit\n  surface: docs/x/{rid}.md\n"
        f"  writes:\n    - docs/x/{rid}.md\n{tail}"
    )


def _repo(tmp_path: Path, *, checkpoint: bool) -> Path:
    repo = seed_repo(tmp_path / "repo")
    rows = _row("A_CODED", "coded") + _row("B_DONE", "done") + _row("C_LANDED") + _row("D_UNSTARTED")
    plan = repo / PLAN_REL
    plan.parent.mkdir(parents=True)
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8", newline="\n"
    )
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "plan"], repo)
    if checkpoint:
        _git(
            [
                "commit", "-q", "--allow-empty", "-m", "checkpoint(wave 0): 1 rows — C_LANDED",
                "-m", f"Checkpoint-Plan: {PLAN_REL}",
            ],
            repo,
        )
    write_run_scripts(repo, RUN)
    return repo


def _refire(repo: Path) -> dict:
    """The refire's stage call: every plan row named as the resume set, no snapshot, default arguments."""
    reply = _handler({"run_id": RUN, "plan_path": PLAN_REL, "only_rows": ALL_ROWS}, repo_root=repo)
    return read_stage_reply(repo, reply)


def test_refire_stages_only_the_row_nothing_has_done(tmp_path: Path) -> None:
    reply = _refire(_repo(tmp_path, checkpoint=True))
    assert "error" not in reply, reply
    assert [r["id"] for r in reply["rows"]] == ["D_UNSTARTED"]


def test_refire_never_reports_the_unstarted_row_finished(tmp_path: Path) -> None:
    reply = _refire(_repo(tmp_path, checkpoint=True))
    staged = {r["id"] for r in reply["rows"]}
    assert "D_UNSTARTED" in staged
    assert "D_UNSTARTED" not in {r["id"] for r in reply.get("remaining_rows", [])}


def test_without_a_landed_checkpoint_the_open_row_is_still_staged(tmp_path: Path) -> None:
    reply = _refire(_repo(tmp_path, checkpoint=False))
    assert "error" not in reply, reply
    assert [r["id"] for r in reply["rows"]] == ["C_LANDED", "D_UNSTARTED"]


def test_a_checkpoint_of_another_plan_lands_nothing_here(tmp_path: Path) -> None:
    repo = _repo(tmp_path, checkpoint=False)
    _git(
        [
            "commit", "-q", "--allow-empty", "-m", "checkpoint(wave 0): 1 rows — C_LANDED",
            "-m", "Checkpoint-Plan: docs/plans/other.md",
        ],
        repo,
    )
    reply = _refire(repo)
    assert [r["id"] for r in reply["rows"]] == ["C_LANDED", "D_UNSTARTED"]

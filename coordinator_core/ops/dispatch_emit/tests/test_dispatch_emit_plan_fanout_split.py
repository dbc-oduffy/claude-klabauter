"""`--fanout split` on the emit `--plan` route, driven through invoke.from_argv's entrypoint runner.

An error reply (no `ok` key) from dispatch.emit is a non-zero exit with its text, never a KeyError.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import invoke_from_argv
from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.fanout_cost import read_fanout

_ENTRYPOINT = "emit-dispatch-workflow"


def _repo_with_plan(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    plan = repo / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    # Four writes per row puts each above half the chunk shaper's WORK_CEILING, so no two rows merge
    # into one before the fan-out grouping under test sees them.
    rows = "".join(
        f"- id: C{i}\n  title: Do thing {i}\n  writes: [a{i}_0.txt, a{i}_1.txt, a{i}_2.txt, a{i}_3.txt]\n"
        f"  body: |\n    Write the a{i} files.\n"
        for i in range(1, 5)
    )
    plan.write_text(f"---\n---\n\n# A plan\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8")
    return repo, plan


def _run(repo: Path, plan: Path, *extra: str) -> dict:
    return invoke_from_argv._run_entrypoint(_ENTRYPOINT, ["--plan", str(plan), *extra], str(repo))


@pytest.fixture(autouse=True)
def _admit_warm(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.dispatch_emit import admission

    # The entrypoint door refuses a bare `--plan` on a cold interpreter; the in-process runner is cold here.
    # Trap: a tree without the residency module has no cold refusal, so there is nothing to stub.
    try:
        from coordinator_core.warm import residency
    except ImportError:
        residency = None
    if residency is not None:
        monkeypatch.setattr(residency, "is_resident", lambda: True)
    monkeypatch.setattr(admission, "should_refuse_warm", lambda _root: None)


def test_split_on_the_plan_route_exits_clean_and_ungroups_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    repo, plan = _repo_with_plan(tmp_path)

    # A bare `--plan` argv is warm-only at the entrypoint door; the auto baseline goes through the in-process CLI.
    monkeypatch.chdir(repo)
    rc = cli_module.main(["--plan", str(plan), "--repo-root", str(repo)])
    assert rc == 0, capsys.readouterr().err
    grouped = read_fanout((plan.parent / "p.workflow.mjs").read_text(encoding="utf-8"))

    split = _run(repo, plan, "--fanout", "split", "--force")
    assert split["exit_code"] == 0, split["stderr"]
    assert "KeyError" not in split["stderr"]
    ungrouped = read_fanout((plan.parent / "p.workflow.mjs").read_text(encoding="utf-8"))

    # `<=`: whether the default plan route groups rows at all is the lane composer's call, not this fix's.
    assert grouped["expectedAgents"] <= ungrouped["expectedAgents"]
    assert not ungrouped["groups"] or all(len(g) == 1 for g in ungrouped["groups"])


def test_split_is_still_refused_on_the_ask_route() -> None:
    reply = op_module._dispatch_emit({"ask": "do x", "fanout_group": "split"})

    assert "queue and ask composers" in reply["error"]


def test_error_reply_without_ok_exits_nonzero_with_the_error_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(cli_module, "_dispatch_emit", lambda params, repo_root=None, **_: {"error": "refused: boom"})
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / "plan.md").write_text("# p\n", encoding="utf-8")

    rc = cli_module.main(["--plan", "plan.md", "--fanout", "split", "--repo-root", str(tmp_path)])

    assert rc == cli_module.EXIT_DATA_ERROR
    assert "refused: boom" in capsys.readouterr().err

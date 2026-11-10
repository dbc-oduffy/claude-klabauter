"""A review_blocked/failing_test halt over base reds offers a terminal commit that lands its finished rows;
an own-red halt offers none."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.wake_digest import TERMINAL_COMMIT_CLI_HELPER_JS
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-rh-e2e"
BASE_RED = "tests/test_old.py::test_red_at_base"
OWN_RED = "tests/test_a.py::test_own"
_PREFIX = "coordinator-invoke dispatch.terminal_commit --from-task-output"
_GETTER = "this.next_action.kind === 'terminal_commit' ? _terminalCommitCli(this.next_action.params) : null"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "wave-review.md").write_text("# review\n", encoding="utf-8")
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _halted_run(repo: Path, failing: list, pre_existing: list, action: dict) -> str:
    """A halted run with finished row C1 (a.py) and unfinished C2 (b.py), both dirty; its task-output path."""
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)), ChunkCommit(id="C2", title="t2", paths=("b.py",))),
        session_id=None,
    )
    script = repo / "scratch" / "warp" / f"{RUN}.workflow.mjs"
    script.parent.mkdir(parents=True)
    script.write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    _git(["add", "-N", "."], repo)
    digest = {
        "run_id": RUN,
        "halted_by": {"halted": "review_blocked", "blocker": "failing_test", "failing": failing},
        "incomplete": ["C2"],
        "ledger": {"items": [{"id": "R1", "status": "met"}]},
        "review": {
            "wave": [{"agent_type": "coordinator:reviewer", "verdict": "BLOCKED", "sidecar_path": "../wave-review.md",
                      "applied": 0}],
            "prep": None,
            "delivery": None,
            "reverify": {"status_before": "fail", "status_after": "fail", "failing": failing,
                         "pre_existing": pre_existing},
        },
        "next_action": action,
    }
    out = repo / "task-output.json"
    out.write_text(json.dumps(digest), encoding="utf-8")
    return str(out)


def _cli(next_action: dict) -> str | None:
    """`terminal_commit_cli` of a digest carrying ``next_action``: the shipped helper and getter run in node."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable to evaluate the digest getter")
    program = (
        TERMINAL_COMMIT_CLI_HELPER_JS
        + f"\nconst d = {{ next_action: {json.dumps(next_action)}, get terminal_commit_cli() {{ return {_GETTER}; }} }};\n"
        "console.log(JSON.stringify(d.terminal_commit_cli));\n"
    )
    proc = subprocess.run(
        [node, "-e", program], capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


_PROMOTED = {"kind": "terminal_commit", "op": "dispatch.terminal_commit", "params": {"incomplete_chunks": ["C2"], "held": []}}
_REFIRE = {"kind": "revise_and_refire", "op": "workflow.revise", "params": {"run_id": RUN, "note": "halted: review_blocked"}}


def test_a_base_red_halt_offers_the_commit_and_running_it_lands_the_finished_row_only(repo):
    path = _halted_run(repo, [BASE_RED], [BASE_RED], _PROMOTED)
    assert _cli(_PROMOTED).startswith(_PREFIX)

    out = terminal_commit._handler({"task_output_path": path}, repo_root=repo / ".git")

    assert out["committed"] is True, out
    assert set(out["chunks_committed"]) == {"C1"}
    landed = _git(["show", "--name-only", "--format=", "HEAD"], repo).split()
    assert "a.py" in landed and "b.py" not in landed


def test_an_own_red_halt_offers_no_commit_and_lands_nothing(repo):
    path = _halted_run(repo, [OWN_RED], [], _REFIRE)
    head = _git(["rev-parse", "HEAD"], repo).strip()
    assert _cli(_REFIRE) is None

    out = terminal_commit._handler({"task_output_path": path}, repo_root=repo / ".git")

    assert out.get("committed") is not True, out
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


def test_a_mixed_halt_with_one_own_red_still_lands_nothing(repo):
    path = _halted_run(repo, [BASE_RED, OWN_RED], [BASE_RED], _PROMOTED)
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = terminal_commit._handler({"task_output_path": path}, repo_root=repo / ".git")

    assert out.get("committed") is not True, out
    assert _git(["rev-parse", "HEAD"], repo).strip() == head

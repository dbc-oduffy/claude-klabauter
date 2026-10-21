"""The warp ask route's stage leg flips the plan it staged to `executing` in process: no commit, no spawn."""

from __future__ import annotations

import subprocess
import time

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.ask_gate import _handler as gate_handler
from coordinator_core.ops.dispatch_emit.ask_stage import PLAN_EXECUTING_STAMP_KEY, _handler, stamp_plan_executing
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL as GATE_PLAN_REL, _plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN, PLAN_REL
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo, *args) -> str:
    done = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, **no_console_creationflags()
    )
    return done.stdout


def _init(repo) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")


def _with_status(text: str, status: str) -> str:
    return text.replace("---\n", f"---\nstatus: {status}\n", 1)


def _put_plan(repo, status: str, rel: str = PLAN_REL, newline: str = "\n") -> bytes:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _with_status(PLAN, status).replace("\n", newline)
    path.write_bytes(text.encode("utf-8"))
    return path.read_bytes()


def _status_on_disk(repo, rel: str) -> str:
    for line in (repo / rel).read_text(encoding="utf-8").splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    return ""


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    return tmp_path


@pytest.mark.parametrize("status", ["draft", "reviewed", "approved"])
def test_ask_stage_handler_flips_the_plan_executing_without_a_commit(tree, status):
    _put_plan(tree, status)
    _init(tree)
    head = _git(tree, "rev-parse", "HEAD")

    reply = _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tree)

    assert reply[PLAN_EXECUTING_STAMP_KEY] == {"plan": PLAN_REL, "outcome": "stamped"}, reply
    assert "manifest_handle" in reply
    assert _status_on_disk(tree, PLAN_REL) == "executing"
    assert _git(tree, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("status", ["draft", "reviewed", "approved"])
def test_ask_gate_s_resume_flips_the_plan_executing_without_a_commit(repo, status):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "open"), ("A2", "open")])
    plan = repo / GATE_PLAN_REL
    plan.write_text(_with_status(plan.read_text(encoding="utf-8"), status), encoding="utf-8", newline="\n")
    _init(repo)
    head = _git(repo, "rev-parse", "HEAD")

    out = gate_handler({"sizing_path": REL, "run_id": "r1", "writes": ["a.py"]}, repo_root=repo)

    assert out["halt"] is None and "manifest_handle" in out, out
    assert out[PLAN_EXECUTING_STAMP_KEY] == {"plan": GATE_PLAN_REL, "outcome": "stamped"}, out
    assert _status_on_disk(repo, GATE_PLAN_REL) == "executing"
    assert _git(repo, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("status", ["executing", "landed", "implemented", "superseded"])
def test_a_plan_past_approval_is_left_byte_identical(tree, status):
    before = _put_plan(tree, status)
    _init(tree)
    head = _git(tree, "rev-parse", "HEAD")

    reply = _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tree)

    assert PLAN_EXECUTING_STAMP_KEY not in reply and "manifest_handle" in reply
    assert stamp_plan_executing(tree, PLAN_REL) == {"plan": PLAN_REL, "outcome": "unchanged"}
    assert (tree / PLAN_REL).read_bytes() == before
    assert _git(tree, "rev-parse", "HEAD") == head


def test_a_minted_spine_is_never_stamped(tree):
    rel = "state/run/x.spine.md"
    before = _put_plan(tree, "reviewed", rel=rel)

    assert stamp_plan_executing(tree, rel) == {"plan": rel, "outcome": "unchanged"}
    assert (tree / rel).read_bytes() == before


def test_a_plan_dirty_against_head_is_flipped_and_its_edits_kept(tree):
    _put_plan(tree, "reviewed")
    _init(tree)
    path = tree / PLAN_REL
    path.write_bytes(path.read_bytes() + b"\nedited after the base commit\n")
    head = _git(tree, "rev-parse", "HEAD")

    assert stamp_plan_executing(tree, PLAN_REL) == {"plan": PLAN_REL, "outcome": "stamped"}
    assert b"status: executing" in path.read_bytes() and b"edited after" in path.read_bytes()
    assert _git(tree, "rev-parse", "HEAD") == head


def test_an_untracked_plan_is_flipped(tree):
    _put_plan(tree, "reviewed", rel="docs/plans/other.md")
    (tree / "keep.txt").write_text("x\n", encoding="utf-8")
    _init(tree)
    _put_plan(tree, "approved")

    assert stamp_plan_executing(tree, PLAN_REL)["outcome"] == "stamped"
    assert b"status: executing" in (tree / PLAN_REL).read_bytes()


def test_a_crlf_plan_keeps_crlf(tree):
    _put_plan(tree, "reviewed", newline="\r\n")
    _init(tree)

    assert stamp_plan_executing(tree, PLAN_REL)["outcome"] == "stamped"
    raw = (tree / PLAN_REL).read_bytes()
    assert b"status: executing\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_a_failed_stamp_does_not_abort_the_stage(tree, monkeypatch):
    _put_plan(tree, "reviewed")
    _init(tree)

    def boom(*_a, **_k):
        raise RuntimeError("write down")

    monkeypatch.setattr("coordinator_core.locked_write.locked_rmw", boom)

    reply = _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tree)

    stamp = reply[PLAN_EXECUTING_STAMP_KEY]
    assert stamp["outcome"] == "refused" and "write down" in stamp["error"], reply
    assert "manifest_handle" in reply and "error" not in reply


def test_the_stamp_spawns_nothing_and_stays_under_50ms(tree, monkeypatch):
    _put_plan(tree, "reviewed")
    _init(tree)

    with _count_spawns_attributed(monkeypatch) as spawns:
        start = time.process_time()
        out = ask_stage.stamp_plan_executing(tree, PLAN_REL)
        elapsed = time.process_time() - start

    assert out["outcome"] == "stamped", out
    assert len(spawns) == 0, [s.argv for s in spawns]
    assert elapsed < 0.05, f"stamp_plan_executing process time {elapsed * 1000:.1f}ms"

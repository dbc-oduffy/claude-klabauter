"""sibling_hold: predecessor_closed evidence arms and the supersession mark's refusals."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.win_portability import no_console_creationflags

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import sibling_hold as sh

RUN = "ask-20261016T152609-443905"
PLAN = "docs/plans/2026-10-16-p.md"
SIZING = "state/sizings/2026-10-16-s.yaml"


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _halt(root, **fields):
    _write(root, f"state/warp/{RUN}/halted.json", json.dumps({"status": "halted", **fields}))


def _plan(root, status, rel=PLAN):
    _write(root, rel, f"---\ntitle: p\nstatus: {status}\n---\n\nbody\n")


def test_closed_by_status_mark(tmp_path):
    _write(tmp_path, c.status_rel(RUN), json.dumps({"status": "halted", "closed_by": PLAN}))
    assert sh.predecessor_closed(tmp_path, RUN) == PLAN


def test_halt_record_plan_implemented(tmp_path):
    _halt(tmp_path, plan=PLAN, sizing_rel=SIZING)
    _plan(tmp_path, "implemented")
    assert sh.predecessor_closed(tmp_path, RUN) == PLAN


def test_sizing_plan_edge_implemented(tmp_path):
    _halt(tmp_path, sizing_rel=SIZING)
    _write(tmp_path, SIZING, f"plan: {PLAN}\nintent: x\n")
    _plan(tmp_path, "implemented")
    assert sh.predecessor_closed(tmp_path, RUN) == PLAN


def test_archived_plan_resolves_on_live_miss(tmp_path):
    _halt(tmp_path, plan=PLAN)
    _plan(tmp_path, "implemented", rel="archive/specs/2026-10/2026-10-16-p.md")
    assert sh.predecessor_closed(tmp_path, RUN) == "archive/specs/2026-10/2026-10-16-p.md"


def test_not_closed_for_unimplemented_plan_or_no_record(tmp_path):
    assert sh.predecessor_closed(tmp_path, RUN) is None
    _halt(tmp_path, plan=PLAN, sizing_rel=SIZING)
    _plan(tmp_path, "reviewed")
    assert sh.predecessor_closed(tmp_path, RUN) is None


def test_unsafe_run_id_is_not_closed(tmp_path):
    assert sh.predecessor_closed(tmp_path, "../x") is None


def _git(root, *args):
    subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, **no_console_creationflags()
    )


def _commit_all(root, msg):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )
    return out.stdout.strip()


@pytest.fixture
def landed_repo(tmp_path):
    """Repo with src/a.py committed at base; returns (root, base_sha)."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _write(root, "src/a.py", "a1\n")
    _write(root, "src/b.py", "b1\n")
    return root, _commit_all(root, "base")


def _declare(root, base_sha, writes, *, base_field=True):
    fields = {"run_base_sha": base_sha} if base_field else {}
    _halt(root, **fields)
    _write(root, f"state/warp/{RUN}/manifest.json", json.dumps({"rows": [{"id": "C1", "writes": writes}]}))


pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]
_spawns = (pytest.mark.spawns_process, pytest.mark.cadence)


def _marked(fn):
    for m in reversed(_spawns):
        fn = m(fn)
    return fn


@_marked
def test_landed_writes_release_the_halted_predecessor(landed_repo):
    root, base = landed_repo
    _declare(root, base, ["src/a.py", "src/b.py", "state/x.md", "scratch/log.txt"])
    _write(root, "src/a.py", "a2\n")
    head = _commit_all(root, "land")
    assert sh.predecessor_closed(root, RUN) == f"landed@{head[:12]}"


@_marked
def test_landed_arm_holds_on_dirty_untracked_or_unchanged(landed_repo):
    root, base = landed_repo
    _declare(root, base, ["src/a.py"])
    assert sh.predecessor_closed(root, RUN) is None
    _write(root, "src/a.py", "a2\n")
    assert sh.predecessor_closed(root, RUN) is None
    _commit_all(root, "land")
    _declare(root, _git_head(root), ["src/a.py"])
    assert sh.predecessor_closed(root, RUN) is None
    _declare(root, base, ["src/a.py", "src/new.py"])
    _write(root, "src/new.py", "n\n")
    assert sh.predecessor_closed(root, RUN) is None


def _git_head(root):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


@_marked
@pytest.mark.parametrize("sha", [None, "abc123", "Z" * 40])
def test_landed_arm_holds_on_absent_or_malformed_base(landed_repo, sha):
    root, base = landed_repo
    _write(root, "src/a.py", "a2\n")
    _commit_all(root, "land")
    _declare(root, sha, ["src/a.py"], base_field=sha is not None)
    assert sh.predecessor_closed(root, RUN) is None


@_marked
def test_landed_arm_holds_with_no_product_writes(landed_repo):
    root, base = landed_repo
    _write(root, "src/a.py", "a2\n")
    _commit_all(root, "land")
    _declare(root, base, ["state/x.md", "scratch/log.txt"])
    assert sh.predecessor_closed(root, RUN) is None


@_marked
def test_landed_arm_holds_on_a_reader_gap(landed_repo, monkeypatch):
    root, base = landed_repo
    _declare(root, base, ["src/a.py"])
    _write(root, "src/a.py", "a2\n")
    _commit_all(root, "land")
    monkeypatch.setattr("coordinator_core.git.scoped_changed.scoped_changed", lambda *a, **k: None)
    assert sh.predecessor_closed(root, RUN) is None


@_marked
def test_landed_arm_spawns_no_process(landed_repo, monkeypatch):
    root, base = landed_repo
    _declare(root, base, ["src/a.py"])
    _write(root, "src/a.py", "a2\n")
    head = _commit_all(root, "land")

    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr("coordinator_core.git.run.run_git", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert sh.predecessor_closed(root, RUN) == f"landed@{head[:12]}"


def test_mark_superseded_writes_once_and_is_idempotent(tmp_path):
    first = sh.mark_superseded(tmp_path, RUN, "ask-new")
    assert first == {"run_id": RUN, "superseded_by": "ask-new", "written": True}
    assert c.read_superseded_by(tmp_path, RUN) == "ask-new"
    assert sh.mark_superseded(tmp_path, RUN, "ask-new")["written"] is False


@pytest.mark.parametrize("state", ["landed", "running"])
def test_refuses_landed_or_running(tmp_path, state):
    _write(tmp_path, c.status_rel(RUN), json.dumps({"status": state}))
    with pytest.raises(sh.SupersedeRefused, match=state):
        sh.mark_superseded(tmp_path, RUN, "ask-new")
    assert c.read_superseded_by(tmp_path, RUN) is None


def test_refuses_different_by_self_and_unsafe_ids(tmp_path):
    sh.mark_superseded(tmp_path, RUN, "ask-new")
    assert "already superseded by ask-new" in sh.supersede_refusal(tmp_path, RUN, "ask-other")
    assert sh.supersede_refusal(tmp_path, "r", "r")
    assert sh.supersede_refusal(tmp_path, "../x", "n")
    assert sh.supersede_refusal(tmp_path, RUN, "a/b")
    assert c.read_superseded_by(tmp_path, RUN) == "ask-new"

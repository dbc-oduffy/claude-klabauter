"""dispatch.record_halted_run_landed accepts any ancestor of HEAD, each checked against its own first parent."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SOLO = "warp-2026-10-20-solo-20261020T000000-abcdef"
SIBLING = "A"
PARENT = "ask-parent-20261015T000000-abcdef"


def _git(repo: Path, *args: str, date: str | None = None) -> str:
    env = dict(os.environ)
    if date:
        env["GIT_COMMITTER_DATE"] = date
        env["GIT_AUTHOR_DATE"] = date
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(repo), check=True, capture_output=True, text=True, env=env,
        **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, name: str, date: str = "2026-01-01T00:00:00Z") -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "--", name)
    _git(repo, "commit", "-q", "-m", name, date=date)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _stub_launch(monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)


def _marker(root: Path, solo: bool, *withheld: str) -> tuple[str, Path]:
    """A halted run (solo or sibling) withholding *withheld*; returns ``(run_id, marker path)``."""
    rid = SOLO if solo else SIBLING
    if not solo:
        runs = [c.SiblingRun("A", "A", (), "state/sizings/A.yaml", c.STATUS_READY)]
        rec = root / c.parent_record_rel(PARENT)
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text(json.dumps(c.ParentRecord(PARENT, tuple(runs)).to_json()), encoding="utf-8")
        st = root / c.status_rel("A")
        st.parent.mkdir(parents=True, exist_ok=True)
        st.write_text(json.dumps({"parent_id": PARENT, "status": "halted"}), encoding="utf-8")
    d = root / "state" / "warp" / rid
    d.mkdir(parents=True, exist_ok=True)
    marker = d / "halted.json"
    body = {
        "status": "halted", "halted": "x", "sizing_rel": None,
        "run_artifacts": {"withheld": {p: "r" for p in withheld}},
    }
    marker.write_text(json.dumps(body), encoding="utf-8")
    return rid, marker


def _call(root, **params):
    return op._handler(params, root)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit(tmp_path, "base")
    return tmp_path


@pytest.mark.parametrize("solo", [True, False])
def test_sha_two_commits_behind_head_is_accepted(repo, solo):
    carrier = _commit(repo, "w.md")
    _commit(repo, "two")
    _commit(repo, "three")
    rid, _ = _marker(repo, solo, "w.md")
    out = _call(repo, run_id=rid, sha=carrier)
    assert out["ok"] is True and out["written"] is True and out["sha"] == carrier


def _side_branch(repo: Path, name: str, date: str = "2026-01-01T00:00:00Z") -> tuple[str, str]:
    """Commit *name* on a side branch off HEAD, merge it ``--no-ff``; returns ``(side_sha, base_sha)``."""
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "side")
    side = _commit(repo, name, date)
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "mainline")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side", date=date)
    return side, base


@pytest.mark.parametrize("solo", [True, False])
def test_merge_reached_commit_is_accepted_against_its_own_first_parent(repo, solo):
    side, _ = _side_branch(repo, "w.md")
    rid, _ = _marker(repo, solo, "w.md")
    out = _call(repo, run_id=rid, sha=side)
    assert out["ok"] is True and out["written"] is True and out["sha"] == side
    assert out["covers"] == {side: ["w.md"]}


@pytest.mark.parametrize("solo", [True, False])
def test_unmerged_side_commit_refuses_and_leaves_marker(repo, solo):
    _git(repo, "checkout", "-q", "-b", "side")
    side = _commit(repo, "w.md")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "mainline")
    rid, marker = _marker(repo, solo, "w.md")
    before = marker.read_bytes()
    out = _call(repo, run_id=rid, sha=side)
    assert not out["ok"] and side in out["error"] and "not an ancestor of HEAD" in out["error"]
    assert marker.read_bytes() == before


@pytest.mark.parametrize("solo", [True, False])
def test_set_mixing_merge_reached_and_first_parent_records_the_later_committer_date(repo, solo):
    side, _ = _side_branch(repo, "a.md", date="2026-03-01T00:00:00Z")
    early = _commit(repo, "b.md", date="2026-02-01T00:00:00Z")
    rid, _ = _marker(repo, solo, "a.md", "b.md")
    out = _call(repo, run_id=rid, shas=[early, side])
    assert out["ok"] is True and out["sha"] == side
    assert out["shas"] == [early, side]


@pytest.mark.parametrize("solo", [True, False])
def test_abbreviation_of_a_merge_reached_commit_resolves_to_its_full_sha(repo, solo):
    side, _ = _side_branch(repo, "w.md")
    rid, _ = _marker(repo, solo, "w.md")
    out = _call(repo, run_id=rid, sha=side[:10])
    assert out["ok"] is True and out["sha"] == side and out["shas"] == [side]

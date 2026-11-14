"""review.prep worktree basis: a declared untracked file and a declared modified file both reach a slice."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-own"
TRACKED = "src/edited.py"
NEW = "src/brand_new.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    for rel, text in (
        (TRACKED, "x = 1\n"),
        ("src/other.py", "y = 1\n"),
        ("docs/plans/p.md", "---\nplan_id: pln-test-1\n---\n\n# p\n"),
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


@pytest.fixture(autouse=True)
def _own_session_claims(monkeypatch):
    from coordinator_core.session import claim_index, liveness

    class _Found(dict):
        recorded_kind = None

    def lookup(paths, **kw):
        found = _Found({p: [SESSION] for p in paths})
        found.recorded_kind = {p: {SESSION: "write"} for p in paths}
        return found

    monkeypatch.setattr(claim_index, "lookup", lookup)
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: sid == SESSION)


def _slice_files(reply: dict) -> set:
    out = set()
    for s in reply.get("slices") or []:
        out.update(s.get("files") or [])
    return out


@pytest.mark.parametrize("declared", [[TRACKED, NEW], ["src/"], ["src"]])
def test_untracked_and_modified_declared_files_reach_a_slice(repo, declared):
    (repo / TRACKED).write_text("x = 2\n" * 40, encoding="utf-8")
    (repo / NEW).write_text("z = 1\n" * 40, encoding="utf-8")
    reply = rp._handler(
        {
            "base": _git(repo, "rev-parse", "HEAD").strip(),
            "worktree": True,
            "paths": declared,
            "slice_prefix": "t-untracked-prep",
            "plan_path": "docs/plans/p.md",
            "session_id": SESSION,
        },
        repo,
    )
    assert reply["error"] is None
    assert reply["product_files"] >= 2
    assert {TRACKED, NEW} <= set(reply["reviewed_sha256"])
    assert {TRACKED, NEW} <= _slice_files(reply)


def test_untracked_member_of_declared_dir_survives_peer_bases(repo, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD").strip()
    monkeypatch.setattr(rp, "_peer_bases", lambda *a, **k: {"src/other.py": base})
    (repo / TRACKED).write_text("x = 2\n" * 40, encoding="utf-8")
    (repo / NEW).write_text("z = 1\n" * 40, encoding="utf-8")
    reply = rp._handler(
        {
            "base": base,
            "worktree": True,
            "paths": ["src/"],
            "slice_prefix": "t-untracked-peer-prep",
            "plan_path": "docs/plans/p.md",
            "session_id": SESSION,
        },
        repo,
    )
    assert reply["error"] is None
    assert {TRACKED, NEW} <= set(reply["reviewed_sha256"])

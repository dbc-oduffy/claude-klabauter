"""review.prep foreign_claims: a declared path dirty in the worktree that a live peer claims is listed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-own"
PEER = "sess-peer"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "seed.py").write_text("x = 1\n", encoding="utf-8")
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nplan_id: pln-test-1\n---\n\n# p\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _claims(monkeypatch, table: dict) -> None:
    monkeypatch.setattr(rp.claim_index, "lookup", lambda paths, **kw: {p: table.get(p, []) for p in paths})


def _no_spawn(monkeypatch) -> None:
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(rp, "run_git", boom)


def test_dirty_declared_path_claimed_by_a_live_peer_is_listed_without_a_spawn(repo, monkeypatch):
    _claims(monkeypatch, {"pkg/held.py": [PEER]})
    _no_spawn(monkeypatch)
    found = rp.foreign_claims(repo, "a..b", ["pkg"], SESSION, frozenset(), frozenset({"pkg/held.py"}))
    assert found == ["pkg/held.py"]


def test_dirty_path_with_only_an_own_claim_is_not_listed(repo, monkeypatch):
    _claims(monkeypatch, {"pkg/own.py": [SESSION]})
    assert rp.foreign_claims(repo, "a..b", ["pkg"], SESSION, frozenset(), frozenset({"pkg/own.py"})) == []


def test_clean_claimed_path_is_not_listed(repo, monkeypatch):
    _claims(monkeypatch, {"pkg/held.py": [PEER]})
    assert rp.foreign_claims(repo, "a..b", ["pkg"], SESSION, frozenset(), None) == []
    assert rp.foreign_claims(repo, "a..b", ["pkg"], SESSION, frozenset(), frozenset()) == []


def test_dirty_joins_the_git_log_changed_set(repo, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "committed.py").write_text("v = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    _claims(monkeypatch, {"pkg/committed.py": [PEER], "pkg/dirty.py": [PEER]})
    found = rp.foreign_claims(repo, f"{base}..HEAD", ["pkg"], SESSION, None, frozenset({"pkg/dirty.py"}))
    assert found == ["pkg/committed.py", "pkg/dirty.py"]


def test_review_prep_lists_an_uncommitted_peer_claimed_declared_file(repo, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "held.py").write_text("v = 1\n" * 3, encoding="utf-8")
    _claims(monkeypatch, {"pkg/held.py": [PEER]})
    reply = rp.review_prep(
        repo, base=base, paths=["pkg/held.py"], slice_prefix="prep-t", plan_path="docs/plans/p.md",
        session_id=SESSION, head=base,
    )
    assert reply["error"] is None
    assert reply["foreign_claims"] == ["pkg/held.py"]

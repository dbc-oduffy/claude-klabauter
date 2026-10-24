"""A touch record an earlier run's agent left is no expectation of the run being landed: a sibling
run's later legitimate rewrite of that path is not false blob-drift, while this run's own touch
still is the expectation."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, terminal_commit
from coordinator_core.session import core as session_core
from coordinator_core.session import touch_record

SELF = "5e1f0000-0000-4000-8000-0000000000b2"
REL = "src/x.py"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / ".git").mkdir()
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    return root


def _touch(repo: Path, text: str, at: float) -> None:
    touch_record.append_event(
        touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SELF),
        session_id=SELF, agent_id=None, verb="T", path=REL, content_hash=_sha(text), kind="w", timestamp=at,
    )


def _expect(module, repo: Path, **kw) -> dict:
    if module is terminal_commit:
        return module._recorded_fingerprints(repo, SELF, [REL], [REL], [], excluded=set(), **kw)
    return module._recorded_fingerprints(
        repo, SELF, [REL], [REL], excluded=set(), reviewed=None, reviewed_at=None,
        fixes_integrated_at=None, wave_paths=frozenset(), **kw,
    )


@pytest.mark.parametrize("module", [terminal_commit, land_run])
def test_an_earlier_runs_touch_is_no_expectation_after_a_sibling_rewrite(repo, module):
    now = time.time()
    _touch(repo, "earlier run's bytes\n", now - 100)
    (repo / REL).write_text("sibling run's later bytes\n", encoding="utf-8")
    assert _expect(module, repo, run_started_at=now - 50) == {}


@pytest.mark.parametrize("module", [terminal_commit, land_run])
def test_this_runs_own_touch_is_still_the_expectation(repo, module):
    now = time.time()
    _touch(repo, "this run's bytes\n", now - 10)
    (repo / REL).write_text("foreign later bytes\n", encoding="utf-8")
    assert _expect(module, repo, run_started_at=now - 50) == {REL: _sha("this run's bytes\n")}


@pytest.mark.parametrize("module", [terminal_commit, land_run])
def test_no_run_start_keeps_the_touch_expectation(repo, module):
    now = time.time()
    _touch(repo, "old\n", now - 100)
    assert _expect(module, repo) == {REL: _sha("old\n")}

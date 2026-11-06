"""research.close attributes untracked docs/research files per run, not per session."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.research_close import _under_a_run_dir
from coordinator_core.ops.tests.test_research_close import (  # noqa: F401  (fixtures)
    _close,
    _hold,
    _git,
    claims,
    repo,
    scratch,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_OTHER = "docs/research/other-topic/2026-11-06-other-run"


def test_a_sibling_runs_untracked_deliverable_is_a_peer_file_not_a_refusal(repo, scratch, claims):
    held = []
    for name in ("synthesis.md", "outputs/advisory.md"):
        path = repo / _OTHER / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("run A\n", encoding="utf-8")
        _hold(claims, "me", f"{_OTHER}/{name}")
        held.append(f"{_OTHER}/{name}")
    out = _close(repo, scratch, "corpus", outputs=["final.md"], session_id="me")
    assert out["exit_code"] == 0 and out["committed"] is True, out
    assert sorted(out["peer_files"]) == sorted(held)
    assert "strays" not in out
    assert not [p for p in _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines() if "other-run" in p]


def test_a_stray_outside_any_run_dir_still_refuses(repo, scratch, claims):
    stray = repo / "docs" / "research" / "other-topic" / "loose.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("loose\n", encoding="utf-8")
    _hold(claims, "me", "docs/research/other-topic/loose.md")
    out = _close(repo, scratch, "corpus", outputs=["final.md"], session_id="me")
    assert out["exit_code"] == 1 and out["deliverable"] is False, out
    assert out["strays"] == ["docs/research/other-topic/loose.md"]


@pytest.mark.parametrize(
    "rel,expected",
    [
        (f"{_OTHER}/synthesis.md", True),
        (f"{_OTHER}/outputs/a.md", True),
        ("docs/research/other-topic/loose.md", False),
        (f"{_OTHER}", False),
        ("docs/research/2026-10-09-elsewhere.md", False),
    ],
)
def test_under_a_run_dir(rel, expected):
    assert _under_a_run_dir(rel) is expected

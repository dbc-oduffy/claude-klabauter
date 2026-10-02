"""Tests for review_mint.supersede in a scratch git repo."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.ops.review_mint import supersede
from coordinator_core.ops.review_mint.supersede import (
    SupersedeRefused,
    record_superseding_review,
)
from coordinator_core.win_portability import no_console_creationflags


pytestmark = pytest.mark.spawns_process


def _git(root, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    shas = []
    for i, trailer in enumerate(["", "\n\nSession-Id: other-session", ""]):
        (tmp_path / f"f{i}.txt").write_text(str(i))
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-q", "-m", f"c{i}{trailer}")
        shas.append(_git(tmp_path, "rev-parse", "HEAD"))
    return tmp_path, shas


def _call(root, base, head, **kw):
    return record_superseding_review(
        repo_root=root, plan="pln-x-123456",
        commit_range={"base": base, "head": head},
        wave_sidecar_paths=[], prep_sidecar=None, stage_returns=None,
        session_id="sess-1", **kw,
    )


def test_record_written_with_pinned_keys(repo):
    root, s = repo
    res = _call(root, s[0], s[2], supersedes=s[1])
    fm = yaml.safe_load((root / res["record_path"]).read_text().split("---\n")[1])
    assert fm["kind"] == "superseding-review"
    assert fm["plan_id"] == "pln-x-123456"
    assert fm["commit_range"] == {"base": s[0], "head": s[2]}
    assert fm["supersedes"] == s[1]
    assert "integrated_from" in fm and "fixes_applied" in fm


def test_second_write_refused(repo):
    root, s = repo
    _call(root, s[0], s[2])
    with pytest.raises(SupersedeRefused):
        _call(root, s[0], s[2])


def test_non_ancestor_head_refused(repo):
    root, s = repo
    _git(root, "checkout", "-q", "-b", "side", s[0])
    (root / "side.txt").write_text("x")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "side")
    side = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "-")
    with pytest.raises(SupersedeRefused):
        _call(root, s[0], side)


def test_base_equals_head_and_reversed_refused(repo):
    root, s = repo
    with pytest.raises(SupersedeRefused):
        _call(root, s[1], s[1])
    with pytest.raises(SupersedeRefused):
        _call(root, s[2], s[0])


def test_foreign_session_trailer_in_range_accepted(repo):
    root, s = repo
    assert "Session-Id: other-session" in _git(root, "log", "-1", "--format=%B", s[1])
    assert _call(root, s[0], s[2])["record_path"].endswith(".superseding.md")


def test_spawn_count_at_most_two(repo, monkeypatch):
    root, s = repo
    calls = []
    real = subprocess.Popen

    class counting(real):
        def __init__(self, *a, **k):
            calls.append(a)
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", counting)
    _call(root, s[0], s[2])
    assert len(calls) <= 2


def test_plan_path_resolves_to_frontmatter_plan_id(repo):
    root, s = repo
    plan = root / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nplan_id: pln-from-fm-abcdef\n---\nbody\n", encoding="utf-8")
    res = record_superseding_review(
        repo_root=root, plan="docs/plans/p.md",
        commit_range={"base": s[0], "head": s[2]},
        wave_sidecar_paths=[], prep_sidecar=None, stage_returns=None,
        session_id="sess-1",
    )
    assert res["record"]["plan_id"] == "pln-from-fm-abcdef"
    assert "docs/plans" not in res["record_path"]


def test_plan_path_without_plan_id_refused(repo):
    root, s = repo
    (root / "p.md").write_text("---\ntitle: x\n---\n", encoding="utf-8")
    with pytest.raises(SupersedeRefused):
        record_superseding_review(
            repo_root=root, plan="p.md",
            commit_range={"base": s[0], "head": s[2]},
            wave_sidecar_paths=[], prep_sidecar=None, stage_returns=None,
            session_id="sess-1",
        )

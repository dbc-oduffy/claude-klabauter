"""Pins `baton_close_commit_write.flip_batch`: provenance, pathspec, no commit, no spawn.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_baton_close_commit_write.py -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops import baton_close_commit_write as write_mod
from coordinator_core.ops.baton_close_commit_write import flip_batch
from coordinator_core.ops.tests.test_deliverable_cascade_kinds import (
    _git,
    _init_repo,
    _seed_handoff,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

EVIDENCE_SHA = "c0ffee1234567890abcdef1234567890abcdef12"


def _fm(path: Path, key: str):
    with open(path, encoding="utf-8", newline="") as handle:
        split = split_frontmatter(handle.read())
    assert split is not None
    return read_fm_field_unquoted(split.fm_text, key)


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _changed(repo: Path) -> set[str]:
    out = _git(repo, "status", "--porcelain").stdout
    return {line[3:].strip() for line in out.splitlines() if line.strip()}


def _seed(tmp_path: Path, n: int = 3) -> tuple[Path, list[Path]]:
    repo = tmp_path / "repo"
    _init_repo(repo)
    paths = [
        _seed_handoff(repo, f"20260102-b{i}.md", deliverable_id=f"dlv-b{i}-00000{i}")
        for i in range(n)
    ]
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    return repo, paths


def _req(repo: Path, path: Path) -> dict:
    return {"path": str(path), "deliverable_id": _fm(path, "deliverable_id"), "shipped_in": EVIDENCE_SHA[:8]}


def test_every_flip_carries_provenance_and_pathspec_equals_changed_set(tmp_path):
    repo, paths = _seed(tmp_path)
    head = _head(repo)
    out = flip_batch([_req(repo, p) for p in paths], repo / ".git", repo)
    assert len(out.applied) == 3 and not out.refused and not out.already_terminal
    for p in paths:
        assert _fm(p, "deployment_state") == "shipped"
        assert _fm(p, "shipped_in_kind") == "successor"
        assert _fm(p, "advanced_by") == _fm(p, "deliverable_id")
        assert _fm(p, "advanced_at") == out.applied[0]["advanced_at"]
        assert _fm(p, "shipped_in") == EVIDENCE_SHA[:8] != head[:8]
    assert set(out.pathspec) == _changed(repo)
    assert out.pathspec == sorted(out.pathspec)
    assert out.commit_subject == "baton.close_commit: 3 baton(s) terminal"
    assert _head(repo) == head


def test_one_scoped_landing_of_the_pathspec_carries_every_flip(tmp_path):
    repo, paths = _seed(tmp_path)
    out = flip_batch([_req(repo, p) for p in paths], repo / ".git", repo)
    _git(repo, "add", "--", *out.pathspec)
    _git(repo, "commit", "-m", out.commit_subject)
    assert _changed(repo) == set()
    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == out.commit_subject


def test_crlf_baton_is_flipped(tmp_path):
    repo, paths = _seed(tmp_path, 1)
    crlf = paths[0]
    crlf.write_bytes(crlf.read_bytes().replace(b"\n", b"\r\n"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "crlf")
    out = flip_batch([_req(repo, crlf)], repo / ".git", repo)
    assert len(out.applied) == 1
    assert _fm(crlf, "deployment_state") == "shipped"
    assert _fm(crlf, "advanced_by") == "dlv-b0-000000"


def test_write_failed_row_is_absent_from_pathspec_and_others_apply(tmp_path):
    repo, paths = _seed(tmp_path, 2)
    bad = repo / "state" / "handoffs" / "20260102-nofm.md"
    bad.write_text("no frontmatter\n", encoding="utf-8")
    missing = repo / "state" / "handoffs" / "20260102-missing.md"
    flips = [_req(repo, paths[0]),
             {"path": str(bad), "deliverable_id": "dlv-x-000000", "shipped_in": "c0ffee12"},
             {"path": str(missing), "deliverable_id": "dlv-y-000000", "shipped_in": "c0ffee12"},
             _req(repo, paths[1])]
    out = flip_batch(flips, repo / ".git", repo)
    assert [a["handoff_path"] for a in out.applied] == [str(paths[0]), str(paths[1])]
    assert [r["reason"] for r in out.refused] == ["write_failed", "write_failed"]
    assert bad.read_text(encoding="utf-8") == "no frontmatter\n"
    assert not missing.exists()
    assert len(out.pathspec) == 2 and not any("nofm" in p or "missing" in p for p in out.pathspec)


def test_baton_made_terminal_between_select_and_flip_is_already_terminal(tmp_path):
    repo, paths = _seed(tmp_path, 2)
    flip_batch([_req(repo, paths[0])], repo / ".git", repo)
    before = paths[0].read_bytes()
    out = flip_batch([_req(repo, paths[0]), _req(repo, paths[1])], repo / ".git", repo)
    assert out.already_terminal == [{"handoff_path": str(paths[0])}]
    assert len(out.applied) == 1 and not out.refused
    assert out.pathspec == ["state/handoffs/20260102-b1.md"]
    assert paths[0].read_bytes() == before


def test_all_refused_batch_returns_empty_pathspec_and_no_subject(tmp_path):
    repo, _paths = _seed(tmp_path, 1)
    out = flip_batch(
        [{"path": str(repo / "state" / "handoffs" / "nope.md"), "deliverable_id": "dlv-n-000000",
          "shipped_in": "c0ffee12"}],
        repo / ".git", repo,
    )
    assert out.pathspec == [] and out.commit_subject is None and len(out.refused) == 1


def test_flip_without_shipped_in_is_refused_and_writes_nothing(tmp_path):
    repo, paths = _seed(tmp_path)
    before = [p.read_bytes() for p in paths]
    flips = [{"path": str(p), "deliverable_id": _fm(p, "deliverable_id")} for p in paths]
    out = flip_batch(flips, repo / ".git", repo)
    assert not out.applied and out.pathspec == [] and out.commit_subject is None
    assert [r["reason"] for r in out.refused] == ["no_evidence_sha"] * 3
    assert [p.read_bytes() for p in paths] == before


def test_write_module_never_reads_head():
    source = Path(write_mod.__file__).read_text(encoding="utf-8")
    assert "head_sha" not in source


def test_flip_batch_spawns_nothing_and_imports_no_commit_helper(tmp_path, monkeypatch):
    repo, paths = _seed(tmp_path)

    def _boom(*_a, **_k):
        raise AssertionError("flip_batch spawned a process")

    monkeypatch.setattr(subprocess.Popen, "__init__", _boom)
    out = flip_batch([_req(repo, p) for p in paths], repo / ".git", repo)
    assert len(out.applied) == 3
    source = Path(write_mod.__file__).read_text(encoding="utf-8")
    assert "git.commit" not in source and "git import commit" not in source

"""Tests for "ceremony.update_docs_worklist": registry, classification, cache, the
git-spawn ceiling and the DR-344 500ms process-time bar at default params.

The git runner is replaced by a counting double, so nothing here spawns a process.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import time
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.update_docs_worklist as uw
from coordinator_core.git.run import GitResult
from coordinator_core.ipc import _REGISTRY

_OP = "ceremony.update_docs_worklist"
_SHA_A = "a" * 40
_SHA_B = "b" * 40
_NOW = _dt.datetime.now(tz=_dt.timezone.utc)


class _Git:
    """Counting `run_git` double: `diff` answers `changed`, `rev-parse` answers `head`."""

    def __init__(self, changed=(), head=_SHA_B, diff_ok=True):
        self.calls: list[list[str]] = []
        self.changed = list(changed)
        self.head = head
        self.diff_ok = diff_ok

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if args[0] == "rev-parse":
            return GitResult(0, self.head + "\n", "", False)
        if not self.diff_ok:
            return GitResult(128, "", "bad object", False)
        return GitResult(0, "".join(p + "\0" for p in self.changed), "", False)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "tasks").mkdir()
    (root / "state" / "cross-repo" / "archive").mkdir(parents=True)
    monkeypatch.setenv("MACHINE_LOCAL_PATHS_UPDATE_DOCS_WORKLIST_CACHE", str(tmp_path / "cache"))
    return root


def _write(path: Path, text: str, *, age_days: float = 0.0) -> Path:
    path.write_text(text, encoding="utf-8")
    if age_days:
        ts = (_NOW - _dt.timedelta(days=age_days)).timestamp()
        os.utime(path, (ts, ts))
    return path


def _plan(repo: Path, name: str, status: str, *, age_days=60.0, supersedes: str = "") -> str:
    sup = f"supersedes: {supersedes}\n" if supersedes else ""
    _write(repo / "docs" / "plans" / name, f"---\nstatus: {status}\n{sup}---\nbody\n", age_days=age_days)
    return f"docs/plans/{name}"


def _run(repo: Path, git: _Git, monkeypatch, params=None) -> dict:
    monkeypatch.setattr(uw, "run_git", git)
    return uw._ceremony_update_docs_worklist(params if params is not None else {}, repo_root=repo / ".git")


def test_op_registered():
    assert _OP in _REGISTRY


def test_repo_root_none_raises():
    with pytest.raises(ValueError):
        uw._ceremony_update_docs_worklist({}, repo_root=None)


def test_prune_classification_and_lineage_hold(repo, monkeypatch):
    old = _plan(repo, "old-superseded.md", "superseded")
    _plan(repo, "fresh-superseded.md", "superseded", age_days=1)
    _plan(repo, "live.md", "in_progress")
    held = _plan(repo, "held-abandoned.md", "abandoned")
    _plan(repo, "successor.md", "in_progress", supersedes="docs/plans/held-abandoned.md")
    _write(repo / "tasks" / "a.md", "---\nstatus: superseded\n---\n")
    _write(repo / "tasks" / "b.md", "---\nstatus: open\n---\n")
    (repo / "tasks" / "c.txt").write_text("---\nstatus: superseded\n---\n")
    picked = (_NOW - _dt.timedelta(days=200)).isoformat()
    _write(repo / "state" / "cross-repo" / "archive" / "old.md", f"---\nstatus: actioned\npicked_up_at: {picked}\n---\n")
    _write(repo / "state" / "cross-repo" / "archive" / "new.md", f"---\nstatus: actioned\npicked_up_at: {_NOW.isoformat()}\n---\n")

    out = _run(repo, _Git(), monkeypatch)

    prune = out["decided_by_op"]["prune"]
    assert prune["plans"] == [old]
    assert prune["tasks"] == ["tasks/a.md"]
    assert prune["crossrepo_archive"] == ["state/cross-repo/archive/old.md"]
    assert out["decided_by_op"]["lineage_blocked"] == 1
    assert held not in prune["plans"]
    kinds = {w["kind"]: w for w in out["agent_worklist"]}
    assert kinds["lineage_held_disposed_plan"]["paths"] == [held]


def test_drift_becomes_agent_worklist_and_directory_staleness(repo, monkeypatch):
    cache_dir = Path(os.environ["MACHINE_LOCAL_PATHS_UPDATE_DOCS_WORKLIST_CACHE"]) / uw.CACHE_SUBDIR
    cache_dir.mkdir(parents=True)
    (cache_dir / uw.CACHE_FILE).write_text(json.dumps({"ack_head": _SHA_A, "files": {}}))

    stale = _run(repo, _Git(changed=["coordinator_core/x.py", "coordinator/bin/y.py"]), monkeypatch)
    assert stale["ack_head"] == _SHA_A
    assert stale["decided_by_op"]["directory_md_stale"] is True
    drift = [w for w in stale["agent_worklist"] if w["kind"] == "doc_drift_review"]
    assert drift[0]["paths"] == ["coordinator_core/x.py"]

    fresh = _run(repo, _Git(changed=["coordinator_core/x.py", "coordinator_core/DIRECTORY.md"]), monkeypatch)
    assert fresh["decided_by_op"]["directory_md_stale"] is False


def test_default_call_one_spawn_cold_seeds_baseline_without_drift(repo, monkeypatch):
    git = _Git()
    out = _run(repo, git, monkeypatch)
    assert [c[0] for c in git.calls] == ["rev-parse"]
    assert out["ack_head"] == _SHA_B
    assert out["changed_since_ack"]["total"] == 0
    assert out["agent_worklist"] == []


def test_unreachable_ack_reseeds_and_stops_respawning(repo, monkeypatch):
    cache_dir = Path(os.environ["MACHINE_LOCAL_PATHS_UPDATE_DOCS_WORKLIST_CACHE"]) / uw.CACHE_SUBDIR
    cache_dir.mkdir(parents=True)
    (cache_dir / uw.CACHE_FILE).write_text(json.dumps({"ack_head": _SHA_A, "files": {}}))

    first = _Git(diff_ok=False)
    out = _run(repo, first, monkeypatch)
    assert out["ack_head"] == _SHA_B and out["changed_since_ack"]["total"] == 0

    second = _Git()
    _run(repo, second, monkeypatch)
    assert [c[0] for c in second.calls] == ["diff"]


def test_ack_advances_baseline_only_when_asked(repo, monkeypatch):
    _run(repo, _Git(head=_SHA_A), monkeypatch)
    default = _Git(head=_SHA_B)
    assert _run(repo, default, monkeypatch)["ack_head"] == _SHA_A
    assert [c[0] for c in default.calls] == ["diff"]

    acked = _Git(head=_SHA_B)
    assert _run(repo, acked, monkeypatch, {"ack": True})["ack_head"] == _SHA_B
    assert len(acked.calls) == 2
    assert _run(repo, _Git(head=_SHA_B), monkeypatch)["ack_head"] == _SHA_B


def test_warm_cache_rereads_nothing_and_rereads_changed_file(repo, monkeypatch):
    for i in range(5):
        _plan(repo, f"p{i}.md", "superseded")
    cold = _run(repo, _Git(), monkeypatch)
    assert cold["cache"]["state"] == "cold" and cold["cache"]["reread"] == 5

    warm = _run(repo, _Git(), monkeypatch)
    assert warm["cache"]["state"] == "warm" and warm["cache"]["reread"] == 0

    _plan(repo, "p0.md", "in_progress")
    edited = _run(repo, _Git(), monkeypatch)
    assert edited["cache"]["reread"] == 1
    assert edited["decided_by_op"]["prune_totals"]["plans"] == 4


def test_reread_budget_leaves_unscanned_files_unprunable(repo, monkeypatch):
    for i in range(5):
        _plan(repo, f"p{i}.md", "superseded")
    monkeypatch.setattr(uw, "REREAD_BUDGET_SECS", -1.0)
    out = _run(repo, _Git(), monkeypatch)
    assert out["cache"]["complete"] is False
    assert out["cache"]["reread"] == 0
    assert out["decided_by_op"]["prune_totals"]["plans"] == 0


def test_crlf_and_preamble_frontmatter_use_shared_parser(tmp_path):
    crlf = _write(tmp_path / "a.md", "---\r\nstatus: superseded\r\nsupersedes: docs/plans/x.md\r\n---\r\nbody\r\n")
    assert uw._frontmatter_fields(str(crlf)) == ("superseded", "docs/plans/x.md", "")
    plain = _write(tmp_path / "b.md", '---\nstatus: "actioned"\npicked_up_at: 2026-01-01T00:00:00Z\n---\n')
    assert uw._frontmatter_fields(str(plain)) == ("actioned", "", "2026-01-01T00:00:00Z")
    none = _write(tmp_path / "c.md", "no frontmatter\n")
    assert uw._frontmatter_fields(str(none)) == ("", "", "")


def test_output_is_bounded(repo, monkeypatch):
    long_names = ["coordinator_core/" + "d" * 480 + f"/m{i}.py" for i in range(400)]
    cache_dir = Path(os.environ["MACHINE_LOCAL_PATHS_UPDATE_DOCS_WORKLIST_CACHE"]) / uw.CACHE_SUBDIR
    cache_dir.mkdir(parents=True)
    (cache_dir / uw.CACHE_FILE).write_text(json.dumps({"ack_head": _SHA_A, "files": {}}))
    out = _run(repo, _Git(changed=long_names), monkeypatch)
    assert len(json.dumps(out, separators=(",", ":"))) <= uw.OUTPUT_BYTE_CAP
    assert out["changed_since_ack"]["total"] == 400
    assert out["changed_since_ack"]["truncated"] is True


def test_cache_root_falls_back_to_repo_local_without_the_key(repo, monkeypatch):
    monkeypatch.setattr(uw, "registry_get", lambda key: None)
    assert uw._cache_path(repo) == repo / ".coordinator-local" / "cache" / uw.CACHE_SUBDIR / uw.CACHE_FILE


def test_default_params_hold_the_500ms_bar_with_at_most_one_git_spawn(repo, monkeypatch):
    """DR-344: {} returns agent_worklist and decided_by_op under 500ms process time, <=1 git spawn."""
    for i in range(1200):
        _plan(repo, f"p{i}.md", "superseded" if i % 3 == 0 else "in_progress")
    for i in range(800):
        _write(repo / "tasks" / f"t{i}.md", "---\nstatus: open\n---\n")
    picked = (_NOW - _dt.timedelta(days=200)).isoformat()
    for i in range(1000):
        _write(repo / "state" / "cross-repo" / "archive" / f"m{i}.md", f"---\nstatus: actioned\npicked_up_at: {picked}\n---\n")

    for label in ("cold", "warm"):
        git = _Git()
        monkeypatch.setattr(uw, "run_git", git)
        start = time.process_time()
        out = uw._ceremony_update_docs_worklist({}, repo_root=repo / ".git")
        elapsed_ms = (time.process_time() - start) * 1000
        assert "agent_worklist" in out and "decided_by_op" in out
        assert len(git.calls) <= 1, label
        assert elapsed_ms < 500, f"{label}: {elapsed_ms:.0f}ms"
    assert out["decided_by_op"]["prune_totals"]["plans"] == 400
    assert out["cache"]["reread"] == 0

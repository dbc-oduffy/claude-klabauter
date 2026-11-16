"""memo.sweep_ref_only counts ref-only memos per fleet repo and, on apply, materialises them.

Fleet repos are real tmp_path git repos registered through a tmp settings home; ref-only memos are
anchors (`refs/coordinator/inbox/<file>/<sha>` naming a blob) with no file in either corpus root.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import write_object
from coordinator_core.ops.fleet import memo_sweep_ref_only
from coordinator_core.ops.fleet.memo_sweep_ref_only import _memo_sweep_ref_only
from coordinator_core.ops.fleet.tests.test_memo_send import (
    _git as _run_git,
    _make_claude_home,
    _make_receiver_git_repo,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_TODAY = datetime.date(2026, 11, 14)
_OPEN = "2026-11-10-alpha-open-ask.md"
_TERMINAL = "2026-10-01-alpha-done-ask.md"
_OLD_OPEN = "2026-01-01-alpha-old-ask.md"
_UNDATED = "undated-ask.md"


def _git(cwd, *args) -> str:
    out = _run_git(cwd, *args).stdout
    return (out.decode() if isinstance(out, bytes) else out).strip()


def _memo(status: str = "open", *, key: str = "status", created: str = "2026-11-10") -> str:
    return f'---\ntitle: "A memo"\nfrom: "peer"\nto: "alpha"\n{key}: {status}\ncreated: {created}\n---\n\nbody\n'


def _anchor(repo: Path, filename: str, content: str, csha: str = "a" * 40) -> None:
    common = resolve_git_common_dir(repo)
    blob = write_object(common, b"blob", content.encode("utf-8"))
    ref = common / "refs" / "coordinator" / "inbox" / filename / csha
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(blob + "\n", encoding="utf-8")


def _tracked(repo: Path, rel: str) -> bool:
    return rel in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(memo_sweep_ref_only, "_today", lambda: _TODAY)


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    alpha = _make_receiver_git_repo(tmp_path, name="alpha-repo")
    beta = _make_receiver_git_repo(tmp_path, name="beta-repo")
    gone = tmp_path / "not-on-this-box"
    monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"alpha": alpha, "beta": beta, "gone": gone})))
    _anchor(alpha, _OPEN, _memo("open"))
    _anchor(alpha, _TERMINAL, _memo("actioned", created="2026-10-01"))
    _anchor(alpha, _OLD_OPEN, _memo("open", created="2026-01-01"))
    _anchor(alpha, _UNDATED, '---\ntitle: "x"\nlifecycle: closed\n---\n\nbody\n')
    return {"alpha": alpha, "beta": beta, "tmp": tmp_path}


def test_dry_run_is_the_default_and_reports_per_repo_counts_without_writing(fleet):
    alpha = fleet["alpha"]
    before = _git(alpha, "status", "--porcelain")
    head = _git(alpha, "rev-parse", "HEAD")

    result = _memo_sweep_ref_only({}, repo_root=None)

    assert result["exit_code"] == 0 and result["dry_run"] is True, result
    a = result["repos"]["alpha"]
    assert (a["refs"], a["inbox_files"]) == (4, 1)
    assert a["ref_only"] == {"open": 2, "terminal": 2, "unreadable": 0}
    assert a["age"]["open"]["<7d"] == 1 and a["age"]["open"][">90d"] == 1
    assert a["age"]["terminal"]["30-90d"] == 1 and a["age"]["terminal"]["unknown"] == 1
    assert result["repos"]["beta"]["ref_only"] == {"open": 0, "terminal": 0, "unreadable": 0}
    assert result["not_local"] == ["gone"]
    assert _git(alpha, "status", "--porcelain") == before
    assert _git(alpha, "rev-parse", "HEAD") == head


def test_a_filename_present_in_either_corpus_root_at_any_depth_is_not_ref_only(fleet):
    alpha = fleet["alpha"]
    nested = alpha / "cross-repo" / "archive" / "2026-10"
    nested.mkdir(parents=True)
    (nested / _OPEN).write_text("archived by hand\n", encoding="utf-8")
    (alpha / "cross-repo" / "inbox" / _TERMINAL).write_text("already here\n", encoding="utf-8")

    result = _memo_sweep_ref_only({"dry_run": True}, repo_root=None)

    assert result["repos"]["alpha"]["ref_only"] == {"open": 1, "terminal": 1, "unreadable": 0}


def test_apply_materialises_open_into_the_inbox_and_files_terminal_into_the_archive(fleet):
    alpha = fleet["alpha"]
    existing = alpha / "cross-repo" / "inbox" / ".gitkeep"
    existing_bytes = existing.read_bytes()

    result = _memo_sweep_ref_only({"dry_run": False, "repos": ["alpha"], "limit": 10}, repo_root=None)

    assert result["exit_code"] == 0, result
    assert result["materialized"] == 4 and result["remaining"] == 0
    assert {r["action"] for r in result["acted"]} == {"materialized", "filed"}
    assert _tracked(alpha, f"cross-repo/inbox/{_OPEN}")
    assert (alpha / "cross-repo" / "inbox" / _OPEN).read_text(encoding="utf-8") == _memo("open")
    assert _tracked(alpha, f"cross-repo/archive/{_TERMINAL}")
    filed = (alpha / "cross-repo" / "archive" / _TERMINAL).read_text(encoding="utf-8")
    assert filed.startswith(_memo("actioned", created="2026-10-01").rstrip("\n"))
    assert filed.rstrip("\n").endswith("filed by memo.sweep_ref_only 2026-11-14")
    assert not (alpha / "cross-repo" / "inbox" / _TERMINAL).exists()
    assert existing.read_bytes() == existing_bytes
    assert "memo.sweep_ref_only" in _git(alpha, "log", "-1", "--format=%B")

    rerun = _memo_sweep_ref_only({}, repo_root=None)
    assert rerun["repos"]["alpha"]["ref_only"] == {"open": 0, "terminal": 0, "unreadable": 0}


def test_apply_never_overwrites_a_file_that_appeared_at_the_target(fleet, monkeypatch):
    alpha = fleet["alpha"]
    target = alpha / "cross-repo" / "inbox" / _OPEN
    real_scan = memo_sweep_ref_only._scan_repo

    def scan_then_a_peer_writes(path, today, *, keep_content):
        scan = real_scan(path, today, keep_content=keep_content)
        target.write_text("peer wrote this first\n", encoding="utf-8")
        return scan

    monkeypatch.setattr(memo_sweep_ref_only, "_scan_repo", scan_then_a_peer_writes)

    result = _memo_sweep_ref_only({"dry_run": False, "repos": ["alpha"], "limit": 10}, repo_root=None)

    assert target.read_text(encoding="utf-8") == "peer wrote this first\n"
    assert [r["id"] for r in result["skipped"]] == [f"alpha:{_OPEN}"]
    assert result["materialized"] == 3


def test_limit_bounds_one_apply_call_and_remaining_counts_the_rest(fleet):
    result = _memo_sweep_ref_only({"dry_run": False, "repos": ["alpha"], "limit": 1}, repo_root=None)

    assert result["materialized"] == 1 and result["remaining"] == 3
    again = _memo_sweep_ref_only({"dry_run": False, "repos": ["alpha"], "limit": 10}, repo_root=None)
    assert again["materialized"] == 3 and again["remaining"] == 0


def test_a_declined_commit_is_a_named_failure_and_the_memo_stays_ref_only(fleet):
    alpha = fleet["alpha"]
    (alpha / ".git" / "refs" / "heads" / "main.lock").write_text("", encoding="utf-8")

    result = _memo_sweep_ref_only({"dry_run": False, "repos": ["alpha"], "limit": 1}, repo_root=None)

    assert result["exit_code"] == 2, result
    assert result["failed"][0]["reason"].startswith("materialize_commit_declined")
    assert result["materialized"] == 0 and result["remaining"] == 4
    assert [p.name for p in (alpha / "cross-repo" / "inbox").glob("*.md")] == []


def test_an_unreadable_anchor_blob_is_counted_and_fails_on_apply(fleet):
    alpha = fleet["alpha"]
    common = resolve_git_common_dir(alpha)
    blob = write_object(common, b"blob", b"\xff\xfe not utf-8 \x80")
    ref = common / "refs" / "coordinator" / "inbox" / "binary-ask.md" / ("b" * 40)
    ref.parent.mkdir(parents=True)
    ref.write_text(blob + "\n", encoding="utf-8")

    dry = _memo_sweep_ref_only({}, repo_root=None)
    assert dry["repos"]["alpha"]["ref_only"]["unreadable"] == 1

    applied = _memo_sweep_ref_only({"dry_run": False, "repos": ["alpha"], "limit": 50}, repo_root=None)
    assert [f["id"] for f in applied["failed"]] == ["alpha:binary-ask.md"]
    assert applied["materialized"] == 4


def test_repos_filter_and_unknown_names(fleet):
    only = _memo_sweep_ref_only({"repos": ["repos.beta"]}, repo_root=None)
    assert list(only["repos"]) == ["beta"] and only["not_local"] == []

    bad = _memo_sweep_ref_only({"repos": ["nope"]}, repo_root=None)
    assert bad["exit_code"] == 1


@pytest.mark.parametrize("params", [{"dry_run": "no"}, {"limit": 0}, {"repos": "alpha"}, {"surprise": 1}])
def test_bad_params_are_setup_errors(params, fleet):
    assert _memo_sweep_ref_only(params, repo_root=None)["exit_code"] == 1

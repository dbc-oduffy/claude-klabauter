"""
coordinator_core.ops.fleet.tests.test_delete_superseded_decisions

Tier-T tests for `fleet.delete_superseded_decisions` (P065-C1). Real git in
tmp_path, one small corpus — mirrors this directory's sibling real-git
fixtures (`test_archive_actioned_memos.py`). One assertion each, per the
plan row's own list (a)-(i).

Calls `_handler` directly (not through the op registry) with a real
`common_dir`, matching the precedent's own import discipline.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.fleet._sweep_receipt import receipt_path
from coordinator_core.ops.fleet.delete_superseded_decisions import (
    _REASON_MAX_ID_FLOOR,
    _handler,
    _live_citations,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort — test-only real-git spawn.
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=_GIT_ENV, timeout=15,
        stdin=subprocess.DEVNULL, **no_console_creationflags(),
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result


def _common_dir(repo: Path) -> Path:
    result = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(result.stdout.strip()).resolve()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _seed_dr(repo: Path, name: str, fm_extra: str, commit: bool = True) -> Path:
    path = repo / "docs" / "decisions" / name
    _write(
        path,
        f'---\ntitle: "{name}"\n{fm_extra}\n---\n\nBody.\n',
    )
    if commit:
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-q", "-m", f"add {name}")
    return path


def _seed_live_file(repo: Path, relpath: str, text: str) -> Path:
    path = repo / relpath
    _write(path, text)
    _git(repo, "add", relpath)
    _git(repo, "commit", "-q", "-m", f"add {relpath}")
    return path


def test_a_dry_run_lists_uncited_superseded_only(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    _seed_dr(repo, "DR-100-foo.md", "status: accepted\n")
    _seed_dr(repo, "DR-101-bar.md", 'status: superseded\nsuperseded_by: "DR-100"\n')
    _seed_dr(repo, "DR-102-ceiling.md", "status: accepted\n")  # keeps 101 off the max-id floor
    common_dir = _common_dir(repo)

    result = _handler(
        {"mode": "already-terminal", "dry_run": True, "cap": 10}, repo_root=common_dir,
    )
    assert result["exit_code"] == 0
    ids = {c["id"] for c in result["candidates"]}
    assert ids == {"docs/decisions/DR-101-bar.md"}


def test_b_live_citation_refuses_with_path_line(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    _seed_dr(repo, "DR-200-foo.md", "status: accepted\n")
    _seed_dr(repo, "DR-201-bar.md", 'status: superseded\nsuperseded_by: "DR-200"\n')
    _seed_dr(repo, "DR-202-ceiling.md", "status: accepted\n")
    _seed_live_file(repo, "docs/wiki/stray.md", "See DR-201 for the old ruling.\n")
    common_dir = _common_dir(repo)

    result = _handler(
        {"mode": "already-terminal", "dry_run": True, "cap": 10}, repo_root=common_dir,
    )
    ids = {c["id"] for c in result["candidates"]}
    assert "docs/decisions/DR-201-bar.md" not in ids

    result2 = _handler(
        {
            "mode": "already-terminal", "dry_run": False, "cap": 10,
            "candidate_ids": ["docs/decisions/DR-201-bar.md"],
        },
        repo_root=common_dir,
    )
    skipped_reasons = {s["id"]: s["reason"] for s in result2["skipped"]}
    reason = skipped_reasons["docs/decisions/DR-201-bar.md"]
    assert "live-citation" in reason
    assert "docs/wiki/stray.md:1" in reason


def test_c_citation_in_successor_file_or_same_line_does_not_refuse(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    _seed_dr(repo, "DR-300-foo.md", 'status: accepted\nsupersedes: "DR-301"\n')
    _seed_dr(repo, "DR-301-bar.md", 'status: superseded\nsuperseded_by: "DR-300"\n')
    _seed_dr(repo, "DR-302-ceiling.md", "status: accepted\n")
    # Successor's own file names the victim — exempt.
    _seed_live_file(repo, "docs/wiki/note.md", "DR-301 (since superseded by DR-300) is history.\n")
    common_dir = _common_dir(repo)

    result = _handler(
        {"mode": "already-terminal", "dry_run": True, "cap": 10}, repo_root=common_dir,
    )
    ids = {c["id"] for c in result["candidates"]}
    assert "docs/decisions/DR-301-bar.md" in ids


def test_d_citation_under_state_or_archive_does_not_refuse(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    _seed_dr(repo, "DR-400-foo.md", "status: accepted\n")
    _seed_dr(repo, "DR-401-bar.md", 'status: superseded\nsuperseded_by: "DR-400"\n')
    _seed_dr(repo, "DR-402-ceiling.md", "status: accepted\n")
    _seed_live_file(repo, "state/handoffs/old.md", "DR-401 was the ruling then.\n")
    _seed_live_file(repo, "archive/specs/old.md", "DR-401 was the ruling then.\n")
    common_dir = _common_dir(repo)

    result = _handler(
        {"mode": "already-terminal", "dry_run": True, "cap": 10}, repo_root=common_dir,
    )
    ids = {c["id"] for c in result["candidates"]}
    assert "docs/decisions/DR-401-bar.md" in ids


def test_e_no_successor_victim_cited_anywhere_live_is_refused(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    _seed_dr(repo, "DR-500-bar.md", "status: superseded\n")
    _seed_live_file(repo, "docs/wiki/note.md", "DR-500 still cited, no successor.\n")
    common_dir = _common_dir(repo)

    result = _handler(
        {"mode": "already-terminal", "dry_run": True, "cap": 10}, repo_root=common_dir,
    )
    ids = {c["id"] for c in result["candidates"]}
    assert "docs/decisions/DR-500-bar.md" not in ids


def test_f_highest_numbered_id_refused_max_id_floor(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    _seed_dr(repo, "DR-600-foo.md", "status: accepted\n")
    _seed_dr(repo, "DR-601-bar.md", 'status: superseded\nsuperseded_by: "DR-600"\n')
    common_dir = _common_dir(repo)

    result = _handler(
        {"mode": "already-terminal", "dry_run": True, "cap": 10}, repo_root=common_dir,
    )
    ids = {c["id"] for c in result["candidates"]}
    assert "docs/decisions/DR-601-bar.md" not in ids


def test_g_act_deletes_in_one_commit_with_recovery_sentence(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    _seed_dr(repo, "DR-700-foo.md", "status: accepted\n")
    _seed_dr(repo, "DR-701-bar.md", 'status: superseded\nsuperseded_by: "DR-700"\n')
    _seed_dr(repo, "DR-702-ceiling.md", "status: accepted\n")
    common_dir = _common_dir(repo)

    result = _handler(
        {
            "mode": "already-terminal", "dry_run": False, "cap": 10,
            "candidate_ids": ["docs/decisions/DR-701-bar.md"],
        },
        repo_root=common_dir,
    )
    assert result["exit_code"] == 0
    assert not (repo / "docs" / "decisions" / "DR-701-bar.md").exists()
    acted = result["acted"]
    assert len(acted) == 1
    assert "stays in git history" in acted[0]["recovery"]

    log = _git(repo, "log", "--diff-filter=D", "--format=%s")
    assert "fleet: delete 1 superseded decision record(s)" in log.stdout

    receipt = receipt_path(common_dir)
    assert receipt.exists()
    lines = receipt.read_text(encoding="utf-8").splitlines()
    assert any("stays in git history" in line for line in lines)


def test_h_act_time_status_drift_is_skipped_never_deleted(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    _seed_dr(repo, "DR-800-foo.md", "status: accepted\n")
    dr_path = _seed_dr(repo, "DR-801-bar.md", 'status: superseded\nsuperseded_by: "DR-800"\n')
    common_dir = _common_dir(repo)

    # Flip back to accepted after the dry_run preview would have seen it.
    _write(dr_path, '---\ntitle: "DR-801-bar.md"\nstatus: accepted\n---\n\nBody.\n')
    _git(repo, "add", "docs/decisions/DR-801-bar.md")
    _git(repo, "commit", "-q", "-m", "flip back")

    result = _handler(
        {
            "mode": "already-terminal", "dry_run": False, "cap": 10,
            "candidate_ids": ["docs/decisions/DR-801-bar.md"],
        },
        repo_root=common_dir,
    )
    assert result["acted"] == []
    reasons = {s["id"]: s["reason"] for s in result["skipped"]}
    assert reasons["docs/decisions/DR-801-bar.md"].startswith("terminality-drift")
    assert dr_path.exists()


def test_i_empty_candidate_path_spawns_no_git_grep(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    _seed_dr(repo, "DR-900-foo.md", "status: accepted\n")
    common_dir = _common_dir(repo)

    with patch(
        "coordinator_core.ops.fleet.delete_superseded_decisions._live_citations",
        wraps=_live_citations,
    ) as spy:
        result = _handler(
            {"mode": "already-terminal", "dry_run": True, "cap": 10}, repo_root=common_dir,
        )
    assert result["candidates"] == []
    spy.assert_not_called()

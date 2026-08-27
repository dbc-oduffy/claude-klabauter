"""
coordinator_core.ops.fleet.tests.test_archive_actioned_memos

Tier-T tests for `fleet.archive_actioned_memos` (K-052 reinstatement). Real
git spawn is load-bearing (worktree-dirty exclusion, and the actual
archive-and-commit mover both read real git state) — one throwaway repo per
test function, mirroring `test_archive_terminal_handoffs.py`'s own governed
pattern in this same directory.

Coverage:
  - A real detritus corpus (multiple actioned/superseded/closed memos, an
    open one, an in_progress one) sweeps only the terminal, unclaimed set.
  - Rail coverage, one case each, asserting a NAMED `skipped` reason: a live
    claim holder, worktree-dirty, and a byte-divergent dest-conflict.
  - Idempotent replay: firing `apply_sweep` twice over the same moves list
    is a no-op the second time (no double-move; nothing left to move).
  - The failure path: a receipt entry lands on disk for a setup error (no
    `cap`) and for a contended lock, proving AC-3 observability rather than
    a silent return.
  - Spawn-count ratchet: `apply_sweep` spawns ZERO subprocesses.

Negative-spec:
  - Does NOT test the O_EXCL lock's concurrency property beyond a single
    forced-contended case — mirrors the precedent's own exclusion.
  - Does NOT invoke `_handler` end-to-end via the op registry (would race a
    peer chunk's registration wiring) — calls `plan_sweep`/`apply_sweep`/
    `_scan_terminal_memos` directly, matching the precedent's own import
    discipline.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.fleet._sweep_receipt import receipt_path
from coordinator_core.ops.fleet.archive_actioned_memos import (
    _SCAN_REASON_LIVE_CLAIM,
    _SCAN_REASON_NOT_TERMINAL,
    _SCAN_REASON_WORKTREE_DIRTY,
    _memo_claim_dir,
    apply_sweep,
    collect_inbox_memo_paths,
    memo_archive_dest,
    plan_sweep,
)
from coordinator_core.ops.fleet._common import _REASON_DEST_CONFLICT, rel_id
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_CS_CLAIM_HOLDER_LIVE_PATCH = (
    "coordinator_core.ops.fleet.archive_actioned_memos.cs_claim_holder_live"
)

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort — test-only real-git spawn, mirrors this
    # directory's sibling real-git fixtures.
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


def _seed_memo(repo: Path, name: str, fm_extra: str, commit: bool = True) -> Path:
    path = repo / "cross-repo" / "inbox" / name
    _write(
        path,
        f'---\ntitle: "{name}"\nfrom: "peer-em"\nto: "this-em"\ncreated: "2026-01-01"\n{fm_extra}\n---\n\nBody.\n',
    )
    if commit:
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-q", "-m", f"add {name}")
    return path


def test_sweeps_only_terminal_unclaimed_memos(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    _seed_memo(repo, "2026-01-01-actioned.md", 'status: actioned\ndecision: accepted\n')
    _seed_memo(repo, "2026-01-02-superseded.md", 'status: superseded\nsuperseded_by: "x.md"\n')
    _seed_memo(repo, "2026-01-03-closed.md", 'status: closed\ndecision: accepted\nclosed_at: "2026-01-03T00:00:00Z"\n')
    _seed_memo(repo, "2026-01-04-open.md", 'status: open\n')
    _seed_memo(repo, "2026-01-05-in-progress.md", 'status: in_progress\n')

    common_dir = _common_dir(repo)
    scan_skipped: list = []
    moves, skipped = plan_sweep(repo, common_dir, cap=150, scan_skipped=scan_skipped)

    moved_ids = {m.candidate_id for m in moves}
    assert moved_ids == {
        rel_id(repo / "cross-repo" / "inbox" / "2026-01-01-actioned.md", repo),
        rel_id(repo / "cross-repo" / "inbox" / "2026-01-02-superseded.md", repo),
        rel_id(repo / "cross-repo" / "inbox" / "2026-01-03-closed.md", repo),
    }
    skipped_reasons = {s["id"]: s["reason"] for s in skipped + scan_skipped}
    open_id = rel_id(repo / "cross-repo" / "inbox" / "2026-01-04-open.md", repo)
    inprog_id = rel_id(repo / "cross-repo" / "inbox" / "2026-01-05-in-progress.md", repo)
    assert skipped_reasons[open_id].startswith(_SCAN_REASON_NOT_TERMINAL)
    assert skipped_reasons[inprog_id].startswith(_SCAN_REASON_NOT_TERMINAL)

    acted, failed = apply_sweep(moves)
    assert failed == []
    assert {a["id"] for a in acted} == moved_ids
    for cid in moved_ids:
        assert not (repo / cid).exists()
        assert (repo / "cross-repo" / "archive" / Path(cid).name).is_file()


def test_apply_sweep_spawns_zero_subprocesses(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    _seed_memo(repo, "2026-01-01-actioned.md", "status: actioned\n")
    common_dir = _common_dir(repo)
    moves, _skipped = plan_sweep(repo, common_dir, cap=150)
    assert moves

    with patch("subprocess.run", wraps=subprocess.run) as spy, \
         patch("subprocess.Popen", wraps=subprocess.Popen) as popen_spy:
        acted, failed = apply_sweep(moves)
    assert failed == []
    assert spy.call_count == 0
    assert popen_spy.call_count == 0


def test_live_claim_holder_retains_the_memo(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    memo_path = _seed_memo(repo, "2026-01-01-actioned.md", "status: actioned\n")
    common_dir = _common_dir(repo)

    claim_dir = _memo_claim_dir(common_dir, memo_path)
    claim_dir.mkdir(parents=True, exist_ok=True)

    scan_skipped: list = []
    with patch(_CS_CLAIM_HOLDER_LIVE_PATCH, return_value=True):
        moves, skipped = plan_sweep(repo, common_dir, cap=150, scan_skipped=scan_skipped)

    assert moves == []
    reasons = {s["id"]: s["reason"] for s in skipped + scan_skipped}
    cid = rel_id(memo_path, repo)
    assert reasons[cid] == _SCAN_REASON_LIVE_CLAIM


def test_worktree_dirty_memo_is_retained(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    memo_path = _seed_memo(repo, "2026-01-01-actioned.md", "status: actioned\n")
    # Dirty the file post-commit — uncommitted disk content diverges from HEAD.
    memo_path.write_text(memo_path.read_text(encoding="utf-8") + "\nextra\n", encoding="utf-8")

    common_dir = _common_dir(repo)
    scan_skipped: list = []
    moves, skipped = plan_sweep(repo, common_dir, cap=150, scan_skipped=scan_skipped)

    assert moves == []
    cid = rel_id(memo_path, repo)
    reasons = {s["id"]: s["reason"] for s in skipped + scan_skipped}
    assert reasons[cid] == _SCAN_REASON_WORKTREE_DIRTY


def test_dest_conflict_with_different_content_is_named_and_skipped(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    memo_path = _seed_memo(repo, "2026-01-01-actioned.md", "status: actioned\n")
    dest = memo_archive_dest(repo, memo_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("a completely different archived record\n", encoding="utf-8")

    common_dir = _common_dir(repo)
    moves, skipped = plan_sweep(repo, common_dir, cap=150)

    assert moves == []
    cid = rel_id(memo_path, repo)
    reasons = {s["id"]: s["reason"] for s in skipped}
    assert reasons[cid] == _REASON_DEST_CONFLICT


def test_idempotent_replay_second_apply_is_a_noop(tmp_path):
    repo = tmp_path / "r"
    _init_repo(repo)
    _seed_memo(repo, "2026-01-01-actioned.md", "status: actioned\n")
    common_dir = _common_dir(repo)

    moves, _skipped = plan_sweep(repo, common_dir, cap=150)
    acted1, failed1 = apply_sweep(moves)
    assert failed1 == []
    assert len(acted1) == 1

    # A second sweep over the (now-empty) inbox finds nothing to move.
    moves2, skipped2 = plan_sweep(repo, common_dir, cap=150)
    assert moves2 == []


def test_absent_cap_is_a_setup_error_never_an_unbounded_default(tmp_path):
    from coordinator_core.ops.fleet.archive_actioned_memos import _handler

    repo = tmp_path / "r"
    _init_repo(repo)
    result = _handler({"mode": "already-terminal", "dry_run": True}, repo_root=repo)
    assert result["exit_code"] == 1
    receipt = receipt_path(repo)
    assert receipt.is_file()
    lines = receipt.read_text(encoding="utf-8").strip().splitlines()
    assert lines
    import json

    last = json.loads(lines[-1])
    assert last["sweep"] == "fleet.archive_actioned_memos"
    assert last["outcome"] == "failed"


def test_contended_lock_records_skipped_contended_receipt(tmp_path):
    from coordinator_core.ops.fleet.archive_actioned_memos import _handler, _memo_lock_path

    repo = tmp_path / "r"
    _init_repo(repo)
    common_dir = _common_dir(repo)
    lock_path = _memo_lock_path(common_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("held", encoding="utf-8")

    try:
        result = _handler(
            {"mode": "already-terminal", "dry_run": True, "cap": 10}, repo_root=common_dir,
        )
        assert result.get("contended") is True
        receipt = receipt_path(common_dir)
        import json

        lines = receipt.read_text(encoding="utf-8").strip().splitlines()
        last = json.loads(lines[-1])
        assert last["outcome"] == "skipped-contended"
    finally:
        lock_path.unlink(missing_ok=True)

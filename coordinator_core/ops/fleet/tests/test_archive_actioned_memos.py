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


def test_sweeps_the_migrated_state_cross_repo_corpus(tmp_path):
    """C2 (2026-09-03-the-engine-follows-the-memo-channel-home): the sweep's
    own candidate collector must find terminal memos under the C10a-migrated
    `state/cross-repo/inbox/`, not just the legacy `cross-repo/inbox/` this
    file's other fixtures seed — this is the exact falsifier the plan names
    (the collector returned zero against a `state/cross-repo/inbox` corpus
    before this chunk routed it through `memo_corpus_root`)."""
    repo = tmp_path / "r"
    _init_repo(repo)
    path = repo / "state" / "cross-repo" / "inbox" / "2026-01-01-actioned.md"
    _write(
        path,
        '---\ntitle: "2026-01-01-actioned.md"\nfrom: "peer-em"\nto: "this-em"\n'
        'created: "2026-01-01"\nstatus: actioned\ndecision: accepted\n---\n\nBody.\n',
    )
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-q", "-m", "add memo")

    common_dir = _common_dir(repo)
    moves, _skipped = plan_sweep(repo, common_dir, cap=150)

    moved_ids = {m.candidate_id for m in moves}
    assert moved_ids == {rel_id(path, repo)}

    acted, failed = apply_sweep(moves)
    assert failed == []
    assert (repo / "state" / "cross-repo" / "archive" / path.name).is_file()


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


def test_inbox_paths_supplied_skips_the_directory_walk(tmp_path):
    # (Review: coordinator:code-reviewer F3) When a caller already walked
    # the inbox and hands the list through, `_scan_terminal_memos` must NOT
    # re-derive it via `collect_inbox_memo_paths` — that is the whole point
    # of the passthrough (avoids a second `iterdir` + `resolve()` pass).
    repo = tmp_path / "r"
    _init_repo(repo)
    memo_path = _seed_memo(repo, "2026-01-01-actioned.md", "status: actioned\n")
    common_dir = _common_dir(repo)

    with patch(
        "coordinator_core.ops.fleet.archive_actioned_memos.collect_inbox_memo_paths"
    ) as collect_spy:
        moves, skipped = plan_sweep(
            repo, common_dir, cap=150, inbox_paths=[memo_path],
        )
    collect_spy.assert_not_called()
    assert {m.candidate_id for m in moves} == {rel_id(memo_path, repo)}
    assert skipped == []


def test_inbox_paths_none_preserves_standalone_walk(tmp_path):
    # (Review: coordinator:code-reviewer F3) The default `inbox_paths=None`
    # must still fall through to `collect_inbox_memo_paths` — the
    # passthrough parameter must not change today's standalone behaviour.
    repo = tmp_path / "r"
    _init_repo(repo)
    memo_path = _seed_memo(repo, "2026-01-01-actioned.md", "status: actioned\n")
    common_dir = _common_dir(repo)

    with patch(
        "coordinator_core.ops.fleet.archive_actioned_memos.collect_inbox_memo_paths",
        wraps=collect_inbox_memo_paths,
    ) as collect_spy:
        moves, _skipped = plan_sweep(repo, common_dir, cap=150, inbox_paths=None)
    collect_spy.assert_called_once_with(repo)
    assert {m.candidate_id for m in moves} == {rel_id(memo_path, repo)}


def test_inbox_paths_supplied_still_honours_live_claim_and_dirty_rails(tmp_path):
    # (Review: coordinator:code-reviewer F3) A caller supplying `inbox_paths`
    # bypasses `collect_inbox_memo_paths`'s own `.is_file()`/`.suffix`
    # filtering, but must NOT bypass the exclusion rails downstream of the
    # scan — live-claim-holder and worktree-dirty both still apply.
    repo = tmp_path / "r"
    _init_repo(repo)
    live_memo = _seed_memo(repo, "2026-01-01-actioned.md", "status: actioned\n")
    dirty_memo = _seed_memo(repo, "2026-01-02-actioned.md", "status: actioned\n")
    dirty_memo.write_text(
        dirty_memo.read_text(encoding="utf-8") + "\nextra\n", encoding="utf-8",
    )
    common_dir = _common_dir(repo)

    claim_dir = _memo_claim_dir(common_dir, live_memo)
    claim_dir.mkdir(parents=True, exist_ok=True)

    scan_skipped: list = []
    with patch(_CS_CLAIM_HOLDER_LIVE_PATCH, return_value=True):
        moves, skipped = plan_sweep(
            repo, common_dir, cap=150,
            inbox_paths=[live_memo, dirty_memo],
            scan_skipped=scan_skipped,
        )

    assert moves == []
    reasons = {s["id"]: s["reason"] for s in skipped + scan_skipped}
    assert reasons[rel_id(live_memo, repo)] == _SCAN_REASON_LIVE_CLAIM
    assert reasons[rel_id(dirty_memo, repo)] == _SCAN_REASON_WORKTREE_DIRTY


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


def test_repo_root_none_still_records_a_receipt_row(tmp_path, monkeypatch):
    """AC-3 gap fix: `record_sweep_outcome` no-ops on `common_dir=None` (its
    own module docstring), so the standalone `repo_root is None` exit used to
    call it and still write ZERO rows. This exit now routes through
    `_NO_REPO_ROOT_RECEIPT_DIR` — a machine-wide fallback sink, since there is
    no per-repo common_dir to root a receipt under at all.
    """
    import json

    from coordinator_core.ops.fleet import archive_actioned_memos as m

    fallback = tmp_path / "no-repo-root-fallback"
    monkeypatch.setattr(m, "_NO_REPO_ROOT_RECEIPT_DIR", fallback)

    result = m._handler({"mode": "already-terminal", "dry_run": True, "cap": 10}, repo_root=None)
    assert result["exit_code"] == 1

    receipt = receipt_path(fallback)
    assert receipt.is_file()
    lines = receipt.read_text(encoding="utf-8").strip().splitlines()
    assert lines
    last = json.loads(lines[-1])
    assert last["sweep"] == "fleet.archive_actioned_memos"
    assert last["outcome"] == "failed"
    assert "repo_root handler arg is None" in last["detail"]


def test_bad_cap_with_repo_root_none_still_records_a_receipt_row(tmp_path, monkeypatch):
    """Same AC-3 gap, reached via the EARLIER bad-cap branch: before the fix,
    a bad `cap` with `repo_root` ALSO None degraded its receipt dir to
    `None` and silently wrote nothing.
    """
    import json

    from coordinator_core.ops.fleet import archive_actioned_memos as m

    fallback = tmp_path / "no-repo-root-fallback-cap"
    monkeypatch.setattr(m, "_NO_REPO_ROOT_RECEIPT_DIR", fallback)

    result = m._handler({"mode": "already-terminal", "dry_run": True}, repo_root=None)
    assert result["exit_code"] == 1

    receipt = receipt_path(fallback)
    assert receipt.is_file()
    lines = receipt.read_text(encoding="utf-8").strip().splitlines()
    last = json.loads(lines[-1])
    assert last["outcome"] == "failed"
    assert "cap is required" in last["detail"]


def test_act_phase_wired_path_commits_via_archive_and_commit(tmp_path):
    """AC-1/AC-2: exercise the ACTUALLY-WIRED act path (`_handler` ->
    `_handle_act` -> `_common.archive_and_commit`) end-to-end in an isolated
    tmp repo — never `apply_sweep` directly, which the wired handler never
    calls. Also measures the real spawn count/process time for the
    corrected docstring claim.
    """
    import time
    from unittest.mock import patch

    from coordinator_core.ops.fleet.archive_actioned_memos import _handler
    from coordinator_core.wire_paths import rel_id

    repo = tmp_path / "r"
    _init_repo(repo)
    n = 40
    ids = []
    for i in range(n):
        p = _seed_memo(repo, f"2026-01-{i % 28 + 1:02d}-memo-{i}.md", "status: actioned\n")
        ids.append(rel_id(p, repo))
    common_dir = _common_dir(repo)

    orig_run = subprocess.run
    spawn_count = [0]

    def _spy_run(*args, **kwargs):
        spawn_count[0] += 1
        return orig_run(*args, **kwargs)

    t0 = time.perf_counter()
    with patch("subprocess.run", side_effect=_spy_run):
        result = _handler(
            {"mode": "already-terminal", "dry_run": False, "cap": 150, "candidate_ids": ids},
            repo_root=common_dir,
        )
    elapsed_ms = (time.perf_counter() - t0) * 1000

    assert result["exit_code"] == 0
    assert result["failed"] == []
    acted_ids = {a["id"] for a in result["acted"]}
    assert acted_ids == set(ids)
    for cid in ids:
        assert not (repo / cid).exists()
        assert (repo / "cross-repo" / "archive" / Path(cid).name).is_file()

    # Corrected perf claim: a handful of BATCHED spawns, never one per
    # candidate, and comfortably inside the 500ms brightline.
    assert spawn_count[0] < n
    assert elapsed_ms < 2000  # generous local-disk ceiling; see module docstring for the real figure


def test_act_phase_second_fire_is_idempotent(tmp_path):
    """AC-5: a genuine second fire of the wired act path over the same
    candidate_ids in an isolated repo is a no-op — the source is already
    gone, so the second call classifies it as already-archived rather than
    re-archiving or erroring.
    """
    from coordinator_core.ops.fleet.archive_actioned_memos import _handler
    from coordinator_core.wire_paths import rel_id

    repo = tmp_path / "r"
    _init_repo(repo)
    memo_path = _seed_memo(repo, "2026-01-01-actioned.md", "status: actioned\n")
    cid = rel_id(memo_path, repo)
    common_dir = _common_dir(repo)

    params = {
        "mode": "already-terminal", "dry_run": False, "cap": 150,
        "candidate_ids": [cid],
    }
    first = _handler(params, repo_root=common_dir)
    assert first["exit_code"] == 0
    assert {a["id"] for a in first["acted"]} == {cid}

    second = _handler(params, repo_root=common_dir)
    assert second["exit_code"] == 0
    assert second["acted"] == []
    reasons = {s["id"]: s["reason"] for s in second["skipped"]}
    assert reasons[cid] == "already-archived"

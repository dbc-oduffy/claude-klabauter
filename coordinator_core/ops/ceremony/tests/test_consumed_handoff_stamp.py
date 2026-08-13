"""
coordinator_core.ops.ceremony.tests.test_consumed_handoff_stamp

Tests for the C5 consumed-handoff ship-stamp + R1-R4 ship-drift correctness
seam (coordinator_core/ops/ceremony/consumed_handoff_stamp.py).

Coverage:
  (a) reject_future_dated_far_future     — R3/AC8: far-future filename ts rejected
  (b) reject_future_dated_within_skew    — R3/AC8: near-future ts within skew accepted
  (c) reject_future_dated_unparseable    — R3/AC8: unparseable filename accepted (graceful-skip)
  (d) reject_future_dated_not_vs_started_at — R3/AC8: no started_at comparison exists in the API
  (d2) reject_future_dated_accepts_utc_plus_14_local_producer_case — C1 regression
      (2026-07-23 cross-repo/inbox timezone-ambiguity fix): the memo's real confirmed
      case, a LOCAL-time-stamped filename from a UTC-ahead machine, is accepted
  (d3) reject_future_dated_accepts_up_to_utc_plus_14_boundary — C1: the full real
      UTC-offset span (+14h) is accepted
  (d4) reject_future_dated_still_rejects_gross_future_date /
      reject_future_dated_still_rejects_next_year — C1: the widened bound does not
      neuter genuinely gross future-dating
  (e) redrive_liveness_recheck_catches_concurrent_unconsume — AC6 cross-session guard:
      a handoff mutated (claimed_by cleared) between the initial resolve and the
      locked re-derive is no longer in the re-derived set
  (f) redrive_liveness_recheck_finds_archived — re-derive still finds a handoff a
      concurrent session has swept to archive/handoffs/ (still claimed_by: sid)
  (g) post_commit_noop_when_not_chain_terminal
  (h) post_commit_happy_path_stamps_and_ships — real handoff.stamp + handoff.transition
      ship verb, stamp-BEFORE-ship ordering (schema-safe deviation — see module
      docstring "DEVIATION")
  (i) post_commit_retains_on_live_children — R4(a): exit_code=0 retains (monkeypatched)
  (j) post_commit_retains_on_indeterminate — R4(a): exit_code=2 retains, never `referenced` (monkeypatched)
  (k) post_commit_empty_consumed_set_loud_report — R2/AC7
  (l) post_commit_future_dated_rejected — R3/AC8 wired into the post-commit pass
  (m) post_commit_live_children_retain_exit_2 — R4(a) wired into the post-commit pass (monkeypatched)
  (n) post_commit_happy_path_stamps_and_follow_up_commits_pushed — AC17 end-to-end,
      real ops + real git remote: shipped_in stamped from the REAL committed_sha,
      follow-up commit lands with an explicit pathspec, is pushed, and the working
      tree is clean afterward (never left as an unswept dirty edit)
  (o) ship_drift_regression_full_pass — the ship-drift regression: a chain-terminal
      close whose predecessor was claimed_by:sid shortly before ends with that
      predecessor at deployment_state: shipped AND shipped_in: <real committed_sha>
  (p) row7_recheck_catches_successor_authored_after_prelock_guard — Row 7: the
      in-lock recheck (call #2 of the SAME _live_children_guard) catches a live
      child the pre-lock filter (call #1) missed; write aborted, reported
  (q) row7_recheck_indeterminate_still_fails_closed — Row 7: fail-closed
      preserved on the in-lock recheck, not just the pre-lock filter
  (r) row6_peer_write_between_stamp_and_ship_aborts_not_lost — Row 6: a peer
      write landing between the stamp and ship locks aborts the ship (CAS
      mismatch) rather than being silently lost or overwritten
  (s) post_commit_non_shipped_terminal_and_archived_is_noop — 2026-08-13
      coordinator-claude-em memo: a baton flipped to ANY terminal deployment_state
      and swept before the stamp step is a no-op, not a containment failure;
      the two companion tests keep that widening narrow (non-terminal
      archived, and `shipped` with no `shipped_in`, both still fail loud)

Spec backlink:
  coordinator_core/ops/ceremony/consumed_handoff_stamp.py
  docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C5, AC6-AC9, AC17
  2026-07-28 handoff-write-cas spinoff (Row 6/Row 7):
  docs/research/2026-07-28-is-the-jettisoned-ceremony-lock-outer-ho.md § (b)
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pytest

from coordinator_core.ops.ceremony import consumed_handoff_stamp as m
from coordinator_core.ops.ceremony.commit_pipeline import (
    PUSH_MODE_NONE,
    PUSH_MODE_SYNC,
    PUSH_STATUS_DECLINED,
)
from .fixtures.real_git import make_diverged_path, real_git_repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _run(coro) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Repo fixture — real git repo (needed for AC17 follow-up commit/push tests)
# ---------------------------------------------------------------------------


class StampRepo:
    """Lightweight real-git repo fixture for consumed_handoff_stamp tests."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
        )

    @property
    def common_dir(self) -> Path:
        return (self.root / ".git").resolve()

    def seed_handoff(
        self,
        name: str,
        *,
        claimed_by: Optional[str] = None,
        deployment_state: Optional[str] = None,
        claimed_at: Optional[str] = "2026-07-15T10:00:00Z",
        predecessor: Optional[str] = None,
        archived_subdir: Optional[str] = None,
        commit: bool = True,
    ) -> Path:
        """Write (and by default commit) a schema-valid handoff frontmatter file.

        archived_subdir writes under archive/handoffs/<archived_subdir>/<name>
        instead of state/handoffs/<name> (mirrors the live/archive dual scan).
        """
        if archived_subdir is not None:
            path = self.root / "archive" / "handoffs" / archived_subdir / name
        else:
            path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            f'title: "Test Handoff {name}"',
            "created: 2026-07-15",
            "branch: work/test/2026-07-15",
            "status: claimed",
            "category: infra",
            'summary: "Test handoff summary for schema post-cutoff compliance."',
        ]
        if predecessor is not None:
            lines.append(f'predecessor: "{predecessor}"')
        else:
            lines.append("predecessor: null")
        if deployment_state is not None:
            lines.append(f"deployment_state: {deployment_state}")
        if claimed_at is not None:
            lines.append(f"claimed_at: {claimed_at}")
        if claimed_by is not None:
            lines.append(f"claimed_by: {claimed_by}")

        fm = "\n".join(lines)
        path.write_text(f"---\n{fm}\n---\n\n# Handoff\n\nBody.\n", encoding="utf-8")

        if commit:
            self._git("add", "-A")
            self._git("commit", "-m", f"add handoff {name}")
        return path

    def read_handoff(self, relpath: str) -> str:
        return (self.root / relpath).read_text(encoding="utf-8")

    def porcelain_status(self) -> str:
        return self._git("status", "--porcelain").stdout

    def head_sha(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def log_messages(self, remote: bool = False, remote_branch: str = "main") -> str:
        ref = f"origin/{remote_branch}" if remote else "HEAD"
        return self._git("log", "--oneline", ref).stdout


@pytest.fixture
def repo(tmp_path) -> StampRepo:
    root = tmp_path / "repo"
    root.mkdir()
    r = StampRepo(root)
    r._git("init", "-b", "main")
    r._git("config", "user.email", "stamp-test@claude-klabauter.test")
    r._git("config", "user.name", "Stamp Test")
    r._git("config", "commit.gpgsign", "false")
    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    r._git("add", "-A")
    r._git("commit", "-m", "chore: initial skeleton")
    return r


@pytest.fixture
def repo_with_remote(tmp_path, repo) -> StampRepo:
    """repo + a bare 'origin' remote, with an initial push (so a later
    follow-up push has something real to land against).

    C7c (docs/plans/2026-08-08-the-push-leg-that-never-asked-which-branch.md):
    checked out onto `work/test/consumed-handoff-stamp` rather than staying
    on the base `repo` fixture's `main` -- both tests that consume this
    fixture (`test_post_commit_happy_path_stamps_and_follow_up_commits_pushed`,
    `test_ship_drift_regression_full_pass`) assert the push itself LANDING
    (`follow_up_pushed is True`, the sha reaching `origin/main`'s log), which
    the real `work/*`-only push-leg branch policy (commit_pipeline.py, same
    plan) would otherwise decline on `main` -- repair (a): move the fixture
    onto an allowed branch rather than invert the assertion, since the push
    landing is the tests' own subject, not the branch-policy contract.

    The remote-side ref is pushed under the SAME name (`work/test/consumed-
    handoff-stamp`), not renamed to `main` on the remote -- git's default
    `push.default=simple` refuses a bare `git push` (the exact call the
    follow-up-push leg under test makes) when the local and remote branch
    names differ, even with `-u` tracking configured; matching names is what
    the tests' own subject (a bare push landing) actually requires.
    `log_messages(remote=...)` takes an explicit `remote_branch` for this
    reason. `repo_with_remote` is consumed only by these two tests (grepped)
    -- no ripple onto any fixture shared with a currently-green test.
    """
    branch = "work/test/consumed-handoff-stamp"
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    repo._git("checkout", "-b", branch)
    repo._git("remote", "add", "origin", str(bare))
    result = repo._git("push", "-u", "origin", branch)
    assert result.returncode == 0, result.stderr
    return repo


# ---------------------------------------------------------------------------
# R3 (AC8) — future-dated filename plausibility guard
# ---------------------------------------------------------------------------


def test_reject_future_dated_far_future():
    now = datetime(2026, 7, 16, 12, 0, 0)
    paths = ["state/handoffs/2026-08-01_120000_far-future.md"]
    accepted, rejected = m.reject_future_dated(paths, now=now)
    assert accepted == []
    assert rejected == paths


def test_reject_future_dated_within_skew():
    now = datetime(2026, 7, 16, 12, 0, 0)
    # 2 minutes ahead — inside the default 5-minute skew.
    paths = ["state/handoffs/2026-07-16_120200_near-future.md"]
    accepted, rejected = m.reject_future_dated(paths, now=now)
    assert accepted == paths
    assert rejected == []


def test_reject_future_dated_unparseable():
    now = datetime(2026, 7, 16, 12, 0, 0)
    paths = ["state/handoffs/no-date-prefix-here.md"]
    accepted, rejected = m.reject_future_dated(paths, now=now)
    assert accepted == paths
    assert rejected == []


def test_reject_future_dated_date_only_prefix_past_accepted():
    now = datetime(2026, 7, 16, 12, 0, 0)
    paths = ["state/handoffs/2026-07-01-old-style.md"]
    accepted, rejected = m.reject_future_dated(paths, now=now)
    assert accepted == paths
    assert rejected == []


def test_reject_future_dated_accepts_utc_plus_14_local_producer_case():
    """C1 regression: cross-repo/inbox/2026-07-23-claude-central-em-wsc-
    tail-stamp-ship-silent-skip.md's real confirmed case. Coordinator-claude's
    `/handoff` and `/spinoff` skills stamp filenames with LOCAL wall-clock
    time; on a machine east of UTC (up to UTC+14) that filename reads as
    "future" relative to naive-UTC now(). The memo's exact numbers: filename
    `2026-07-23_222148_...` (22:21:48 BST local = 21:21:48 UTC) evaluated
    against `now=21:26 UTC` (the un-widened 5-minute-skew bound tripped by
    ~55 minutes). The widened bound (`_FILENAME_TZ_AMBIGUITY` + `skew`) must
    accept it."""
    now = datetime(2026, 7, 23, 21, 26, 0)
    paths = ["state/handoffs/2026-07-23_222148_344992df-91e7-4d42-8f10-0473f69b4992.md"]
    accepted, rejected = m.reject_future_dated(paths, now=now)
    assert accepted == paths
    assert rejected == []


def test_reject_future_dated_accepts_up_to_utc_plus_14_boundary():
    """A filename timestamped exactly at the widest legitimate real UTC
    offset (+14h, e.g. Kiribati/Line Islands) relative to naive-UTC now()
    is accepted -- the full producer-timezone-ambiguity span, not just the
    memo's BST case."""
    now = datetime(2026, 7, 16, 12, 0, 0)
    # 14h ahead, well within the default 5-minute skew slack on top.
    paths = ["state/handoffs/2026-07-17_015900_local-producer.md"]
    accepted, rejected = m.reject_future_dated(paths, now=now)
    assert accepted == paths
    assert rejected == []


def test_reject_future_dated_still_rejects_gross_future_date():
    """The widened bound does NOT neuter the guard: a genuinely gross
    future-dated filename (weeks/months ahead -- a mis-generated or
    hand-typed filename, not a timezone artifact) is still rejected."""
    now = datetime(2026, 7, 16, 12, 0, 0)
    paths = ["state/handoffs/2026-08-15_120000_gross-future.md"]
    accepted, rejected = m.reject_future_dated(paths, now=now)
    assert accepted == []
    assert rejected == paths


def test_reject_future_dated_still_rejects_next_year():
    """Same as above, an even grosser case (next year)."""
    now = datetime(2026, 7, 16, 12, 0, 0)
    paths = ["state/handoffs/2027-07-16_120000_next-year.md"]
    accepted, rejected = m.reject_future_dated(paths, now=now)
    assert accepted == []
    assert rejected == paths


def test_reject_future_dated_not_compared_against_started_at():
    """R3: the guard's signature carries no started_at parameter at all —
    started_at is never the plausibility bound (AC8 negative-spec)."""
    import inspect

    sig = inspect.signature(m.reject_future_dated)
    assert "started_at" not in sig.parameters


# ---------------------------------------------------------------------------
# R1 (AC6) — liveness re-check / re-derive the consumed set
# ---------------------------------------------------------------------------


def test_redrive_liveness_recheck_catches_concurrent_unconsume(repo):
    sid = "sess-abc123"
    hf = repo.seed_handoff("2026-07-15_100000_pred.md", claimed_by=sid)

    initial = m.redrive_consumed_set(repo.root, sid)
    assert len(initial) == 1
    assert initial[0][0] == "state/handoffs/2026-07-15_100000_pred.md"

    # Simulate a concurrent session clearing claimed_by between the
    # ceremony's initial resolve and this locked re-derive.
    text = hf.read_text(encoding="utf-8")
    hf.write_text(text.replace(f"claimed_by: {sid}\n", ""), encoding="utf-8")

    redrived = m.redrive_consumed_set(repo.root, sid)
    assert redrived == []


def test_redrive_liveness_recheck_finds_archived(repo):
    sid = "sess-def456"
    repo.seed_handoff(
        "2026-07-15_100000_swept.md", claimed_by=sid, archived_subdir="2026-07"
    )
    redrived = m.redrive_consumed_set(repo.root, sid)
    assert len(redrived) == 1
    assert redrived[0][0] == "archive/handoffs/2026-07/2026-07-15_100000_swept.md"


# ---------------------------------------------------------------------------
# Post-commit stamp — R1-R4 full pass
# ---------------------------------------------------------------------------


def test_post_commit_happy_path_stamps_and_ships(repo):
    """Real handoff.stamp + handoff.transition ship verb, stamp-BEFORE-ship
    ordering (see module docstring 'DEVIATION' / Negative-spec)."""
    sid = "sess-ship-1"
    repo.seed_handoff("2026-07-15_100000_pred.md", claimed_by=sid)
    committed_sha = repo.head_sha()

    outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, committed_sha, chain_terminal=True
        )
    )
    assert outcome.stamped == ["state/handoffs/2026-07-15_100000_pred.md"]
    assert outcome.skipped_live_children == []
    assert outcome.skipped_indeterminate == []
    assert outcome.errors == []

    on_disk = repo.read_handoff("state/handoffs/2026-07-15_100000_pred.md")
    assert "deployment_state: shipped" in on_disk
    assert f"shipped_in: {committed_sha}" in on_disk
    # DR-096: the ceremony's post-commit stamp is the canonical ship-commit
    # case -- kind is routed through, not left untagged.
    assert "shipped_in_kind: ship-commit" in on_disk


def test_post_commit_retains_on_live_children(monkeypatch, repo):
    sid = "sess-ship-2"
    repo.seed_handoff("2026-07-15_100000_pred.md", claimed_by=sid)

    async def _fake_handler(params, repo_root):
        return {"exit_code": 0, "referenced": True, "children": ["x"]}

    monkeypatch.setattr(
        m, "get_op_handler", lambda name: _fake_handler if name == "handoff.has_live_children" else None
    )

    outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, "deadbeef", chain_terminal=True
        )
    )
    assert outcome.stamped == []
    assert outcome.skipped_live_children == ["state/handoffs/2026-07-15_100000_pred.md"]
    on_disk = repo.read_handoff("state/handoffs/2026-07-15_100000_pred.md")
    assert "deployment_state: shipped" not in on_disk
    assert "shipped_in" not in on_disk


def test_post_commit_live_children_check_narrows_edge_kinds_so_a_live_spinoff_does_not_retain(
    monkeypatch, repo
):
    """`_live_children_guard` must not inherit `handoff.has_live_children`'s
    archival-shaped default edge set. `forked_from` is the spinoff edge — a
    live spinoff is a niece, not a descendant, and must not retain this WSC
    tail's writer from stamping/shipping (example-cockpit-repo-em, 2026-08-05,
    cross-repo/inbox/2026-08-05-example-cockpit-repo-em-wsc-leg-b-counts-spinoffs-
    as-live-children.md). Asserted on the params actually handed to the op —
    one call covers both the pre-lock filter and the in-lock recheck, since
    both route through this same `_live_children_guard`."""
    sid = "sess-ship-edge-kinds"
    repo.seed_handoff("2026-07-15_100000_pred.md", claimed_by=sid)

    captured: list[dict] = []

    async def _fake_handler(params, repo_root):
        captured.append(dict(params))
        return {"exit_code": 1, "referenced": False}

    monkeypatch.setattr(
        m, "get_op_handler", lambda name: _fake_handler if name == "handoff.has_live_children" else None
    )

    outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, "deadbeef", chain_terminal=True
        )
    )
    assert outcome.stamped == ["state/handoffs/2026-07-15_100000_pred.md"]
    assert captured, "handoff.has_live_children was never dispatched"
    for params in captured:
        assert "edge_kinds" in params
        edge_kinds = {k.strip() for k in params["edge_kinds"].split(",")}
        assert edge_kinds == {"predecessor", "additional_predecessors"}
        assert "forked_from" not in edge_kinds


def test_post_commit_retains_on_indeterminate_never_keys_off_referenced(monkeypatch, repo):
    """R4(a): exit_code=2 retains even though `referenced` is entirely ABSENT
    from the reply (mirrors handoff_children.py's _indeterminate() shape)."""
    sid = "sess-ship-3"
    repo.seed_handoff("2026-07-15_100000_pred.md", claimed_by=sid)

    async def _fake_handler(params, repo_root):
        return {"exit_code": 2, "error": "empty live set"}  # no 'referenced' key

    monkeypatch.setattr(
        m, "get_op_handler", lambda name: _fake_handler if name == "handoff.has_live_children" else None
    )

    outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, "deadbeef", chain_terminal=True
        )
    )
    assert outcome.stamped == []
    assert outcome.skipped_indeterminate == ["state/handoffs/2026-07-15_100000_pred.md"]


def test_post_commit_live_children_handler_crash_is_indeterminate_not_batch_crash(monkeypatch, repo):
    """An UNEXPECTED exception from the `handoff.has_live_children` handler
    (not a clean exit-code return) must not crash the whole batch: it is
    caught inside `_live_children_guard` and translated into the same
    fail-closed exit_code=2 shape an ordinary indeterminate result takes --
    the candidate is retained (never shipped) and lands in
    `StampOutcome.skipped_indeterminate`, distinguishable from a genuine
    exit-2 indeterminate via the `crashed`/`exception_type` reply keys."""
    sid = "sess-crash-1"
    hf_rel = "state/handoffs/2026-07-15_100000_pred.md"
    repo.seed_handoff("2026-07-15_100000_pred.md", claimed_by=sid)

    async def _crashing_handler(params, repo_root):
        raise RuntimeError("boom: unexpected handler failure")

    monkeypatch.setattr(
        m, "get_op_handler",
        lambda name: _crashing_handler if name == "handoff.has_live_children" else None,
    )

    outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, "deadbeef", chain_terminal=True
        )
    )

    assert outcome.stamped == []
    assert outcome.skipped_indeterminate == [hf_rel]
    assert outcome.errors == []
    on_disk = repo.read_handoff(hf_rel)
    assert "shipped_in" not in on_disk
    assert "deployment_state: shipped" not in on_disk


def test_live_children_guard_crash_is_not_swallowed_bare(repo):
    """Direct unit check on `_live_children_guard` itself: retain=True,
    exit_code=2, and the exception detail is carried in the reply so an
    operator can tell a crashed handler apart from a genuine indeterminate
    exit code."""

    async def _crashing_handler(params, repo_root):
        raise ValueError("some unexpected failure")

    async def _call():
        return await m._live_children_guard(
            "state/handoffs/whatever.md", repo_root=repo.common_dir
        )

    orig = m.get_op_handler
    m.get_op_handler = lambda name: _crashing_handler if name == "handoff.has_live_children" else orig(name)
    try:
        retain, reply = _run(_call())
    finally:
        m.get_op_handler = orig

    assert retain is True
    assert reply["exit_code"] == 2
    assert reply.get("crashed") is True
    assert reply.get("exception_type") == "ValueError"


def test_post_commit_empty_consumed_set_loud_report(repo):
    sid = "sess-empty-1"
    outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, "deadbeef", chain_terminal=True
        )
    )
    assert outcome.empty_consumed_set is True
    assert outcome.stamped == []


def test_post_commit_noop_when_not_chain_terminal(repo):
    sid = "sess-not-terminal"
    repo.seed_handoff("2026-07-15_100000_pred.md", claimed_by=sid)
    outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, "deadbeef", chain_terminal=False
        )
    )
    assert outcome == m.StampOutcome()


def test_post_commit_future_dated_rejected(repo, monkeypatch):
    sid = "sess-future-1"
    far_future_name = "2099-01-01_120000_pred.md"
    repo.seed_handoff(far_future_name, claimed_by=sid)

    outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, "deadbeef", chain_terminal=True
        )
    )
    assert outcome.stamped == []
    assert outcome.skipped_future_dated == [f"state/handoffs/{far_future_name}"]
    on_disk = repo.read_handoff(f"state/handoffs/{far_future_name}")
    assert "shipped_in" not in on_disk


def test_post_commit_live_children_retain_exit_2(repo, monkeypatch):
    sid = "sess-indet-1"
    repo.seed_handoff("2026-07-15_100000_pred.md", claimed_by=sid)

    async def _fake_handler(params, repo_root):
        return {"exit_code": 2, "error": "empty live set"}

    monkeypatch.setattr(
        m, "get_op_handler",
        lambda name: _fake_handler if name == "handoff.has_live_children" else None,
    )

    outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, "deadbeef", chain_terminal=True
        )
    )
    assert outcome.stamped == []
    assert outcome.skipped_indeterminate == ["state/handoffs/2026-07-15_100000_pred.md"]


def test_post_commit_already_shipped_and_archived_is_noop(repo):
    """Reproduces the wsc-tail archived-handoff soft-fail defect (2026-07-28
    handoff): a predecessor stamped `shipped_in` + `deployment_state:
    shipped` directly (e.g. `archive-stamp-cli ship-handoff`), then swept to
    archive/handoffs/ by an async sweep, BEFORE this ceremony's stamp pass
    ran. `handoff.stamp` refuses any archive/handoffs/ path by design (see
    `_already_terminal_and_archived`'s docstring) -- the stamp this ceremony
    wants to apply is already on disk, so this must resolve as a no-op skip,
    never as `failed`."""
    sid = "sess-already-shipped-1"
    hf = repo.seed_handoff(
        "2026-07-26_082943_pred.md",
        claimed_by=sid,
        deployment_state="shipped",
        archived_subdir="2026-07",
    )
    text = hf.read_text(encoding="utf-8")
    text = text.replace(
        "deployment_state: shipped\n", "deployment_state: shipped\nshipped_in: 5a5563c7\n"
    )
    hf.write_text(text, encoding="utf-8")

    outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, "deadbeef", chain_terminal=True
        )
    )
    assert outcome.stamped == []
    assert outcome.errors == []
    assert outcome.skipped_already_terminal == [
        "archive/handoffs/2026-07/2026-07-26_082943_pred.md"
    ]

    # Untouched -- the no-op guard never mutates the already-terminal file.
    on_disk = repo.read_handoff("archive/handoffs/2026-07/2026-07-26_082943_pred.md")
    assert "shipped_in: 5a5563c7" in on_disk


@pytest.mark.parametrize("terminal_state", ["closed", "abandoned", "continued"])
def test_post_commit_non_shipped_terminal_and_archived_is_noop(repo, terminal_state):
    """The archived no-op guard covers EVERY terminal `deployment_state`, not
    just `shipped` (coordinator-claude-em, 2026-08-13, cross-repo/inbox/2026-08-13-doe-
    claude-em-wsc-tail-consumed-stamp-refuses-archived-baton.md).

    A baton flipped terminal before the close ceremony -- e.g. a displaced
    roadmap stub closed via `handoff-reconcile-close-terminal` -- becomes
    archive-eligible immediately, so the detached sweep can move it to
    archive/handoffs/ before the tail's stamp step runs. `shipped_in` MUST NOT
    be written for a non-shipped terminus (the work was not delivered), so the
    only correct outcome is a no-op skip, never the containment refusal that
    was surfacing as a tail soft-fail (exit 2) on correct on-disk state."""
    sid = f"sess-archived-{terminal_state}-1"
    repo.seed_handoff(
        "2026-07-26_082943_pred.md",
        claimed_by=sid,
        deployment_state=terminal_state,
        archived_subdir="2026-07",
    )

    outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, "deadbeef", chain_terminal=True
        )
    )
    assert outcome.stamped == []
    assert outcome.errors == []
    assert outcome.skipped_already_terminal == [
        "archive/handoffs/2026-07/2026-07-26_082943_pred.md"
    ]

    # Never stamped: a non-shipped terminus records no delivery sha.
    on_disk = repo.read_handoff("archive/handoffs/2026-07/2026-07-26_082943_pred.md")
    assert "shipped_in:" not in on_disk
    assert f"deployment_state: {terminal_state}" in on_disk


def test_post_commit_archived_but_not_terminal_still_fails_containment(repo):
    """The archived no-op guard stays narrow: an archived handoff still in a
    NON-terminal state reaches `handoff.stamp` and hits its containment guard
    for real -- surfaced as `errors`, never silently swallowed alongside the
    genuine no-op case. That shape is an archival anomaly (a live baton should
    not be in archive/handoffs/ at all), not the benign terminal-then-swept
    race. Guards against widening the skip into a general archive/handoffs/
    bypass."""
    sid = "sess-archived-unshipped-1"
    repo.seed_handoff(
        "2026-07-26_082943_pred.md",
        claimed_by=sid,
        archived_subdir="2026-07",
    )

    outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, "deadbeef", chain_terminal=True
        )
    )
    assert outcome.stamped == []
    assert outcome.skipped_already_terminal == []
    assert len(outcome.errors) == 1
    assert "escapes state/handoffs" in outcome.errors[0]["error"]


def test_post_commit_archived_shipped_without_sha_still_fails_containment(repo):
    """`shipped` is the one terminal state the guard still qualifies on
    `shipped_in`: an archived handoff claiming `deployment_state: shipped`
    with no sha recorded has NOT had this ceremony's stamp applied, so it is
    not a no-op -- it stays a loud containment error rather than quietly
    losing the shipped_in record the close was supposed to write."""
    sid = "sess-archived-shipped-nosha-1"
    repo.seed_handoff(
        "2026-07-26_082943_pred.md",
        claimed_by=sid,
        deployment_state="shipped",
        archived_subdir="2026-07",
    )

    outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, "deadbeef", chain_terminal=True
        )
    )
    assert outcome.stamped == []
    assert outcome.skipped_already_terminal == []
    assert len(outcome.errors) == 1
    assert "escapes state/handoffs" in outcome.errors[0]["error"]


# ---------------------------------------------------------------------------
# Row 7 / Row 6 — 2026-07-28 ceremony-lock-hold-resurrection spinoff.
# `_live_children_guard` runs TWICE per candidate (pre-lock filter, then
# in-lock recheck immediately before the stamp write); the stamp/ship pair
# is a CAS against the exact text the stamp write produced.
# ---------------------------------------------------------------------------


def test_row7_recheck_catches_successor_authored_after_prelock_guard(repo, monkeypatch):
    """Row 7 regression: the pre-lock guard call (call #1) sees no live
    children; a successor is "authored" in the gap before the stamp write
    lands. The in-lock recheck (call #2, inside the stamp's own locked_rmw
    hold) sees the live child and aborts the write via MutateAbort -- the
    handoff is NOT shipped, and the skip is reported through
    StampOutcome.skipped_live_children, the same bucket the pre-lock retain
    path already uses."""
    sid = "sess-row7-1"
    hf_rel = "state/handoffs/2026-07-15_100000_pred.md"
    repo.seed_handoff("2026-07-15_100000_pred.md", claimed_by=sid)

    calls = {"n": 0}

    async def _fake_handler(params, repo_root):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"exit_code": 1, "referenced": False, "children": []}
        return {"exit_code": 0, "referenced": True, "children": ["successor.md"]}

    monkeypatch.setattr(
        m, "get_op_handler",
        lambda name: _fake_handler if name == "handoff.has_live_children" else None,
    )

    outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, "deadbeef", chain_terminal=True
        )
    )

    # Exactly one live-children implementation, called twice.
    assert calls["n"] == 2
    assert outcome.stamped == []
    assert outcome.skipped_live_children == [hf_rel]
    on_disk = repo.read_handoff(hf_rel)
    assert "shipped_in" not in on_disk
    assert "deployment_state: shipped" not in on_disk


def test_row7_recheck_indeterminate_still_fails_closed(repo, monkeypatch):
    """Row 7: fail-closed is preserved on the SECOND (in-lock) call too --
    an exit_code=2 (indeterminate) on the recheck retains, never keying off
    `referenced` (deliberately absent on exit_code=2)."""
    sid = "sess-row7-2"
    hf_rel = "state/handoffs/2026-07-15_100000_pred.md"
    repo.seed_handoff("2026-07-15_100000_pred.md", claimed_by=sid)

    calls = {"n": 0}

    async def _fake_handler(params, repo_root):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"exit_code": 1, "referenced": False, "children": []}
        return {"exit_code": 2, "error": "empty live set"}  # no 'referenced' key

    monkeypatch.setattr(
        m, "get_op_handler",
        lambda name: _fake_handler if name == "handoff.has_live_children" else None,
    )

    outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, "deadbeef", chain_terminal=True
        )
    )

    assert calls["n"] == 2
    assert outcome.stamped == []
    assert outcome.skipped_indeterminate == [hf_rel]
    on_disk = repo.read_handoff(hf_rel)
    assert "shipped_in" not in on_disk


def test_row6_peer_write_between_stamp_and_ship_aborts_not_lost(repo, monkeypatch):
    """Row 6 regression: a peer session writes this handoff in the window
    between the stamp write's lock release and the ship attempt's lock
    acquisition. The ship attempt's CAS (`_ship_with_cas`) sees the on-disk
    text no longer matches what the stamp write produced and aborts via
    MutateAbort rather than silently proceeding on top of content this
    ceremony never saw -- the peer's write is never lost (it survives on
    disk untouched) and deployment_state never flips to shipped. The stamp
    half (shipped_in) still landed and is still staged for the follow-up
    commit (AC17), same fall-through shape an ordinary ship failure already
    used before this fix."""
    sid = "sess-row6-1"
    hf_rel = "state/handoffs/2026-07-15_100000_pred.md"
    repo.seed_handoff("2026-07-15_100000_pred.md", claimed_by=sid)

    real_stamp_fn = m._stamp_with_live_children_recheck

    async def _stamp_then_peer_write(*args, **kwargs):
        attempt = await real_stamp_fn(*args, **kwargs)
        # Simulate a concurrent peer session writing this handoff in the
        # gap between the stamp's lock release and the ship attempt below.
        p = repo.root / hf_rel
        text = p.read_text(encoding="utf-8")
        p.write_text(
            text.replace("category: infra", "category: infra-peer-edited"),
            encoding="utf-8",
        )
        return attempt

    monkeypatch.setattr(m, "_stamp_with_live_children_recheck", _stamp_then_peer_write)

    outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, "deadbeef", chain_terminal=True
        )
    )

    on_disk = repo.read_handoff(hf_rel)
    # The stamp landed -- Row 6 guards only the SECOND write, not the first.
    assert "shipped_in: deadbeef" in on_disk
    # The ship never landed: the CAS aborted it.
    assert "deployment_state: shipped" not in on_disk
    # The peer's write survived untouched -- neither lost nor overwritten.
    assert "category: infra-peer-edited" in on_disk
    # The stamp mutation must still be staged into the follow-up commit
    # (AC17 -- never left as an unswept dirty edit), even though ship
    # failed for this candidate.
    assert outcome.stamped == [hf_rel]
    assert len(outcome.errors) == 1
    assert "Row 6 CAS" in outcome.errors[0]["error"]


# ---------------------------------------------------------------------------
# AC17 — post-commit stamp lands in its own follow-up commit, pushed
# ---------------------------------------------------------------------------


def test_post_commit_happy_path_stamps_and_follow_up_commits_pushed(repo_with_remote):
    """AC17 end-to-end: the follow-up commit lands and is really pushed.

    C7c: this test's subject is the push itself LANDING -- it used to run on
    the fixture's `main`, which the real `work/*`-only push-leg branch
    policy now correctly declines; `repo_with_remote` was moved onto
    `work/test/consumed-handoff-stamp` (repair (a)) so this test still
    exercises what it names, rather than inverting the assertion to a
    decline that isn't this test's point."""
    repo = repo_with_remote
    sid = "sess-happy-1"
    repo.seed_handoff("2026-07-15_100000_pred.md", claimed_by=sid)
    real_sha = repo.head_sha()

    outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, real_sha, chain_terminal=True
        )
    )

    assert outcome.stamped == ["state/handoffs/2026-07-15_100000_pred.md"]
    assert outcome.errors == []
    assert outcome.follow_up_committed_sha is not None
    assert outcome.follow_up_committed_sha != real_sha
    assert outcome.follow_up_pushed is True
    assert outcome.follow_up_error is None

    on_disk = repo.read_handoff("state/handoffs/2026-07-15_100000_pred.md")
    assert f"shipped_in: {real_sha}" in on_disk

    # Never left as an unswept dirty working-tree edit.
    assert repo.porcelain_status() == ""

    # The follow-up commit really landed (local HEAD) and really pushed
    # (remote main advanced to the same sha).
    assert repo.head_sha() == outcome.follow_up_committed_sha
    remote_log = repo.log_messages(remote=True, remote_branch="work/test/consumed-handoff-stamp")
    assert outcome.follow_up_committed_sha[:7] in remote_log


# ---------------------------------------------------------------------------
# AC17 follow-up commit routes through commit_scoped -- a peer's
# deliberately-staged partial-hunk content on a path in the stamped set
# survives verbatim (the claude-klabauter 506748a0 incident shape, closed).
# Real git required (fixtures.real_git) -- divergence cannot be exhibited by
# a mocked git.
# ---------------------------------------------------------------------------


def test_follow_up_commit_preserves_peer_staged_divergence(tmp_path):
    repo = real_git_repo(tmp_path)
    make_diverged_path(
        repo, "state/handoffs/some-handoff.md", staged_content="STAGED\n", worktree_content="WORKTREE\n"
    )

    follow_up_sha, pushed, push_status, error = m._commit_and_push_follow_up(
        repo, ["state/handoffs/some-handoff.md"], "deadbeef", push_mode=PUSH_MODE_NONE
    )

    assert error is None, error
    assert follow_up_sha is not None
    assert pushed is None  # push_mode="none" -- no attempt, not a failure
    assert push_status == m.PUSH_STATUS_NOT_ATTEMPTED
    result = subprocess.run(
        ["git", "show", "HEAD:state/handoffs/some-handoff.md"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    assert result.stdout == "STAGED\n"
    # Worktree content is untouched -- commit_scoped never re-derives the
    # diverged path's content from the worktree.
    assert (repo / "state/handoffs/some-handoff.md").read_text(encoding="utf-8") == "WORKTREE\n"


# ---------------------------------------------------------------------------
# C6e -- branch-policy decline is carried as a distinct signal, never routed
# through the error channel. (docs/plans/2026-08-08-the-push-leg-that-never-
# asked-which-branch.md, chunk C6e)
# ---------------------------------------------------------------------------


def test_follow_up_push_declined_by_branch_policy_not_reported_as_error(tmp_path):
    """A sync push attempted on a non-`work/*` branch (the fixture's default
    branch here) is declined by `branch_gate()` -- reported via a distinct
    `push_status`, not through `error`."""
    repo = real_git_repo(tmp_path)
    # A configured remote, so `push_with_retry` reaches the branch-policy
    # gate rather than short-circuiting on its own earlier no-remote check
    # -- this test isolates the POLICY decline, not a missing remote.
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "does-not-exist.git")],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    (repo / "seed.txt").write_text("declined\n", encoding="utf-8")

    follow_up_sha, pushed, push_status, error = m._commit_and_push_follow_up(
        repo, ["seed.txt"], "deadbeef", push_mode=PUSH_MODE_SYNC
    )

    assert follow_up_sha is not None
    # A decline is NEVER `pushed=False` -- that shape reads as "attempted
    # and did not land" to every other reader in the repo (integrity_breach,
    # _resolve_push_report). `None` matches the no-attempt shape;
    # `push_status` is what disambiguates a decline from a genuine no-attempt.
    assert pushed is None
    assert push_status == PUSH_STATUS_DECLINED
    assert error is None  # decline is NEVER routed through the error channel


def test_follow_up_push_genuine_failure_still_routes_through_error(tmp_path):
    """A push attempted on a `work/*` branch (passes the policy gate), with a
    remote configured but unreachable, fails for a real reason and still
    routes through `error` exactly as before -- only a POLICY decline (or a
    missing remote) is carried separately, never as `pushed=False`+`error`."""
    repo = real_git_repo(tmp_path)
    subprocess.run(
        ["git", "checkout", "-b", "work/test/c6e"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    # A configured-but-unreachable remote: `git remote` reports non-empty
    # (so `push_with_retry` does NOT take its no-remote skip), but the push
    # itself fails for a genuine reason.
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "does-not-exist.git")],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    (repo / "seed.txt").write_text("failure\n", encoding="utf-8")

    follow_up_sha, pushed, push_status, error = m._commit_and_push_follow_up(
        repo, ["seed.txt"], "deadbeef", push_mode=PUSH_MODE_SYNC
    )

    assert follow_up_sha is not None
    assert pushed is False
    assert push_status == m.PUSH_STATUS_FAILED
    assert error is not None  # a genuine git failure, still an error


def test_follow_up_push_mode_none_keeps_distinct_not_attempted_shape(tmp_path):
    """`push_mode="none"` keeps its own prior, distinct shape -- `pushed=None`
    (no attempt at all, never considered), never conflated with a
    considered-then-declined push."""
    repo = real_git_repo(tmp_path)
    (repo / "seed.txt").write_text("no-attempt\n", encoding="utf-8")

    follow_up_sha, pushed, push_status, error = m._commit_and_push_follow_up(
        repo, ["seed.txt"], "deadbeef", push_mode=PUSH_MODE_NONE
    )

    assert follow_up_sha is not None
    assert pushed is None
    assert push_status == m.PUSH_STATUS_NOT_ATTEMPTED
    assert error is None


# ---------------------------------------------------------------------------
# Full ship-drift regression
# ---------------------------------------------------------------------------


def test_ship_drift_regression_full_pass(repo_with_remote):
    """A chain-terminal close whose real predecessor was claimed_by:sid
    shortly before ends with that predecessor at deployment_state: shipped
    AND shipped_in: <the real committed_sha> — no branch-tip fallback, no
    sibling-correction (Position A).

    C7c: also asserts the follow-up push LANDING (`follow_up_pushed is
    True`) -- `repo_with_remote` moved onto `work/test/consumed-handoff-
    stamp` for the same repair-(a) reason as the sibling AC17 test above;
    see that fixture's own docstring."""
    repo = repo_with_remote
    sid = "sess-regression-1"
    repo.seed_handoff("2026-07-15_100000_pred.md", claimed_by=sid)

    # Simulate the ceremony's main commit landing (C4's job — the seeded
    # handoff's own commit stands in for it here) and being pushed.
    repo._git("push", "origin", "work/test/consumed-handoff-stamp")
    real_committed_sha = repo.head_sha()

    # Post-commit stamp+ship, inside the (simulated-held) ceremony_lock.
    post_outcome = _run(
        m.post_commit_stamp_and_ship(
            repo.root, repo.common_dir, sid, real_committed_sha, chain_terminal=True
        )
    )
    assert post_outcome.stamped == ["state/handoffs/2026-07-15_100000_pred.md"]
    assert post_outcome.follow_up_pushed is True

    on_disk = repo.read_handoff("state/handoffs/2026-07-15_100000_pred.md")
    assert "deployment_state: shipped" in on_disk
    assert f"shipped_in: {real_committed_sha}" in on_disk
    assert repo.porcelain_status() == ""

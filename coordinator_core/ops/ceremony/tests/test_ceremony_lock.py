"""
coordinator_core.ops.ceremony.tests.test_ceremony_lock

Tests for ceremony_lock -- the per-worktree ceremony critical-section lock.

Coverage:
  (a) acquire_release             -- basic acquire/release roundtrip clears the lock dir
  (b) contention_blocks_then_succeeds -- a different session's acquire blocks until
                                      the first holder releases, then succeeds
  (c) dead_holder_reclaimed       -- cs_claim_holder_live() mocked False -> stale
                                      lock dir is removed and reclaimed immediately
  (d) reentrant_same_holder       -- a second acquire by the SAME session_id succeeds
                                      idempotently without deadlocking
  (e) grep_guard_no_raw_liveness_reads -- module source never reads meta.json /
                                      stable_pid / last_activity directly; it only
                                      calls the cs_claim_holder_live bridge
  (f) real_bridge_live_holder_not_reclaimed / real_bridge_dead_holder_reclaimed --
                                      review-integrator Finding 2: routes through the
                                      REAL cs_claim_holder_live bridge (no mock) against
                                      an actual bash-registered session, proving the
                                      dead-holder check's positive/negative liveness
                                      verdicts are correct end-to-end, not merely that
                                      the bridge is called. This is the test that must
                                      fail against the pre-fix synthetic-never-created
                                      claim-dir wiring (Finding 1) and pass against the
                                      fix (lock dir reused as the claim carrier).

Spec backlink: chunk C4, docs/plans/2026-07-11-wsc-concurrent-tree-safety-hardening.md;
review-integrator Findings 1/2, state/review-trail/findings/2026-07-12-codereview-
slicewsc-concurrent-tree-safety-hardening-coordinator-core-ops-ceremony-wsc-resolv.md
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.ceremony import ceremony_lock as _cl
from coordinator_core.session import core as _session_core


def _common_dir(tmp_path: Path) -> Path:
    common = tmp_path / ".git"
    common.mkdir()
    return common


def _real_git_repo(tmp_path: Path) -> Path:
    """Init a REAL git repo at tmp_path -- required for the bash lib's
    _cs_git_root (``git rev-parse --show-toplevel``) to resolve a
    coordinator-sessions/ dir under it. A bare ``mkdir(".git")`` (as
    ``_common_dir`` uses for the mocked-bridge tests) is not a real repo and
    git rev-parse fails against it."""
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.example"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True)
    (tmp_path / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=str(tmp_path), check=True)
    return tmp_path / ".git"


def _cs_init(worktree_root: Path, sid: str) -> None:
    """Register a REAL live session via the native ``session.core.init`` --
    Layer 2 recency fallback (no ``claude`` PPID in the test harness, so
    stable_pid stays empty and last_activity/now-recency is the live
    signal). Bash ``cs_init`` oracle retired -- the session hub trio
    (``coordinator-session.sh``, ``session/scope.sh``, ``session-shape.sh``)
    is deleted example-doctrine-repo-side per the 2026-07-22
    coordinator-session-family-repoint-and-delete plan, so the native port
    is now the only truth."""
    ok = _session_core.init(sid, cwd=str(worktree_root))
    assert ok, "session.core.init failed"


def test_acquire_release(tmp_path):
    common = _common_dir(tmp_path)
    lock_path = _cl.acquire(
        tmp_path, name="wsc-commit", session_id="sid-a", git_common_dir=common
    )
    assert lock_path.exists()
    assert (lock_path / "holder").read_text(encoding="utf-8").strip() == "sid-a"

    _cl.release(lock_path, session_id="sid-a")
    assert not lock_path.exists()


def test_contention_blocks_then_succeeds(tmp_path):
    common = _common_dir(tmp_path)

    lock_path_a = _cl.acquire(
        tmp_path, name="wsc-commit", session_id="sid-a", git_common_dir=common
    )

    results = {}

    def _second_acquire():
        with patch.object(_cl, "_is_holder_dead", return_value=False):
            lp = _cl.acquire(
                tmp_path,
                name="wsc-commit",
                session_id="sid-b",
                timeout=5.0,
                git_common_dir=common,
            )
            results["lock_path"] = lp

    thread = threading.Thread(target=_second_acquire)
    thread.start()

    time.sleep(0.2)
    assert thread.is_alive(), "second acquire should still be blocked on live holder"

    _cl.release(lock_path_a, session_id="sid-a")
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "second acquire should have succeeded after release"

    assert results["lock_path"] == lock_path_a
    assert (results["lock_path"] / "holder").read_text(encoding="utf-8").strip() == "sid-b"

    _cl.release(results["lock_path"], session_id="sid-b")


def test_dead_holder_reclaimed(tmp_path):
    common = _common_dir(tmp_path)

    lock_path_a = _cl.acquire(
        tmp_path, name="wsc-commit", session_id="sid-dead", git_common_dir=common
    )
    assert lock_path_a.exists()

    with patch.object(_cl, "cs_claim_holder_live", return_value=False) as mock_live:
        lock_path_b = _cl.acquire(
            tmp_path,
            name="wsc-commit",
            session_id="sid-b",
            timeout=5.0,
            git_common_dir=common,
        )

    assert mock_live.called
    assert lock_path_b == lock_path_a
    assert (lock_path_b / "holder").read_text(encoding="utf-8").strip() == "sid-b"

    _cl.release(lock_path_b, session_id="sid-b")


def test_indeterminate_liveness_read_not_reclaimed(tmp_path):
    """An errored/indeterminate liveness read (cs_claim_holder_live raises)
    must NOT be treated as a dead holder -- _is_holder_dead must return False
    (holder presumed alive), never reclaim the lock. Regression guard for the
    2026-07-21 fix: cs_claim_holder_live used to swallow every exception into
    a bare False (confirmed-dead), which _is_holder_dead then inverted into
    "dead" -- reclaiming a lock that might still be held by a live session
    mid-critical-section. Now the exception propagates out of
    cs_claim_holder_live and _is_holder_dead's own try/except must catch it
    and fail closed to "not dead"."""
    common = _common_dir(tmp_path)

    lock_path_a = _cl.acquire(
        tmp_path, name="wsc-commit", session_id="sid-indeterminate", git_common_dir=common
    )
    assert lock_path_a.exists()

    with patch.object(
        _cl, "cs_claim_holder_live", side_effect=OSError("simulated indeterminate read")
    ):
        assert _cl._is_holder_dead(lock_path_a, "sid-indeterminate") is False

        with pytest.raises(_cl.CeremonyLockTimeout):
            _cl.acquire(
                tmp_path,
                name="wsc-commit",
                session_id="sid-b",
                timeout=0.2,
                git_common_dir=common,
            )

    # Lock is still intact -- nothing was reclaimed out from under sid-indeterminate.
    assert lock_path_a.exists()
    assert (lock_path_a / "holder").read_text(encoding="utf-8").strip() == "sid-indeterminate"

    _cl.release(lock_path_a, session_id="sid-indeterminate")


@pytest.mark.real_home
def test_real_bridge_live_holder_not_reclaimed(tmp_path, monkeypatch):
    """Review-integrator Finding 2: routes through the REAL cs_claim_holder_live
    bridge (not mocked/patched) against a genuinely live, bash-registered
    session. Fails against the pre-fix synthetic-never-created claim-dir
    wiring (Finding 1) -- that wiring reports EVERY holder dead, so a second
    acquire would reclaim the lock immediately instead of blocking. Passes
    against the fix, which points the bridge at the lock's own populated
    directory.

    ``@pytest.mark.real_home`` (STALE as of the 2026-07-22 session-family
    repoint, kept harmless): this marker dates from when ``_cs_init`` shelled
    out to the real ``coordinator-session.sh`` bash lib via ``_lib_path()``,
    which fell through to a real-``$HOME``-anchored resolver strategy that
    the suite-root ``_quarantine_real_home`` autouse fixture
    (``coordinator_core/conftest.py``) would otherwise starve. ``_cs_init``
    now registers the session natively via ``session.core.init`` and no
    longer touches ``$HOME`` at all, so the marker is no longer load-bearing
    for this test -- left in place because it is a no-op restore, not a
    correctness dependency. All session-registry state this test writes
    lives under ``tmp_path/.git/coordinator-sessions/`` (via
    ``_cs_git_root``'s ``cwd``-relative ``git rev-parse``), never under
    ``$HOME``.
    """
    common = _real_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)  # _cs_git_root resolves via cwd's `git rev-parse`

    live_sid = "real-bridge-live-sid"
    _cs_init(tmp_path, live_sid)

    lock_path_a = _cl.acquire(
        tmp_path, name="wsc-commit", session_id=live_sid, git_common_dir=common
    )
    assert lock_path_a.exists()

    # No patch.object anywhere here -- _is_holder_dead's cs_claim_holder_live
    # call goes all the way through the real bash bridge.
    assert _cl._is_holder_dead(lock_path_a, live_sid) is False, (
        "a genuinely live, registered holder must NOT be reported dead -- "
        "if this fails, the claim-dir wiring is feeding cs_claim_holder_live "
        "a path it can never resolve (Finding 1's regression)"
    )

    with pytest.raises(_cl.CeremonyLockTimeout):
        _cl.acquire(
            tmp_path,
            name="wsc-commit",
            session_id="real-bridge-contender-sid",
            timeout=0.3,
            git_common_dir=common,
        )

    _cl.release(lock_path_a, session_id=live_sid)


def test_real_bridge_dead_holder_reclaimed(tmp_path, monkeypatch):
    """Companion to test_real_bridge_live_holder_not_reclaimed: a holder sid
    that was never registered with cs_init (no session dir exists anywhere
    in the registry) must resolve dead via the REAL bridge, and acquire()
    must reclaim the lock rather than block/timeout."""
    common = _real_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    lock_path_a = _cl.acquire(
        tmp_path, name="wsc-commit", session_id="real-bridge-never-registered-sid",
        git_common_dir=common,
    )
    assert lock_path_a.exists()

    assert _cl._is_holder_dead(lock_path_a, "real-bridge-never-registered-sid") is True

    lock_path_b = _cl.acquire(
        tmp_path,
        name="wsc-commit",
        session_id="real-bridge-reclaimer-sid",
        timeout=5.0,
        git_common_dir=common,
    )
    assert lock_path_b == lock_path_a
    assert (lock_path_b / "holder").read_text(encoding="utf-8").strip() == "real-bridge-reclaimer-sid"

    _cl.release(lock_path_b, session_id="real-bridge-reclaimer-sid")


def test_reentrant_same_holder(tmp_path):
    common = _common_dir(tmp_path)

    lock_path_1 = _cl.acquire(
        tmp_path, name="wsc-commit", session_id="sid-a", git_common_dir=common
    )
    lock_path_2 = _cl.acquire(
        tmp_path,
        name="wsc-commit",
        session_id="sid-a",
        timeout=1.0,
        git_common_dir=common,
    )

    assert lock_path_1 == lock_path_2
    assert lock_path_1.exists()
    assert (lock_path_1 / "holder").read_text(encoding="utf-8").strip() == "sid-a"

    _cl.release(lock_path_2, session_id="sid-a")
    assert not lock_path_1.exists()


def test_context_manager_reentrant_resume_safety(tmp_path):
    common = _common_dir(tmp_path)

    with _cl.ceremony_lock(
        tmp_path, name="wsc-commit", session_id="sid-a", git_common_dir=common
    ) as outer_path:
        assert outer_path.exists()
        with _cl.ceremony_lock(
            tmp_path,
            name="wsc-commit",
            session_id="sid-a",
            timeout=1.0,
            git_common_dir=common,
        ) as inner_path:
            assert inner_path == outer_path
            assert inner_path.exists()
        assert not inner_path.exists()

    assert not outer_path.exists()


def test_grep_guard_no_raw_liveness_reads():
    """Grep-guard: the module CODE (not its explanatory docstrings) must never
    read meta.json / stable_pid / last_activity directly, and must never
    invoke ps / kill / psutil -- liveness verdicts route solely through the
    cs_claim_holder_live bridge. The module docstring legitimately mentions
    these tokens as prohibitions, so this guard strips docstrings/comments
    before scanning rather than grepping the raw source."""
    import ast

    source = Path(_cl.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    code_lines = set(range(1, len(source.splitlines()) + 1))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                doc_node = node.body[0]
                for lineno in range(doc_node.lineno, doc_node.end_lineno + 1):
                    code_lines.discard(lineno)

    stripped_lines = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        if lineno in code_lines:
            code_part = line.split("#", 1)[0]
            stripped_lines.append(code_part)
    code_only = "\n".join(stripped_lines)

    for banned in ("meta.json", "stable_pid", "last_activity"):
        assert banned not in code_only, (
            f"ceremony_lock.py code must not reference {banned!r} directly -- "
            "liveness verdicts must route through cs_claim_holder_live"
        )

    for banned in ('subprocess.run(["ps"', "os.kill(", "psutil"):
        assert banned not in code_only, (
            f"ceremony_lock.py code must not invoke {banned!r} -- "
            "delegate liveness to cs_claim_holder_live"
        )

    assert "cs_claim_holder_live" in source

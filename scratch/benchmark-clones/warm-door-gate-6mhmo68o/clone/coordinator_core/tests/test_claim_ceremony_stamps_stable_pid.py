"""
coordinator_core.tests.test_claim_ceremony_stamps_stable_pid — C4 guard
(state/dispatch-briefs/2026-08-22-track-touched-files-pays-only-for-the-
append/C4.md) pinning the claiming ceremony's `stable_pid` stamp so it
cannot silently regress.

The PM's chosen shape — `cs_claim_handoff` stamps `stable_pid` alongside the
`claimed_at` it already writes — is a SIDE EFFECT of
`_record_session_goal_best_effort` calling
`coordinator_core.session.core.ensure_session`, itself wrapped in a bare
`except Exception`. Nothing states that liveness depends on it, and nothing
fails if a future edit narrows `_record_session_goal_best_effort` to call
`update_meta_field` directly instead of `ensure_session` — this module is that
statement, made executable.

Two distinct regressions are pinned:
  1. `ensure_session`, not a plain `update_meta_field`, is the call the ceremony
     relies on for a FRESH session (no meta.json yet) — `update_meta_field`
     no-ops on an absent file by contract, so a swap silently drops the
     stamp with no error anywhere.
  2. The repair gap this chunk's brief calls out: a session whose init() ran
     but whose Guard-1 missed (psutil absent, or a partial write) leaves an
     EXISTING meta.json with an empty `stable_pid`. Both other writers on
     this path (`ensure_session`'s own former early return, `session/scope.py`
     `cs_touch`) gate re-stamping on meta.json PRESENCE, not on the stamp
     itself, so that session was PERMANENTLY unstamped before this chunk's
     `ensure_session` re-stamp arm. This module also exercises that repair path
     directly.

Negative-spec: does NOT cover `session/stable_pid_watch.py`'s AC8 branch
(no-meta.json-but-touched.txt counted as a miss) — that is a `scan_stable_
pid_misses`-shaped module-level probe, not a claim-ceremony behavior, and is
out of this file's own single-purpose scope; see that module's own
docstring/tests for its coverage.

Spec backlink: state/dispatch-briefs/2026-08-22-track-touched-files-pays-
only-for-the-append/C4.md
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.handoff_transition  # noqa: F401 — @register_op side effect
import coordinator_core.ops.session.record_pickup  # noqa: F401 — @register_op side effect

import coordinator_core.archive_stamp as arstamp
from coordinator_core.session import core as session_core

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-safe-env-suppressed
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _seed_handoff(repo: Path, name: str, status: str, deployment_state: str, extra: str = "") -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        f"status: {status}\n"
        'predecessor: "none"\n'
        f"deployment_state: {deployment_state}\n"
    )
    if extra:
        fm += extra
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


@pytest.fixture(autouse=True)
def _default_caller_session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.fixture
def _force_stable_pid_capture(monkeypatch):
    """Deterministically forces `session.core.init`'s POSIX Guard-1 to
    stamp `stable_pid`, regardless of the actual platform/parent-process
    running this test — mirrors
    `session/tests/test_core.py::test_init_populates_stable_pid_when_parent_
    comm_is_claude`'s own mechanism (the same production seam), so this
    module is not a second, independently-drifting way to fake the capture.
    """
    monkeypatch.setattr(session_core, "_IS_WINDOWS", False)

    fake_create_time = 1785000000.0

    class FakeParentProcess:
        def __init__(self, pid):
            self._pid = pid

        def name(self):
            return "claude"

        def create_time(self):
            return fake_create_time

    monkeypatch.setattr(session_core._psutil(), "Process", FakeParentProcess)
    return fake_create_time


def test_claim_handoff_stamps_stable_pid_on_fresh_session(tmp_path, monkeypatch, _force_stable_pid_capture):
    """Claiming a handoff in a repo with NO prior session record must leave
    a `meta.json` carrying a non-empty `stable_pid`. Fails if
    `_record_session_goal_best_effort`'s `ensure_session` call is replaced with
    a plain `update_meta_field` — that call no-ops on the absent file this
    fresh-session path starts from, so `meta.json` would never even be
    created, let alone stamped."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    hp = _seed_handoff(repo, "h1.md", "open", "active")
    sid = "22222222-2222-2222-2222-222222222222"

    rc = arstamp.cs_claim_handoff(str(hp))
    assert rc == 0

    sdir = Path(session_core.session_dir(sid, cwd=str(repo)))
    meta_path = sdir / "meta.json"
    assert meta_path.is_file(), "claim must create meta.json (ensure_session, not a no-op update_meta_field)"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta.get("stable_pid"), "claim must stamp a non-empty stable_pid"


def test_claim_handoff_repairs_preexisting_unstamped_meta(tmp_path, monkeypatch, _force_stable_pid_capture):
    """The repair-gap path this chunk's brief names: a meta.json that
    ALREADY EXISTS (simulating an earlier init() whose Guard-1 missed —
    psutil absent, or a partial write) but carries an EMPTY stable_pid must
    be RE-STAMPED by a subsequent claim, not left permanently unstamped.

    Before this chunk's `ensure_session` re-stamp arm, the early `is_file()`
    return skipped `init()` entirely whenever a record already existed —
    this pins that the claim ceremony now repairs, rather than perpetuates,
    that state."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    hp = _seed_handoff(repo, "h2.md", "open", "active")
    sid = "22222222-2222-2222-2222-222222222222"

    sdir = Path(session_core.session_dir(sid, cwd=str(repo)))
    sdir.mkdir(parents=True, exist_ok=True)
    pre_existing_meta = {
        "session_id": sid,
        "branch": "unknown",
        "pid": "1",
        "last_activity": "2026-01-01T00:00:00Z",
        "goal": "",
        "stable_pid_capture": "psutil-absent",
    }
    (sdir / "meta.json").write_text(json.dumps(pre_existing_meta, indent=2) + "\n", encoding="utf-8")
    assert not session_core.read_meta_field(str(sdir), "stable_pid"), "precondition: unstamped"

    rc = arstamp.cs_claim_handoff(str(hp))
    assert rc == 0

    meta = json.loads((sdir / "meta.json").read_text(encoding="utf-8"))
    assert meta.get("stable_pid"), "a pre-existing unstamped meta.json must be repaired by the next claim"

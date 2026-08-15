"""End-to-end test proving the EM-exercisable in-band grant route feature
(AC-1, AC-2), per docs/plans/2026-08-13-em-exercisable-in-band-grant-route.md
§ Verification.

Exercises the REAL registered guard chain end to end
(`bash_guards.dispatch.evaluate_payload_json`), not `bump_foreign_repo_write.
check_bump_foreign_repo_write` called in isolation -- DR-280 is explicit that
a whole unreachable deny leg once survived a fully green suite that bypassed
the dispatcher (see `test_bump_outside_repo_write.py`'s own C2/AC7 section),
and the sentinel-consumption logic this test pins (`_consume_unlock`) lives
in `dispatch.py`, not in the guard module itself.

Sequence (AC-1, AC-2):
  1. A Bash write that `bump-foreign-repo-write` denies -- assert the deny.
  2. Run the grant CLI (`em_guard_grant.main`, called in-process -- the same
     entrypoint `python3 -m coordinator_core.session.em_guard_grant grant
     ...` resolves to, with no subprocess spawn of its own) for that guard
     name.
  3. Re-attempt the identical write -- assert it now proceeds (AC-1).
  4. Assert the DR-260 sentinel is gone -- CONSUMED by step 3's dispatch
     call, not merely read.
  5. Assert the durable grant record (`em-guard-grant.json`) gained the
     entry.
  6. Re-attempt a third time -- assert it refuses again (AC-2: one-shot).

Two mandatory test conventions (§ Verification, both from
`test_scoped_git_commit_ownership.py`'s DR-260 block, both already the
convention `test_em_guard_grant.py`'s own `TestAC14...` class follows):
(1) never a literal session id -- `sentinel_path()` resolves under the real,
shared platform temp dir, and this box runs 50-70 concurrent sessions, so
`_unique_sid(prefix)` mints `f"{prefix}-{uuid.uuid4().hex[:12]}"`; (2) the
sentinel is removed in a `finally:`, best-effort, swallowing `OSError` -- a
leaked sentinel silently grants a later, unrelated call.

Spawn ratchet (module docstring, `coordinator_core/tests/
test_no_new_spawning_tests.py`, Rule 2): this file spawns real `git`
(fixture repo setup, mirroring `test_bump_foreign_repo_write.py`'s own
`repos` fixture) and is admitted via the module-level `pytestmark` below --
a real gate, not advisory.

Spec backlink: pln-an-em-exercisable-in-band-gran-6bfb4a § Verification
Precedent: coordinator_core/bash_guards/tests/test_guard_unlock_dispatch_intercept.py
Precedent: coordinator_core/bash_guards/tests/test_bump_foreign_repo_write.py
Precedent: coordinator_core/session/tests/test_em_guard_grant.py (TestAC14SubagentCommitNeverComposesWithGrant)
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _write_bump_session_start as session_start
from coordinator_core.bash_guards import dispatch
from coordinator_core.session import em_guard_grant as eg
from coordinator_core.session import guard_unlock_sentinel

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

GUARD_NAME = "bump-foreign-repo-write"


def _unique_sid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(str(root), "init", "-q")
    _git(str(root), "config", "user.email", "t@example.com")
    _git(str(root), "config", "user.name", "Test")
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(str(root), "add", "README.md")
    _git(str(root), "commit", "-q", "-m", "init")
    return root


def _posix(p) -> str:
    # Review: coordinator:code-reviewer — the hasattr/PureWindowsPath fallback
    # was dead code at this file's only call site (`repos["foreign"]` is
    # always a `pathlib.Path`, which always has `.as_posix()`). No caller
    # here ever passes a bare string, so the branch is collapsed rather than
    # kept as untested dead code; still not a hardcoded `\\`-replace.
    return p.as_posix()


@pytest.fixture()
def repos(tmp_path):
    """Anchor repo (the session's own -- also where the grant module's
    durable record lands, via `core.session_dir`) and a foreign sibling
    repo, mirroring `test_bump_foreign_repo_write.py`'s own `repos`
    fixture."""
    anchor = _init_repo(tmp_path, "anchor")
    foreign = _init_repo(tmp_path, "foreign")
    home = tmp_path / "home"
    home.mkdir()
    settings_home = tmp_path / "isolated-settings-home"
    settings_home.mkdir()
    return {"anchor": anchor, "foreign": foreign, "home": home, "settings_home": settings_home}


def _set_anchor(monkeypatch, repos, session_id: str) -> None:
    """Establishes applicability the same way a real session does -- a
    settings-home `write_session_start_record`, not `CLAUDE_PROJECT_DIR`
    (mirrors `test_bump_foreign_repo_write.py::_set_anchor`). Also isolates
    `COORDINATOR_SETTINGS_HOME` per-test (mirrors `test_bump_outside_repo_
    write.py::_clean_bump_env`) so this test never writes into a real
    developer's `~/.coordinator-claude-settings/`."""
    monkeypatch.setenv("HOME", str(repos["home"]))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(repos["settings_home"]))
    session_start.write_session_start_record(session_id, launch_cwd=str(repos["anchor"]))


def _dispatch_decision(cmd: str, session_id: str, cwd: str) -> str:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": session_id,
        "cwd": cwd,
    }
    out = dispatch.evaluate_payload_json(json.dumps(payload))
    if out is None:
        return "allow"
    return out.get("hookSpecificOutput", {}).get("permissionDecision")


class TestGrantRouteClearsBumpForeignRepoWriteOnce:
    """AC-1 (a granted write proceeds) and AC-2 (the grant is one-shot)."""

    def test_deny_grant_allow_consumed_record_written_re_deny(self, repos, monkeypatch):
        sid = _unique_sid("em-grant-e2e")
        _set_anchor(monkeypatch, repos, sid)
        cmd = f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x"
        sentinel = guard_unlock_sentinel.sentinel_path(sid, GUARD_NAME)

        try:
            # 1. Attempt a Bash write bump-foreign-repo-write denies.
            assert _dispatch_decision(cmd, sid, str(repos["anchor"])) == "deny"

            # 2. Run the grant CLI for that guard name -- the in-process
            # entrypoint `python3 -m coordinator_core.session.em_guard_grant
            # grant <guard> <reason>` resolves to. `main()` takes no
            # explicit session/cwd override (matching the real CLI's own
            # argv shape), so the calling session is resolved the same way
            # the real CLI resolves it: `COORDINATOR_SESSION_ID` plus the
            # process cwd -- both pinned to this test's own sid/anchor here
            # so the grant lands where this test's assertions expect it,
            # never onto whatever ambient session happens to be running
            # this suite.
            monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
            monkeypatch.chdir(repos["anchor"])
            exit_code = eg.main(["grant", GUARD_NAME, "clearing a genuine cross-repo write"])
            assert exit_code == 0

            # 3. Re-attempt -- it now proceeds (AC-1).
            assert _dispatch_decision(cmd, sid, str(repos["anchor"])) == "allow"

            # 4. The sentinel is gone -- consumed by step 3, not merely read.
            assert not sentinel.exists()

            # 5. The durable grant record gained the entry.
            record = eg.read_em_guard_grant(session_id=sid, cwd=str(repos["anchor"]))
            assert record is not None
            assert record["guard_name"] == GUARD_NAME
            assert record["granted_by"] == "em"
            assert record["session_id"] == sid
            assert record["reason"] == "clearing a genuine cross-repo write"

            # 6. Re-attempt a third time -- refused again (AC-2, one-shot).
            assert _dispatch_decision(cmd, sid, str(repos["anchor"])) == "deny"
        finally:
            try:
                sentinel.unlink()
            except OSError:
                pass

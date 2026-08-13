"""
coordinator_core.session.tests.test_js_bridge_cli — parity tests for
coordinator_core.session.js_bridge_cli, the Python-side CLI target ported
from coordinator/lib/coordinator_session.js (Node self-claim shim, retired
2026-07-27 along with its sibling coordinator/lib/resolve-claude-klabauter-node.js).

Oracle (historical): coordinator_session.js's public API
(resolveLiveSessionIds, claimPath, selfClaim), each documented "never throws
/ never causes a non-zero exit" — that best-effort, fail-open contract is
the thing under test here, not the underlying claim/liveness logic (already
covered by test_claims.py / test_liveness.py). The JS file itself is gone;
this test asserts the contract this module ported from it, not against the
live JS source.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import js_bridge_cli
from coordinator_core.session import scope

# Every test in this file builds its repo via `_make_repo(tmp_path)`, spawning
# real git (init/config/add/commit) because `core.git_root()` and session-hub
# resolution read real git state no mock stands in for; the live-pid test
# also spawns real `ps` to get a genuine `lstart=` value the create-time
# epoch check can compare against. `tmp_path` is function-scoped and tests
# write session state under reused session ids, so the repo fixture stays
# per-test rather than hoisted to module scope. The spawn ratchet's
# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the
# route for this file -- coordinator_core/tests/test_no_new_spawning_tests.py
# Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
    return tmp_path


def _write_session(repo, sid, meta: dict):
    sdir = Path(repo) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return sdir


class TestUsageAndDispatch:
    def test_no_args_prints_usage_and_returns_zero(self, capsys):
        rc = js_bridge_cli.main([])
        assert rc == 0
        assert "usage" in capsys.readouterr().err

    def test_unknown_subcommand_returns_zero(self, capsys):
        rc = js_bridge_cli.main(["bogus-command"])
        assert rc == 0
        assert "unknown subcommand" in capsys.readouterr().err


class TestClaimPath:
    def test_appends_entry_and_dedups(self, tmp_path, capsys):
        touched = tmp_path / "touched.txt"
        rc = js_bridge_cli.main(["claim-path", str(touched), "coordinator/foo.py"])
        assert rc == 0
        rc = js_bridge_cli.main(["claim-path", str(touched), "coordinator/foo.py"])
        assert rc == 0
        lines = touched.read_text(encoding="utf-8").splitlines()
        # Review: coordinatorcode-reviewer-7ca5d82a Finding 1 — atomic_dedup_append
        # now writes an event line (scope.format_touch_event), not a bare path;
        # parse via scope.parse_touch_event rather than pinning the literal
        # (timestamped) line.
        assert len(lines) == 1
        verb, _ts, path = scope.parse_touch_event(lines[0])
        assert (verb, path) == ("T", "coordinator/foo.py")

    def test_wrong_arg_count_never_raises(self, capsys):
        rc = js_bridge_cli.main(["claim-path", "only-one-arg"])
        assert rc == 0
        assert "requires exactly 2 args" in capsys.readouterr().err


class TestSelfClaim:
    def test_self_claim_records_path_in_session_touched(self, tmp_path, monkeypatch, capsys):
        repo = _make_repo(tmp_path)
        _write_session(repo, "sidA", {"pid": "999999", "last_activity": "2026-01-01T00:00:00Z"})
        monkeypatch.chdir(repo)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sidA")

        rc = js_bridge_cli.main(["self-claim", "coordinator/bar.py"])
        assert rc == 0

        touched = repo / ".git" / "coordinator-sessions" / "sidA" / "touched.txt"
        assert touched.is_file()
        # Review: coordinatorcode-reviewer-7ca5d82a Finding 1 — event-line format,
        # parse rather than assert exact-membership of the bare path.
        lines = touched.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        verb, _ts, path = scope.parse_touch_event(lines[0])
        assert (verb, path) == ("T", "coordinator/bar.py")

    def test_wrong_arg_count_never_raises(self, capsys):
        rc = js_bridge_cli.main(["self-claim"])
        assert rc == 0
        assert "requires exactly 1 arg" in capsys.readouterr().err

    def test_no_session_never_raises(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.chdir(repo)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        rc = js_bridge_cli.main(["self-claim", "coordinator/baz.py"])
        assert rc == 0


class TestLiveSessionIds:
    def test_empty_when_no_sessions(self, tmp_path, monkeypatch, capsys):
        repo = _make_repo(tmp_path)
        monkeypatch.chdir(repo)
        rc = js_bridge_cli.main(["live-session-ids"])
        assert rc == 0
        assert capsys.readouterr().out == ""

    def test_prints_one_sid_per_line_for_live_sessions(self, tmp_path, monkeypatch, capsys):
        import os

        from coordinator_core.session import core

        repo = _make_repo(tmp_path)
        # Portable liveness fixture: since the 2026-07-27 ps-to-psutil port,
        # BOTH platforms resolve Layer 1 via psutil.Process.create_time()
        # (core._win_create_time_epoch — no longer Windows-exclusive despite
        # the name; see test_liveness.py's TestWindowsCreateTimePath for the
        # pattern this mirrors). The POSIX branch below exercises this
        # meta.json's LEGACY read arm on purpose (stable_pid_lstart only, no
        # stable_pid_start_epoch) — pre-port records still on disk have
        # exactly this shape, and stable_pid_alive parses the ps-format
        # lstart string back into an epoch (lstart_to_epoch) to compare
        # against the live create_time() epoch. Building the fixture off
        # core's own birth-instant helper for the current platform keeps
        # this a genuine end-to-end check on both OSes rather than a
        # POSIX-only mock.
        if core._IS_WINDOWS:
            epoch = core._win_create_time_epoch(os.getpid())
            assert epoch, "psutil create_time() must succeed on a live test process"
            meta = {
                "stable_pid": str(os.getpid()),
                "stable_pid_lstart": str(epoch),
                "stable_pid_start_epoch": str(epoch),
            }
        else:
            result = subprocess.run(
                ["ps", "-p", str(os.getpid()), "-o", "lstart="],
                capture_output=True,
                text=True,
            )
            lstart = result.stdout.strip()
            assert lstart, "ps -p <self> -o lstart= must succeed on a live test process"
            meta = {
                "stable_pid": str(os.getpid()),
                "stable_pid_lstart": lstart,
            }
        _write_session(repo, "sidLive", meta)
        monkeypatch.chdir(repo)
        rc = js_bridge_cli.main(["live-session-ids"])
        assert rc == 0
        out = capsys.readouterr().out.splitlines()
        assert out == ["sidLive"]


from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import claim_index
from coordinator_core.session import js_bridge_cli
from coordinator_core.session import scope
from coordinator_core.win_portability import no_console_creationflags, no_console_passthrough_kwargs

# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, **no_console_passthrough_kwargs())
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


def _sink_for(touched):
    return touched.parent / scope._TOUCH_RECORD_FILENAME


class TestClaimPath:
    def test_appends_entry_and_dedups(self, tmp_path, capsys):
        touched = tmp_path / "touched.txt"
        rc = js_bridge_cli.main(["claim-path", str(touched), "coordinator/foo.py"])
        assert rc == 0
        rc = js_bridge_cli.main(["claim-path", str(touched), "coordinator/foo.py"])
        assert rc == 0
        lines, degraded = scope._read_touch_record_as_legacy_lines(_sink_for(touched))
        assert not degraded
        assert len(lines) == 1
        verb, _ts, path = scope.parse_touch_event(lines[0])
        assert (verb, path) == ("T", "coordinator/foo.py")

    def test_wrong_arg_count_never_raises(self, capsys):
        rc = js_bridge_cli.main(["claim-path", "only-one-arg"])
        assert rc == 0
        assert "requires exactly 2 args" in capsys.readouterr().err

    def test_backslashed_relative_entry_lands_in_the_readers_dialect(self, tmp_path):
        touched = tmp_path / "touched.txt"

        assert js_bridge_cli.main(["claim-path", str(touched), r"coordinator\a\b.py"]) == 0

        lines, _degraded = scope._read_touch_record_as_legacy_lines(_sink_for(touched))
        assert len(lines) == 1
        verb, _ts, path = scope.parse_touch_event(lines[0])
        assert (verb, path) == ("T", "coordinator/a/b.py")
        assert claim_index._normalize_key(path) == path

    def test_backslashed_and_slashed_forms_dedup_against_each_other(self, tmp_path):
        touched = tmp_path / "touched.txt"

        js_bridge_cli.main(["claim-path", str(touched), r"coordinator\a\b.py"])
        js_bridge_cli.main(["claim-path", str(touched), "coordinator/a/b.py"])

        lines, _degraded = scope._read_touch_record_as_legacy_lines(_sink_for(touched))
        assert len(lines) == 1

    @pytest.mark.parametrize(
        "absolute_entry",
        [
            r"C:\Users\x\.claude\projects\p\memory\MEMORY.md",
            "/home/x/.claude/memory/MEMORY.md",
            r"\\?\C:\Users\x\scratch\f.py",
            r"\\server\share\f.py",
        ],
    )
    def test_absolute_entry_is_refused_visibly_and_never_written(
        self, tmp_path, capsys, absolute_entry
    ):
        touched = tmp_path / "touched.txt"

        rc = js_bridge_cli.main(["claim-path", str(touched), absolute_entry])

        assert rc == 0
        assert not touched.exists()
        assert not _sink_for(touched).exists()
        err = capsys.readouterr().err
        assert "is absolute" in err
        assert repr(absolute_entry) in err
        assert "self-claim" in err

    def test_claim_path_spawns_no_subprocess(self, tmp_path, monkeypatch):
        def _explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("claim-path spawned a subprocess: %r" % (args,))

        monkeypatch.setattr(subprocess, "run", _explode)
        monkeypatch.setattr(subprocess, "Popen", _explode)
        monkeypatch.setattr(subprocess, "check_output", _explode)

        touched = tmp_path / "touched.txt"
        assert js_bridge_cli.main(["claim-path", str(touched), r"pkg\mod.py"]) == 0
        assert (
            js_bridge_cli.main(
                ["claim-path", str(touched), r"C:\Users\x\out.md"]
            )
            == 0
        )

        lines, _degraded = scope._read_touch_record_as_legacy_lines(_sink_for(touched))
        assert [scope.parse_touch_event(line)[2] for line in lines] == ["pkg/mod.py"]


class TestSelfClaim:
    def test_self_claim_records_path_in_session_touched(self, tmp_path, monkeypatch, capsys):
        repo = _make_repo(tmp_path)
        _write_session(repo, "sidA", {"pid": "999999", "last_activity": "2026-01-01T00:00:00Z"})
        monkeypatch.chdir(repo)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sidA")

        rc = js_bridge_cli.main(["self-claim", "coordinator/bar.py"])
        assert rc == 0

        sink = (
            repo / ".git" / "coordinator-sessions" / "sidA" / scope._TOUCH_RECORD_FILENAME
        )
        assert sink.is_file()
        lines, degraded = scope._read_touch_record_as_legacy_lines(sink)
        assert not degraded
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
                **no_console_creationflags(),
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

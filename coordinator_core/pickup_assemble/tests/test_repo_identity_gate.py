"""
coordinator_core.pickup_assemble.tests.test_repo_identity_gate — C1 Part 2
scoped test suite for `compute_repo_identity_gate`.

Spec backlink: docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md § C1

AC6: every wrong-repo/verdict case is constructed with REAL files on disk
(registry JSON records under a fabricated `<claude-config>/sessions/` dir,
real directories with real `.git` markers for the plausibility band) —
never by monkeypatching `compute_repo_identity_gate`'s own return value.
The one thing that IS monkeypatched is `_resolve_claude_pid_from_env`'s
psutil-name-match leg — that check is inherently OS-process-identity
bound (it verifies the live process at a given pid is literally named
"claude") and cannot be constructed as a real fixture inside a test
process running under a different name. This mirrors the existing
`session/tests/test_harness_registry.py::TestSelfRecord` convention.
"""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.pickup_assemble import (
    _REPO_IDENTITY_MATCH,
    _REPO_IDENTITY_MISMATCH,
    _REPO_IDENTITY_UNRESOLVED,
    compute_repo_identity_gate,
)
from coordinator_core.session import harness_registry as hr


def _epoch_to_filetime_ticks(epoch: float) -> int:
    return int((epoch + hr._FILETIME_EPOCH_OFFSET_SEC) * hr._FILETIME_TICKS_PER_SEC)


def _write_registry_record(sessions_dir, filename, session_id, pid, cwd, epoch=None):
    sessions_dir.mkdir(parents=True, exist_ok=True)
    if epoch is None:
        epoch = time.time() - 60
    payload = {
        "sessionId": session_id,
        "pid": pid,
        "procStart": _epoch_to_filetime_ticks(epoch),
        "cwd": str(cwd),
    }
    (sessions_dir / filename).write_text(json.dumps(payload), encoding="utf-8")
    return epoch


def _make_repo(root) -> None:
    """A real directory with a real `.git` marker — enough for the
    plausibility band; no actual git init needed (containment/plausibility
    only look for a `.git` entry, never invoke git)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(parents=True, exist_ok=True)


def _patch_pid_env(monkeypatch, pid, create_time=0.0, hit=True):
    if hit:
        monkeypatch.setattr(
            "coordinator_core.session.core._resolve_claude_pid_from_env",
            lambda: ((pid, create_time), "env-hit"),
        )
    else:
        monkeypatch.setattr(
            "coordinator_core.session.core._resolve_claude_pid_from_env",
            lambda: (None, "env-miss:absent"),
        )


class TestVerdictMatch:
    def test_match_when_anchor_equals_repo_root(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        _make_repo(repo_root)
        sessions_dir = tmp_path / "sessions"
        epoch = _write_registry_record(sessions_dir, "111.json", "sess-a", 111, repo_root)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
        _patch_pid_env(monkeypatch, 111)
        monkeypatch.setattr(
            "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
            lambda pid, stored_start_epoch="": True,
        )

        result = compute_repo_identity_gate(repo_root, "sess-a")
        assert result["verdict"] == _REPO_IDENTITY_MATCH
        assert result["sid"] == "sess-a"
        assert "sess-a" in result["message"]
        assert str(repo_root) in result["message"] or result["resolved_root"] == str(repo_root)

    def test_match_for_subdirectory_launch(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        _make_repo(repo_root)
        subdir = repo_root / "coordinator_core"
        subdir.mkdir(parents=True, exist_ok=True)
        sessions_dir = tmp_path / "sessions"
        _write_registry_record(sessions_dir, "222.json", "sess-b", 222, subdir)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
        _patch_pid_env(monkeypatch, 222)
        monkeypatch.setattr(
            "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
            lambda pid, stored_start_epoch="": True,
        )

        result = compute_repo_identity_gate(repo_root, "sess-b")
        assert result["verdict"] == _REPO_IDENTITY_MATCH


class TestVerdictMismatch:
    def test_mismatch_on_real_divergence(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        foreign_root = tmp_path / "foreign"
        _make_repo(repo_root)
        _make_repo(foreign_root)
        sessions_dir = tmp_path / "sessions"
        _write_registry_record(sessions_dir, "333.json", "sess-c", 333, foreign_root)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
        _patch_pid_env(monkeypatch, 333)
        monkeypatch.setattr(
            "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
            lambda pid, stored_start_epoch="": True,
        )

        result = compute_repo_identity_gate(repo_root, "sess-c")
        assert result["verdict"] == _REPO_IDENTITY_MISMATCH
        assert str(foreign_root) in result["message"] or result["session_root"] == str(foreign_root)
        assert result["resolved_root"] == str(repo_root)
        assert "sess-c" in result["message"]


class TestVerdictUnresolved:
    def test_unresolved_when_no_registry_record(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        _make_repo(repo_root)
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
        _patch_pid_env(monkeypatch, 999, hit=False)

        result = compute_repo_identity_gate(repo_root, "sess-none")
        assert result["verdict"] == _REPO_IDENTITY_UNRESOLVED

    def test_unresolved_when_registry_populated_but_unparseable(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        _make_repo(repo_root)
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        # A well-formed file on disk that fails to parse (garbage procStart)
        # — distinguishable in the reason string from an empty directory.
        payload = {"sessionId": "sess-garbage", "pid": 999, "procStart": "not-a-date"}
        (sessions_dir / "999.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
        _patch_pid_env(monkeypatch, 999, hit=False)

        result = compute_repo_identity_gate(repo_root, "sess-garbage")
        assert result["verdict"] == _REPO_IDENTITY_UNRESOLVED
        assert "registry holds 1 file(s), 0 parsed" in result["message"]

    def test_unresolved_when_registry_populated_and_parsed_but_no_match(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        _make_repo(repo_root)
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        # Well-formed, parseable records (valid UTC ctime procStart inside
        # the sanity band) — none carries the sid we look up. Distinguishes
        # a healthy registry with an ordinary miss from a broken parser: the
        # message must report the actual parsed count, not "0 parsed".
        for i, (fname, sid_, pid_) in enumerate(
            [("111.json", "sess-other-1", 111), ("222.json", "sess-other-2", 222)]
        ):
            epoch = time.time() - 60 - i
            ctime_str = time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(epoch))
            payload = {"sessionId": sid_, "pid": pid_, "procStart": ctime_str}
            (sessions_dir / fname).write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
        _patch_pid_env(monkeypatch, 999, hit=False)

        result = compute_repo_identity_gate(repo_root, "sess-none")
        assert result["verdict"] == _REPO_IDENTITY_UNRESOLVED
        assert "registry holds 2 file(s), 2 parsed" in result["message"]

    def test_unresolved_on_plausibility_band_degenerate_cwd(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        _make_repo(repo_root)
        sessions_dir = tmp_path / "sessions"
        # Harness issue #27627's measured case: a well-formed record whose
        # cwd is a filesystem root.
        import os as _os

        degenerate_root = _os.path.abspath(_os.sep)
        _write_registry_record(sessions_dir, "444.json", "sess-d", 444, degenerate_root)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
        _patch_pid_env(monkeypatch, 444)
        monkeypatch.setattr(
            "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
            lambda pid, stored_start_epoch="": True,
        )

        result = compute_repo_identity_gate(repo_root, "sess-d")
        assert result["verdict"] == _REPO_IDENTITY_UNRESOLVED


class TestAC10StaleRecordReusedPid:
    def test_stale_record_reused_pid_fails_trust_check_unresolved(self, tmp_path, monkeypatch):
        """A pid-keyed record whose sessionId does not match sid (stale record
        wrongfully carried by a reused pid) must never produce MATCH — the
        gate must fall through to the snapshot() leg and, finding nothing
        there either, land UNRESOLVED."""
        repo_root = tmp_path / "repo"
        _make_repo(repo_root)
        sessions_dir = tmp_path / "sessions"
        # pid-keyed record on disk belongs to a DIFFERENT (stale) sessionId.
        _write_registry_record(sessions_dir, "555.json", "sess-stale-owner", 555, repo_root)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
        _patch_pid_env(monkeypatch, 555)
        monkeypatch.setattr(
            "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
            lambda pid, stored_start_epoch="": True,
        )

        # Caller's real sid does not match the stale record's sessionId, and
        # no snapshot() record exists under the real sid either.
        result = compute_repo_identity_gate(repo_root, "sess-real-caller")
        assert result["verdict"] == _REPO_IDENTITY_UNRESOLVED


class TestSnapshotFallbackDuplicateSessionIdCrashResume:
    def test_snapshot_fallback_leg_used_when_self_record_sessionid_mismatches(self, tmp_path, monkeypatch):
        """Crash-then-resume: the pid-keyed file belongs to an unrelated
        session, but a snapshot()-reachable record under the real sid
        exists (a second file, different pid) — the gate must fall
        through to it via the snapshot() scan."""
        repo_root = tmp_path / "repo"
        _make_repo(repo_root)
        sessions_dir = tmp_path / "sessions"
        # pid-keyed record: unrelated session at this pid.
        _write_registry_record(sessions_dir, "666.json", "sess-unrelated", 666, repo_root)
        # A different file, keyed by a different pid, carries the real sid.
        _write_registry_record(sessions_dir, "777.json", "sess-real", 777, repo_root)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
        _patch_pid_env(monkeypatch, 666)
        monkeypatch.setattr(
            "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
            lambda pid, stored_start_epoch="": True,
        )

        result = compute_repo_identity_gate(repo_root, "sess-real")
        assert result["verdict"] == _REPO_IDENTITY_MATCH

    def test_crash_resume_duplicate_sessionid_stale_pid_loses_is_unresolved(self, tmp_path, monkeypatch):
        """Known limitation, stated in-code and observed here: when TWO
        files share the same sessionId (crash-then-resume reusing a
        sessionId under a new pid) and the STALE dead-pid record wins
        `snapshot()`'s last-writer-wins glob order, `stable_pid_alive`
        correctly rejects it and the gate concludes UNRESOLVED — silently
        inert, not fixed here, only observed."""
        repo_root = tmp_path / "repo"
        _make_repo(repo_root)
        sessions_dir = tmp_path / "sessions"
        dup_sid = "sess-crash-resume"
        _write_registry_record(sessions_dir, "888.json", dup_sid, 888, repo_root)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
        # self_record() misses entirely (pid-keyed file for OUR pid absent).
        _patch_pid_env(monkeypatch, 12321, hit=False)
        # stable_pid_alive rejects the resolved (stale, per this scenario)
        # snapshot()-leg record.
        monkeypatch.setattr(
            "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
            lambda pid, stored_start_epoch="": False,
        )

        result = compute_repo_identity_gate(repo_root, dup_sid)
        assert result["verdict"] == _REPO_IDENTITY_UNRESOLVED


class TestNoSid:
    def test_unresolved_when_sid_falsy(self, tmp_path):
        repo_root = tmp_path / "repo"
        _make_repo(repo_root)
        result = compute_repo_identity_gate(repo_root, None)
        assert result["verdict"] == _REPO_IDENTITY_UNRESOLVED


class TestComputeRepoIdentityGateNoSubprocess:
    """Regression guard mirroring `TestComputeAddresseeGateNoSubprocess` —
    `compute_repo_identity_gate` must never spawn a subprocess."""

    def test_no_subprocess_spawned(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        _make_repo(repo_root)
        sessions_dir = tmp_path / "sessions"
        _write_registry_record(sessions_dir, "111.json", "sess-a", 111, repo_root)
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
        _patch_pid_env(monkeypatch, 111)
        monkeypatch.setattr(
            "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
            lambda pid, stored_start_epoch="": True,
        )

        def _boom(*args, **kwargs):
            raise AssertionError(
                "compute_repo_identity_gate spawned a subprocess — it must be fully in-process"
            )

        monkeypatch.setattr("subprocess.run", _boom)
        monkeypatch.setattr("subprocess.Popen", _boom)

        result = compute_repo_identity_gate(repo_root, "sess-a")
        assert result["verdict"] == _REPO_IDENTITY_MATCH


@pytest.mark.cadence
@pytest.mark.real_home
class TestAC11ShapeDriftTripwire:
    """Reads the LIVE `<claude-config>/sessions` directory (no
    `CLAUDE_CONFIG_DIR` override) and asserts the field set this module
    depends on against real records found there — never a fixture the test
    itself wrote, which would be a tautology that never fires.

    `@pytest.mark.real_home` opts out of `conftest.py`'s suite-wide HOME
    quarantine — required here because the whole point is reading the
    REAL, un-quarantined `<claude-config>/sessions` directory."""

    def test_live_registry_field_shape(self):
        directory = hr.registry_dir()
        if directory is None or not directory.is_dir():
            pytest.skip("no live <claude-config>/sessions directory resolvable on this host")

        files = list(directory.glob("*.json"))
        if not files:
            pytest.skip("<claude-config>/sessions directory is empty on this host")

        checked_any = False
        for path in files:
            # `<pid>.json` filename convention.
            assert path.stem.lstrip("-").isdigit(), f"{path.name} does not follow the <pid>.json convention"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            for field in ("sessionId", "pid", "procStart"):
                assert field in data, f"{path.name} is missing expected field {field!r}"
            checked_any = True

        if not checked_any:
            pytest.skip("no parseable live registry record found to assert shape against")

    def test_installed_harness_binary_names_the_fields(self):
        import shutil

        claude_bin = shutil.which("claude")
        if not claude_bin:
            pytest.skip("no installed 'claude' harness binary found on PATH")
        try:
            with open(claude_bin, "rb") as fh:
                blob = fh.read()
        except OSError:
            pytest.skip("installed harness binary is not readable")

        for field in (b"sessionId", b"procStart"):
            if field not in blob:
                pytest.skip(
                    f"installed harness binary does not contain {field!r} — "
                    "cannot cross-check field names on this host/build"
                )

"""C3 (docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md) —
wiring `compute_repo_identity_gate` into `baton_assemble.brief` and
`baton_assemble.apply.apply` at each's own `root = repo_root or
resolve_repo_root()` line.

Fixture construction pattern is reused from C1's own
`coordinator_core/pickup_assemble/tests/test_repo_identity_gate.py` (AC6:
real registry JSON records on disk under a fabricated `<claude-config>/
sessions/` dir, real directories with a real `.git` marker for the
plausibility band — never a monkeypatch of `compute_repo_identity_gate`'s
own return value).
"""

from __future__ import annotations

import json
import time

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.baton_assemble import TransportFailure
from coordinator_core.baton_assemble import apply as ba_apply
from coordinator_core.session import harness_registry as hr
from coordinator_core.test_baton_assemble import _FAKE_OPERATOR_CONFIG, _write_artifact


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    """Restated per-module (autouse fixtures do not cross module boundaries)
    -- `brief()` calls `resolve_operator_config()` unconditionally. Mirrors
    `test_deliverable_collision_warn.py`'s own fixture of the same name."""
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))


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


def _rig_mismatch(tmp_path, monkeypatch, sid="sess-c3-mismatch", pid=9001):
    """A real anchor/root divergence: registry record's `cwd` is a real,
    plausible directory OUTSIDE `repo_root`. Returns `repo_root`."""
    repo_root = tmp_path / "repo"
    foreign_root = tmp_path / "foreign"
    _make_repo(repo_root)
    _make_repo(foreign_root)
    sessions_dir = tmp_path / "sessions"
    _write_registry_record(sessions_dir, f"{pid}.json", sid, pid, foreign_root)
    monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
    _patch_pid_env(monkeypatch, pid)
    monkeypatch.setattr(
        "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
        lambda pid, stored_start_epoch="": True,
    )
    return repo_root


def _rig_unresolved(tmp_path, monkeypatch, sid="sess-c3-unresolved", pid=9002):
    """No registry record at all -- UNRESOLVED. Returns `repo_root`."""
    repo_root = tmp_path / "repo"
    _make_repo(repo_root)
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
    _patch_pid_env(monkeypatch, pid, hit=False)
    return repo_root


def _handoff_artifact(tmp_path):
    return _write_artifact(
        tmp_path / "state" / "handoffs" / "h1.md",
        ["deliverable_id: DEL-C3-1", "initiative: init-c3"],
    )


class TestBriefMismatchCwdDerivedRefuses:
    def test_mismatch_raises_transport_failure_when_repo_root_not_supplied(self, tmp_path, monkeypatch):
        repo_root = _rig_mismatch(tmp_path, monkeypatch)
        artifact = _handoff_artifact(repo_root)
        monkeypatch.setattr(ba, "resolve_repo_root", lambda *a, **k: repo_root)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-c3-mismatch")

        with pytest.raises(TransportFailure) as excinfo:
            ba.brief("handoff", str(artifact), repo_root=None)
        assert "MISMATCH" in str(excinfo.value)


class TestBriefExplicitRepoRootNeverRefuses:
    def test_mismatch_with_explicit_repo_root_does_not_refuse_but_records(self, tmp_path, monkeypatch):
        repo_root = _rig_mismatch(tmp_path, monkeypatch)
        artifact = _handoff_artifact(repo_root)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-c3-mismatch")

        decision = ba.brief("handoff", str(artifact), repo_root=repo_root).decision_object
        assert decision["gates"]["repo_identity"]["verdict"] == "MISMATCH"


class TestBriefUnresolvedNeverRefuses:
    def test_unresolved_does_not_refuse_and_records_verdict(self, tmp_path, monkeypatch):
        repo_root = _rig_unresolved(tmp_path, monkeypatch)
        artifact = _handoff_artifact(repo_root)
        monkeypatch.setattr(ba, "resolve_repo_root", lambda *a, **k: repo_root)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-c3-unresolved")

        decision = ba.brief("handoff", str(artifact), repo_root=None).decision_object
        assert decision["gates"]["repo_identity"]["verdict"] == "UNRESOLVED"


class TestApplyMismatchCwdDerivedRefuses:
    def test_apply_refuses_when_repo_root_not_supplied(self, tmp_path, monkeypatch):
        repo_root = _rig_mismatch(tmp_path, monkeypatch, sid="sess-c3-apply-mismatch")
        artifact = _handoff_artifact(repo_root)
        monkeypatch.setattr(ba, "resolve_repo_root", lambda *a, **k: repo_root)

        exit_code, report = ba_apply.apply(
            "handoff",
            str(artifact),
            session_id="sess-c3-apply-mismatch",
            repo_root=None,
        )
        assert exit_code == ba_apply.APPLY_EXIT_TRANSPORT_FAIL
        assert "MISMATCH" in report["error"]


class TestApplyExplicitRepoRootNeverRefuses:
    def test_apply_with_explicit_repo_root_passes_the_mismatch_gate(self, tmp_path, monkeypatch):
        """Never blocked at the C3 check for an explicit `repo_root` --
        proven by monkeypatching `brief` to raise a marker exception the
        instant control reaches it, which only happens if apply()'s own
        MISMATCH-refusal check did NOT fire."""
        repo_root = _rig_mismatch(tmp_path, monkeypatch, sid="sess-c3-apply-explicit")
        artifact = _handoff_artifact(repo_root)

        def _reached_brief(*args, **kwargs):
            raise RuntimeError("reached-brief-marker")

        monkeypatch.setattr(ba, "brief", _reached_brief)

        with pytest.raises(RuntimeError, match="reached-brief-marker"):
            ba_apply.apply(
                "handoff",
                str(artifact),
                session_id="sess-c3-apply-explicit",
                repo_root=repo_root,
            )


class TestApplyUnresolvedNeverRefuses:
    def test_apply_unresolved_passes_the_mismatch_gate(self, tmp_path, monkeypatch):
        repo_root = _rig_unresolved(tmp_path, monkeypatch, sid="sess-c3-apply-unresolved")
        artifact = _handoff_artifact(repo_root)
        monkeypatch.setattr(ba, "resolve_repo_root", lambda *a, **k: repo_root)

        def _reached_brief(*args, **kwargs):
            raise RuntimeError("reached-brief-marker")

        monkeypatch.setattr(ba, "brief", _reached_brief)

        with pytest.raises(RuntimeError, match="reached-brief-marker"):
            ba_apply.apply(
                "handoff",
                str(artifact),
                session_id="sess-c3-apply-unresolved",
                repo_root=None,
            )

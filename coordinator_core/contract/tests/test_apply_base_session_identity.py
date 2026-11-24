from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from coordinator_core.contract import apply_base
from coordinator_core.session.core import (
    session_identity_override,
    warm_served_request,
)
import pytest


_GATE_BACKSTOP_S = 60.0


class _Gated:
    """Event gates whose wait() result is recorded; a gate that never opened fails the test."""

    def __init__(self) -> None:
        self.missed: list[str] = []

    def wait(self, event: threading.Event, name: str) -> None:
        if not event.wait(timeout=_GATE_BACKSTOP_S):
            self.missed.append(name)

    def run_pair(self, target_a, target_b) -> None:
        threads = [threading.Thread(target=target_a), threading.Thread(target=target_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_GATE_BACKSTOP_S * 2)
            assert not t.is_alive()
        assert self.missed == []


def test_overlapping_session_identities_do_not_cross_contaminate() -> None:
    gate = _Gated()
    entered_a = threading.Event()
    observed_inside_a: dict[str, Optional[str]] = {}
    observed_inside_b: dict[str, Optional[str]] = {}
    let_b_finish = threading.Event()
    b_done = threading.Event()

    def _session_a() -> None:
        with apply_base.session_identity("session-A"):
            entered_a.set()
            gate.wait(b_done, "b_done")
            observed_inside_a["COORDINATOR_SESSION_ID"] = apply_base.current_session_env().get(
                "COORDINATOR_SESSION_ID"
            )
            let_b_finish.set()

    def _session_b() -> None:
        gate.wait(entered_a, "entered_a")
        with apply_base.session_identity("session-B"):
            observed_inside_b["COORDINATOR_SESSION_ID"] = apply_base.current_session_env().get(
                "COORDINATOR_SESSION_ID"
            )
            b_done.set()
            gate.wait(let_b_finish, "let_b_finish")

    gate.run_pair(_session_a, _session_b)

    assert observed_inside_a["COORDINATOR_SESSION_ID"] == "session-A"
    assert observed_inside_b["COORDINATOR_SESSION_ID"] == "session-B"


def test_overlapping_session_identities_leave_ambient_environ_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in apply_base.SESSION_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    gate = _Gated()
    entered_a = threading.Event()
    observed_environ_inside_a: dict[str, Optional[str]] = {}
    let_b_finish = threading.Event()
    b_done = threading.Event()

    def _session_a() -> None:
        with apply_base.session_identity("session-A"):
            entered_a.set()
            gate.wait(b_done, "b_done")
            observed_environ_inside_a["COORDINATOR_SESSION_ID"] = os.environ.get(
                "COORDINATOR_SESSION_ID"
            )
            let_b_finish.set()

    def _session_b() -> None:
        gate.wait(entered_a, "entered_a")
        with apply_base.session_identity("session-B"):
            b_done.set()
            gate.wait(let_b_finish, "let_b_finish")

    gate.run_pair(_session_a, _session_b)

    assert observed_environ_inside_a["COORDINATOR_SESSION_ID"] is None
    for var in apply_base.SESSION_ENV_VARS:
        assert var not in os.environ


class _RecordingRunGit:
    def __init__(self) -> None:
        self.observed_env: list[dict[str, Optional[str]]] = []

    def __call__(self, args: list[str], cwd: Path) -> SimpleNamespace:
        self.observed_env.append(
            {var: os.environ.get(var) for var in apply_base.SESSION_ENV_VARS}
        )
        if args[0] == "add":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ["diff", "--cached"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if args[0] == "commit":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "rev-parse":
            return SimpleNamespace(returncode=0, stdout="deadbeef\n", stderr="")
        raise AssertionError(f"unexpected git invocation: {args}")


def test_scoped_commit_mirrors_session_id_to_environ_only_around_run_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in apply_base.SESSION_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    artifact = tmp_path / "artifact.md"
    artifact.write_text("content\n", encoding="utf-8")
    run_git = _RecordingRunGit()

    with apply_base.session_identity("session-mirror"):
        sha = apply_base.scoped_commit(tmp_path, "artifact.md", "msg", run_git)
    assert sha == "deadbeef"
    assert run_git.observed_env
    for observed in run_git.observed_env:
        assert observed["COORDINATOR_SESSION_ID"] == "session-mirror"
        assert observed["CLAUDE_SESSION_ID"] == "session-mirror"

    for var in apply_base.SESSION_ENV_VARS:
        assert var not in os.environ


_SERVER_OWNER_SID = "a12e2a71-df13-414e-bfc7-bb4df5834a20"
_CALLING_SESSION_SID = "e2e739d9-2d4c-4f0e-8acf-833388113035"


def _with_server_owner_environ(monkeypatch) -> None:
    for var in apply_base.SESSION_ENV_READ_ORDER:
        monkeypatch.setenv(var, _SERVER_OWNER_SID)


def test_warm_served_apply_resolves_the_caller_not_the_server_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_server_owner_environ(monkeypatch)
    with warm_served_request(True), session_identity_override(_CALLING_SESSION_SID):
        assert apply_base.resolve_explicit_session_id(None) == _CALLING_SESSION_SID


def test_warm_served_apply_fails_closed_when_the_caller_carried_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_server_owner_environ(monkeypatch)
    with warm_served_request(True):
        assert apply_base.resolve_explicit_session_id(None) is None


def test_explicit_session_id_still_wins_under_a_warm_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_server_owner_environ(monkeypatch)
    with warm_served_request(True), session_identity_override(_CALLING_SESSION_SID):
        assert apply_base.resolve_explicit_session_id("explicit-id") == "explicit-id"


def test_cold_resolution_is_unchanged_by_the_warm_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in apply_base.SESSION_ENV_READ_ORDER:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(apply_base.SESSION_ENV_READ_ORDER[-1], _CALLING_SESSION_SID)
    assert apply_base.resolve_explicit_session_id(None) == _CALLING_SESSION_SID


def test_cold_resolution_walks_the_ladder_in_precedence_order(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in apply_base.SESSION_ENV_READ_ORDER:
        monkeypatch.setenv(var, f"{var}-value")
    expected = f"{apply_base.SESSION_ENV_READ_ORDER[0]}-value"
    assert apply_base.resolve_explicit_session_id(None) == expected


def test_cold_resolution_returns_none_with_no_identity_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in apply_base.SESSION_ENV_READ_ORDER:
        monkeypatch.delenv(var, raising=False)
    assert apply_base.resolve_explicit_session_id(None) is None


def test_record_ledger_entry_gates_on_unresolvable_committer(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    import logging

    resolver_calls: list[object] = []
    append_calls: list[object] = []

    monkeypatch.setattr(
        "coordinator_core.commit_ledger.resolve_owner.resolve_owner_handoff_id",
        lambda *a, **k: resolver_calls.append(a) or ("h", False),
    )
    monkeypatch.setattr(
        "coordinator_core.commit_ledger.store.append_entry",
        lambda *a, **k: append_calls.append(a),
    )
    monkeypatch.setattr(
        apply_base, "_ledger_kind_and_weight", lambda *a, **k: ("artifact", None)
    )
    monkeypatch.setattr(apply_base, "_committer_id_for_ledger", lambda _root: "")

    with caplog.at_level(logging.WARNING):
        apply_base.record_ledger_entry(tmp_path, ["some/path.md"], "deadbeef")

    assert resolver_calls == []
    assert append_calls == []
    assert any("no committer identity" in r.getMessage() for r in caplog.records)

"""chunk C6 (docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-
cold-start.md): `apply_base.session_identity()` moved from an `os.environ`
process-wide mutation to a per-context `contextvars.ContextVar` scope, with
`os.environ` mirrored only at the one outermost boundary a subprocess spawn
needs it (`scoped_commit`'s own `run_git` calls). This file proves the two
assertions the C2 characterization test (`coordinator_core/warm/tests/
test_process_global_characterization.py::
test_session_identity_cross_contaminates_ambient_environ_under_interleave`)
established were MISSING at HEAD: two overlapping identities do not
cross-contaminate, and the ambient `os.environ` is unchanged once both
overlapping blocks have exited.

Spec backlink: this plan, chunk C6.
"""
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


def test_overlapping_session_identities_do_not_cross_contaminate():
    """Two interleaved `session_identity()` blocks (the warm-dispatch
    shape: two threads, each holding its own identity for its own block's
    lifetime) each observe ONLY their own session id for the duration of
    their own block -- the opposite of the C2 characterization test's
    proven-wrong behaviour."""
    entered_a = threading.Event()
    observed_inside_a: dict[str, Optional[str]] = {}
    observed_inside_b: dict[str, Optional[str]] = {}
    let_b_finish = threading.Event()
    b_done = threading.Event()

    def _session_a():
        with apply_base.session_identity("session-A"):
            entered_a.set()
            b_done.wait(timeout=5)
            observed_inside_a["COORDINATOR_SESSION_ID"] = apply_base.current_session_env().get(
                "COORDINATOR_SESSION_ID"
            )
            let_b_finish.set()

    def _session_b():
        entered_a.wait(timeout=5)
        with apply_base.session_identity("session-B"):
            observed_inside_b["COORDINATOR_SESSION_ID"] = apply_base.current_session_env().get(
                "COORDINATOR_SESSION_ID"
            )
            b_done.set()
            let_b_finish.wait(timeout=5)

    thread_a = threading.Thread(target=_session_a)
    thread_b = threading.Thread(target=_session_b)
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)

    assert observed_inside_a["COORDINATOR_SESSION_ID"] == "session-A"
    assert observed_inside_b["COORDINATOR_SESSION_ID"] == "session-B"


def test_overlapping_session_identities_leave_ambient_environ_unchanged():
    """`os.environ` is never written by `session_identity()` itself -- only
    `_mirror_session_env_for_subprocess` does, and only for the duration of
    one subprocess call. Two overlapping blocks that never call
    `scoped_commit` must leave the ambient `os.environ` exactly as it was
    before either block was entered, both DURING the overlap and after both
    have exited."""
    original = {var: os.environ.get(var) for var in apply_base.SESSION_ENV_VARS}
    for var in apply_base.SESSION_ENV_VARS:
        os.environ.pop(var, None)

    entered_a = threading.Event()
    observed_environ_inside_a: dict[str, Optional[str]] = {}
    let_b_finish = threading.Event()
    b_done = threading.Event()

    def _session_a():
        with apply_base.session_identity("session-A"):
            entered_a.set()
            b_done.wait(timeout=5)
            observed_environ_inside_a["COORDINATOR_SESSION_ID"] = os.environ.get(
                "COORDINATOR_SESSION_ID"
            )
            let_b_finish.set()

    def _session_b():
        entered_a.wait(timeout=5)
        with apply_base.session_identity("session-B"):
            b_done.set()
            let_b_finish.wait(timeout=5)

    try:
        thread_a = threading.Thread(target=_session_a)
        thread_b = threading.Thread(target=_session_b)
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=5)
        thread_b.join(timeout=5)

        assert observed_environ_inside_a["COORDINATOR_SESSION_ID"] is None
        for var in apply_base.SESSION_ENV_VARS:
            assert os.environ.get(var) == original[var]
    finally:
        for var, value in original.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value


class _RecordingRunGit:
    def __init__(self) -> None:
        self.observed_env: list[dict[str, Optional[str]]] = []

    def __call__(self, args: list[str], cwd: Path):
        self.observed_env.append(
            {var: os.environ.get(var) for var in apply_base.SESSION_ENV_VARS}
        )
        if args[0] == "add":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ["diff", "--cached"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")  # "changed"
        if args[0] == "commit":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "rev-parse":
            return SimpleNamespace(returncode=0, stdout="deadbeef\n", stderr="")
        raise AssertionError(f"unexpected git invocation: {args}")


def test_scoped_commit_mirrors_session_id_to_environ_only_around_run_git(tmp_path):
    """The ONE outermost boundary: while `session_identity()` is active,
    `scoped_commit`'s own `run_git` calls observe the session id mirrored
    into `os.environ` -- and the mirror is undone immediately after each
    call, so `os.environ` is not left holding it in between."""
    original = {var: os.environ.get(var) for var in apply_base.SESSION_ENV_VARS}
    for var in apply_base.SESSION_ENV_VARS:
        os.environ.pop(var, None)

    artifact = tmp_path / "artifact.md"
    artifact.write_text("content\n", encoding="utf-8")
    run_git = _RecordingRunGit()

    try:
        with apply_base.session_identity("session-mirror"):
            sha = apply_base.scoped_commit(tmp_path, "artifact.md", "msg", run_git)
        assert sha == "deadbeef"
        assert run_git.observed_env
        for observed in run_git.observed_env:
            assert observed["COORDINATOR_SESSION_ID"] == "session-mirror"
            assert observed["CLAUDE_SESSION_ID"] == "session-mirror"

        # Outside the session_identity() block (and between each run_git
        # call, though this only asserts the post-block state), the mirror
        # must be undone -- never left resident in os.environ.
        for var in apply_base.SESSION_ENV_VARS:
            assert os.environ.get(var) == original[var]
    finally:
        for var, value in original.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value


# ---------------------------------------------------------------------------
# `resolve_explicit_session_id` — the warm arm.
#
# The sibling half of the same defect this file's first tests cover: identity
# SET per-context was fixed at C6, identity READ was not. Under a warm dispatch
# `os.environ` names whoever spawned the server, so the bare env walk this
# resolver used to be handed a live peer's id to every door-routed assembler
# `apply` and fed it straight into the repo-identity gate, an anti-forgery
# input. Reproduced verbatim before the fix (the stand-in below uses the two
# real session ids from
# state/bug-backlog/2026-08-30-baton-assemble-apply-resolves-a-foreign-session-
# identity.yaml): warm-served, caller carrying e2e739d9…, the resolver returned
# a12e2a71… — the server owner's.
# ---------------------------------------------------------------------------

_SERVER_OWNER_SID = "a12e2a71-df13-414e-bfc7-bb4df5834a20"
_CALLING_SESSION_SID = "e2e739d9-2d4c-4f0e-8acf-833388113035"


def _with_server_owner_environ(monkeypatch):
    """Stand in for the resident warm server's own environment: the id of
    whoever won the last warm election, in every tier of the ladder."""
    for var in apply_base.SESSION_ENV_READ_ORDER:
        monkeypatch.setenv(var, _SERVER_OWNER_SID)


def test_warm_served_apply_resolves_the_caller_not_the_server_owner(monkeypatch):
    _with_server_owner_environ(monkeypatch)
    with warm_served_request(True), session_identity_override(_CALLING_SESSION_SID):
        assert apply_base.resolve_explicit_session_id(None) == _CALLING_SESSION_SID


def test_warm_served_apply_fails_closed_when_the_caller_carried_nothing(monkeypatch):
    """A door image that sends no `_session_id` leaves tier 0 empty. The
    resolver must return None so its consumers refuse — never substitute the
    ambient id, which is the misattribution the whole seam exists to close."""
    _with_server_owner_environ(monkeypatch)
    with warm_served_request(True):
        assert apply_base.resolve_explicit_session_id(None) is None


def test_explicit_session_id_still_wins_under_a_warm_dispatch(monkeypatch):
    """`--session-id` is the caller stating identity outright; the warm arm
    must not shadow it."""
    _with_server_owner_environ(monkeypatch)
    with warm_served_request(True), session_identity_override(_CALLING_SESSION_SID):
        assert apply_base.resolve_explicit_session_id("explicit-id") == "explicit-id"


def test_cold_resolution_is_unchanged_by_the_warm_arm(monkeypatch):
    """Cold, `os.environ` IS the caller's own and the env walk is correct.
    Nothing binds the warm flag on a cold invocation, so this is the arm every
    existing consumer keeps."""
    for var in apply_base.SESSION_ENV_READ_ORDER:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(apply_base.SESSION_ENV_READ_ORDER[-1], _CALLING_SESSION_SID)
    assert apply_base.resolve_explicit_session_id(None) == _CALLING_SESSION_SID


def test_cold_resolution_walks_the_ladder_in_precedence_order(monkeypatch):
    for var in apply_base.SESSION_ENV_READ_ORDER:
        monkeypatch.setenv(var, f"{var}-value")
    expected = f"{apply_base.SESSION_ENV_READ_ORDER[0]}-value"
    assert apply_base.resolve_explicit_session_id(None) == expected


def test_cold_resolution_returns_none_with_no_identity_anywhere(monkeypatch):
    for var in apply_base.SESSION_ENV_READ_ORDER:
        monkeypatch.delenv(var, raising=False)
    assert apply_base.resolve_explicit_session_id(None) is None


def test_record_ledger_entry_gates_on_unresolvable_committer(monkeypatch, caplog, tmp_path):
    """`resolve_session_id` documents empty as its legal "unresolvable"
    return that callers gate on; `resolve_owner_handoff_id` hard-raises on
    it. Before this gate, a warm-served commit -- whose process env is the
    supervisor's and whose per-request identity scope was never bound --
    reached the owner resolver with `""` and produced a `ValueError`
    traceback under the "commit ledger write failed" warning, reading as a
    ledger bug rather than an identity one. Observed live on an
    `archive-stamp-cli action-memo` commit, 2026-09-02.

    Asserts the gate, not the message text beyond its identity token: the
    owner resolver is never reached, nothing is appended, and the miss is
    still WARNed rather than silently dropped.
    """
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

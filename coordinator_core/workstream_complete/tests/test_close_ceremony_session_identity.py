"""The close ceremony resolves the CALLER, or refuses — never the engine's owner.

THE INCIDENT (2026-08-30, state/bug-backlog/2026-08-30-close-ceremony-clis-resolve-a-
live-peer-b558b27c74e7.yaml). `workstream-complete-assemble.exe brief`, dialled through
the warm published door by session 56043240-f71b-447a-bf56-4ee49f92ab33, returned a
ceremony keyed to `--sid 1189eead-f3eb-4c54-a790-236258043b0d` — a LIVE PEER, holding six
unrelated `Deliverable-Id` trailers and an archived baton. `apply` on that brief would
have written a completion entry and fired `d-complete-entry` / `review-brightline-gate`
crediting the peer's workstream to this session's close. The same tree's cold `.cmd`
door, seconds later, resolved 56043240 correctly. Nothing on the path guarded it; the
close was aborted by an EM who happened to recognise the deliverable ids as unfamiliar.

WHY AN IN-PROCESS TEST AND NOT A DOOR REPRODUCTION. The warm server serves the PUBLISHED
engine (the klabauter twin, `machine-local get repos.klabauter`), so a fix landed here is inert against that door until a
publish round — re-running the `.exe` after this change reproduces the old behaviour from
a different copy and says nothing about this one (the trap
state/sizings/2026-08-30-the-c-door-never-sends-session-id.yaml names). These tests bind
`session.core.warm_served_request` / `session_identity_override` directly, which is the
same per-request state `warm.entry_seam.per_request_state` binds, so they exercise the
resolver's warm branch against THIS copy without a server.

Sibling coverage: `coordinator_core/tests/test_warm_identity_env_reads.py` is the AST
ratchet that keeps `coordinator/bin/wsc-session-disposition.py` off `os.environ` for the
governed vars. It cannot see behaviour — that the cold branch still answers, that the
warm branch prefers the carried id over a conflicting environment, and that an
unidentified warm caller is REFUSED rather than defaulted — which is what these are for.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from coordinator_core import workstream_complete as wsc
from coordinator_core.session.core import session_identity_override, warm_served_request
from coordinator_core.workstream_complete import (
    SessionIdentityUnresolved,
    compute_session_shape_gate,
)

CALLER = "56043240-f71b-447a-bf56-4ee49f92ab33"
ENGINE_OWNER = "1189eead-f3eb-4c54-a790-236258043b0d"


def _load_real_disposition_module():
    """The REAL `coordinator/bin/wsc-session-disposition.py`, loaded by file path —
    the same idiom `test_workstream_complete.py` uses, and for the same reason: the
    hyphenated filename bars a plain import, and the ceremony's own
    `_load_session_disposition_module` resolves operator config (`claude_klabauter_root`) that a
    bare test process has no business needing. This is the same source file the
    ceremony loads; only the locating step differs."""
    bin_dir = Path(__file__).resolve().parents[3] / "coordinator" / "bin"
    loader = importlib.machinery.SourceFileLoader(
        "wsc_session_disposition_identity", str(bin_dir / "wsc-session-disposition.py")
    )
    spec = importlib.util.spec_from_loader("wsc_session_disposition_identity", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


@pytest.fixture()
def disposition_module():
    return _load_real_disposition_module()


def _clear_session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "em_sid",
        "COORDINATOR_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
    ):
        monkeypatch.delenv(var, raising=False)


def test_cold_resolution_reads_the_callers_own_environment(
    disposition_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cold is untouched: there `os.environ` IS the caller's own process."""
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CALLER)
    assert disposition_module.resolve_session_id(Path(".")) == CALLER


def test_cold_resolution_accepts_coordinator_session_id(
    disposition_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-fix ladder here was `em_sid`/`CLAUDE_SESSION_ID`/
    `CLAUDE_CODE_SESSION_ID` — a fourth private copy missing the highest-precedence
    var, so a session carrying only `COORDINATOR_SESSION_ID` resolved to nothing and
    the ceremony refused a session that had identified itself perfectly well. Routing
    through the canonical resolver closes that too."""
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", CALLER)
    assert disposition_module.resolve_session_id(Path(".")) == CALLER


def test_cold_resolution_keeps_the_legacy_em_sid_tier(
    disposition_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`em_sid` predates `SESSION_ENV_PRECEDENCE` and is not in it. It stays a
    cold-only first tier so no operator whose environment carries only that var loses
    the ability to close."""
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("em_sid", CALLER)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ENGINE_OWNER)
    assert disposition_module.resolve_session_id(Path(".")) == CALLER


def test_warm_resolution_prefers_the_carried_identity_over_the_environment(
    disposition_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident's exact shape, inverted: the environment names the engine's owner
    and the request carries the caller. The caller must win."""
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ENGINE_OWNER)
    with warm_served_request(True), session_identity_override(CALLER):
        assert disposition_module.resolve_session_id(Path(".")) == CALLER


def test_warm_resolution_refuses_the_ambient_environment_when_nothing_was_carried(
    disposition_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident itself. The door sent no `_session_id`, so the request carries
    nothing and `os.environ` holds whoever spawned the server. The resolver must
    answer "" — NOT the engine owner's id, which no downstream reader could tell from
    a genuine one."""
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ENGINE_OWNER)
    with warm_served_request(True):
        assert disposition_module.resolve_session_id(Path(".")) == ""


def test_warm_resolution_ignores_em_sid_too(
    disposition_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`em_sid` is outside `SESSION_ENV_PRECEDENCE`, so the canonical resolver does not
    govern it and the AST ratchet does not scan for it. Reading it on the warm branch
    would reinstate the whole defect through the one var nothing else watches."""
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("em_sid", ENGINE_OWNER)
    with warm_served_request(True):
        assert disposition_module.resolve_session_id(Path(".")) == ""


def test_ceremony_refuses_to_build_on_an_unresolved_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The hard stop the incident had no equivalent of.

    `_cmd_resolve` (the CLI path) already returned `_SESSION_ID_UNRESOLVED` on an empty
    sid. `compute_session_shape_gate` — the IN-PROCESS path the warm door actually
    takes — did not, and would have classified `single-session` against `""`, which
    reads downstream as a clean close. The refusal must be an exception, not a
    diagnostic: every gate, directive and completion entry below is keyed on this
    value.
    """
    _clear_session_env(monkeypatch)
    monkeypatch.setattr(wsc, "_load_session_disposition_module", _load_real_disposition_module)
    with warm_served_request(True):
        with pytest.raises(SessionIdentityUnresolved) as excinfo:
            compute_session_shape_gate(tmp_path)
    message = str(excinfo.value)
    assert "cannot identify the calling session" in message
    assert "COORDINATOR_SESSION_ID" in message


def test_refusal_is_not_a_transport_failure() -> None:
    """`TransportFailure`'s operator text tells the reader to check their git worktree.
    That is the one thing that is not wrong here, so the classes must not be
    interchangeable — and the CLI arm for this one sits AHEAD of the transport arm."""
    from coordinator_core.workstream_complete import TransportFailure

    assert not issubclass(SessionIdentityUnresolved, TransportFailure)
    assert not issubclass(TransportFailure, SessionIdentityUnresolved)

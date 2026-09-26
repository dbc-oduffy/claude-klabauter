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
import json
import os
import sys
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
    bin_dir = Path(__file__).resolve().parents[3] / "coordinator" / "bin"
    for entry in (bin_dir, bin_dir / "lib"):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))
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
        "COORDINATOR_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
    ):
        monkeypatch.delenv(var, raising=False)


def test_cold_resolution_reads_the_callers_own_environment(
    disposition_module, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_warm_resolution_prefers_the_carried_identity_over_the_environment(
    disposition_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ENGINE_OWNER)
    with warm_served_request(True), session_identity_override(CALLER):
        assert disposition_module.resolve_session_id(Path(".")) == CALLER


def test_warm_resolution_refuses_the_ambient_environment_when_nothing_was_carried(
    disposition_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ENGINE_OWNER)
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
    from coordinator_core.workstream_complete import TransportFailure

    assert not issubclass(SessionIdentityUnresolved, TransportFailure)
    assert not issubclass(TransportFailure, SessionIdentityUnresolved)


def test_source_is_reported_for_a_cold_env_resolution(
    disposition_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CALLER)
    resolved = disposition_module.resolve_session_id_with_source(Path("."))
    assert resolved.session_id == CALLER
    assert resolved.source == "CLAUDE_CODE_SESSION_ID"
    assert resolved.warm is False
    assert resolved.pid == os.getpid()


def test_source_names_the_precedence_winner_not_merely_a_holder(
    disposition_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two vars holding DIFFERENT ids: the label must name the one that won, or it
    is not provenance — it is a guess that happens to be right whenever the ladder
    is unambiguous, which is exactly when nobody needs it."""
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", CALLER)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ENGINE_OWNER)
    resolved = disposition_module.resolve_session_id_with_source(Path("."))
    assert (resolved.session_id, resolved.source) == (CALLER, "COORDINATOR_SESSION_ID")


def test_carried_identity_is_reported_as_carried_even_when_env_agrees(
    disposition_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CALLER)
    with warm_served_request(True), session_identity_override(CALLER):
        resolved = disposition_module.resolve_session_id_with_source(Path("."))
    assert (resolved.session_id, resolved.source, resolved.warm) == (CALLER, "carried", True)


def test_the_incident_is_legible_in_the_record(
    disposition_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ENGINE_OWNER)
    with warm_served_request(True):
        resolved = disposition_module.resolve_session_id_with_source(Path("."))
    assert resolved.session_id == ""
    assert resolved.source == "unresolved"
    assert resolved.warm is True


def test_the_refusal_names_the_input_it_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_session_env(monkeypatch)
    monkeypatch.setattr(wsc, "_load_session_disposition_module", _load_real_disposition_module)
    with warm_served_request(True):
        with pytest.raises(wsc.SessionIdentityUnresolved) as excinfo:
            wsc.compute_session_shape_gate(tmp_path)
    message = str(excinfo.value)
    assert "source='unresolved'" in message
    assert "warm=True" in message
    assert f"pid={os.getpid()}" in message


def test_provenance_rides_the_gate_on_the_SUCCESS_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The load-bearing one. On the day of the incident resolution SUCCEEDED — at
    naming the wrong session — so provenance that only appeared on failure would
    have been silent. `gates.session_shape.sid_source` is emitted beside the id it
    describes, every time.
    """
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", CALLER)
    monkeypatch.setattr(wsc, "_load_session_disposition_module", _load_real_disposition_module)
    gate = wsc.compute_session_shape_gate(tmp_path)
    assert gate.sid == CALLER
    assert gate.sid_source == {
        "source": "COORDINATOR_SESSION_ID",
        "warm": False,
        "pid": os.getpid(),
    }


def test_the_gate_stays_json_serialisable_with_provenance_on_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_SESSION_ID", CALLER)
    monkeypatch.setattr(wsc, "_load_session_disposition_module", _load_real_disposition_module)
    gate = wsc.compute_session_shape_gate(tmp_path)
    assert json.loads(json.dumps(gate._asdict()))["sid_source"]["source"] == "CLAUDE_SESSION_ID"


# The provenance is a nicety; the RESOLUTION is not. These pin that a copy skew


def test_an_engine_without_provenance_still_resolves_the_session(
    disposition_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CALLER)

    real_core = disposition_module._session_core()

    class _OldEngine:
        attributable_session_id = staticmethod(real_core.attributable_session_id)
        resolve_session_id = staticmethod(real_core.resolve_session_id)
        in_warm_served_request = staticmethod(real_core.in_warm_served_request)

    monkeypatch.setattr(disposition_module, "_session_core", lambda: _OldEngine)
    resolved = disposition_module.resolve_session_id_with_source(Path("."))
    assert resolved.session_id == CALLER
    assert resolved.source == "unreported-engine-predates-provenance"
    assert resolved.warm is False


def test_an_engine_without_even_the_warm_accessor_still_resolves(
    disposition_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CALLER)

    real_core = disposition_module._session_core()

    class _OlderEngine:
        resolve_session_id = staticmethod(real_core.resolve_session_id)
        in_warm_served_request = staticmethod(real_core.in_warm_served_request)

    monkeypatch.setattr(disposition_module, "_session_core", lambda: _OlderEngine)
    assert disposition_module.resolve_session_id_with_source(Path(".")).session_id == CALLER


def test_an_engine_without_even_the_warm_accessor_still_resolves_under_warm(
    disposition_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """# The sibling test above
    only exercises the blend-reinstatement rung on the cold path, where blending
    ambient `os.environ` is harmless because it IS the caller. The rung this
    module falls to when even `attributable_session_id` is missing reinstates
    exactly the defect `attributable_session_id`'s own docstring documents and
    `state/bug-backlog/2026-08-29-the-warm-door-s-exe-route-stamps-the-ser-
    47373b19c77e.yaml` measured live: under a warm-served request, plain
    `resolve_session_id` reads `os.environ`, which belongs to whoever SPAWNED
    the process, not to the current session. This test PINS that the degrade
    knowingly reinstates that misattribution risk under warm with only ambient
    env set (no carried id) rather than pretending the blend is safe there —
    refusing outright in this rung would be STRICTER than the copy being
    degraded to, which would break every close against that copy. The
    resolution still returns an id; it is the SPAWNER's id, not the caller's,
    and that is the documented, deliberate cost of degrading to an engine this
    old, not a defect in this test or the degrade."""
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CALLER)

    real_core = disposition_module._session_core()

    class _OlderEngine:
        resolve_session_id = staticmethod(real_core.resolve_session_id)
        in_warm_served_request = staticmethod(real_core.in_warm_served_request)

    monkeypatch.setattr(disposition_module, "_session_core", lambda: _OlderEngine)
    with warm_served_request(True):
        resolved = disposition_module.resolve_session_id_with_source(Path("."))
    assert resolved.session_id == CALLER


def _pre_provenance_bin_module():
    """A stand-in for a bin script predating `resolve_session_id_with_source`.

    Built as a stand-in rather than by deleting the attribute off the real
    module: that module's `resolve_session_id` DELEGATES to the provenance
    function, so removing the one leaves the other raising `NameError` — a
    shape no released copy of this file ever had, and a test that would then
    pin a failure mode that cannot occur.

    # This stand-in's
    # `resolve_session_id` calls `core.resolve_session_id`, the actual older
    # accessor a real pre-provenance bin script shipped with, not today's
    # warm/cold-safe `attributable_session_id`. Calling the current accessor
    # behind an old-shaped name pinned the mirror-direction ROUTING switch
    # (`with_source is None` -> `mod.resolve_session_id(repo_root)`) without
    # ever exercising the CLI-side degrade against a genuinely old bin script
    # that itself used the unsafe blend — a strictly weaker claim than the
    # engine-side degrade this file's split-copy tests are worried about.
    """
    real = _load_real_disposition_module()

    class _OldBinModule:
        resolve_session_id = staticmethod(
            lambda repo_root=None: real._session_core().resolve_session_id() or ""
        )
        resolve_disposition = staticmethod(real.resolve_disposition)

    return _OldBinModule


def test_an_older_bin_script_costs_provenance_not_the_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", CALLER)
    monkeypatch.setattr(
        wsc, "_load_session_disposition_module", lambda: _pre_provenance_bin_module()
    )
    gate = wsc.compute_session_shape_gate(tmp_path)
    assert gate.sid == CALLER
    assert gate.sid_source is None


def test_the_refusal_survives_an_older_bin_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_session_env(monkeypatch)
    monkeypatch.setattr(
        wsc, "_load_session_disposition_module", lambda: _pre_provenance_bin_module()
    )
    with warm_served_request(True):
        with pytest.raises(wsc.SessionIdentityUnresolved) as excinfo:
            wsc.compute_session_shape_gate(tmp_path)
    assert "source='unavailable'" in str(excinfo.value)

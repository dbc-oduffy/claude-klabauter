"""Wiring tests for the blanket-disarm marker's integration into
``dispatch.evaluate_payload_json`` (M-disarm-wiring, 2026-07-30).

``_blanket_disarm.py`` itself (39 tests in ``test_blanket_disarm.py``) was
built and tested in a prior dispatch but deliberately left UNWIRED --
nothing in ``dispatch.py`` consulted it. This file is the wiring's OWN
test coverage: does the marker actually suppress what the loop in
``evaluate_payload_json`` runs, band by band, with the same fail-closed
guarantees the marker module itself promises.

Two test classes:

  - ``TestControlledBandSuppression`` -- monkeypatches
    ``dispatch._build_guard_chain`` to return a small, fully-controlled
    three-entry chain (one guard per band, each returning a distinguishable
    envelope) so band suppression can be asserted deterministically without
    depending on interactions among the ~30 real guards (several of which
    overlap in what command shapes they match, which would make a
    real-guard-only test fragile and hard to reason about). Exercises the
    REAL ``evaluate_payload_json`` loop and the REAL ``_blanket_disarm``
    marker-evaluation code -- only the guard *registration* is swapped in.
  - ``TestRealGuardSuppression`` -- one positive control against an actual
    registered guard (``sed-range-read-advise``, ADVISORY_REWRITE), proving
    the wiring holds against the real chain, not merely the synthetic one.

Spec backlink: coordinator_core/bash_guards/dispatch.py (guard_chain loop),
coordinator_core/bash_guards/_blanket_disarm.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.bash_guards import _blanket_disarm as bd
from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards.dispatch import GuardBand, GuardEntry


@pytest.fixture(autouse=True)
def _isolated_marker(tmp_path, monkeypatch):
    """Same isolation discipline as test_blanket_disarm.py's own fixture --
    redirect the marker's settings-home root and clear the per-process
    cache between tests."""
    monkeypatch.setattr(bd, "settings_home", lambda: tmp_path)
    bd._cache.clear()
    yield
    bd._cache.clear()


def _write_marker(tmp_path, text: str) -> None:
    (tmp_path / bd.MARKER_BASENAME).write_text(text, encoding="utf-8")


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _payload(session_id="sess-wire-1", agent_id=None, agent_type=None, cmd="echo probe"):
    d = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": session_id,
        "cwd": "/tmp",
    }
    if agent_id is not None:
        d["agent_id"] = agent_id
    if agent_type is not None:
        d["agent_type"] = agent_type
    return d


EM_PAYLOAD = _payload()
SUBAGENT_PAYLOAD = _payload(agent_id="agent-123", agent_type="executor")


def _run(payload):
    return dispatch.evaluate_payload_json(json.dumps(payload))


# ---------------------------------------------------------------------------
# Controlled three-entry chain: one guard per band, each distinguishable.
# ---------------------------------------------------------------------------

_FAKE_CHAIN = [
    GuardEntry("fake-confinement", lambda: None, True, GuardBand.CONFINEMENT_DENY),
    GuardEntry("fake-advisory", lambda: {"marker": "advisory"}, False, GuardBand.ADVISORY_REWRITE),
    GuardEntry("fake-platform", lambda: {"marker": "platform"}, True, GuardBand.PLATFORM_CONDITIONED_DENY),
]


class TestControlledBandSuppression:
    @pytest.fixture(autouse=True)
    def _fake_guard_chain(self, monkeypatch):
        # Scoped to THIS class only -- TestRealGuardSuppression below needs
        # the real, unpatched `_build_guard_chain`.
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: list(_FAKE_CHAIN))

    def test_no_marker_runs_every_guard_first_non_none_wins(self):
        # fake-confinement returns None -> falls through to fake-advisory,
        # which returns non-None and wins (fake-platform never reached).
        assert _run(EM_PAYLOAD) == {"marker": "advisory"}

    def test_advisory_rewrite_suppressed_falls_through_to_platform(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {_iso(now)}\nBands: advisory-rewrite\nReason: x\n",
        )
        assert _run(EM_PAYLOAD) == {"marker": "platform"}

    def test_both_suppressible_bands_suppressed_yields_none(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            "Scope: machine-total\n"
            f"Since: {_iso(now)}\n"
            "Bands: advisory-rewrite,platform-conditioned-deny\nReason: x\n",
        )
        assert _run(EM_PAYLOAD) is None

    def test_confinement_deny_band_cannot_be_named_marker_rejected_whole(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            "Scope: machine-total\n"
            f"Since: {_iso(now)}\n"
            "Bands: advisory-rewrite,confinement-deny\nReason: x\n",
        )
        # The whole marker is malformed (see _blanket_disarm's own "BAND-
        # SCOPED SUPPRESSION" doctring) -- so NEITHER band is suppressed,
        # not even the otherwise-legitimate advisory-rewrite one. Proves
        # this at the dispatcher level: fake-advisory still fires.
        assert _run(EM_PAYLOAD) == {"marker": "advisory"}

    def test_confinement_deny_never_suppressed_even_if_fake_confinement_denied(self, monkeypatch, tmp_path):
        """Belt-and-suspenders: even if a CONFINEMENT_DENY guard actually
        DENIES (rather than this suite's fake always-None one), a marker
        naming every band (rejected whole for naming confinement-deny)
        must not suppress it."""
        denying_chain = [
            GuardEntry("fake-confinement-deny", lambda: {"marker": "confinement-deny-fired"}, True, GuardBand.CONFINEMENT_DENY),
            GuardEntry("fake-advisory", lambda: {"marker": "advisory"}, False, GuardBand.ADVISORY_REWRITE),
        ]
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: list(denying_chain))
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            "Scope: machine-total\n"
            f"Since: {_iso(now)}\n"
            "Bands: advisory-rewrite,confinement-deny\nReason: x\n",
        )
        assert _run(EM_PAYLOAD) == {"marker": "confinement-deny-fired"}

    def test_session_scope_does_not_leak_to_dispatched_subagent(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            "Scope: session\nSession: sess-wire-1\n"
            f"Since: {_iso(now)}\nExpires: {_iso(now + timedelta(hours=1))}\n"
            "Bands: advisory-rewrite,platform-conditioned-deny\nReason: x\n",
        )
        # EM: both suppressible bands suppressed -> None.
        assert _run(EM_PAYLOAD) is None
        # Same session_id, but a dispatched subagent -- no-inherit means the
        # marker does not apply; fake-advisory still fires.
        assert _run(SUBAGENT_PAYLOAD) == {"marker": "advisory"}

    def test_machine_total_scope_never_disarms_a_subagent(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {_iso(now)}\n"
            "Bands: advisory-rewrite,platform-conditioned-deny\nReason: x\n",
        )
        assert _run(EM_PAYLOAD) is None
        assert _run(SUBAGENT_PAYLOAD) == {"marker": "advisory"}

    def test_absent_marker_stays_fully_armed(self):
        assert _run(EM_PAYLOAD) == {"marker": "advisory"}

    def test_malformed_marker_stays_fully_armed(self, tmp_path):
        _write_marker(tmp_path, "this is garbage, not Key: value shaped \x00")
        assert _run(EM_PAYLOAD) == {"marker": "advisory"}

    def test_expired_marker_stays_fully_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            "Scope: time\n"
            f"Since: {_iso(now - timedelta(hours=2))}\nExpires: {_iso(now - timedelta(hours=1))}\n"
            "Bands: advisory-rewrite,platform-conditioned-deny\nReason: x\n",
        )
        assert _run(EM_PAYLOAD) == {"marker": "advisory"}


class TestRealGuardSuppression:
    """Positive control against a REAL registered guard (not the synthetic
    chain above), proving the wiring holds end to end."""

    def test_sed_range_read_advise_suppressed_by_machine_total_marker(self, tmp_path):
        payload = _payload(cmd="sed -n '10,20p' path/to/file.py")

        # Baseline: the real guard fires without a marker.
        baseline = _run(payload)
        assert baseline is not None
        assert "sed" in baseline["hookSpecificOutput"]["additionalContext"]

        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {_iso(now)}\nBands: advisory-rewrite\nReason: x\n",
        )
        # `_blanket_disarm`'s own per-process cache is keyed by (session_id,
        # is_em) and is documented safe ONLY because a real hook process
        # never outlives one event -- this test calls the dispatcher twice
        # in the SAME process, so it must clear the cache itself to
        # simulate "a new event", exactly as this suite's own autouse
        # fixture does between tests.
        bd._cache.clear()
        # Marker only covers the EM (payload above carries no agent_id/
        # agent_type -- resolves as EM) and only suppresses advisory-rewrite,
        # which is exactly this guard's own band.
        assert _run(payload) is None

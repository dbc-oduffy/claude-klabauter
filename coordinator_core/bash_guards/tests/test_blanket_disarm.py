
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.bash_guards import _blanket_disarm as bd
from coordinator_core.bash_guards.dispatch import GuardBand


@pytest.fixture(autouse=True)
def _isolated_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(bd, "settings_home", lambda: tmp_path)
    bd._cache.clear()
    yield
    bd._cache.clear()


def _write_marker(tmp_path, text: str) -> None:
    (tmp_path / bd.MARKER_BASENAME).write_text(text, encoding="utf-8")


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


EM_PAYLOAD = {"session_id": "sess-em-1"}
SUBAGENT_PAYLOAD = {"session_id": "sess-em-1", "agent_id": "agent-123", "agent_type": "executor"}


class TestAbsentUnreadableEmptyMarker:
    def test_no_marker_file_stays_armed(self):
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_directory_in_place_of_marker_stays_armed(self, tmp_path):
        (tmp_path / bd.MARKER_BASENAME).mkdir()
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_empty_marker_stays_armed(self, tmp_path):
        _write_marker(tmp_path, "")
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_whitespace_only_marker_stays_armed(self, tmp_path):
        _write_marker(tmp_path, "   \n\n  ")
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False


class TestScopeFieldFailsClosed:
    def test_missing_scope_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Since: {_iso(now)}\nExpires: {_iso(now + timedelta(hours=1))}\nReason: x\n",
        )
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_unrecognised_scope_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: everything\nSince: {_iso(now)}\nExpires: {_iso(now + timedelta(hours=1))}\nReason: x\n",
        )
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_blank_scope_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: \nSince: {_iso(now)}\nExpires: {_iso(now + timedelta(hours=1))}\nReason: x\n",
        )
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False


class TestTimeScope:
    def test_positive_control_active_within_window(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: time\nSince: {_iso(now)}\nExpires: {_iso(now + timedelta(hours=1))}\n"
            "Bands: advisory-rewrite\nReason: testing\n",
        )
        assert bd.blanket_disarm_active(EM_PAYLOAD) is True
        assert bd.blanket_disarm_active(SUBAGENT_PAYLOAD) is True

    def test_missing_since_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(tmp_path, f"Scope: time\nExpires: {_iso(now + timedelta(hours=1))}\nReason: x\n")
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_missing_expires_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(tmp_path, f"Scope: time\nSince: {_iso(now)}\nReason: x\n")
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_unparseable_expires_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(tmp_path, f"Scope: time\nSince: {_iso(now)}\nExpires: not-a-date\nReason: x\n")
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_unparseable_since_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(tmp_path, f"Scope: time\nSince: not-a-date\nExpires: {_iso(now + timedelta(hours=1))}\nReason: x\n")
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_naive_timestamp_treated_as_unparseable(self, tmp_path):
        now = datetime.now(timezone.utc)
        naive_expires = (now + timedelta(hours=1)).replace(tzinfo=None).isoformat()
        _write_marker(tmp_path, f"Scope: time\nSince: {_iso(now)}\nExpires: {naive_expires}\nReason: x\n")
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_past_expiry_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: time\nSince: {_iso(now - timedelta(hours=2))}\nExpires: {_iso(now - timedelta(hours=1))}\nReason: x\n",
        )
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_expiry_equal_to_now_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: time\nSince: {_iso(now - timedelta(hours=1))}\nExpires: {_iso(now)}\nReason: x\n",
        )
        result = bd._evaluate(now, session_id="", is_em=True)
        assert result.active is False

    def test_beyond_cap_span_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: time\nSince: {_iso(now)}\nExpires: {_iso(now + bd._MAX_DISARM_DURATION + timedelta(hours=1))}\nReason: x\n",
        )
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_exactly_at_cap_span_is_active(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: time\nSince: {_iso(now)}\nExpires: {_iso(now + bd._MAX_DISARM_DURATION)}\n"
            "Bands: advisory-rewrite\nReason: x\n",
        )
        assert bd.blanket_disarm_active(EM_PAYLOAD) is True


class TestSessionScope:
    def test_positive_control_matching_session_em_active(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            "Scope: session\nSession: sess-em-1\n"
            f"Since: {_iso(now)}\nExpires: {_iso(now + timedelta(hours=1))}\n"
            "Bands: advisory-rewrite\nReason: x\n",
        )
        assert bd.blanket_disarm_active(EM_PAYLOAD) is True

    def test_no_inherit_to_dispatched_subagent_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            "Scope: session\nSession: sess-em-1\n"
            f"Since: {_iso(now)}\nExpires: {_iso(now + timedelta(hours=1))}\nReason: x\n",
        )
        assert bd.blanket_disarm_active(SUBAGENT_PAYLOAD) is False

    def test_missing_session_field_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: session\nSince: {_iso(now)}\nExpires: {_iso(now + timedelta(hours=1))}\nReason: x\n",
        )
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_mismatched_session_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            "Scope: session\nSession: some-other-session\n"
            f"Since: {_iso(now)}\nExpires: {_iso(now + timedelta(hours=1))}\nReason: x\n",
        )
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_missing_expires_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(tmp_path, f"Scope: session\nSession: sess-em-1\nSince: {_iso(now)}\nReason: x\n")
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_beyond_cap_span_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            "Scope: session\nSession: sess-em-1\n"
            f"Since: {_iso(now)}\nExpires: {_iso(now + bd._MAX_DISARM_DURATION + timedelta(hours=1))}\nReason: x\n",
        )
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_no_payload_at_all_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            "Scope: session\nSession: sess-em-1\n"
            f"Since: {_iso(now)}\nExpires: {_iso(now + timedelta(hours=1))}\nReason: x\n",
        )
        assert bd.blanket_disarm_active(None) is False


class TestMachineTotalScope:
    def test_positive_control_em_standing_no_expiry_active(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {_iso(now)}\nBands: advisory-rewrite\nReason: mac dev box\n",
        )
        assert bd.blanket_disarm_active(EM_PAYLOAD) is True

    def test_subagent_never_disarmed_even_when_marker_valid(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {_iso(now)}\nBands: advisory-rewrite\nReason: mac dev box\n",
        )
        assert bd.blanket_disarm_active(SUBAGENT_PAYLOAD) is False

    def test_no_payload_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(tmp_path, f"Scope: machine-total\nSince: {_iso(now)}\nReason: x\n")
        assert bd.blanket_disarm_active(None) is False

    def test_missing_since_stays_armed(self, tmp_path):
        _write_marker(tmp_path, "Scope: machine-total\nReason: x\n")
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_with_expiry_honoured_when_present_and_future(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {_iso(now)}\n"
            f"Expires: {_iso(now + bd._MAX_DISARM_DURATION * 3)}\n"
            "Bands: advisory-rewrite\nReason: x\n",
        )
        assert bd.blanket_disarm_active(EM_PAYLOAD) is True

    def test_with_past_expiry_stays_armed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {_iso(now - timedelta(hours=2))}\n"
            f"Expires: {_iso(now - timedelta(hours=1))}\nReason: x\n",
        )
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False


class TestBandsSuppression:
    def test_absent_bands_suppresses_nothing(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(tmp_path, f"Scope: machine-total\nSince: {_iso(now)}\nReason: x\n")
        status = bd.disarm_status(EM_PAYLOAD)
        assert status.active is False
        assert status.bands == frozenset()
        assert bd.disarm_covers_band(GuardBand.ADVISORY_REWRITE, EM_PAYLOAD) is False

    def test_positive_control_named_bands_suppressed(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {_iso(now)}\n"
            "Bands: advisory-rewrite,platform-conditioned-deny\nReason: x\n",
        )
        status = bd.disarm_status(EM_PAYLOAD)
        assert status.active is True
        assert status.bands == frozenset({"advisory-rewrite", "platform-conditioned-deny"})
        assert bd.disarm_covers_band(GuardBand.ADVISORY_REWRITE, EM_PAYLOAD) is True
        assert bd.disarm_covers_band(GuardBand.PLATFORM_CONDITIONED_DENY, EM_PAYLOAD) is True
        assert bd.disarm_covers_band(GuardBand.CONFINEMENT_DENY, EM_PAYLOAD) is False

    def test_confinement_deny_named_rejects_whole_marker(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {_iso(now)}\n"
            "Bands: advisory-rewrite,confinement-deny\nReason: x\n",
        )
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False
        status = bd.disarm_status(EM_PAYLOAD)
        assert status.bands is None

    def test_confinement_deny_alone_rejects_whole_marker(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {_iso(now)}\nBands: confinement-deny\nReason: x\n",
        )
        assert bd.blanket_disarm_active(EM_PAYLOAD) is False

    def test_unrecognised_band_token_suppresses_nothing(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {_iso(now)}\nBands: made-up-band\nReason: x\n",
        )
        status = bd.disarm_status(EM_PAYLOAD)
        assert status.active is False
        assert status.bands == frozenset()

    def test_partly_unrecognised_band_list_suppresses_nothing(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {_iso(now)}\n"
            "Bands: advisory-rewrite,made-up-band\nReason: x\n",
        )
        status = bd.disarm_status(EM_PAYLOAD)
        assert status.active is False
        assert status.bands == frozenset()

    def test_empty_bands_field_suppresses_nothing(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(tmp_path, f"Scope: machine-total\nSince: {_iso(now)}\nBands: \nReason: x\n")
        status = bd.disarm_status(EM_PAYLOAD)
        assert status.active is False
        assert status.bands == frozenset()

    def test_time_and_session_scope_also_support_bands(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: time\nSince: {_iso(now)}\nExpires: {_iso(now + timedelta(hours=1))}\n"
            "Bands: advisory-rewrite\nReason: x\n",
        )
        status = bd.disarm_status(EM_PAYLOAD)
        assert status.active is True
        assert status.bands == frozenset({"advisory-rewrite"})


class TestM18ActiveRequiresNamedBands:
    """M18 (2026-07-30, second dispatch): the SIMPLEST marker shape an
    operator would actually hand-write -- no `Bands:` line at all -- used
    to report `active=True` despite suppressing nothing. This class is the
    dedicated home for that "default-shaped marker" property across all
    three scopes, per the team-lead's own note that "all 11 of your wiring
    tests specify Bands:, which is exactly why this got through" -- every
    test in THIS class deliberately omits `Bands:` entirely, mirroring the
    exact marker shape that exposed the bug.
    """

    def test_default_shaped_time_marker_is_inert(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: time\nSince: {_iso(now)}\nExpires: {_iso(now + timedelta(hours=1))}\nReason: x\n",
        )
        status = bd.disarm_status(EM_PAYLOAD)
        assert status.active is False
        assert status.bands == frozenset()
        assert "Bands" in status.detail
        for band in GuardBand:
            assert bd.disarm_covers_band(band, EM_PAYLOAD) is False

    def test_default_shaped_session_marker_is_inert(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            "Scope: session\nSession: sess-em-1\n"
            f"Since: {_iso(now)}\nExpires: {_iso(now + timedelta(hours=1))}\nReason: x\n",
        )
        status = bd.disarm_status(EM_PAYLOAD)
        assert status.active is False
        assert status.bands == frozenset()
        assert "Bands" in status.detail

    def test_default_shaped_machine_total_marker_is_inert(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(tmp_path, f"Scope: machine-total\nSince: {_iso(now)}\nReason: mac dev box\n")
        status = bd.disarm_status(EM_PAYLOAD)
        assert status.active is False
        assert status.bands == frozenset()
        assert "Bands" in status.detail
        for band in GuardBand:
            assert bd.disarm_covers_band(band, EM_PAYLOAD) is False

    def test_default_shaped_machine_total_marker_with_expiry_is_still_inert(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {_iso(now)}\n"
            f"Expires: {_iso(now + bd._MAX_DISARM_DURATION * 3)}\nReason: x\n",
        )
        status = bd.disarm_status(EM_PAYLOAD)
        assert status.active is False
        assert status.bands == frozenset()

    def test_adding_bands_line_flips_the_identical_marker_active(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(tmp_path, f"Scope: machine-total\nSince: {_iso(now)}\nReason: mac dev box\n")
        assert bd.disarm_status(EM_PAYLOAD).active is False

        bd._cache.clear()
        _write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {_iso(now)}\nBands: advisory-rewrite\nReason: mac dev box\n",
        )
        assert bd.disarm_status(EM_PAYLOAD).active is True


class TestC4CacheCannotOutliveExpiryOrMarkerEdit:

    def test_cached_active_verdict_does_not_survive_its_own_expiry(self, tmp_path):
        now = datetime.now(timezone.utc)
        expires_in = timedelta(seconds=2)
        _write_marker(
            tmp_path,
            f"Scope: time\nSince: {_iso(now)}\nExpires: {_iso(now + expires_in)}\n"
            "Bands: advisory-rewrite\nReason: x\n",
        )
        assert bd.disarm_status(EM_PAYLOAD).active is True

        # Marker is UNCHANGED on disk (same stat key) -- only wall-clock
        later = bd._evaluate(
            now + expires_in + timedelta(seconds=1), session_id="", is_em=True, home=bd.settings_home()
        )
        assert later.active is False

        import time as _time

        _time.sleep(expires_in.total_seconds() + 0.5)
        assert bd.disarm_status(EM_PAYLOAD).active is False

    def test_cache_key_changes_when_marker_is_edited_without_explicit_clear(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {_iso(now)}\nBands: advisory-rewrite\nReason: x\n",
        )
        assert bd.disarm_status(EM_PAYLOAD).active is True

        import time as _time

        _time.sleep(0.01)
        _write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {_iso(now)}\nReason: x\n",
        )
        assert bd.disarm_status(EM_PAYLOAD).active is False

    def test_cache_key_changes_when_marker_is_deleted(self, tmp_path):
        now = datetime.now(timezone.utc)
        marker = tmp_path / bd.MARKER_BASENAME
        _write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {_iso(now)}\nBands: advisory-rewrite\nReason: x\n",
        )
        assert bd.disarm_status(EM_PAYLOAD).active is True

        marker.unlink()
        assert bd.disarm_status(EM_PAYLOAD).active is False


class TestAC11NamedTeammateSessionIdAbsentIsNotEM:

    NAMED_AGENT_ID = "aReviewBot-0123456789abcdef"

    @pytest.mark.parametrize("session_id", [None, "short7"])
    def test_named_agent_id_with_absent_or_short_session_id_is_not_em(self, session_id):
        payload = {"agent_id": self.NAMED_AGENT_ID, "session_id": session_id}
        assert bd._is_em_caller(payload, None) is False


class TestDisarmResultDetailIsInformative:
    def test_disarm_status_never_raises_on_garbage_marker(self, tmp_path):
        _write_marker(tmp_path, "this is not key: value shaped garbage \x00\x01")
        result = bd.disarm_status(EM_PAYLOAD)
        assert result.active is False
        assert isinstance(result.detail, str) and result.detail

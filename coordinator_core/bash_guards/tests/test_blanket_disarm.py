"""Tests for coordinator_core.bash_guards._blanket_disarm.

Covers every fail-closed leg documented in that module's own docstring, for
all three `Scope:` values (`time` / `session` / `machine-total`) and the
`Bands:` suppression axis layered on top of them -- plus, for each
scope/axis, a REAL positive control proving the corresponding valid marker
actually flips `blanket_disarm_active()`/`disarm_status()` to active. An
all-negative suite would pass against a function that always returns
`False`, which would be a tautological pin (this repo has been bitten by
exactly that shape before) -- the positive controls close that gap.

Pure Python -- no shell spawns, no real settings-home writes (the marker
path is redirected into `tmp_path` via monkeypatching `settings_home`).

Spec backlink: coordinator_core/bash_guards/_blanket_disarm.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.bash_guards import _blanket_disarm as bd
from coordinator_core.bash_guards.dispatch import GuardBand


@pytest.fixture(autouse=True)
def _isolated_marker(tmp_path, monkeypatch):
    """Redirect the marker's settings-home root into a per-test tmp dir, and
    reset the per-process cache -- the module-level cache is documented as
    safe ONLY because a real hook process never outlives one event; a test
    process calls this repeatedly, so the cache must be cleared between
    tests or a later test would see an earlier test's cached verdict."""
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
        # M18: `active` now requires a non-empty Bands: set (see
        # TestM18ActiveRequiresNamedBands below for the "no Bands: at all"
        # case this positive control does NOT exercise) -- this test's own
        # purpose is the Since/Expires/audience mechanics, so it carries a
        # Bands: line to stay a genuine positive control post-fix.
        _write_marker(
            tmp_path,
            f"Scope: time\nSince: {_iso(now)}\nExpires: {_iso(now + timedelta(hours=1))}\n"
            "Bands: advisory-rewrite\nReason: testing\n",
        )
        assert bd.blanket_disarm_active(EM_PAYLOAD) is True
        # unrestricted audience -- also active for a dispatched subagent
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
        # No trailing 'Z'/offset -- naive, must be rejected (module docstring).
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
        # Exercise the pure evaluator directly with `now` pinned to exactly
        # the marker's own Expires instant, rather than racing the wall
        # clock against a freshly-written marker.
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
        # M18: carries a Bands: line so this stays a genuine positive
        # control post-fix -- see TestM18ActiveRequiresNamedBands for the
        # default-shaped (no Bands: at all) case.
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
        # Same session_id, but the payload describes a dispatched subagent.
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
        # No payload -> _is_em_caller fails closed toward "not EM".
        assert bd.blanket_disarm_active(None) is False


class TestMachineTotalScope:
    def test_positive_control_em_standing_no_expiry_active(self, tmp_path):
        now = datetime.now(timezone.utc)
        # M18: carries a Bands: line so this stays a genuine positive
        # control post-fix -- see TestM18ActiveRequiresNamedBands for the
        # default-shaped (no Bands: at all) case, which is now INERT.
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
        # Deliberately beyond the time/session cap -- machine-total is
        # exempt from the cap rule when it does carry an Expires. Carries a
        # Bands: line (M18) to stay a genuine positive control.
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
        # M18 (2026-07-30): this exact shape -- validates, but names no
        # suppressible band -- USED to report active=True here, a false
        # capability claim (see _blanket_disarm's own "M18" module-
        # docstring section). It is now correctly reported as inert.
        # TestM18ActiveRequiresNamedBands below is the dedicated home for
        # this property; kept here too since this is the test that first
        # established "absent Bands: suppresses nothing" as a fact and
        # should keep asserting that fact's full consequence.
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
        # The WHOLE marker is rejected -- not just the illegal band token --
        # so the otherwise-legitimate advisory-rewrite suppression is lost too.
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
        # M18: a Bands: field naming only unrecognised tokens ends up with
        # the same empty suppressed-bands set as an absent field, so it is
        # inert (active=False) for the identical reason.
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
        # M18: partly-unrecognised still resolves to an EMPTY suppressed
        # set (see _parse_suppressed_bands -- a mix suppresses nothing),
        # so this is inert too, not merely "some bands missing."
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
        """Same property, but with an Expires field present -- the M18 fix
        must fire on the "carries Expires" branch of
        `_evaluate_machine_total_scope` too, not only the standing one."""
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
        """Positive control proving the ONLY difference between an inert
        and an active marker is the presence of a Bands: line -- not a
        second, hidden gate this fix might have introduced."""
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
    """C4 -- the cache used to be keyed ONLY on (session_id, is_em), so a
    long-lived (warm-engine) process could keep replaying a cached
    `active=True` verdict past the marker's own `Expires:` instant, or past
    an operator editing/deleting the marker entirely. Both are the ONE
    staleness case in this module that trades safety (fail-open), so both
    get a dedicated regression here rather than relying on the scope tests
    above (which all clear the cache explicitly and never call twice with
    the clock advanced)."""

    def test_cached_active_verdict_does_not_survive_its_own_expiry(self, tmp_path):
        # Review: coordinatorreview-integrator (failopen-caches P3) -- the
        # initial "still active" assertion below is widened from a 1ms
        # Expires window to several seconds. A 1ms window made THIS
        # assertion (not the later, deliberately-slept one) flaky on a
        # loaded/contended box (this repo's own CLAUDE.md notes 50-70
        # concurrent LLM sessions as the norm): more than 1ms can elapse
        # between the marker write and the very next line's disarm_status
        # call for reasons unrelated to the expiry logic under test. The
        # tight window is preserved only for the second assertion below,
        # which explicitly sleeps past it.
        now = datetime.now(timezone.utc)
        expires_in = timedelta(seconds=2)
        _write_marker(
            tmp_path,
            f"Scope: time\nSince: {_iso(now)}\nExpires: {_iso(now + expires_in)}\n"
            "Bands: advisory-rewrite\nReason: x\n",
        )
        # Widened window (was 1ms): a loaded/contended box (this repo's own
        # CLAUDE.md notes 50-70 concurrent LLM sessions as the norm) can let
        # more than 1ms elapse between the marker write above and this very
        # next line, making a 1ms-Expires "still active" assertion flaky for
        # reasons unrelated to the expiry logic under test. 2 seconds gives
        # this line comfortable headroom.
        assert bd.disarm_status(EM_PAYLOAD).active is True

        # Marker is UNCHANGED on disk (same stat key) -- only wall-clock
        # time has moved past Expires. The old (session_id, is_em)-only
        # cache key would replay the still-True verdict forever within this
        # process; the fix must re-check expires_at live on every call.
        later = bd._evaluate(
            now + expires_in + timedelta(seconds=1), session_id="", is_em=True, home=bd.settings_home()
        )
        assert later.active is False  # sanity: the pure evaluator agrees

        # Tight window preserved HERE, on the real expiry-crossing check --
        # sleep comfortably past the marker's own Expires, then confirm the
        # cached (still-True) verdict does not survive it.
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

        # Rewrite with a Bands field that names nothing suppressible --
        # deliberately WITHOUT calling bd._cache.clear() (unlike the M18
        # test with the same shape), to prove the stat-keyed cache detects
        # the edit on its own rather than requiring a test-only reset.
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
    """AC11 (docs/plans/2026-08-25-a-named-dispatch-keeps-its-report.md):
    the plan's own named verification vehicle for this AC is "a test driving
    `_blanket_disarm._is_em_caller` with a named, session_id-absent payload
    and confirming it is NOT classified as the EM" -- this class exists
    because that row was ticked in the plan with no such test on disk (the
    only prior coverage,
    `subagent_sandbox/tests/test_canonical_agent_id_adopts_named_form.py`,
    pins `_canonical_agent_id`'s return value only, never the downstream
    `_is_em_caller` classification this AC actually names as the guard).

    Mechanism this pins: `resolve_effective_types` -> `_canonical_agent_id`
    retains its F4 fallback (returns the raw named agent_id verbatim, never
    `""`) when `resolve_subagent_identity` fails closed on an absent or
    shorter-than-8-char `session_id`. `_is_em_caller` classifies EM iff
    `agent_id`/`agent_type`/`subagent_type` are ALL empty, so a non-empty
    F4-fallback `agent_id` must keep a named subagent OUT of EM-class. A
    regression that made `_canonical_agent_id` return `""` for this shape
    (or made `_is_em_caller` ignore `agent_id`) would silently let a named
    subagent through this module's `machine-total`/`session` EM-only
    audience narrowing -- this test is the guard against exactly that."""

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

"""
coordinator_core.hooks.tests.test_postuse_context_pressure — tests for the
sidecar-sourced context-pressure measurement path in
coordinator_core.hooks.postuse_advisory_dispatch._check_context_pressure_sync.

This is the sidecar-sourced measurement path's own test file, superseding the
transcript-scan coverage in coordinator_core/hooks/test_postuse_advisory_dispatch.py
rather than duplicating it. That sibling file still covers the parts of
postuse_advisory_dispatch.py this one does not touch: the compaction sentinel
bridge (Phase 1), the runtime tripwire, the first-Agent-dispatch sidecar
advisory, the unauthorized-handoff nudge, and _handler composition.

Spec backlink: docs/plans/2026-08-17-the-advisory-reads-the-harness.md, C4,
as amended by the 2026-08-18 PM ruling on bands and silence.

The model under test, in full (2026-09-19 "tell a cloud EM compaction is
inbound" C2 revision):

    < threshold - _ORANGE_RUNWAY_TOKENS   nothing
    >= threshold - _ORANGE_RUNWAY_TOKENS  INFORMATIONAL — checkpoint so the
                                           run is resumable; no handoff
                                           recommendation (PM ruling 2026-08-29)
    >= threshold - _RED_RUNWAY_TOKENS     HANDOFF NOW — ahead of the
                                           session's own resolved
                                           auto-compact cut
    no usable reading                     silence, on every fire, for the
                                           whole session

`threshold` is `_auto_compact_threshold_tokens` — the session's own resolved
`CLAUDE_CODE_AUTO_COMPACT_WINDOW` (or model window) minus the fixed
33,000-token reserve. This file's fixtures default to a 1,000,000-token
`context_window_size` with the env override cleared, so `_orange_bound_pct`
and `_red_bound_pct` (defined below) give the percentage each band fires at
for that shape; a test using a different `context_window_size` or env value
must compute its own bound rather than reuse these.

Negative-spec:
    - Do NOT reach for the transcript anywhere in this file's fixtures — every
      measurement scenario here is driven by writing (or omitting) a
      context-usage sidecar via `context_usage_sidecar.write_usage`. A test
      here that opens transcript_path for anything but the (unchanged)
      compaction-sentinel bridge would be testing a mechanism that no longer
      exists.
    - The no-reading case is asserted here as SILENCE and that is the point of
      several tests, not an oversight to "improve" into a heads-up later. It
      replaced a bounded UNKNOWN escalation ladder (1st/3rd/10th consecutive
      miss) that ran fleet-wide for a day because the reader resolved a path
      nothing wrote. Re-adding any sub-40% emission — including a one-time,
      politely-worded one — reverts a PM ruling; see
      test_no_emission_below_the_orange_band and
      test_unmeasured_never_escalates_however_many_fires.
    - The bands are token-runway distances back from the session's own
      resolved auto-compact threshold (`_auto_compact_threshold_tokens`),
      never a percentage of a reported window and never a fixed ceiling
      constant. A test that pins a literal percentage without also pinning
      the `context_window_size`/env it was computed against would drift
      silently the moment either changes.
"""

from __future__ import annotations

import tempfile
import time

import pytest

from coordinator_core.hooks import postuse_advisory_dispatch as pad
from coordinator_core.session import context_usage_sidecar as sidecar_module
from coordinator_core.session.context_usage_sidecar import write_usage


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Sandbox both durable surfaces this check touches.

    They no longer share a root: the advisory-state files still land under
    `pad._tempfile().gettempdir()`, while the context-usage sidecar resolves
    under `$COORDINATOR_SETTINGS_HOME` (the producer's settings home). Two
    patches, deliberately — a single tempdir patch would silently stop
    isolating the sidecar, and the tests would start reading whatever the real
    machine's live sessions had written.
    """
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    # This container runs with CLAUDE_CODE_AUTO_COMPACT_WINDOW=500000 live in
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
    sidecar_module._last_written.clear()
    yield


def _threshold_tokens(context_window_size: int = 1_000_000) -> int:
    return context_window_size - 33_000


def _orange_bound_pct(context_window_size: int = 1_000_000) -> float:
    bound = _threshold_tokens(context_window_size) - pad._ORANGE_RUNWAY_TOKENS
    return bound / context_window_size * 100


def _red_bound_pct(context_window_size: int = 1_000_000) -> float:
    bound = _threshold_tokens(context_window_size) - pad._RED_RUNWAY_TOKENS
    return bound / context_window_size * 100


def _bypass_throttle(session_id: str) -> None:
    tmpdir = tempfile.gettempdir()
    state = pad._load_advisory_state(tmpdir, session_id)
    state["throttle_last_check"] = 0.0
    pad._save_advisory_state(tmpdir, session_id, state)


def _write_sidecar(
    session_id: str,
    used_percentage,
    *,
    context_window_size=1_000_000,
    now: float | None = None,
) -> None:
    block: dict = {
        "used_percentage": used_percentage,
        "remaining_percentage": (
            100 - used_percentage if isinstance(used_percentage, int) else None
        ),
        "context_window_size": context_window_size,
        "current_usage": {
            "input_tokens": 2,
            "output_tokens": 400,
            "cache_creation_input_tokens": 500,
            "cache_read_input_tokens": 150_000,
        },
    }
    write_usage(session_id, block, now=now if now is not None else time.time())


def _check(session_id: str) -> str:
    return pad._check_context_pressure_sync(session_id, "")


def test_absent_sidecar_is_silent():
    assert _check("session-absent-sidecar") == ""


def test_headless_session_with_no_sidecar_is_silent():
    assert _check("session-headless") == ""


def test_sidecar_with_no_usable_percentage_is_silent():
    session_id = "session-unusable"
    write_usage(session_id, {"context_window_size": 1_000_000}, now=time.time())
    assert _check(session_id) == ""


@pytest.mark.parametrize("bad", ["87", None, True, float("nan")])
def test_sidecar_with_non_numeric_percentage_is_silent(bad):
    session_id = f"session-bad-{type(bad).__name__}-{bad}"
    _write_sidecar(session_id, bad)
    result = _check(session_id)
    assert result == "" or "nan" not in result.lower()
    assert result == ""


def test_unmeasured_never_escalates_however_many_fires():
    session_id = "session-never-escalates"
    for _ in range(20):
        _bypass_throttle(session_id)
        assert _check(session_id) == ""
    state = pad._load_advisory_state(tempfile.gettempdir(), session_id)
    assert "unmeasured_streak" not in state


@pytest.mark.parametrize("pct", [0, 1, 12, 15, 20, 25, 33, 39, 60, 80, 86])
def test_no_emission_below_the_orange_band(pct):
    session_id = f"session-quiet-{pct}"
    _write_sidecar(session_id, pct)
    assert _check(session_id) == ""


def test_just_under_the_orange_bound_is_silent_and_the_bound_itself_is_not():
    below = _orange_bound_pct() - 0.1
    at = _orange_bound_pct()
    _write_sidecar("session-below-orange", below)
    assert _check("session-below-orange") == ""
    _write_sidecar("session-at-orange", at)
    assert "INFORMATIONAL" in _check("session-at-orange")


def test_orange_band_fires_informational_and_never_recommends_handoff():
    """PM ruling 2026-08-29: the orange band is an orientation reading, not a
    call to stop.

    The `/handoff` assertion is the load-bearing one. The band previously read
    "start moving toward /handoff", and the whole point of the ruling is that
    there is no posture in which that is the right response in the orange
    band -- so a test that only checked for the new INFORMATIONAL header
    would pass against a composer that still appended the recommendation
    underneath it.
    """
    pct = _orange_bound_pct() + 1
    session_id = "session-orange"
    _write_sidecar(session_id, pct)
    text = _check(session_id)
    assert "CONTEXT PRESSURE — INFORMATIONAL" in text
    assert f"~{round(pct)}% of window used" in text
    threshold_pct = round(_threshold_tokens() / 1_000_000 * 100)
    assert f"{threshold_pct}%" in text
    assert "/handoff" not in text
    assert "ADVISORY" not in text


@pytest.mark.parametrize("offset", [0, 0.5, 1.5])
def test_orange_band_spans_up_to_the_red_bound(offset):
    pct = _orange_bound_pct() + offset
    assert pct < _red_bound_pct()
    session_id = f"session-orange-{offset}"
    _write_sidecar(session_id, pct)
    text = _check(session_id)
    assert "INFORMATIONAL" in text
    assert "HANDOFF NOW" not in text
    assert "/handoff" not in text


def test_advisory_barks_once():
    session_id = "session-orange-once"
    _write_sidecar(session_id, _orange_bound_pct() + 1)
    assert "INFORMATIONAL" in _check(session_id)
    _bypass_throttle(session_id)
    assert _check(session_id) == ""


def test_the_red_bound_fires_handoff_now():
    session_id = "session-red"
    pct = _red_bound_pct()
    _write_sidecar(session_id, pct)
    text = _check(session_id)
    assert "CONTEXT PRESSURE — HANDOFF NOW" in text
    assert f"~{round(pct)}% of window used" in text
    assert "/handoff" in text


def test_deep_into_the_red_band_still_fires_handoff_now():
    _write_sidecar("session-red-deep", _red_bound_pct() + 5)
    assert "HANDOFF NOW" in _check("session-red-deep")
    _write_sidecar("session-red-deeper", _red_bound_pct() + 10)
    assert "HANDOFF NOW" in _check("session-red-deeper")


def test_red_band_fires_ahead_of_the_resolved_threshold_with_positive_runway():
    threshold = _threshold_tokens()
    red_bound_tokens = threshold - pad._RED_RUNWAY_TOKENS
    assert red_bound_tokens < threshold
    assert pad._RED_RUNWAY_TOKENS > 0


def test_threshold_matches_the_established_cloud_cut_under_the_env_override(
    monkeypatch,
):
    """The formula this whole plan is anchored on: with
    CLAUDE_CODE_AUTO_COMPACT_WINDOW=500000 (this PM's fleet-wide setting),
    the resolved threshold is 467,000 tokens regardless of what
    `context_window_size` a given venue reports."""
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "500000")
    assert pad._auto_compact_threshold_tokens(1_000_000, None) == 467_000
    assert pad._auto_compact_threshold_tokens(500_000, None) == 467_000


def test_critical_suppresses_a_later_advisory_for_the_same_session():
    session_id = "session-jumped"
    _write_sidecar(session_id, _red_bound_pct() + 10)
    assert "HANDOFF NOW" in _check(session_id)
    _bypass_throttle(session_id)
    _write_sidecar(session_id, _orange_bound_pct() + 1, now=time.time())
    assert _check(session_id) == ""


def test_critical_barks_once():
    session_id = "session-red-once"
    _write_sidecar(session_id, _red_bound_pct() + 10)
    assert "HANDOFF NOW" in _check(session_id)
    _bypass_throttle(session_id)
    assert _check(session_id) == ""


def test_percentage_is_the_harness_figure_not_a_recomputation():
    session_id = "session-verbatim"
    pct = _red_bound_pct() + 10
    _write_sidecar(session_id, pct, context_window_size=1_000_000)
    assert f"~{round(pct)}% of window used" in _check(session_id)


def test_stale_reading_is_reported_with_its_age_not_discarded():
    session_id = "session-stale"
    _write_sidecar(session_id, _red_bound_pct() + 10, now=time.time() - 900)
    text = _check(session_id)
    assert "HANDOFF NOW" in text
    assert "measured 9" in text and "s ago" in text


def _under_sentinel(tmp_path, monkeypatch, session_id: str, mode: str = "autonomous") -> None:
    from coordinator_core.session import autonomous_sentinel

    sentinel = tmp_path / f"autonomous-{session_id}"
    sentinel.write_text(mode, encoding="utf-8")
    monkeypatch.setattr(autonomous_sentinel, "sentinel_path", lambda sid: sentinel)
    monkeypatch.setattr(pad, "sentinel_path", lambda sid: sentinel)


class TestMiseContinuanceRedBand:
    """cross-repo/archive/2026-08-03-...-mise-continuance-context-pressure-
    text.md: the sentinel's `mode` field (mise-en-place | autonomous) was
    written and never read -- every reader branched on mere presence, so a
    mise-en-place session got the same 'informational, keep going' text as an
    autonomous one, instead of a CONTINUANCE tail-then-handoff instruction.
    """

    def test_mise_en_place_red_band_names_the_tail_not_a_bare_handoff(
        self, tmp_path, monkeypatch
    ):
        session_id = "session-mise-continuance"
        _under_sentinel(tmp_path, monkeypatch, session_id, mode="mise-en-place")
        _write_sidecar(session_id, _red_bound_pct() + 10)
        text = _check(session_id)
        assert "Phase 6" in text
        assert "then" in text and "author the handoff" in text
        assert "This is the point to run /handoff" not in text
        assert "INFORMATIONAL" not in text

    def test_autonomous_mode_content_still_gets_the_informational_text(
        self, tmp_path, monkeypatch
    ):
        session_id = "session-mise-not-continuance"
        _under_sentinel(tmp_path, monkeypatch, session_id, mode="autonomous")
        _write_sidecar(session_id, _red_bound_pct() + 10)
        text = _check(session_id)
        assert "INFORMATIONAL" in text
        assert "Phase 6" not in text

    def test_absent_sentinel_gets_the_plain_handoff_text(self, tmp_path):
        session_id = "session-mise-no-sentinel"
        _write_sidecar(session_id, 60)
        text = _check(session_id)
        assert "HANDOFF NOW" in text
        assert "Phase 6" not in text

    def test_unrecognised_sentinel_content_degrades_to_current_behaviour(
        self, tmp_path, monkeypatch
    ):
        """A sentinel that exists but carries neither known mode string must
        not crash the hook and must not be treated as autonomous OR as a
        CONTINUANCE run -- C5: branch on sentinel CONTENT, not mere presence.
        Both `mise_continuance` and `autonomous_recognized` stay False, so the
        red band falls straight through to the bare non-autonomous HANDOFF NOW
        text, current (sentinel-absent) behaviour -- not the autonomous
        informational text a mere-presence check would have picked."""
        session_id = "session-mise-garbage-sentinel"
        _under_sentinel(tmp_path, monkeypatch, session_id, mode="not-a-real-mode")
        _write_sidecar(session_id, 60)
        text = _check(session_id)
        assert "HANDOFF NOW" in text
        assert "INFORMATIONAL" not in text
        assert "Phase 6" not in text


class TestAutonomousSentinelSuppressesTheRecommendation:

    def test_advisory_band_carries_no_handoff_recommendation(self, tmp_path, monkeypatch):
        session_id = "session-autonomous"
        _under_sentinel(tmp_path, monkeypatch, session_id)
        _write_sidecar(session_id, _orange_bound_pct() + 1)
        text = _check(session_id)
        assert "INFORMATIONAL" in text
        assert "heckpoint state to disk" in text
        assert "/handoff" not in text
        assert "ADVISORY" not in text

    def test_critical_band_carries_no_handoff_recommendation(self, tmp_path, monkeypatch):
        session_id = "session-autonomous-red"
        _under_sentinel(tmp_path, monkeypatch, session_id)
        _write_sidecar(session_id, _red_bound_pct() + 10)
        text = _check(session_id)
        assert "INFORMATIONAL" in text
        assert "/handoff" not in text
        assert "HANDOFF NOW" not in text

    def test_the_reading_itself_still_reaches_the_session(self, tmp_path, monkeypatch):
        session_id = "session-autonomous-pct"
        _under_sentinel(tmp_path, monkeypatch, session_id)
        _write_sidecar(session_id, _red_bound_pct() + 10)
        assert "~58% of window used" in _check(session_id)

    def test_without_the_sentinel_only_the_critical_band_recommends_handoff(self):
        _write_sidecar("session-no-sentinel-orange", _orange_bound_pct() + 1)
        assert "/handoff" not in _check("session-no-sentinel-orange")
        _write_sidecar("session-no-sentinel-red", _red_bound_pct() + 10)
        assert "HANDOFF NOW" in _check("session-no-sentinel-red")


def test_throttle_holds_between_checks():
    session_id = "session-throttled"
    _write_sidecar(session_id, _red_bound_pct() + 5)
    assert "HANDOFF NOW" in _check(session_id)
    _write_sidecar(session_id, _red_bound_pct() + 6, now=time.time())
    assert _check(session_id) == ""


def test_fractional_percentage_is_an_exact_token_compare_not_a_rounded_one():
    """The DECISION boundary is an exact float-token compare against
    `orange_bound_tokens`/`red_bound_tokens` -- `round()` governs only the
    DISPLAY text, matching the statusline's own rendering. A value a hair
    below the exact bound must stay silent; the bound itself, and anything
    past it, must fire.
    """
    below = _orange_bound_pct() - 0.05
    _write_sidecar("session-just-under", below)
    assert _check("session-just-under") == ""

    at = _orange_bound_pct()
    _write_sidecar("session-at-bound", at)
    assert "INFORMATIONAL" in _check("session-at-bound")

    red = _red_bound_pct() + 0.1
    _write_sidecar("session-past-red", red)
    assert "HANDOFF NOW" in _check("session-past-red")


def test_half_values_use_bankers_rounding_on_both_surfaces():
    pct = _orange_bound_pct() + 1.5
    half_display = round(pct)
    _write_sidecar("session-half-even", pct)
    text = _check("session-half-even")
    assert f"~{half_display}% of window used" in text
    assert "HANDOFF NOW" not in text


def test_unmeasured_path_writes_no_state_at_all(monkeypatch):
    saves = []
    real_save = pad._save_advisory_state
    monkeypatch.setattr(
        pad,
        "_save_advisory_state",
        lambda tmpdir, sid, state: (saves.append(sid), real_save(tmpdir, sid, state))[1],
    )
    assert _check("session-single-write") == ""
    assert saves.count("session-single-write") == 0
    assert "throttle_last_check" not in pad._load_advisory_state(
        tempfile.gettempdir(), "session-single-write"
    )


def _under_fleet_informational(monkeypatch) -> None:
    from coordinator_core.session import mode_resolution

    monkeypatch.setattr(
        mode_resolution, "read_fleet_mode", lambda: {"compaction_warnings": "informational"}
    )


class TestModeClauseNamesOnlyWhatIsTrue:
    """The red band's informational text opens with a mode clause, and which
    clause it opens with is decided by WHICH side selected the variant.

    The defect this pins: the text was written for the session-scoped sentinel
    and hardcoded "Autonomous run:". `compaction_warnings` is fleet-wins with
    `session_pair=None`, so it selects the same text for sessions that are not
    autonomous — every one of which would have been told it was an autonomous
    run. A message that asserts something untrue about its own reader is a
    register defect (docs/wiki/guard-messaging.md), and it is invisible to any
    test that only checks the INFORMATIONAL header is present.
    """

    def test_the_sentinel_path_still_names_the_autonomous_run(self, tmp_path, monkeypatch):
        session_id = "session-clause-sentinel"
        _under_sentinel(tmp_path, monkeypatch, session_id)
        _write_sidecar(session_id, _red_bound_pct() + 10)
        text = _check(session_id)
        assert "INFORMATIONAL" in text
        assert "Autonomous run:" in text

    def test_the_fleet_path_never_claims_the_session_is_autonomous(self, monkeypatch):
        session_id = "session-clause-fleet"
        _under_fleet_informational(monkeypatch)
        _write_sidecar(session_id, _red_bound_pct() + 10)
        text = _check(session_id)
        assert "INFORMATIONAL" in text
        assert "Autonomous run" not in text
        assert "Informational mode:" in text

    def test_the_fleet_path_still_suppresses_the_recommendation(self, monkeypatch):
        session_id = "session-clause-fleet-handoff"
        _under_fleet_informational(monkeypatch)
        _write_sidecar(session_id, _red_bound_pct() + 10)
        text = _check(session_id)
        assert "/handoff" not in text
        assert "HANDOFF NOW" not in text


def _in_a_cloud_reading(monkeypatch) -> None:
    """Select the informational variant the way a CLOUD CONTAINER does — via
    the environment rung of `compaction_warnings`, with no fleet value and no
    venue re-derived at the call site. Patched at the resolver's own seam
    rather than by setting `CLAUDE_CODE_REMOTE`, because `env_locality`'s
    machine rung would still answer for a box whose harness rung says
    nothing."""
    monkeypatch.setattr(
        "coordinator_core.session.mode_resolution."
        "_compaction_default_for_environment",
        lambda env=None: "informational",
    )


class TestMiseEnPlaceTerminalIsVenueConditional:

    def test_a_cloud_reading_never_recommends_authoring_a_handoff(
        self, tmp_path, monkeypatch
    ):
        session_id = "session-mise-cloud"
        _in_a_cloud_reading(monkeypatch)
        _under_sentinel(tmp_path, monkeypatch, session_id, mode="mise-en-place")
        _write_sidecar(session_id, _red_bound_pct() + 10)
        text = _check(session_id)
        assert "author the handoff" not in text
        assert "/handoff" not in text
        assert "HANDOFF NOW" not in text

    def test_a_cloud_reading_still_owes_the_full_phase_six_tail(
        self, tmp_path, monkeypatch
    ):
        session_id = "session-mise-cloud-tail"
        _in_a_cloud_reading(monkeypatch)
        _under_sentinel(tmp_path, monkeypatch, session_id, mode="mise-en-place")
        _write_sidecar(session_id, _red_bound_pct() + 10)
        text = _check(session_id)
        assert "Phase 6" in text
        for owed in (
            "review loop to zero findings",
            "end-of-run verification",
            "tracker sweep",
            "baton disposition",
        ):
            assert owed in text

    def test_a_cloud_reading_terminates_in_continuing_the_run(
        self, tmp_path, monkeypatch
    ):
        session_id = "session-mise-cloud-continue"
        _in_a_cloud_reading(monkeypatch)
        _under_sentinel(tmp_path, monkeypatch, session_id, mode="mise-en-place")
        _write_sidecar(session_id, _red_bound_pct() + 10)
        text = _check(session_id)
        assert "commit and checkpoint" in text
        assert "Continue the run." in text

    def test_the_fleet_key_selects_the_same_terminal_as_the_venue(
        self, tmp_path, monkeypatch
    ):
        """The predicate is the RESOLVED variant, never a venue re-derived
        here — so an operator who states `informational` fleet-wide gets the
        same mise terminal a cloud box gets, on a box of any kind."""
        session_id = "session-mise-fleet-informational"
        _under_fleet_informational(monkeypatch)
        _under_sentinel(tmp_path, monkeypatch, session_id, mode="mise-en-place")
        _write_sidecar(session_id, _red_bound_pct() + 10)
        text = _check(session_id)
        assert "Phase 6" in text
        assert "author the handoff" not in text

    def test_an_attended_box_keeps_the_tail_then_handoff_terminal(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "coordinator_core.session.mode_resolution."
            "_compaction_default_for_environment",
            lambda env=None: None,
        )
        session_id = "session-mise-attended"
        _under_sentinel(tmp_path, monkeypatch, session_id, mode="mise-en-place")
        _write_sidecar(session_id, _red_bound_pct() + 10)
        text = _check(session_id)
        assert "HANDOFF NOW" in text
        assert "Phase 6" in text
        assert "then author the handoff" in text

    def test_an_autonomous_sentinel_is_not_a_mise_sentinel_in_either_venue(
        self, tmp_path, monkeypatch
    ):
        session_id = "session-autonomous-cloud"
        _in_a_cloud_reading(monkeypatch)
        _under_sentinel(tmp_path, monkeypatch, session_id, mode="autonomous")
        _write_sidecar(session_id, _red_bound_pct() + 10)
        text = _check(session_id)
        assert "INFORMATIONAL" in text
        assert "Phase 6" not in text


class TestTheCallerEnvReachesTheModeSeam:
    """`resolve_mode`'s environment rung ends at `env_locality.locality(env)`,
    whose contract is "`env` IS A PARAMETER, NEVER AN AMBIENT READ". Reading it
    ambiently is correct on the cold rung and on the warm `isolated=True` leg,
    and wrong on the warm `isolated=False` leg, where `os.environ` belongs to
    the daemon rather than to the session that dispatched the hook.

    These pin the THREADING — that whatever env the caller carries is the env
    the rung resolves against — not a venue answer.
    """

    def test_an_explicit_caller_env_is_what_the_environment_rung_sees(
        self, monkeypatch
    ):
        seen = []

        def _record(env=None):
            seen.append(env)
            return "informational" if (env or {}).get("CLAUDE_CODE_REMOTE") == "true" else None

        monkeypatch.setattr(
            "coordinator_core.session.mode_resolution."
            "_compaction_default_for_environment",
            _record,
        )
        session_id = "session-env-threaded"
        _write_sidecar(session_id, _red_bound_pct() + 10)
        text = pad._check_context_pressure_sync(
            session_id, "", {"CLAUDE_CODE_REMOTE": "true"}
        )
        assert seen and seen[0] == {"CLAUDE_CODE_REMOTE": "true"}
        assert "INFORMATIONAL" in text
        assert "HANDOFF NOW" not in text

    def test_a_caller_carrying_no_env_resolves_ambiently_not_by_accident(
        self, monkeypatch
    ):
        seen = []

        def _record(env=None):
            seen.append(env)
            return None

        monkeypatch.setattr(
            "coordinator_core.session.mode_resolution."
            "_compaction_default_for_environment",
            _record,
        )
        session_id = "session-env-absent"
        _write_sidecar(session_id, _red_bound_pct() + 10)
        assert "HANDOFF NOW" in _check(session_id)
        assert seen == [None]

    def test_both_mode_reads_carry_the_same_caller_env(self, monkeypatch):
        calls = []
        real = pad.resolve_mode

        def _spy(key, session_id, env=None):
            calls.append((key, env))
            return real(key, session_id, env=env)

        monkeypatch.setattr(pad, "resolve_mode", _spy)
        session_id = "session-env-both-keys"
        _write_sidecar(session_id, _red_bound_pct() + 10)
        carried = {"CLAUDE_CODE_REMOTE": "true"}
        pad._check_context_pressure_sync(session_id, "", carried)
        assert [key for key, _ in calls] == ["autonomous", "compaction_warnings"]
        assert all(env is carried for _, env in calls)


def test_windowless_reading_is_silent_when_nothing_names_a_window(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
    block = {"total_input_tokens": 900_000}
    assert pad._model_window_tokens(block) is None
    assert pad._auto_compact_threshold_tokens(None, None) is None
    assert pad._used_tokens_and_display_pct(block, None) == (None, None)


def test_windowless_reading_resolves_from_the_env_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "500000")
    assert pad._effective_auto_compact_window(None, None) == 500_000
    assert pad._auto_compact_threshold_tokens(None, None) == 467_000


def test_override_is_not_clamped_away_when_no_model_window_is_known(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "200000")
    assert pad._effective_auto_compact_window(None, None) == 200_000


def test_a_smaller_tier_gets_its_own_bands(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
    assert pad._auto_compact_threshold_tokens(200_000, None) == 167_000

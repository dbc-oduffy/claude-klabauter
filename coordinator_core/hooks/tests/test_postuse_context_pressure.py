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

The model under test, in full:

    < 40%   nothing
    >= 40%  INFORMATIONAL — checkpoint so the run is resumable; no handoff
                            recommendation (PM ruling 2026-08-29)
    >= 43%  HANDOFF NOW — ahead of the fixed ~500K auto-compaction ceiling
    no usable reading  — silence, on every fire, for the whole session

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
    - The bands are 40 and 43 as literals. They are not derived from
      _AUTO_COMPACT_CEILING_TOKENS_1M, and a test that recomputes them from it
      would pass while the shipped numbers drifted.
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
    sidecar_module._last_written.clear()
    yield


def _bypass_throttle(session_id: str) -> None:
    """Force the 5-min throttle to be considered expired for `session_id`,
    preserving any other state (bark-once dedup) already on disk — a
    full-state overwrite here would silently reset the very counters several
    tests below are asserting on."""
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
    """Write a context-usage sidecar block shaped like the real harness
    payload. `used_percentage` is the only figure the check consults; the rest
    is present so the fixture stays recognisable against a real record."""
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


# ---------------------------------------------------------------------------
# Silence — no reading at all.
# ---------------------------------------------------------------------------


def test_absent_sidecar_is_silent():
    assert _check("session-absent-sidecar") == ""


def test_headless_session_with_no_sidecar_is_silent():
    """No statusline renders in a headless run, so no sidecar is ever written.
    That population gets silence — it is not told its context is unmeasurable,
    because there is nothing it could do about it and the notice would fire on
    every tool call of every headless session on the machine."""
    assert _check("session-headless") == ""


def test_sidecar_with_no_usable_percentage_is_silent():
    session_id = "session-unusable"
    write_usage(session_id, {"context_window_size": 1_000_000}, now=time.time())
    assert _check(session_id) == ""


@pytest.mark.parametrize("bad", ["87", None, True, float("nan")])
def test_sidecar_with_non_numeric_percentage_is_silent(bad):
    """A string, a null, a bool, or a NaN is not a percentage. Each yields
    silence rather than a coerced number — `True` in particular would evaluate
    as 1 under a bare isinstance(x, int) check."""
    session_id = f"session-bad-{type(bad).__name__}-{bad}"
    _write_sidecar(session_id, bad)
    result = _check(session_id)
    assert result == "" or "nan" not in result.lower()
    assert result == ""


def test_unmeasured_never_escalates_however_many_fires():
    """The regression this file exists to prevent.

    A previous model escalated through UNKNOWN notices at the 1st, 3rd and
    10th consecutive unmeasured check. With the reader pointed at a path
    nothing wrote, every session on the machine climbed that ladder all day.
    Twenty fires, no text, no state key counting toward one."""
    session_id = "session-never-escalates"
    for _ in range(20):
        _bypass_throttle(session_id)
        assert _check(session_id) == ""
    state = pad._load_advisory_state(tempfile.gettempdir(), session_id)
    assert "unmeasured_streak" not in state


# ---------------------------------------------------------------------------
# Silence — measured, but below the orange band.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pct", [0, 1, 12, 15, 20, 25, 33, 39])
def test_no_emission_below_the_orange_band(pct):
    """PM floor: nothing prescribes a checkpoint below 40% of window. The
    reported symptom that produced this ruling was agents wrapping up at
    15-20%, so those values are pinned explicitly rather than left to a
    boundary test alone."""
    session_id = f"session-quiet-{pct}"
    _write_sidecar(session_id, pct)
    assert _check(session_id) == ""


def test_thirty_nine_is_silent_and_forty_is_not():
    """The boundary, both sides, same session shape."""
    _write_sidecar("session-39", 39)
    assert _check("session-39") == ""
    _write_sidecar("session-40", 40)
    assert "INFORMATIONAL" in _check("session-40")


# ---------------------------------------------------------------------------
# The orange band — 40%.
# ---------------------------------------------------------------------------


def test_forty_percent_fires_informational_and_never_recommends_handoff():
    """PM ruling 2026-08-29: 40 is an orientation reading, not a call to stop.

    The `/handoff` assertion is the load-bearing one. The band previously read
    "start moving toward /handoff", and the whole point of the ruling is that
    there is no posture in which that is the right response at 40 -- so a test
    that only checked for the new INFORMATIONAL header would pass against a
    composer that still appended the recommendation underneath it.
    """
    session_id = "session-orange"
    _write_sidecar(session_id, 42)
    text = _check(session_id)
    assert "CONTEXT PRESSURE — INFORMATIONAL" in text
    assert "~42% of window used" in text
    assert "43%" in text
    assert "/handoff" not in text
    assert "ADVISORY" not in text


@pytest.mark.parametrize("pct", [40, 41, 42])
def test_orange_band_spans_forty_to_fortytwo(pct):
    session_id = f"session-orange-{pct}"
    _write_sidecar(session_id, pct)
    text = _check(session_id)
    assert "INFORMATIONAL" in text
    assert "HANDOFF NOW" not in text
    assert "/handoff" not in text


def test_advisory_barks_once():
    session_id = "session-orange-once"
    _write_sidecar(session_id, 41)
    assert "INFORMATIONAL" in _check(session_id)
    _bypass_throttle(session_id)
    assert _check(session_id) == ""


# ---------------------------------------------------------------------------
# The red band — 43%.
# ---------------------------------------------------------------------------


def test_fortythree_percent_fires_handoff_now():
    session_id = "session-red"
    _write_sidecar(session_id, 43)
    text = _check(session_id)
    assert "CONTEXT PRESSURE — HANDOFF NOW" in text
    assert "~43% of window used" in text
    assert "/handoff" in text


def test_fortyseven_is_inside_the_red_band_not_its_edge():
    """The band moved off 47, then off 45, on 2026-08-30 — auto-compaction was
    observed firing at 47 both times. 47 must read as already-past the call,
    and so must 45."""
    _write_sidecar("session-red-47", 47)
    assert "HANDOFF NOW" in _check("session-red-47")
    _write_sidecar("session-red-45", 45)
    assert "HANDOFF NOW" in _check("session-red-45")


def test_red_band_fires_below_the_auto_compaction_ceiling():
    """43% of a 1M window is ~430K, ahead of the fixed ~500K ceiling — the
    whole reason the red band is not at 50."""
    ceiling = pad._AUTO_COMPACT_CEILING_TOKENS_1M
    assert 43 * 1_000_000 // 100 < ceiling


def test_critical_suppresses_a_later_advisory_for_the_same_session():
    """A session that jumps straight past 40 into the red band must not then
    emit the orange text on a later fire."""
    session_id = "session-jumped"
    _write_sidecar(session_id, 55)
    assert "HANDOFF NOW" in _check(session_id)
    _bypass_throttle(session_id)
    _write_sidecar(session_id, 42, now=time.time())
    assert _check(session_id) == ""


def test_critical_barks_once():
    session_id = "session-red-once"
    _write_sidecar(session_id, 60)
    assert "HANDOFF NOW" in _check(session_id)
    _bypass_throttle(session_id)
    assert _check(session_id) == ""


# ---------------------------------------------------------------------------
# Reporting details.
# ---------------------------------------------------------------------------


def test_percentage_is_the_harness_figure_not_a_recomputation():
    """The check reports what the harness reported. The occupancy breakdown in
    the same block would compute a different number; it is not consulted."""
    session_id = "session-verbatim"
    _write_sidecar(session_id, 52, context_window_size=1_000_000)
    assert "~52% of window used" in _check(session_id)


def test_stale_reading_is_reported_with_its_age_not_discarded():
    session_id = "session-stale"
    _write_sidecar(session_id, 48, now=time.time() - 900)
    text = _check(session_id)
    assert "HANDOFF NOW" in text
    assert "measured 9" in text and "s ago" in text


def _under_sentinel(tmp_path, monkeypatch, session_id: str) -> None:
    from coordinator_core.session import autonomous_sentinel

    sentinel = tmp_path / f"autonomous-{session_id}"
    sentinel.write_text("1", encoding="utf-8")
    monkeypatch.setattr(autonomous_sentinel, "sentinel_path", lambda sid: sentinel)
    monkeypatch.setattr(pad, "sentinel_path", lambda sid: sentinel)


class TestAutonomousSentinelSuppressesTheRecommendation:
    """The sentinel's contract (`coordinator/commands/autonomous.md`): context
    pressure messages become informational-only, with NO `/handoff`
    recommendation.

    The defect these pin: the composer read the sentinel to APPEND a checkpoint
    clause while leaving the handoff recommendation in the text it appended to.
    One condition, two consumers, only one wired -- so the mode delivered the
    exact nudge it exists to remove, at the point in a long run where a session
    is most likely to take it. Asserting the absence of the recommendation is
    the whole point; a test that only checks the appended clause is present
    passes against the defect.
    """

    def test_advisory_band_carries_no_handoff_recommendation(self, tmp_path, monkeypatch):
        session_id = "session-autonomous"
        _under_sentinel(tmp_path, monkeypatch, session_id)
        _write_sidecar(session_id, 41)
        text = _check(session_id)
        assert "INFORMATIONAL" in text
        assert "heckpoint state to disk" in text
        assert "/handoff" not in text
        assert "ADVISORY" not in text
        # No "Autonomous run" assertion here any more: since the 2026-08-29
        # ruling this band is informational for EVERY session, so its text is
        # mode-neutral by design and naming the sentinel would be a claim the
        # band does not make. The mode clause is asserted in the red band,
        # where it is
        # actually selected -- see TestModeClauseNamesOnlyWhatIsTrue.

    def test_critical_band_carries_no_handoff_recommendation(self, tmp_path, monkeypatch):
        session_id = "session-autonomous-red"
        _under_sentinel(tmp_path, monkeypatch, session_id)
        _write_sidecar(session_id, 60)
        text = _check(session_id)
        assert "INFORMATIONAL" in text
        assert "/handoff" not in text
        assert "HANDOFF NOW" not in text

    def test_the_reading_itself_still_reaches_the_session(self, tmp_path, monkeypatch):
        """Informational-only is not silent -- suppressing the reading would
        strip the one fact the session needs to decide when to checkpoint."""
        session_id = "session-autonomous-pct"
        _under_sentinel(tmp_path, monkeypatch, session_id)
        _write_sidecar(session_id, 58)
        assert "~58% of window used" in _check(session_id)

    def test_without_the_sentinel_only_the_critical_band_recommends_handoff(self):
        """The other half of the branch, as the 2026-08-29 ruling leaves it.

        43 without a sentinel still says HANDOFF NOW -- that is the band the
        mode key governs and the suppression this class is about. 40 no longer
        recommends anything to anyone, sentinel or not, so asserting a
        recommendation there would re-pin the behaviour the ruling removed.
        """
        _write_sidecar("session-no-sentinel-orange", 41)
        assert "/handoff" not in _check("session-no-sentinel-orange")
        _write_sidecar("session-no-sentinel-red", 60)
        assert "HANDOFF NOW" in _check("session-no-sentinel-red")


def test_throttle_holds_between_checks():
    """Two fires inside the 5-minute window: the second is throttled even
    though the reading would otherwise fire."""
    session_id = "session-throttled"
    _write_sidecar(session_id, 48)
    assert "HANDOFF NOW" in _check(session_id)
    _write_sidecar(session_id, 49, now=time.time())
    assert _check(session_id) == ""


def test_fractional_percentage_rounds_to_match_the_status_line():
    """The producer renders with round(); this check must band with round().

    A raw 39.6 shows the operator an orange "40%" in the terminal. Truncating
    here would leave that colour change unexplained, with no advisory behind
    it. Review: code-reviewer (P2).
    """
    _write_sidecar("session-round-up", 39.6)
    assert "INFORMATIONAL" in _check("session-round-up")

    _write_sidecar("session-round-down", 39.4)
    assert _check("session-round-down") == ""

    _write_sidecar("session-round-red", 42.6)
    assert "HANDOFF NOW" in _check("session-round-red")


def test_half_values_use_bankers_rounding_on_both_surfaces():
    """`round()` is half-to-even in Python, so 42.5 renders 42 and stays in the
    orange band. Pinned rather than corrected: the statusline uses the same
    `round()`, so the terminal and the advisory agree on the odd case too, and
    agreement is what the boundary needs. Changing one surface to half-up
    without the other reintroduces exactly the mismatch this pair fixes."""
    _write_sidecar("session-half-even", 42.5)
    text = _check("session-half-even")
    assert "~42% of window used" in text
    assert "HANDOFF NOW" not in text


def test_unmeasured_path_writes_state_once_not_twice(monkeypatch):
    """The silent path is the common case for headless sessions and runs on
    every tool call; it persists the throttle stamp once and does no second
    write. Review: code-reviewer (nit)."""
    saves = []
    real_save = pad._save_advisory_state
    monkeypatch.setattr(
        pad,
        "_save_advisory_state",
        lambda tmpdir, sid, state: (saves.append(sid), real_save(tmpdir, sid, state))[1],
    )
    assert _check("session-single-write") == ""
    assert saves.count("session-single-write") == 1


def _under_fleet_informational(monkeypatch) -> None:
    """Select the informational variant the way the FLEET key does — with no
    session sentinel anywhere. Patches the record read rather than writing a
    real settings-home file so the test never touches machine-wide state that
    ~50 concurrent peers resolve against."""
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
        _write_sidecar(session_id, 60)
        text = _check(session_id)
        assert "INFORMATIONAL" in text
        assert "Autonomous run:" in text

    def test_the_fleet_path_never_claims_the_session_is_autonomous(self, monkeypatch):
        session_id = "session-clause-fleet"
        _under_fleet_informational(monkeypatch)
        _write_sidecar(session_id, 60)
        text = _check(session_id)
        assert "INFORMATIONAL" in text
        assert "Autonomous run" not in text
        assert "Informational mode:" in text

    def test_the_fleet_path_still_suppresses_the_recommendation(self, monkeypatch):
        """The clause fix must not cost the key its actual job."""
        session_id = "session-clause-fleet-handoff"
        _under_fleet_informational(monkeypatch)
        _write_sidecar(session_id, 60)
        text = _check(session_id)
        assert "/handoff" not in text
        assert "HANDOFF NOW" not in text

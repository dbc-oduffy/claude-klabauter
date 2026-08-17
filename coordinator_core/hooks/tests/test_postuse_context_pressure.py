"""
coordinator_core.hooks.tests.test_postuse_context_pressure — tests for the
sidecar-sourced context-pressure measurement path in
coordinator_core.hooks.postuse_advisory_dispatch._check_context_pressure_sync.

This is the sidecar-sourced measurement path's own test file (AC10 of the
2026-08-17 "the advisory reads the harness" plan), superseding the
transcript-scan coverage in coordinator_core/hooks/test_postuse_advisory_dispatch.py
rather than duplicating it. That sibling file still covers the parts of
postuse_advisory_dispatch.py this plan does not touch: the compaction
sentinel bridge (Phase 1), the runtime tripwire, the first-Agent-dispatch
sidecar advisory, the unauthorized-handoff nudge, and _handler composition.

Migrated here from that file, re-pointed at the sidecar-sourced path rather
than the deleted transcript extractor: the 1M-tier
_AUTO_COMPACT_CEILING_TOKENS_1M headroom behaviour and the sub-1M
percentage-of-window model.

Spec backlink: docs/plans/2026-08-17-the-advisory-reads-the-harness.md, C4.

Negative-spec:
    - Do NOT reach for the transcript anywhere in this file's fixtures --
      every measurement scenario here is driven by writing (or omitting) a
      context-usage sidecar via `context_usage_sidecar.write_usage`. A test
      here that opens transcript_path for anything but the (unchanged)
      compaction-sentinel bridge would be testing the mechanism this plan
      deleted.
    - The headless/no-sidecar UNKNOWN case (AC15) is asserted here as
      INTENDED behaviour, not a gap to "fix" later by reaching for the
      transcript -- see test_headless_session_with_no_sidecar_yields_unknown.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from coordinator_core.hooks import postuse_advisory_dispatch as pad
from coordinator_core.session import context_usage_sidecar as sidecar_module
from coordinator_core.session.context_usage_sidecar import write_usage


@pytest.fixture(autouse=True)
def _isolated_tempdir(tmp_path, monkeypatch):
    """Sandbox every test into its own tempdir.

    Both the advisory-state files (pad._tempfile().gettempdir()) and the
    context-usage sidecar (context_usage_sidecar.sidecar_path(), built on
    tempfile.gettempdir()) resolve through the SAME real `tempfile` module
    object -- `pad._tempfile()` does a plain `import tempfile; return
    tempfile`, which Python's module cache hands back as the identical
    object this fixture patches. One monkeypatch isolates both.
    """
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    sidecar_module._last_written.clear()
    yield


def _bypass_throttle(session_id: str) -> None:
    """Force the 5-min throttle to be considered expired for `session_id`,
    preserving any other state (bark-once dedup, unmeasured_streak) already
    on disk -- a full-state overwrite here would silently reset the very
    counters several tests below are asserting on."""
    tmpdir = tempfile.gettempdir()
    state = pad._load_advisory_state(tmpdir, session_id)
    state["throttle_last_check"] = 0.0
    pad._save_advisory_state(tmpdir, session_id, state)


def _write_sidecar(
    session_id: str,
    *,
    input_tokens: int = 0,
    cache_creation: int = 0,
    cache_read: int = 0,
    output_tokens: int = 500,
    context_window_size=200_000,
    used_percentage=None,
    now: float | None = None,
) -> None:
    """Write a context-usage sidecar block shaped like the real harness
    payload. `used_percentage` defaults to the value the harness would
    compute from the occupancy sum and window size, but callers may
    override it independently (AC4's "display only" tests rely on this)."""
    occupancy = input_tokens + cache_creation + cache_read
    if used_percentage is None:
        used_percentage = (
            occupancy * 100 // context_window_size if context_window_size else 0
        )
    block: dict = {
        "used_percentage": used_percentage,
        "remaining_percentage": 100 - used_percentage if isinstance(used_percentage, int) else None,
        "current_usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
        },
    }
    if context_window_size is not None:
        block["context_window_size"] = context_window_size
    write_usage(session_id, block, now=now if now is not None else time.time())


# ---------------------------------------------------------------------------
# AC5 / AC15 -- absent sidecar (including the headless population).
# ---------------------------------------------------------------------------


def test_absent_sidecar_yields_unknown_with_no_percentage(tmp_path):
    session_id = "test-session-absent-sidecar"
    _bypass_throttle(session_id)

    result = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))

    assert "CONTEXT PRESSURE — UNKNOWN" in result
    assert "%" not in result


def test_headless_session_with_no_sidecar_yields_unknown(tmp_path):
    """AC15: a non-interactive session renders no statusline, so no sidecar
    is ever written. UNKNOWN here is the intended, covered outcome for that
    population -- not a defect to be worked around by reaching for the
    transcript. transcript_path is deliberately a real, readable file here
    (unlike test_absent_sidecar_yields_unknown_with_no_percentage) so this
    test cannot be satisfied by a transcript-missing early exit; only
    sidecar-absence drives the UNKNOWN."""
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")

    session_id = "test-session-headless-no-statusline"
    _bypass_throttle(session_id)

    result = pad._check_context_pressure_sync(session_id, str(transcript))

    assert "CONTEXT PRESSURE — UNKNOWN" in result
    assert "%" not in result


# ---------------------------------------------------------------------------
# AC4 -- fresh sidecar, full measurement path.
# ---------------------------------------------------------------------------


def test_fresh_sidecar_yields_measured_percentage_and_fires_advisory(tmp_path):
    session_id = "test-session-fresh-sidecar-advisory"
    _bypass_throttle(session_id)
    # 95_000 / 200_000 = 47.5% -- above the 40% advisory, below 50% critical.
    _write_sidecar(session_id, input_tokens=2, cache_creation=1103, cache_read=93_895)

    result = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))

    assert "CONTEXT PRESSURE — ADVISORY" in result
    assert "CONTEXT PRESSURE — HIGH" not in result
    assert "~47%" in result
    assert "measured via the harness's own context_window usage block" in result


def test_fresh_sidecar_below_threshold_yields_no_advisory(tmp_path):
    session_id = "test-session-fresh-sidecar-quiet"
    _bypass_throttle(session_id)
    _write_sidecar(session_id, input_tokens=2, cache_creation=100, cache_read=900)

    result = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))

    assert result == ""


def test_occupancy_sum_excludes_output_tokens(tmp_path):
    """AC4: the threshold comparison is input + cache_creation + cache_read
    ONLY -- a huge output_tokens figure must never push the sum over a
    threshold it wouldn't otherwise cross."""
    session_id = "test-session-output-tokens-excluded"
    _bypass_throttle(session_id)
    # Occupancy-relevant sum is a mere 1_000 tokens (well under 40% of 200K);
    # output_tokens alone is 300_000 -- if it were wrongly included, this
    # would fire critical.
    _write_sidecar(
        session_id,
        input_tokens=500,
        cache_creation=300,
        cache_read=200,
        output_tokens=300_000,
    )

    result = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))

    assert result == ""


def test_used_percentage_never_drives_the_threshold_comparison(tmp_path):
    """AC4: used_percentage is DISPLAY ONLY. A sidecar reporting a low
    occupancy sum but a (deliberately inconsistent) high used_percentage
    must not fire -- the occupancy sum is what's compared, never the
    percentage field."""
    session_id = "test-session-pct-display-only"
    _bypass_throttle(session_id)
    _write_sidecar(
        session_id,
        input_tokens=100,
        cache_creation=100,
        cache_read=100,  # 300 tokens -- far under any threshold
        used_percentage=99,  # inconsistent, deliberately -- must be ignored for gating
    )

    result = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))

    assert result == ""


# ---------------------------------------------------------------------------
# Migrated: 1M-tier absolute-token anchoring (unchanged threshold values,
# now sourced from the sidecar instead of the transcript).
# ---------------------------------------------------------------------------


def test_1m_tier_critical_fires_below_auto_compact_ceiling(tmp_path):
    """PM ruling 2026-07-27 (unchanged by this rewire): on the 1M tier,
    CRITICAL must fire strictly BELOW Anthropic's fixed ~500K auto-compact
    ceiling, with real headroom -- not coincident with it."""
    session_id = "test-session-1m-critical-headroom"
    _bypass_throttle(session_id)
    usage_tokens_total = 460_000  # above the anchored 450K critical, below the 500K ceiling
    _write_sidecar(
        session_id,
        input_tokens=2,
        cache_creation=1103,
        cache_read=usage_tokens_total - 2 - 1103,
        context_window_size=1_000_000,
    )

    result = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))

    assert result != ""
    assert "CONTEXT PRESSURE — HIGH" in result

    critical_tokens = 450_000
    ceiling = pad._AUTO_COMPACT_CEILING_TOKENS_1M
    assert ceiling == 500_000
    headroom = ceiling - critical_tokens
    assert critical_tokens < ceiling
    assert headroom >= 50_000
    assert usage_tokens_total < ceiling


def test_1m_tier_advisory_fires_before_critical(tmp_path):
    session_id = "test-session-1m-advisory-before-critical"
    _bypass_throttle(session_id)
    usage_tokens_total = 410_000  # above 400K advisory, below 450K critical
    _write_sidecar(
        session_id,
        input_tokens=2,
        cache_creation=1103,
        cache_read=usage_tokens_total - 2 - 1103,
        context_window_size=1_000_000,
    )

    result = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))

    assert result != ""
    assert "CONTEXT PRESSURE — ADVISORY" in result
    assert "CONTEXT PRESSURE — HIGH" not in result


def test_200k_tier_unchanged_percentage_model(tmp_path):
    """Sub-1M tiers keep the 40%/50%-of-window behaviour unchanged -- no
    fixed-ceiling collision to guard against at this window size. This test
    would fail if the 1M-tier absolute anchoring were wrongly reused here."""
    session_id = "test-session-200k-unchanged"
    _bypass_throttle(session_id)
    context_window = 200_000
    usage_tokens_total = 95_000  # 47.5% -- above 40% advisory, below 50% critical
    _write_sidecar(
        session_id,
        input_tokens=2,
        cache_creation=1103,
        cache_read=usage_tokens_total - 2 - 1103,
        context_window_size=context_window,
    )

    result = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))

    assert result != ""
    assert "CONTEXT PRESSURE — ADVISORY" in result
    assert "CONTEXT PRESSURE — HIGH" not in result
    expected_pct = usage_tokens_total * 100 // context_window
    assert f"~{expected_pct}%" in result


# ---------------------------------------------------------------------------
# AC13a -- usable percentage, no usable window size.
# ---------------------------------------------------------------------------


def test_percentage_only_path_fires_advisory_without_window(tmp_path):
    session_id = "test-session-pct-only-advisory"
    _bypass_throttle(session_id)
    _write_sidecar(
        session_id,
        input_tokens=2,
        cache_creation=1103,
        cache_read=100,
        context_window_size=None,
        used_percentage=45,
    )

    result = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))

    assert "CONTEXT PRESSURE — ADVISORY" in result
    assert "~45%" in result
    # No tier-specific compact_desc and no absolute-token parenthetical --
    # neither can be stated honestly without a known window size.
    assert "auto-compaction" not in result
    assert "-token window)" not in result


def test_percentage_only_path_fires_critical_without_window(tmp_path):
    session_id = "test-session-pct-only-critical"
    _bypass_throttle(session_id)
    _write_sidecar(
        session_id,
        input_tokens=2,
        cache_creation=1103,
        cache_read=100,
        context_window_size=None,
        used_percentage=62,
    )

    result = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))

    assert "CONTEXT PRESSURE — HIGH" in result
    assert "~62%" in result
    assert "auto-compaction" not in result


def test_percentage_only_path_below_threshold_yields_no_advisory(tmp_path):
    session_id = "test-session-pct-only-quiet"
    _bypass_throttle(session_id)
    _write_sidecar(
        session_id,
        input_tokens=2,
        cache_creation=100,
        cache_read=100,
        context_window_size=None,
        used_percentage=10,
    )

    result = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))

    assert result == ""


# ---------------------------------------------------------------------------
# AC13b -- neither figure usable.
# ---------------------------------------------------------------------------


def test_sidecar_present_but_unusable_yields_unknown(tmp_path):
    session_id = "test-session-sidecar-unusable"
    _bypass_throttle(session_id)
    # A block missing current_usage entirely and carrying no used_percentage
    # -- present, but neither figure this function needs is usable.
    write_usage(session_id, {"remaining_percentage": None}, now=time.time())

    result = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))

    assert "CONTEXT PRESSURE — UNKNOWN" in result
    assert "%" not in result


def test_sidecar_present_with_non_numeric_fields_yields_unknown(tmp_path):
    session_id = "test-session-sidecar-non-numeric"
    _bypass_throttle(session_id)
    write_usage(
        session_id,
        {
            "used_percentage": "not-a-number",
            "context_window_size": "also-not-a-number",
            "current_usage": {
                "input_tokens": "nope",
                "output_tokens": 10,
                "cache_creation_input_tokens": "nope",
                "cache_read_input_tokens": "nope",
            },
        },
        now=time.time(),
    )

    result = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))

    assert "CONTEXT PRESSURE — UNKNOWN" in result


# ---------------------------------------------------------------------------
# AC6 -- staleness. A non-trivial age_seconds is reported, never discarded
# and never presented as current.
# ---------------------------------------------------------------------------


def test_stale_reading_is_reported_with_its_age_not_discarded(tmp_path):
    session_id = "test-session-stale-reading"
    write_time = time.time() - 900  # written 15 minutes ago
    _write_sidecar(
        session_id,
        input_tokens=2,
        cache_creation=1103,
        cache_read=93_895,  # 47.5% of 200K -- crosses advisory
        now=write_time,
    )
    _bypass_throttle(session_id)

    result = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))

    assert "CONTEXT PRESSURE — ADVISORY" in result
    assert "measured ~" not in result  # exact-seconds phrasing, not an approximation
    assert "measured 9" in result or "measured 8" in result  # ~900s ago, ± scheduling slack
    assert "s ago)" in result


# ---------------------------------------------------------------------------
# Bark-once dedup -- unchanged discipline, now on the sidecar-sourced path.
# ---------------------------------------------------------------------------


def test_bark_once_holds_across_repeated_measured_checks(tmp_path):
    session_id = "test-session-bark-once"
    _write_sidecar(session_id, input_tokens=2, cache_creation=1103, cache_read=93_895)
    _bypass_throttle(session_id)

    first = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))
    assert "CONTEXT PRESSURE — ADVISORY" in first

    _bypass_throttle(session_id)
    second = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))
    assert second == ""


def test_bark_once_holds_on_percentage_only_path(tmp_path):
    session_id = "test-session-bark-once-pct-only"
    _write_sidecar(
        session_id,
        input_tokens=2,
        cache_creation=100,
        cache_read=100,
        context_window_size=None,
        used_percentage=45,
    )
    _bypass_throttle(session_id)

    first = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))
    assert "CONTEXT PRESSURE — ADVISORY" in first

    _bypass_throttle(session_id)
    second = pad._check_context_pressure_sync(session_id, str(tmp_path / "transcript.jsonl"))
    assert second == ""


# ---------------------------------------------------------------------------
# AC7 -- escalating UNKNOWN: 1st, 3rd, and 10th consecutive unmeasured
# throttled check fire; every other count (including past the 10th) holds
# silent. A measured reading resets the streak.
# ---------------------------------------------------------------------------


def test_unmeasured_streak_escalates_at_1st_3rd_10th_then_holds_silent(tmp_path):
    session_id = "test-session-escalating-unknown"
    transcript = str(tmp_path / "transcript.jsonl")

    fired_at = {}
    for check_number in range(1, 13):
        _bypass_throttle(session_id)
        result = pad._check_context_pressure_sync(session_id, transcript)
        if result:
            fired_at[check_number] = result

    assert set(fired_at) == {1, 3, 10}

    # Rising urgency changes the stated fact (how long it's been unavailable),
    # not emphasis or repetition -- the three firings must not be identical.
    assert fired_at[1] != fired_at[3] != fired_at[10]
    assert fired_at[1] != fired_at[10]
    for text in fired_at.values():
        assert "CONTEXT PRESSURE — UNKNOWN" in text
        assert "%" not in text


def test_measured_reading_resets_the_unmeasured_streak(tmp_path):
    session_id = "test-session-streak-reset"
    transcript = str(tmp_path / "transcript.jsonl")

    # Two unmeasured misses (streak -> 2, no firing on #2).
    _bypass_throttle(session_id)
    first = pad._check_context_pressure_sync(session_id, transcript)
    assert "CONTEXT PRESSURE — UNKNOWN" in first
    _bypass_throttle(session_id)
    second = pad._check_context_pressure_sync(session_id, transcript)
    assert second == ""

    # A measured (but sub-threshold) reading resets the streak to 0.
    _write_sidecar(session_id, input_tokens=1, cache_creation=1, cache_read=1)
    _bypass_throttle(session_id)
    third = pad._check_context_pressure_sync(session_id, transcript)
    assert third == ""

    tmpdir = tempfile.gettempdir()
    state = pad._load_advisory_state(tmpdir, session_id)
    assert state.get("unmeasured_streak") == 0

    # Sidecar goes missing again -- the NEXT miss is streak 1, so it fires
    # (proof the counter actually reset rather than continuing from 2).
    import os as _os

    _os.unlink(sidecar_module.sidecar_path(session_id))
    _bypass_throttle(session_id)
    fourth = pad._check_context_pressure_sync(session_id, transcript)
    assert "CONTEXT PRESSURE — UNKNOWN" in fourth

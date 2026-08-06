"""
coordinator_core.hooks.test_postuse_advisory_dispatch -- tests for the token-accurate
context-pressure estimation path in postuse_advisory_dispatch.py.

Covers `_extract_last_usage_tokens` (pure helper reading real Anthropic API usage
blocks from a transcript tail), `_resolve_context_window` (three-tier context-window
resolution: env-var override table, built-in ordered table, safe default), and
`_check_context_pressure_sync`'s two parallel paths: the token-accurate path (usage
blocks present) and the legacy byte-proxy fallback path (no usage blocks found).

Spec backlink: coordinator_core/hooks/postuse_advisory_dispatch.py (module under test).
"""

from __future__ import annotations

import builtins
import glob
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.hooks import postuse_advisory_dispatch as pad  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixture: clean up durable per-session state files between tests.
#
# All tests below use session ids prefixed "test-session-" (by convention) so
# this fixture can sweep them without touching real session state. The state
# is now file-backed (see postuse_advisory_dispatch.py's module-level comment
# above _advisory_state_path for why the former in-memory dicts this fixture
# used to clear were the bug, not the fix).
# ---------------------------------------------------------------------------

_TEST_SESSION_STATE_GLOBS = (
    "advisory-hook-state-test-session-*.json",
    ".advisory-hook-state-*.tmp",
    "rt-bark-once-test-session-*",
    "compaction-occurred-test-session-*",
    "compaction-state-test-session-*.md",
    "first-agent-dispatch-advisory-test-session-*",
)

# Not session-scoped -- deliberately durable across sessions (see the comment
# above _check_unrecognised_sonnet_generation). Swept unconditionally between
# tests anyway so one test's "first sighting" of a fake model_id can't leak
# into another's expectations.
_SONNET_MONITOR_STATE_GLOBS = (
    "sonnet-generation-monitor-seen.json",
    ".sonnet-generation-monitor-*.tmp",
)


def _sweep_test_session_state_files() -> None:
    tmpdir = tempfile.gettempdir()
    for pattern in _TEST_SESSION_STATE_GLOBS + _SONNET_MONITOR_STATE_GLOBS:
        for path in glob.glob(os.path.join(tmpdir, pattern)):
            try:
                os.unlink(path)
            except OSError:
                pass


@pytest.fixture(autouse=True)
def _reset_advisory_state_files():
    _sweep_test_session_state_files()
    yield
    _sweep_test_session_state_files()


def _assistant_line(input_tokens, cache_creation, cache_read, output_tokens=336):
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": input_tokens,
                    "cache_creation_input_tokens": cache_creation,
                    "cache_read_input_tokens": cache_read,
                    "output_tokens": output_tokens,
                }
            },
        }
    )


# ---------------------------------------------------------------------------
# _extract_last_usage_tokens unit tests
# ---------------------------------------------------------------------------


def test_extract_last_usage_tokens_returns_sum_from_last_assistant_record(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {"content": "hi"}}),
        _assistant_line(2, 1103, 209944),
    ]
    transcript.write_text("\n".join(lines) + "\n")

    result = pad._extract_last_usage_tokens(str(transcript))

    assert result == 211049


def test_extract_last_usage_tokens_picks_last_not_first_or_sum(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    lines = [
        _assistant_line(1, 100, 100),  # total 201 -- must NOT be picked
        json.dumps({"type": "user", "message": {"content": "hi"}}),
        _assistant_line(2, 1103, 209944),  # total 211049 -- LAST, must be picked
    ]
    transcript.write_text("\n".join(lines) + "\n")

    result = pad._extract_last_usage_tokens(str(transcript))

    assert result == 211049


def test_extract_last_usage_tokens_returns_none_when_no_usage_key(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {"content": "hi"}}),
        json.dumps({"type": "assistant", "message": {"content": [{"text": "hello"}]}}),
    ]
    transcript.write_text("\n".join(lines) + "\n")

    assert pad._extract_last_usage_tokens(str(transcript)) is None


def test_extract_last_usage_tokens_returns_none_for_missing_file(tmp_path):
    missing = tmp_path / "does-not-exist.jsonl"

    assert pad._extract_last_usage_tokens(str(missing)) is None


def test_extract_last_usage_tokens_tail_read_finds_last_line_past_300kb(tmp_path):
    transcript = tmp_path / "big-transcript.jsonl"
    filler_line = json.dumps({"type": "user", "message": {"content": "x" * 500}})
    # Pad well past the default 300_000-byte tail window.
    filler_block = "\n".join([filler_line] * 1000)
    final_line = _assistant_line(2, 1103, 209944)
    transcript.write_text(filler_block + "\n" + final_line + "\n")

    assert transcript.stat().st_size > 300_000

    result = pad._extract_last_usage_tokens(str(transcript))

    assert result == 211049


# ---------------------------------------------------------------------------
# _resolve_context_window unit tests
# ---------------------------------------------------------------------------


def test_resolve_context_window_sonnet_5_is_1m():
    # In Claude Code on a paid plan, Sonnet 5 gets a 1M window -- diverges from
    # the Sonnet family's 200k default. Source: support.claude.com 8606394
    # ("How large is the context window on paid Claude plans"), confirmed
    # against this PM's own Max-plan account, 2026-07-24.
    assert pad._resolve_context_window("claude-sonnet-5") == 1_000_000


def test_resolve_context_window_sonnet_4_6_is_200k_no_credits_assumed():
    # Same source names Sonnet 4.6 as ALSO getting 1M in Claude Code -- but
    # only with usage credits enabled, which this hook can't verify and
    # doesn't assume (this PM doesn't use credits). Deliberately NOT a
    # _GENERATION_EXCEPTIONS row -- resolves via the 200k Sonnet family
    # default instead.
    assert pad._resolve_context_window("claude-sonnet-4-6") == 200_000


def test_resolve_context_window_older_sonnet_generation_is_200k():
    # A Sonnet generation NOT named in the support article -- stays at the
    # Sonnet family's base 200k. Guards the exception-matching itself: this
    # model_id must not accidentally substring-match "sonnet-5".
    model_id = "claude-sonnet-4-5-20250929"
    assert "sonnet-5" not in model_id
    assert pad._resolve_context_window(model_id) == 200_000


def test_resolve_context_window_opus_is_1m():
    assert pad._resolve_context_window("claude-opus-4-8") == 1_000_000


def test_resolve_context_window_haiku_is_200k():
    assert pad._resolve_context_window("claude-haiku-4-5-20251001") == 200_000


def test_resolve_context_window_mythos_not_assumed_falls_through_to_safe_default(
    monkeypatch,
):
    # Mythos is NOT on the PM's confirmed 1M-in-Claude-Code list -- unlike Opus,
    # Sonnet, and Fable, it has no _FAMILY_DEFAULTS row and must not silently
    # resolve to a guessed figure. It still lands on 1M today, but via the
    # generous safe default (tier 4), not a confirmed family convention.
    monkeypatch.delenv("COORDINATOR_DEFAULT_CONTEXT_WINDOW", raising=False)
    monkeypatch.delenv("COORDINATOR_MODEL_CONTEXT_WINDOWS", raising=False)

    assert pad._resolve_context_window("claude-mythos-5") == 1_000_000
    assert not any(
        "mythos" in substr for substr, _ in pad._FAMILY_DEFAULTS
    )


def test_resolve_context_window_unrecognized_model_falls_through_to_default(monkeypatch):
    monkeypatch.delenv("COORDINATOR_DEFAULT_CONTEXT_WINDOW", raising=False)
    monkeypatch.delenv("COORDINATOR_MODEL_CONTEXT_WINDOWS", raising=False)

    assert pad._resolve_context_window("claude-nova-1") == 1_000_000


def test_resolve_context_window_env_override_wins_over_default(monkeypatch):
    monkeypatch.setenv("COORDINATOR_MODEL_CONTEXT_WINDOWS", '{"nova-1": 500000}')

    assert pad._resolve_context_window("claude-nova-1") == 500_000


def test_resolve_context_window_malformed_env_override_does_not_raise(monkeypatch):
    monkeypatch.setenv("COORDINATOR_MODEL_CONTEXT_WINDOWS", "not json")
    monkeypatch.delenv("COORDINATOR_DEFAULT_CONTEXT_WINDOW", raising=False)

    # Falls through cleanly to tier 2 (generation exceptions) -- no raise.
    assert pad._resolve_context_window("claude-sonnet-5") == 1_000_000
    # And falls through to tier 4 (safe default) when nothing else matches.
    assert pad._resolve_context_window("claude-nova-1") == 1_000_000


def test_resolve_context_window_haiku_still_correct_after_c1_changes():
    # AC1/C1 named this the one live small-window model to re-verify unaffected
    # by the fallback/monitoring changes -- _resolve_context_window itself is
    # untouched, so this is a direct re-assertion, not a new code path.
    assert pad._resolve_context_window("claude-haiku-4-5-20251001") == 200_000


# ---------------------------------------------------------------------------
# _check_unrecognised_sonnet_generation unit tests (C1b -- observability only,
# does NOT change _resolve_context_window's resolution or the invariant tests
# above it).
# ---------------------------------------------------------------------------


def test_unrecognised_sonnet_generation_fires_once_for_new_model_id(tmp_path):
    tmpdir = str(tmp_path)

    first = pad._check_unrecognised_sonnet_generation(tmpdir, "claude-sonnet-7-20270101")
    assert first is not None
    assert "SONNET GENERATION MONITOR" in first
    assert "claude-sonnet-7-20270101" in first

    # Same model_id, same (durable) tmpdir -- must not re-fire.
    second = pad._check_unrecognised_sonnet_generation(tmpdir, "claude-sonnet-7-20270101")
    assert second is None


def test_unrecognised_sonnet_generation_silent_for_known_generations(tmp_path):
    tmpdir = str(tmp_path)

    for model_id in (
        "claude-sonnet-4-5-20250929",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
    ):
        assert pad._check_unrecognised_sonnet_generation(tmpdir, model_id) is None


def test_unrecognised_sonnet_generation_silent_for_non_sonnet_models(tmp_path):
    tmpdir = str(tmp_path)

    assert pad._check_unrecognised_sonnet_generation(tmpdir, "claude-opus-4-8") is None
    assert pad._check_unrecognised_sonnet_generation(tmpdir, "claude-haiku-4-5") is None
    assert pad._check_unrecognised_sonnet_generation(tmpdir, "claude-mythos-5") is None


def test_unrecognised_sonnet_generation_does_not_invert_resolution(tmp_path):
    """Firing the monitor must not change what _resolve_context_window returns
    for the same, still-unconfirmed model_id -- it stays at the 200k family
    default (the correct, conservative behaviour) even after being flagged."""
    tmpdir = str(tmp_path)
    model_id = "claude-sonnet-7-20270101"

    fired = pad._check_unrecognised_sonnet_generation(tmpdir, model_id)
    assert fired is not None
    assert pad._resolve_context_window(model_id) == 200_000


# ---------------------------------------------------------------------------
# _check_context_pressure_sync integration-style tests
# ---------------------------------------------------------------------------


def _bypass_throttle(session_id):
    # Force the 5-min throttle to be considered "expired" for this session --
    # writes the durable state file directly, since that (not process memory)
    # is now the source of truth.
    pad._save_advisory_state(tempfile.gettempdir(), session_id, {"throttle_last_check": 0.0})


def test_check_context_pressure_sonnet_5_uses_correct_1m_window_not_false_panic(
    tmp_path, monkeypatch
):
    """A claude-sonnet-5 session with ~185k usage tokens must resolve against the
    1M Claude Code window (~18%), not the base Sonnet-family 200k window (~92%) --
    see support.claude.com 8606394 and the _GENERATION_EXCEPTIONS comment."""
    monkeypatch.delenv("CONTEXT_ADVISORY_THRESHOLD", raising=False)
    monkeypatch.delenv("CONTEXT_CRITICAL_THRESHOLD", raising=False)
    monkeypatch.delenv("COORDINATOR_MODEL_CONTEXT_WINDOWS", raising=False)

    transcript = tmp_path / "transcript.jsonl"
    model_line = json.dumps({"model": "claude-sonnet-5"})
    usage_tokens_total = 185_000
    usage_line = _assistant_line(2, 1103, usage_tokens_total - 2 - 1103)
    transcript.write_text("\n".join([model_line, usage_line]) + "\n")

    session_id = "test-session-sonnet-5-correct-window"
    _bypass_throttle(session_id)

    result = pad._check_context_pressure_sync(session_id, str(transcript))

    context_window = 1_000_000
    expected_pct = usage_tokens_total * 100 // context_window
    assert expected_pct < 20  # correct reading is ~18%, well under both thresholds

    # Below both the 40% advisory and 50% critical thresholds against the correct
    # 1M window -- no advisory should fire at all.
    assert result == ""


def test_check_context_pressure_1m_tier_critical_fires_below_auto_compact_ceiling(
    tmp_path, monkeypatch
):
    """PM ruling 2026-07-27: on the 1M tier, CRITICAL must fire strictly BELOW
    Anthropic's fixed ~500K auto-compact ceiling, with real headroom -- not
    coincident with it. A flat 50%-of-window threshold on a 1M window lands
    exactly ON the 500K ceiling (500_000 == 500_000), which fires the warning
    level with the cut instead of ahead of it and defeats the whole point of a
    pre-emptive advisory. This test fails under that old flat-percentage
    behaviour and passes under the absolute-anchored (400K/450K) tier."""
    monkeypatch.delenv("CONTEXT_ADVISORY_THRESHOLD", raising=False)
    monkeypatch.delenv("CONTEXT_CRITICAL_THRESHOLD", raising=False)
    monkeypatch.delenv("COORDINATOR_MODEL_CONTEXT_WINDOWS", raising=False)

    transcript = tmp_path / "transcript.jsonl"
    model_line = json.dumps({"model": "claude-sonnet-5"})  # 1M tier exception
    # 460K usage tokens: above the anchored 450K critical threshold, but well
    # below the 500K auto-compact ceiling -- the case the old 50%-of-window
    # threshold (500K) would have missed by firing coincident with the cut
    # instead of ahead of it.
    usage_tokens_total = 460_000
    usage_line = _assistant_line(2, 1103, usage_tokens_total - 2 - 1103)
    transcript.write_text("\n".join([model_line, usage_line]) + "\n")

    session_id = "test-session-1m-critical-headroom"
    _bypass_throttle(session_id)

    result = pad._check_context_pressure_sync(session_id, str(transcript))

    assert result != ""
    assert "CONTEXT PRESSURE — HIGH" in result

    # The threshold itself must sit strictly below the fixed ceiling, with
    # explicit, non-trivial headroom -- not just "less than", but a real gap.
    critical_tokens = 450_000
    ceiling = pad._AUTO_COMPACT_CEILING_TOKENS_1M
    assert ceiling == 500_000
    headroom = ceiling - critical_tokens
    assert critical_tokens < ceiling
    assert headroom >= 50_000, (
        f"critical threshold ({critical_tokens}) must sit at least 50K tokens "
        f"below the fixed auto-compact ceiling ({ceiling}); headroom was only "
        f"{headroom}"
    )
    # And the fired usage level (460K) is itself still below the ceiling.
    assert usage_tokens_total < ceiling


def test_check_context_pressure_1m_tier_advisory_fires_before_critical(
    tmp_path, monkeypatch
):
    """Advisory (400K) must fire meaningfully before critical (450K) on the
    1M tier -- both anchored absolute tokens, not the old flat percentages."""
    monkeypatch.delenv("CONTEXT_ADVISORY_THRESHOLD", raising=False)
    monkeypatch.delenv("CONTEXT_CRITICAL_THRESHOLD", raising=False)
    monkeypatch.delenv("COORDINATOR_MODEL_CONTEXT_WINDOWS", raising=False)

    transcript = tmp_path / "transcript.jsonl"
    model_line = json.dumps({"model": "claude-sonnet-5"})
    usage_tokens_total = 410_000  # above 400K advisory, below 450K critical
    usage_line = _assistant_line(2, 1103, usage_tokens_total - 2 - 1103)
    transcript.write_text("\n".join([model_line, usage_line]) + "\n")

    session_id = "test-session-1m-advisory-before-critical"
    _bypass_throttle(session_id)

    result = pad._check_context_pressure_sync(session_id, str(transcript))

    assert result != ""
    assert "CONTEXT PRESSURE — ADVISORY" in result
    assert "CONTEXT PRESSURE — HIGH" not in result


def test_check_context_pressure_200k_tier_unchanged_percentage_model(
    tmp_path, monkeypatch
):
    """Sub-1M tiers (Sonnet/Haiku 200K default) must keep the 40%/50%-of-window
    behaviour unchanged -- there is no fixed-ceiling collision to guard against
    at this window size, and this test would fail if the 1M-tier absolute
    anchoring were accidentally applied here too."""
    monkeypatch.delenv("CONTEXT_ADVISORY_THRESHOLD", raising=False)
    monkeypatch.delenv("CONTEXT_CRITICAL_THRESHOLD", raising=False)
    monkeypatch.delenv("COORDINATOR_MODEL_CONTEXT_WINDOWS", raising=False)

    transcript = tmp_path / "transcript.jsonl"
    model_line = json.dumps({"model": "claude-haiku-4-5-20251001"})  # 200K default
    context_window = 200_000
    assert pad._resolve_context_window("claude-haiku-4-5-20251001") == context_window

    # 95_000 tokens is below the 40%-of-200K (80_000) advisory threshold that
    # would apply here if the 1M absolute anchoring (400K) were wrongly
    # reused -- but well above 40% of the correct 200K window, so it SHOULD
    # fire advisory under the correct (unchanged) percentage model.
    usage_tokens_total = 95_000  # 47.5% of 200K -- above 40% advisory, below 50% critical
    usage_line = _assistant_line(2, 1103, usage_tokens_total - 2 - 1103)
    transcript.write_text("\n".join([model_line, usage_line]) + "\n")

    session_id = "test-session-200k-haiku-unchanged"
    _bypass_throttle(session_id)

    result = pad._check_context_pressure_sync(session_id, str(transcript))

    assert result != ""
    assert "CONTEXT PRESSURE — ADVISORY" in result
    assert "CONTEXT PRESSURE — HIGH" not in result

    expected_pct = usage_tokens_total * 100 // context_window
    assert f"~{expected_pct}%" in result


def test_check_context_pressure_uses_usage_tokens_when_available(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTEXT_ADVISORY_THRESHOLD", raising=False)
    monkeypatch.delenv("CONTEXT_CRITICAL_THRESHOLD", raising=False)
    monkeypatch.delenv("COORDINATOR_MODEL_CONTEXT_WINDOWS", raising=False)

    transcript = tmp_path / "transcript.jsonl"
    # Any Sonnet-family model id resolves to the 200k window used by this scenario.
    model_line = json.dumps({"model": "claude-sonnet-4-5-20250929"})
    # Real usage totalling ~95% of a 200k window -- the reported bug scenario.
    usage_line = _assistant_line(2, 1103, 189997)  # total 191102 -> ~95%
    # Keep the file's raw byte size small so the OLD byte-proxy formula would
    # have estimated well under the critical threshold for this same content.
    lines = [model_line, usage_line]
    transcript.write_text("\n".join(lines) + "\n")

    session_id = "test-session-usage-critical"
    _bypass_throttle(session_id)

    # Sanity-check the scenario: byte-proxy-derived pct must be well below 95%,
    # while the usage-derived pct correctly reports >= the 50% critical threshold.
    file_size = transcript.stat().st_size
    context_window = 200_000
    byte_proxy_pct = file_size * 100 // (context_window * 7)
    assert byte_proxy_pct < 50

    result = pad._check_context_pressure_sync(session_id, str(transcript))

    assert result != ""
    assert "measured from the transcript's own Anthropic API usage blocks" in result
    assert "byte-based proxy" not in result
    usage_tokens = 191102
    expected_pct = usage_tokens * 100 // context_window
    assert expected_pct >= 50
    assert f"~{expected_pct}%" in result
    assert "CONTEXT PRESSURE — HIGH" in result


def test_check_context_pressure_no_usage_no_advisory_under_threshold(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    model_line = json.dumps({"model": "claude-sonnet-4-5-20250929"})
    # Light, prose-heavy content -- no usage block, small file size.
    user_line = json.dumps({"type": "user", "message": {"content": "just chatting"}})
    transcript.write_text("\n".join([model_line, user_line]) + "\n")

    session_id = "test-session-light-no-advisory"
    _bypass_throttle(session_id)

    result = pad._check_context_pressure_sync(session_id, str(transcript))

    assert result == ""


def test_check_context_pressure_fallback_fails_loud_not_a_percentage(tmp_path):
    """C1(a): the old 7-bytes/token proxy is gone. When no usage block is found,
    a large-enough transcript gets an explicit "can't measure this" disclosure --
    never a confident (and historically ~2x wrong) percentage."""
    transcript = tmp_path / "transcript.jsonl"
    model_line = json.dumps({"model": "claude-sonnet-4-5-20250929"})
    # No usage block anywhere. Build a file large enough to cross the old
    # byte-proxy critical threshold: context_window(200_000) * 50% * 7 bytes/token
    # = 7_000_000 bytes -- that threshold is now used only as a "worth
    # mentioning at all" size gate, never to compute a displayed percentage.
    context_window = 200_000
    bytes_per_token = 7
    critical_pct = 50
    critical_bytes = context_window * critical_pct * bytes_per_token // 100
    filler_line = json.dumps({"type": "user", "message": {"content": "x" * 500}})
    lines = [model_line]
    # Each filler line is > 500 bytes; pad comfortably past critical_bytes.
    needed_lines = (critical_bytes // len(filler_line)) + 10
    lines.extend([filler_line] * needed_lines)
    transcript.write_text("\n".join(lines) + "\n")

    assert transcript.stat().st_size >= critical_bytes

    session_id = "test-session-fallback-fails-loud"
    _bypass_throttle(session_id)

    result = pad._check_context_pressure_sync(session_id, str(transcript))

    assert result != ""
    assert "CONTEXT PRESSURE — UNKNOWN" in result
    assert "cannot be measured" in result
    assert "no Anthropic API usage block was found" in result
    # No percentage of any kind is fabricated for this path.
    assert "%" not in result
    assert "byte-based proxy" not in result
    assert "measured from the transcript's own Anthropic API usage blocks" not in result


def test_check_context_pressure_fallback_bark_once_per_transcript(tmp_path):
    """The fail-loud disclosure fires at most once per transcript_hash, same
    dedup discipline as the token-accurate path -- not on every throttle-gated
    call for the life of a long, usage-block-less session."""
    transcript = tmp_path / "transcript.jsonl"
    model_line = json.dumps({"model": "claude-sonnet-4-5-20250929"})
    context_window = 200_000
    bytes_per_token = 7
    critical_pct = 50
    critical_bytes = context_window * critical_pct * bytes_per_token // 100
    filler_line = json.dumps({"type": "user", "message": {"content": "x" * 500}})
    lines = [model_line]
    needed_lines = (critical_bytes // len(filler_line)) + 10
    lines.extend([filler_line] * needed_lines)
    transcript.write_text("\n".join(lines) + "\n")

    session_id = "test-session-fallback-bark-once"
    _bypass_throttle(session_id)
    first = pad._check_context_pressure_sync(session_id, str(transcript))
    assert "CONTEXT PRESSURE — UNKNOWN" in first

    # Re-arm ONLY the throttle timestamp -- `_bypass_throttle` overwrites the
    # whole state file, which would also wipe the critical_fired/advisory_fired
    # dedup this test is specifically checking survives a throttle re-arm.
    tmpdir = tempfile.gettempdir()
    state = pad._load_advisory_state(tmpdir, session_id)
    state["throttle_last_check"] = 0.0
    pad._save_advisory_state(tmpdir, session_id, state)

    second = pad._check_context_pressure_sync(session_id, str(transcript))
    assert second == ""


# ---------------------------------------------------------------------------
# Durable-state regression tests.
#
# These cover the actual bug: this op is dispatched via a FRESH process per
# PostToolUse fire (no resident daemon, DR-215), so any guard kept only in a
# module-level dict/set re-initializes empty on every call and never
# suppresses anything. None of these tests share Python-object state between
# calls -- there is none any more, by construction -- so a call "seeing" a
# prior call's effect here is proof the durable state FILE (not process
# memory) is what's doing the suppressing, faithfully simulating what a
# second, wholly separate process invocation would observe.
# ---------------------------------------------------------------------------


def test_throttle_suppresses_second_call_within_window_across_separate_invocations(
    tmp_path,
):
    transcript = tmp_path / "transcript.jsonl"
    model_line = json.dumps({"model": "claude-sonnet-4-5-20250929"})
    user_line = json.dumps({"type": "user", "message": {"content": "hi"}})
    transcript.write_text("\n".join([model_line, user_line]) + "\n")

    session_id = "test-session-throttle-cross-invocation"

    # "Invocation" 1: no prior state on disk anywhere for this session, so the
    # throttle does NOT suppress -- the real check runs and persists
    # throttle_last_check as a side effect.
    first = pad._check_context_pressure_sync(session_id, str(transcript))
    assert first == ""  # light content -- nothing crosses a threshold

    state_path = pad._advisory_state_path(tempfile.gettempdir(), session_id)
    assert os.path.isfile(state_path)
    with open(state_path, encoding="utf-8") as fh:
        persisted = json.load(fh)
    assert persisted["throttle_last_check"] > 0.0

    # "Invocation" 2: same session, well within the 5-minute throttle window.
    # Nothing in this test process hands state from call 1 to call 2 directly
    # -- only the file on disk does.
    second = pad._check_context_pressure_sync(session_id, str(transcript))
    assert second == ""


def test_throttle_suppresses_even_when_content_would_otherwise_fire(tmp_path):
    """Isolates the throttle guard specifically (not bark-once): pre-seed
    throttle_last_check to "just now" for a session that has NEVER fired
    before, then confirm a critical-sized transcript is still suppressed."""
    transcript = tmp_path / "transcript.jsonl"
    model_line = json.dumps({"model": "claude-sonnet-4-5-20250929"})
    context_window = 200_000
    bytes_per_token = 7
    critical_pct = 50
    critical_bytes = context_window * critical_pct * bytes_per_token // 100
    filler_line = json.dumps({"type": "user", "message": {"content": "x" * 500}})
    lines = [model_line]
    needed_lines = (critical_bytes // len(filler_line)) + 10
    lines.extend([filler_line] * needed_lines)
    transcript.write_text("\n".join(lines) + "\n")
    assert transcript.stat().st_size >= critical_bytes

    session_id = "test-session-throttle-isolated"
    pad._save_advisory_state(
        tempfile.gettempdir(), session_id, {"throttle_last_check": time.time()}
    )

    result = pad._check_context_pressure_sync(session_id, str(transcript))

    assert result == ""  # throttled despite content that would otherwise fire critical


def test_compaction_advisory_fires_exactly_once_per_sentinel_and_rearms(tmp_path):
    """Covers the most severe instance of the regression: the sentinel used to
    never be deleted (B-F1) and the in-memory consumption marker never
    survived the fresh-process-per-fire model, so the advisory re-fired on
    every subsequent call for the rest of the session. Also proves consumption
    is per-EVENT (delete-on-read), not an eternal per-session flag -- a second,
    later compaction in the same session must fire again."""
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("small transcript content\n" * 5)
    post_size = transcript.stat().st_size
    pre_size = post_size * 10  # comfortably satisfies the 85%-shrink real-compaction guard

    session_id = "test-session-compaction-once"
    tmpdir = tempfile.gettempdir()
    sentinel = os.path.join(tmpdir, f"compaction-occurred-{session_id}")
    state_snapshot = os.path.join(tmpdir, f"compaction-state-{session_id}.md")

    with open(sentinel, "w", encoding="utf-8") as fh:
        fh.write(str(pre_size))
    with open(state_snapshot, "w", encoding="utf-8") as fh:
        fh.write("first snapshot")

    first = pad._check_context_pressure_sync(session_id, str(transcript))
    assert "COMPACTION OCCURRED" in first
    assert "first snapshot" in first
    # Consumed by delete -- the once-only firing guard for THIS event.
    assert not os.path.isfile(sentinel)
    assert not os.path.isfile(state_snapshot)

    # Second call, same session, no new sentinel written: must NOT re-fire.
    second = pad._check_context_pressure_sync(session_id, str(transcript))
    assert "COMPACTION OCCURRED" not in second

    # A later compaction event in the same long session re-arms correctly.
    with open(sentinel, "w", encoding="utf-8") as fh:
        fh.write(str(pre_size))
    with open(state_snapshot, "w", encoding="utf-8") as fh:
        fh.write("second snapshot")

    third = pad._check_context_pressure_sync(session_id, str(transcript))
    assert "COMPACTION OCCURRED" in third
    assert "second snapshot" in third
    assert not os.path.isfile(sentinel)
    assert not os.path.isfile(state_snapshot)


# ---------------------------------------------------------------------------
# _check_first_agent_dispatch_sync unit tests.
#
# One-time-per-session advisory telling the EM that coordinator subagents write
# full findings to an on-disk sidecar. Gated on tool_name == "Agent" (not a
# subagent_type prefix -- the payload this op receives carries no
# subagent_type field) plus a durable once-per-session sentinel.
# ---------------------------------------------------------------------------


def test_first_agent_dispatch_fires_once_on_first_agent_call():
    session_id = "test-session-first-agent-dispatch-fires"

    first = pad._check_first_agent_dispatch_sync(session_id, "Agent")
    assert first != ""
    assert session_id in first
    assert "state/subagent-share/" in first

    # Second Agent-tool call, same session -- must not re-fire.
    second = pad._check_first_agent_dispatch_sync(session_id, "Agent")
    assert second == ""


def test_first_agent_dispatch_silent_for_non_agent_tool_even_on_first_call():
    session_id = "test-session-first-agent-dispatch-non-agent"

    for tool_name in ("Bash", "Read", "Explore", "general-purpose", ""):
        assert pad._check_first_agent_dispatch_sync(session_id, tool_name) == ""

    # No sentinel written by a non-Agent tool_name -- a later real Agent
    # dispatch in the same session must still fire.
    fired = pad._check_first_agent_dispatch_sync(session_id, "Agent")
    assert fired != ""


def test_first_agent_dispatch_silent_when_session_id_absent():
    assert pad._check_first_agent_dispatch_sync("", "Agent") == ""


def test_first_agent_dispatch_sentinel_write_failure_degrades_to_silence(monkeypatch):
    session_id = "test-session-first-agent-dispatch-write-fail"

    def _raise(*args, **kwargs):
        raise OSError("simulated sentinel-write failure")

    monkeypatch.setattr(pad, "open", _raise, raising=False)

    result = pad._check_first_agent_dispatch_sync(session_id, "Agent")
    assert result == ""

    tmpdir = tempfile.gettempdir()
    sentinel = pad._first_agent_dispatch_sentinel_path(tmpdir, session_id)
    assert not os.path.isfile(sentinel)


def test_first_agent_dispatch_sentinel_partial_write_failure_allows_retry(monkeypatch):
    """Review: code-reviewer (Finding 3) -- if open() succeeds but write()
    raises mid-write (e.g. disk full), the sentinel file already exists on
    disk. Without cleanup, every later call in the session would see the
    partial file and stay silent forever. The failed write must remove the
    sentinel so a later Agent dispatch in the same session can retry."""
    session_id = "test-session-first-agent-dispatch-partial-write"

    real_open = builtins.open
    tmpdir = tempfile.gettempdir()
    sentinel = pad._first_agent_dispatch_sentinel_path(tmpdir, session_id)

    class _FailingFile:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def write(self, data):
            raise OSError("simulated mid-write failure")

    def _fake_open(path, mode="r", *args, **kwargs):
        if str(path) == sentinel and mode == "w":
            # Mirrors open() succeeding (the file lands on disk) then
            # write() raising mid-write.
            real_open(path, "w", encoding=kwargs.get("encoding", "utf-8")).close()
            return _FailingFile()
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(pad, "open", _fake_open, raising=False)

    result = pad._check_first_agent_dispatch_sync(session_id, "Agent")
    assert result == ""
    assert not os.path.isfile(sentinel)

    # Retry (real open() now, monkeypatch still active but path differs after
    # the sentinel was removed -- same code path, no partial file to trip on):
    monkeypatch.setattr(pad, "open", real_open, raising=False)
    retried = pad._check_first_agent_dispatch_sync(session_id, "Agent")
    assert retried != ""
    assert os.path.isfile(sentinel)


# ---------------------------------------------------------------------------
# _handler integration: composition with the other two checks (the regression
# that matters most -- the new third check must never clobber or short-circuit
# the existing context-pressure / runtime-tripwire advisories).
# ---------------------------------------------------------------------------


def test_handler_first_agent_dispatch_composes_with_existing_advisories():
    import asyncio
    import unittest.mock as mock

    session_id = "test-session-handler-three-way-merge"

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value="cp text"):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value="rt text"):
            result = asyncio.run(
                pad._handler({"session_id": session_id, "tool_name": "Agent"})
            )

    hso = result["hookSpecificOutput"]
    context = hso["additionalContext"]
    assert "cp text" in context
    assert "rt text" in context
    assert "COORDINATOR SIDECAR ADVISORY" in context
    assert session_id in context
    assert "\n\n" in context

    # Review: code-reviewer (Finding 4) -- membership alone doesn't pin the
    # merge order the commit message claims (cp -> rt -> first-agent-dispatch);
    # a future reorder of the join would pass the assertions above unnoticed.
    assert context.index("cp text") < context.index("rt text") < context.index(
        "COORDINATOR SIDECAR ADVISORY"
    )


def test_handler_first_agent_dispatch_alone_still_post_advisory():
    import asyncio
    import unittest.mock as mock

    session_id = "test-session-handler-agent-only"

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value=""):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value=""):
            result = asyncio.run(
                pad._handler({"session_id": session_id, "tool_name": "Agent"})
            )

    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert "COORDINATOR SIDECAR ADVISORY" in hso["additionalContext"]


def test_handler_existing_advisories_unaffected_by_non_agent_tool_name():
    import asyncio
    import unittest.mock as mock

    session_id = "test-session-handler-non-agent-cp-still-fires"

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value="cp text"):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value=""):
            result = asyncio.run(
                pad._handler({"session_id": session_id, "tool_name": "Bash"})
            )

    hso = result["hookSpecificOutput"]
    assert hso["additionalContext"].endswith("cp text")
    assert "COORDINATOR SIDECAR ADVISORY" not in hso["additionalContext"]


# ---------------------------------------------------------------------------
# Fourth check: the unauthorized-handoff nudge folded in from example-doctrine-repo's separate
# PostToolUse(Write) registration (cross-repo memo
# 2026-08-06-example-doctrine-repo-em-postuse-fold-nudge-unauthorized-handoff.md).
# ---------------------------------------------------------------------------


def _handoff_write_params(session_id, **overrides):
    params = {
        "session_id": session_id,
        "tool_name": "Write",
        "file_path": "state/handoffs/2026-08-06_120000_some-topic.md",
        "content": "---\ntitle: something\n---\n",
    }
    params.update(overrides)
    return params


def test_handler_unauthorized_handoff_nudge_fires_on_handoff_write():
    import asyncio
    import unittest.mock as mock

    session_id = "test-session-handler-uh-fires"

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value=""):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value=""):
            result = asyncio.run(pad._handler(_handoff_write_params(session_id)))

    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert "[nudge]" in hso["additionalContext"]
    assert "state/handoffs" in hso["additionalContext"]


def test_handler_unauthorized_handoff_silent_for_ordinary_write():
    import asyncio
    import unittest.mock as mock

    session_id = "test-session-handler-uh-ordinary-write"

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value=""):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value=""):
            result = asyncio.run(
                pad._handler(
                    _handoff_write_params(
                        session_id, file_path="coordinator_core/hooks/whatever.py"
                    )
                )
            )

    assert result == {}


def test_handler_unauthorized_handoff_merges_last_after_existing_advisories():
    import asyncio
    import unittest.mock as mock

    session_id = "test-session-handler-uh-four-way-merge"

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value="cp text"):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value="rt text"):
            result = asyncio.run(pad._handler(_handoff_write_params(session_id)))

    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.index("cp text") < context.index("rt text") < context.index("[nudge]")


def test_handler_unauthorized_handoff_survives_absent_session_id():
    """The nudge's predicate is the Write payload alone — the session_id
    short-circuit that silences the other three must not swallow it."""
    import asyncio

    result = asyncio.run(pad._handler(_handoff_write_params("")))

    assert "[nudge]" in result["hookSpecificOutput"]["additionalContext"]


def test_handler_unauthorized_handoff_silent_when_stub_omits_file_path():
    """example-doctrine-repo's dispatcher stub must map tool_input.file_path/content into params.
    Until it does, the fourth check stays silent and the other three still fire."""
    import asyncio
    import unittest.mock as mock

    session_id = "test-session-handler-uh-unplumbed-stub"

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value="cp text"):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value=""):
            result = asyncio.run(
                pad._handler({"session_id": session_id, "tool_name": "Write"})
            )

    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.endswith("cp text")
    assert "[nudge]" not in context


def test_handler_unauthorized_handoff_respects_kind_recovery_suppression():
    import asyncio
    import unittest.mock as mock

    session_id = "test-session-handler-uh-recovery-suppressed"

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value=""):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value=""):
            result = asyncio.run(
                pad._handler(
                    _handoff_write_params(
                        session_id, content="---\nkind: recovery\n---\n"
                    )
                )
            )

    assert result == {}


# ---------------------------------------------------------------------------
# _check_runtime_tripwire_sync — repo-root resolution goes through the shared
# memoized seam (coordinator_core.git.repo_root), never a per-tool-call
# `git rev-parse --show-toplevel` spawn. This check runs from an EMPTY-matcher
# PostToolUse hook, so a spawn here is one process per tool call.
# ---------------------------------------------------------------------------


def test_runtime_tripwire_resolves_repo_root_via_seam_not_a_spawn(monkeypatch):
    # Review: code-reviewer (P2, W2) -- `_fail_on_spawn` raising AssertionError
    # is itself an Exception, and every spawn site it could intercept lives
    # inside `_check_runtime_tripwire_sync`'s own `except Exception: return ""`,
    # so a bare `assert result == ""` still passes against the OLD (spawning)
    # implementation -- the guard could never fail. Record the spawn attempt in
    # a list BEFORE raising, and assert against that list after the call, so
    # the evidence survives the swallow and the test genuinely discriminates
    # old (spawns) vs. new (walks) behavior.
    import subprocess as _subprocess

    from coordinator_core.git import repo_root as repo_root_seam

    spawned = []

    def _fail_on_spawn(*args, **kwargs):
        spawned.append(args)
        raise AssertionError(f"unexpected subprocess spawn: {args!r}")

    monkeypatch.setattr(_subprocess, "run", _fail_on_spawn)

    calls = []

    def _fake_show_toplevel(cwd=None):
        calls.append(cwd)
        return None

    monkeypatch.setattr(repo_root_seam, "show_toplevel", _fake_show_toplevel)

    assert pad._check_runtime_tripwire_sync("test-session-rt-seam", "") == ""
    assert calls, "show_toplevel was not consulted"
    assert not spawned, f"unexpected subprocess spawn: {spawned!r}"


def test_runtime_tripwire_happy_path_resolves_through_seam_and_fires(
    tmp_path, monkeypatch
):
    """Review: code-reviewer (P3, W4) -- both existing seam tests stub
    show_toplevel to None/raise, so only the earliest early-exit
    (`if not git_root: return ""`) is ever driven. This test resolves a real
    root through the seam and continues into the agents-dir / back-pointer /
    dispatch-record / threshold logic, covering the path that actually
    changed."""
    from coordinator_core.git import repo_root as repo_root_seam

    git_root = tmp_path
    session_id = "test-session-rt-happy-path"
    em_sid = "test-session-rt-happy-path-em"

    agent_dir = git_root / ".git" / "coordinator-sessions" / ".agents" / session_id
    agent_dir.mkdir(parents=True)
    (agent_dir / "em-session-id.txt").write_text(em_sid + "\n", encoding="utf-8")

    em_dir = git_root / ".git" / "coordinator-sessions" / em_sid
    em_dir.mkdir(parents=True)
    dispatched_at = int(time.time()) - 999_999  # comfortably past every threshold
    (em_dir / "dispatched-agents.txt").write_text(
        f"{session_id}\tclaude-sonnet-4-5\tgeneral-purpose\t{dispatched_at}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        repo_root_seam, "show_toplevel", lambda cwd=None: str(git_root)
    )

    # Review: code-reviewer (F6) -- this test's bark-once sentinel
    # (rt-bark-once-{session_id}) lives under the REAL tempfile.gettempdir(),
    # the same directory this module's autouse _sweep_test_session_state_files
    # globs before/after every test in the file. Under xdist (--dist load
    # splits one file across workers), a peer worker's sweep can land in the
    # microsecond window between this test's two calls and unlink the
    # sentinel, flipping `second` from "" to a fire -- a latent flake, not
    # something to paper over with timing. Route this test's tempdir through a
    # tmp_path-backed shim instead, so its sentinel lives somewhere no other
    # worker's glob ever reaches.
    isolated_tmpdir = tmp_path / "rt-tripwire-tmpdir"
    isolated_tmpdir.mkdir()
    monkeypatch.setattr(pad, "_tempfile", lambda: type(
        "_Shim", (), {"gettempdir": staticmethod(lambda: str(isolated_tmpdir))}
    )())

    result = pad._check_runtime_tripwire_sync(session_id, "")

    assert result != ""
    assert "RUNTIME TRIPWIRE" in result
    assert "stop starting new work" in result

    # Bark-once sentinel now present -- second call for the same session_id
    # must not re-fire.
    second = pad._check_runtime_tripwire_sync(session_id, "")
    assert second == ""


def test_runtime_tripwire_fails_open_when_seam_raises(monkeypatch):
    """Contract test, not a change test -- Review: code-reviewer (P3, W3): this
    also passes against the pre-image (the stub is never consulted there; real
    git resolves and the function early-exits at the agents_dir check). It pins
    the never-raises contract going forward, it does not discriminate the
    repo-root-via-seam change itself -- see
    test_runtime_tripwire_resolves_repo_root_via_seam_not_a_spawn for that."""
    from coordinator_core.git import repo_root as repo_root_seam

    def _boom(cwd=None):
        raise OSError("cwd vanished")

    monkeypatch.setattr(repo_root_seam, "show_toplevel", _boom)

    assert pad._check_runtime_tripwire_sync("test-session-rt-seam-raises", "") == ""

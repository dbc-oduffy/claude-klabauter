"""
Tests for coordinator_core/hooks/nudge_em_code_dispatch.py and
coordinator_core/hooks/postuse_advisory_dispatch.py — THE POSITIVE CONTROL
proving the fleet file reaches the hooks that actually run, not merely
`resolve_mode` in isolation.

Two properties, both required:
    1. With no fleet file, each converted call site behaves bit-identically
       to the pre-plan sentinel-only path (the baseline C3 must not disturb).
    2. With a fleet file setting a key, the HOOK ENTRY POINT that actually
       runs (``op()`` / ``_check_context_pressure_sync`` /
       ``_check_runtime_tripwire_sync`` — never ``resolve_mode`` called
       directly) behaves differently.

Covers both precedence branches, since C3 is the only chunk that can:
    - ``autonomous`` (session-wins) via ``nudge_em_code_dispatch.op()`` and
      ``postuse_advisory_dispatch._check_runtime_tripwire_sync``.
    - ``compaction_warnings`` (fleet-wins) via
      ``postuse_advisory_dispatch._check_context_pressure_sync``.

Spec backlink: docs/plans/2026-08-28-the-fleet-gets-one-file-and-the-floor-
moves-to-the-reader.md § C3.
"""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.hooks import nudge_em_code_dispatch
from coordinator_core.hooks import postuse_advisory_dispatch
from coordinator_core.session.mode_resolution import MODE_KEYS


@pytest.fixture(autouse=True)
def _isolate_sentinel_and_fleet(tmp_path, monkeypatch):
    """Isolate the autonomous sentinel's temp dir, the fleet record's
    settings home, and the context-usage-sidecar's settings home, so tests
    never touch real machine-wide files. Mirrors
    coordinator_core/session/tests/test_mode_resolution.py's fixture."""
    monkeypatch.setattr(
        "coordinator_core.session.autonomous_sentinel.tempfile.gettempdir",
        lambda: str(tmp_path),
    )
    settings_home = tmp_path / "settings-home"
    settings_home.mkdir()
    monkeypatch.setattr(
        "coordinator_core.session.fleet_mode.settings_home",
        lambda: settings_home,
    )
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    # postuse_advisory_dispatch's durable per-session state (throttle/bark-once)
    # and the runtime-tripwire bark-once sentinel both go through
    # tempfile.gettempdir() at the module's own `_tempfile()` accessor, which
    # re-imports the real `tempfile` module -- patch it globally too.
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    return tmp_path, settings_home


def _touch_autonomous_sentinel(tmp_path, session_id):
    """Mint an autonomous-run sentinel with real content, not a bare touch.

    `resolve_mode`'s session-wins leg (`_autonomous_session_value`) only
    checks `.exists()`, so an empty file passed every presence-only test
    here -- but `_check_context_pressure_sync`'s red-band
    `autonomous_recognized` check reads the file's CONTENT and compares it
    against the literal string ``"autonomous"`` (the real writer's other
    value is ``"mise-en-place"``); an empty sentinel never matches either,
    so a presence-only fixture silently fails that content check without
    raising. Writing the real content here covers both call shapes.
    """
    from coordinator_core.session import autonomous_sentinel

    autonomous_sentinel.sentinel_path(session_id).write_text("autonomous", encoding="utf-8")


def _write_fleet(record):
    from coordinator_core.session.fleet_mode import write_fleet_mode

    assert write_fleet_mode(record)


# ---------------------------------------------------------------------------
# nudge_em_code_dispatch.op() -- Bypass 4, `autonomous` key, session-wins.
# ---------------------------------------------------------------------------


def _op_payload(session_id: str, file_path: str = "foo.py") -> dict:
    return {
        "session_id": session_id,
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": "print('substantive change')\n"},
    }


class TestNudgeEmCodeDispatchOpAutonomous:
    def test_no_fleet_file_no_sentinel_nudge_fires(self, _isolate_sentinel_and_fleet):
        """Baseline unchanged: no fleet file, no sentinel -> nudge fires."""
        result = nudge_em_code_dispatch.op(_op_payload("s1"))
        assert result is not None

    def test_no_fleet_file_sentinel_present_suppressed(self, _isolate_sentinel_and_fleet):
        """Baseline unchanged: no fleet file, sentinel present -> suppressed."""
        tmp_path, _home = _isolate_sentinel_and_fleet
        _touch_autonomous_sentinel(tmp_path, "s2")
        result = nudge_em_code_dispatch.op(_op_payload("s2"))
        assert result is None

    def test_fleet_autonomous_on_does_not_override_absent_sentinel(
        self, _isolate_sentinel_and_fleet
    ):
        """session-wins proof, through the hook entry point: a fleet
        autonomous:on value must NOT suppress the nudge when this session's
        own sentinel is absent."""
        _write_fleet({"autonomous": True})
        result = nudge_em_code_dispatch.op(_op_payload("s3"))
        assert result is not None

    def test_fleet_autonomous_off_does_not_unsuppress_present_sentinel(
        self, _isolate_sentinel_and_fleet
    ):
        """session-wins proof, through the hook entry point: a fleet
        autonomous:off value must NOT re-enable the nudge when this
        session's own sentinel is present."""
        tmp_path, _home = _isolate_sentinel_and_fleet
        _touch_autonomous_sentinel(tmp_path, "s4")
        _write_fleet({"autonomous": False})
        result = nudge_em_code_dispatch.op(_op_payload("s4"))
        assert result is None


class TestNudgeEmCodeDispatchHandlerAutonomous:
    """Same properties through the async pcore-04 `_handler` op.

    House convention (coordinator_core/ops/tests/test_cutover_gate_handler.py):
    plain sync tests wrapping the handler in `asyncio.run(...)` — pytest-asyncio
    is deliberately absent from this tree (see pyproject.toml comment).
    """

    def test_no_fleet_file_no_sentinel_nudge_fires(self, _isolate_sentinel_and_fleet):
        params = {"session_id": "s5", "file_path": "foo.py"}
        result = asyncio.run(nudge_em_code_dispatch._handler(params))
        assert result["hookSpecificOutput"].get("additionalContext")

    def test_no_fleet_file_sentinel_present_suppressed(self, _isolate_sentinel_and_fleet):
        tmp_path, _home = _isolate_sentinel_and_fleet
        _touch_autonomous_sentinel(tmp_path, "s6")
        params = {"session_id": "s6", "file_path": "foo.py"}
        result = asyncio.run(nudge_em_code_dispatch._handler(params))
        assert result == {}

    def test_fleet_autonomous_on_does_not_override_absent_sentinel(
        self, _isolate_sentinel_and_fleet
    ):
        _write_fleet({"autonomous": True})
        params = {"session_id": "s7", "file_path": "foo.py"}
        result = asyncio.run(nudge_em_code_dispatch._handler(params))
        assert result["hookSpecificOutput"].get("additionalContext")


# ---------------------------------------------------------------------------
# postuse_advisory_dispatch -- `compaction_warnings` key, fleet-wins, and
# `autonomous` key, session-wins (runtime tripwire leg).
# ---------------------------------------------------------------------------


#: A real sidecar record always carries `context_window_size` alongside
#: `used_percentage` (see context_usage_sidecar's module docstring) --
#: `_check_context_pressure_sync` derives its token-runway bands
#: (`_ORANGE_RUNWAY_TOKENS`/`_RED_RUNWAY_TOKENS`, both back from
#: `window - _AUTO_COMPACT_RESERVE_TOKENS`) from that figure via
#: `_model_window_tokens`, and with it absent every reading in this file
#: resolved to `used_tokens is None` -- silent at every percentage, which is
#: why all 13 tests below read empty text regardless of the percentage
#: written. Chosen so `orange_bound_tokens` lands at an exact 30% of the
#: window (190_000 * 0.70 == 133_000 == reserve + orange runway): the 40/41
#: fixtures below (orange band) clear it with room, and the resulting
#: red_bound_tokens (87_000, ~45.79%) sits comfortably below the 47/48/50
#: fixtures (red band) and above the 40/41 pair -- not the literal legacy
#: 40%/43% cut (this module no longer computes fixed percentages, see
#: `_ORANGE_RUNWAY_TOKENS`'s own comment), just a window where this file's
#: existing percentage fixtures fall on the intended side of both bounds.
_WINDOW_TOKENS = 190_000


def _write_usage(session_id: str, used_percentage: float, now: float):
    from coordinator_core.session.context_usage_sidecar import write_usage

    write_usage(
        session_id,
        {
            "used_percentage": used_percentage,
            "remaining_percentage": 100 - used_percentage,
            "context_window_size": _WINDOW_TOKENS,
        },
        now=now,
    )


class TestContextPressureCompactionWarningsFleetWins:
    """`compaction_warnings` is a VARIANT SELECTOR, never an off switch:
    for every value the key admits, the function still returns non-empty
    advisory text at the 40% and 43% bands."""

    def test_no_fleet_file_standard_variant_in_the_red_band(self, _isolate_sentinel_and_fleet):
        """Baseline unchanged: no fleet file -> standard HANDOFF NOW text."""
        now = 1_000_000.0
        _write_usage("cp1", 50.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp1", "/does/not/matter/transcript.jsonl"
        )
        assert "HANDOFF NOW" in text
        assert "INFORMATIONAL" not in text

    def test_fleet_informational_selects_variant_in_the_red_band(self, _isolate_sentinel_and_fleet):
        """fleet-wins proof, through the hook entry point: a fleet
        compaction_warnings:informational value selects the informational
        variant even with no session-scoped sentinel for this key."""
        _write_fleet({"compaction_warnings": "informational"})
        now = 1_000_000.0
        _write_usage("cp2", 50.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp2", "/does/not/matter/transcript.jsonl"
        )
        assert text
        assert "INFORMATIONAL" in text
        # Matches the informational-variant text verbatim (postuse_advisory_
        # dispatch._check_context_pressure_sync's red-band branch) -- lower-
        # case and mid-sentence, not a standalone imperative.
        assert "commit and checkpoint now" in text

    def test_cloud_box_gets_the_informational_variant_with_no_config_at_all(
        self, _isolate_sentinel_and_fleet, monkeypatch
    ):
        """The whole point of the environment leg: on a box where `/handoff`
        is not an available remedy, the advisory stops recommending it WITHOUT
        anyone having set anything. Nothing to install, nothing to remember."""
        # ONE POSITIONAL `env`, matching the real signature: the registry
        # entry calls this with the caller's env, so a zero-arg stub answered
        # nothing -- it raised `TypeError` into the resolver's fail-open
        # `except`, and this test asserted against the STATIC default with the
        # leg it names never run. A stub whose arity does not match the thing
        # it stands in for pins the fallback, not the seam.
        monkeypatch.setattr(
            "coordinator_core.session.mode_resolution."
            "_compaction_default_for_environment",
            lambda env=None: "informational",
        )
        _write_usage("cp-cloud", 50.0, 1_000_000.0)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp-cloud", "/does/not/matter/transcript.jsonl"
        )
        assert "INFORMATIONAL" in text
        assert "HANDOFF NOW" not in text, (
            "a cloud box must not be told to run a ceremony it cannot run"
        )

    def test_an_explicit_fleet_value_still_beats_the_environment(
        self, _isolate_sentinel_and_fleet, monkeypatch
    ):
        """An operator who states a value wins over the environment's inference
        -- including stating `standard` on a box the environment reads as
        cloud."""
        monkeypatch.setattr(
            "coordinator_core.session.mode_resolution."
            "_compaction_default_for_environment",
            lambda env=None: "informational",
        )
        _write_fleet({"compaction_warnings": "standard"})
        _write_usage("cp-override", 50.0, 1_000_000.0)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp-override", "/does/not/matter/transcript.jsonl"
        )
        assert "HANDOFF NOW" in text

    def test_fleet_informational_selects_variant_at_40(self, _isolate_sentinel_and_fleet):
        _write_fleet({"compaction_warnings": "informational"})
        now = 1_000_000.0
        _write_usage("cp3", 41.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp3", "/does/not/matter/transcript.jsonl"
        )
        assert text
        assert "INFORMATIONAL" in text

    def test_fleet_standard_never_returns_empty_at_band(self, _isolate_sentinel_and_fleet):
        _write_fleet({"compaction_warnings": "standard"})
        now = 1_000_000.0
        _write_usage("cp4", 48.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp4", "/does/not/matter/transcript.jsonl"
        )
        assert text  # never "" -- selector, not an off switch

    def test_fleet_malformed_value_never_returns_empty_at_band(self, _isolate_sentinel_and_fleet):
        """A fleet value outside the declared enum degrades to the default
        ("standard") -- still non-empty advisory text, never "" ."""
        _write_fleet({"compaction_warnings": "silent"})
        now = 1_000_000.0
        _write_usage("cp5", 48.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp5", "/does/not/matter/transcript.jsonl"
        )
        assert text
        assert "HANDOFF NOW" in text  # degraded to standard

    def test_absent_key_never_returns_empty_at_band(self, _isolate_sentinel_and_fleet):
        """No fleet file at all (key entirely absent) -- still non-empty."""
        now = 1_000_000.0
        _write_usage("cp6", 40.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp6", "/does/not/matter/transcript.jsonl"
        )
        assert text

    def test_autonomous_sentinel_still_selects_informational_baseline(
        self, _isolate_sentinel_and_fleet
    ):
        """Baseline unchanged: the pre-existing autonomous-sentinel path
        (leg 1) still selects the informational variant with no fleet file
        involved at all."""
        tmp_path, _home = _isolate_sentinel_and_fleet
        _touch_autonomous_sentinel(tmp_path, "cp7")
        now = 1_000_000.0
        _write_usage("cp7", 47.0, now)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp7", "/does/not/matter/transcript.jsonl"
        )
        assert "INFORMATIONAL" in text


class TestModeKeysRegistryStillValid:
    def test_compaction_warnings_key_registered(self):
        assert "compaction_warnings" in MODE_KEYS
        assert MODE_KEYS["compaction_warnings"].precedence == "fleet-wins"

    def test_autonomous_key_registered(self):
        assert "autonomous" in MODE_KEYS
        assert MODE_KEYS["autonomous"].precedence == "session-wins"


class TestBatonAffordanceIsNamedInBothBands:
    """The session usually does not know it has a baton. Both informational
    bands must name it — 40 especially, where there is still runway to write a
    considered note rather than a hurried one."""

    def _with_baton(self, monkeypatch, tmp_path, sid):
        baton = tmp_path / f"{sid}-baton.json"
        baton.write_text("{}")
        monkeypatch.setattr(
            "coordinator_core.session_baton.store.baton_path",
            lambda s, cwd=None: baton,
        )
        return baton

    def test_orange_band_names_the_baton(self, _isolate_sentinel_and_fleet, monkeypatch):
        tmp_path = _isolate_sentinel_and_fleet[0]
        baton = self._with_baton(monkeypatch, tmp_path, "cp-40")
        _write_usage("cp-40", 41.0, 1_000_000.0)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp-40", "/does/not/matter/transcript.jsonl"
        )
        assert "INFORMATIONAL" in text
        assert str(baton) in text
        assert "baton.carry_forward" in text

    def test_red_band_informational_names_the_baton(
        self, _isolate_sentinel_and_fleet, monkeypatch
    ):
        tmp_path = _isolate_sentinel_and_fleet[0]
        baton = self._with_baton(monkeypatch, tmp_path, "cp-43")
        monkeypatch.setattr(
            "coordinator_core.session.mode_resolution."
            "_compaction_default_for_environment",
            lambda env=None: "informational",
        )
        _write_usage("cp-43", 50.0, 1_000_000.0)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp-43", "/does/not/matter/transcript.jsonl"
        )
        assert str(baton) in text

    def test_no_baton_on_disk_means_no_clause_not_a_broken_promise(
        self, _isolate_sentinel_and_fleet, monkeypatch
    ):
        """An advisory that names a file the reader cannot find teaches them to
        distrust the next one."""
        monkeypatch.setattr(
            "coordinator_core.session_baton.store.baton_path",
            lambda s, cwd=None: None,
        )
        _write_usage("cp-nobaton", 41.0, 1_000_000.0)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp-nobaton", "/does/not/matter/transcript.jsonl"
        )
        assert "INFORMATIONAL" in text
        assert "baton" not in text.lower()

    def test_the_40_band_still_reads_no_mode_keys(
        self, _isolate_sentinel_and_fleet, monkeypatch
    ):
        """PM ruling: 40 is informational for everyone. The baton clause is
        mode-independent and must not reintroduce a variant selection here."""
        def _explode(*a, **k):
            raise AssertionError("the 40 band must not resolve a mode key")

        monkeypatch.setattr(
            "coordinator_core.session.mode_resolution.resolve_mode", _explode
        )
        _write_usage("cp-nomode", 41.0, 1_000_000.0)
        text = postuse_advisory_dispatch._check_context_pressure_sync(
            "cp-nomode", "/does/not/matter/transcript.jsonl"
        )
        assert "INFORMATIONAL" in text


#: Marker distinguishing "no fleet file at all" from a fleet file whose
#: `compaction_warnings` value is the JSON null / Python `None` -- both are
#: legitimate cases in the cross product below and must not collapse into
#: one branch.
_NO_FLEET_FILE = object()

#: The full cross product this row pins: every fleet value class the key can
#: hold, valid or malformed, non-string included -- `compaction_warnings` is
#: a variant selector, never an off switch, so none of these may ever
#: produce empty advisory text at either band. `id=` labels keep pytest's
#: node ids readable instead of dumping raw objects/dicts into the name.
_FLEET_VALUE_CASES = [
    pytest.param(_NO_FLEET_FILE, id="no_fleet_file"),
    pytest.param("standard", id="standard"),
    pytest.param("informational", id="informational"),
    pytest.param("silent", id="out_of_enum_string"),
    pytest.param(True, id="bool_true"),
    pytest.param(1, id="int_one"),
    pytest.param(None, id="null"),
    pytest.param(["informational"], id="list"),
    pytest.param({"value": "informational"}, id="dict"),
]

#: Non-string classes (including the out-of-enum string) that must degrade
#: to the STANDARD variant at the red band -- never silently coerced to
#: `informational`. `_NO_FLEET_FILE`/"standard" already assert STANDARD via
#: the baseline tests above and are excluded here to avoid duplicating that
#: assertion under a different fixture id.
_DEGRADES_TO_STANDARD_IDS = {
    "out_of_enum_string",
    "bool_true",
    "int_one",
    "null",
    "list",
    "dict",
}


class TestCompactionWarningsFullCrossProduct:
    """C1: `compaction_warnings` is pinned to non-empty advisory text at
    both bands for every fleet value class -- valid, out-of-enum, and every
    non-string shape `_validate_value` can be handed. Malformed input
    degrades to the declared default and is never coerced to
    `informational`."""

    def _resolve(self, fleet_value, session_id, percentage):
        if fleet_value is not _NO_FLEET_FILE:
            _write_fleet({"compaction_warnings": fleet_value})
        _write_usage(session_id, percentage, 1_000_000.0)
        return postuse_advisory_dispatch._check_context_pressure_sync(
            session_id, "/does/not/matter/transcript.jsonl"
        )

    @pytest.mark.parametrize("fleet_value", _FLEET_VALUE_CASES)
    def test_orange_band_never_empty(self, _isolate_sentinel_and_fleet, fleet_value, request):
        session_id = f"xp-orange-{request.node.callspec.id}"
        text = self._resolve(fleet_value, session_id, 41.0)
        assert text

    @pytest.mark.parametrize("fleet_value", _FLEET_VALUE_CASES)
    def test_red_band_never_empty(self, _isolate_sentinel_and_fleet, fleet_value, request):
        session_id = f"xp-red-{request.node.callspec.id}"
        text = self._resolve(fleet_value, session_id, 48.0)
        assert text
        if request.node.callspec.id in _DEGRADES_TO_STANDARD_IDS:
            assert "HANDOFF NOW" in text
            assert "INFORMATIONAL" not in text

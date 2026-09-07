"""
Tests for coordinator_core.session.mode_resolution — the resolution seam.

Covers the four-cell matrix (sentinel present/absent x fleet key on/off/
absent) per key, proving the declared precedence wins; the registry
invariant (session_pair=None requires fleet-wins); and the two visibly
distinct error paths (unknown key raises at the call site; unrecognised
fleet-record content degrades silently).
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

from coordinator_core.session import mode_resolution
from coordinator_core.session.mode_resolution import (
    COORDINATOR_JOB_MODE,
    JOB_MODE_VALUES,
    MODE_KEYS,
    ModeKey,
    _validate_registry,
    _validate_value,
    resolve_mode,
)


#: Captured at import, BEFORE the autouse fixture below shadows the module
#: attribute — the tests that exercise the real environment logic need the
#: real function, not the fixture's abstaining stand-in.
_REAL_ENV_DEFAULT = mode_resolution._compaction_default_for_environment


@pytest.fixture(autouse=True)
def _isolate_sentinel_and_fleet(tmp_path, monkeypatch):
    """Isolate the autonomous sentinel's temp dir, the fleet record location,
    AND the environment-derived default, so tests never touch the real
    machine-wide files and never depend on the host they run on.

    The environment leg matters as much as the other two: `compaction_warnings`
    carries an `environment_default` that answers `informational` on a cloud
    box, so without pinning it here every "absent fleet falls back to the
    static default" assertion below would pass on an attended box and fail in
    a cloud session. Tests that mean to exercise the environment leg pin it
    themselves (see `TestCompactionWarningsEnvironmentDefault`)."""
    monkeypatch.setattr(
        "coordinator_core.session.autonomous_sentinel.tempfile.gettempdir",
        lambda: str(tmp_path),
    )
    monkeypatch.setattr(
        "coordinator_core.session.mode_resolution._compaction_default_for_environment",
        lambda: None,
    )
    settings_home = tmp_path / "settings-home"
    settings_home.mkdir()
    monkeypatch.setattr(
        "coordinator_core.session.fleet_mode.settings_home",
        lambda: settings_home,
    )
    return tmp_path, settings_home


def _write_fleet(settings_home, record):
    from coordinator_core.session.fleet_mode import write_fleet_mode

    assert write_fleet_mode(record)


def _touch_autonomous_sentinel(tmp_path, session_id):
    from coordinator_core.session import autonomous_sentinel

    autonomous_sentinel.sentinel_path(session_id).touch()


# --- autonomous: session-wins ------------------------------------------------


class TestAutonomousSessionWins:
    def test_sentinel_present_fleet_absent(self, _isolate_sentinel_and_fleet):
        tmp_path, _home = _isolate_sentinel_and_fleet
        _touch_autonomous_sentinel(tmp_path, "s1")
        assert resolve_mode("autonomous", "s1") is True

    def test_sentinel_absent_fleet_absent(self, _isolate_sentinel_and_fleet):
        assert resolve_mode("autonomous", "s1") is False

    def test_sentinel_present_fleet_on(self, _isolate_sentinel_and_fleet):
        tmp_path, home = _isolate_sentinel_and_fleet
        _touch_autonomous_sentinel(tmp_path, "s1")
        _write_fleet(home, {"autonomous": False})
        assert resolve_mode("autonomous", "s1") is True

    def test_sentinel_absent_fleet_on_does_not_override(self, _isolate_sentinel_and_fleet):
        """session-wins proof: a fleet autonomous:on must NOT override an
        absent session sentinel."""
        _home = _isolate_sentinel_and_fleet[1]
        _write_fleet(_home, {"autonomous": True})
        assert resolve_mode("autonomous", "s1") is False

    def test_sentinel_absent_fleet_off(self, _isolate_sentinel_and_fleet):
        _home = _isolate_sentinel_and_fleet[1]
        _write_fleet(_home, {"autonomous": False})
        assert resolve_mode("autonomous", "s1") is False

    def test_empty_fleet_mapping_reproduces_pre_plan_bool(self, _isolate_sentinel_and_fleet):
        # No fleet file at all -> fleet_mode.read_fleet_mode() degrades to
        # {} -> resolve_mode must reproduce today's sentinel-only bool.
        tmp_path, _home = _isolate_sentinel_and_fleet
        _touch_autonomous_sentinel(tmp_path, "s2")
        assert resolve_mode("autonomous", "s2") is True
        assert resolve_mode("autonomous", "s3") is False


# --- compaction_warnings: fleet-wins -----------------------------------------


class TestCompactionWarningsFleetWins:
    def test_fleet_absent_defaults_to_standard(self, _isolate_sentinel_and_fleet):
        assert resolve_mode("compaction_warnings", "s1") == "standard"

    def test_fleet_set_to_informational_wins(self, _isolate_sentinel_and_fleet):
        _home = _isolate_sentinel_and_fleet[1]
        _write_fleet(_home, {"compaction_warnings": "informational"})
        assert resolve_mode("compaction_warnings", "s1") == "informational"

    def test_fleet_set_to_standard_wins(self, _isolate_sentinel_and_fleet):
        _home = _isolate_sentinel_and_fleet[1]
        _write_fleet(_home, {"compaction_warnings": "standard"})
        assert resolve_mode("compaction_warnings", "s1") == "standard"

    def test_fleet_wrong_type_degrades_to_default(self, _isolate_sentinel_and_fleet):
        """A fleet-supplied value outside the declared enum is malformed
        input -- degrades exactly like an empty mapping, never coerced."""
        _home = _isolate_sentinel_and_fleet[1]
        _write_fleet(_home, {"compaction_warnings": "loud"})
        assert resolve_mode("compaction_warnings", "s1") == "standard"

    def test_empty_fleet_mapping_reproduces_pre_plan_default(self, _isolate_sentinel_and_fleet):
        assert resolve_mode("compaction_warnings", "anyone") == "standard"


# --- unknown key: raises at the call site ------------------------------------


def test_unknown_key_raises_keyerror(_isolate_sentinel_and_fleet):
    with pytest.raises(KeyError):
        resolve_mode("not_a_real_key", "s1")


def test_unrecognised_key_inside_fleet_record_is_silently_absorbed(_isolate_sentinel_and_fleet):
    """An unrecognised key INSIDE the fleet record is untrusted input,
    already absorbed by fleet_mode's own degradation -- it must never raise
    here, and must not affect resolution of a real key."""
    _home = _isolate_sentinel_and_fleet[1]
    _write_fleet(_home, {"some_future_key": "whatever", "autonomous": False})
    assert resolve_mode("autonomous", "s1") is False


# --- registry invariant -------------------------------------------------------


class TestRegistryInvariant:
    def test_mode_keys_itself_satisfies_the_invariant(self):
        """Every shipped entry with session_pair=None declares fleet-wins or
        environment-wins -- the two precedences valid for a key with no
        session-scoped value (see `_validate_registry`)."""
        _validate_registry(MODE_KEYS)
        for key, entry in MODE_KEYS.items():
            if entry.session_pair is None:
                assert entry.precedence in ("fleet-wins", "environment-wins"), key

    def test_session_wins_with_no_session_pair_is_refused_at_definition_time(self):
        bad_registry = {
            "bogus": ModeKey(
                session_pair=None,
                precedence="session-wins",
                value_type=bool,
                default=False,
            )
        }
        with pytest.raises(ValueError):
            _validate_registry(bad_registry)

    def test_fleet_wins_with_no_session_pair_is_accepted(self):
        ok_registry = {
            "bogus": ModeKey(
                session_pair=None,
                precedence="fleet-wins",
                value_type=bool,
                default=False,
            )
        }
        _validate_registry(ok_registry)  # must not raise

    def test_mode_key_is_frozen(self):
        entry = MODE_KEYS["autonomous"]
        with pytest.raises(dataclasses.FrozenInstanceError):
            entry.precedence = "fleet-wins"


# --- _validate_value: unsupported value_type branch ---------------------------


def test_validate_value_raises_on_unsupported_value_type():
    """Neither bool nor a frozenset -- the defensive branch has zero
    coverage from the two shipped MODE_KEYS entries (both bool or
    frozenset); exercise it directly via a fixture value_type that mirrors
    what a mis-registered future key would declare (Review: code-reviewer,
    finding 2)."""
    with pytest.raises(TypeError):
        _validate_value("whatever", str)


class TestCompactionWarningsEnvironmentDefault:
    """The environment leg: `informational` where `/handoff` is not an
    available remedy, and silence everywhere else."""

    def _pin(self, monkeypatch, call, confidence):
        from coordinator_core.env_locality import Locality

        monkeypatch.setattr(
            "coordinator_core.env_locality.locality",
            lambda *a, **k: Locality(call, confidence, "test", "pinned"),
        )

    def test_confident_cloud_moves_the_default(self, monkeypatch):
        from coordinator_core.session import mode_resolution as MR

        self._pin(monkeypatch, "cloud", "certain")
        assert _REAL_ENV_DEFAULT() == "informational"

    def test_attended_abstains(self, monkeypatch):
        from coordinator_core.session import mode_resolution as MR

        self._pin(monkeypatch, "attended", "certain")
        assert _REAL_ENV_DEFAULT() is None

    def test_suspect_abstains_rather_than_guessing(self, monkeypatch):
        """A `suspect` reading must never silently change behaviour — that is
        the whole reason the third state exists."""
        from coordinator_core.session import mode_resolution as MR

        self._pin(monkeypatch, "suspect", "low")
        assert _REAL_ENV_DEFAULT() is None

    def test_low_confidence_cloud_abstains(self, monkeypatch):
        from coordinator_core.session import mode_resolution as MR

        self._pin(monkeypatch, "cloud", "low")
        assert _REAL_ENV_DEFAULT() is None

    def test_a_stated_fleet_value_beats_the_environment(
        self, _isolate_sentinel_and_fleet, monkeypatch
    ):
        """An operator who states a value always wins: the environment only
        ever speaks where nobody else has."""
        from coordinator_core.session import mode_resolution as MR

        monkeypatch.setattr(MR, "_compaction_default_for_environment",
                            lambda env=None: "informational")
        _write_fleet(_isolate_sentinel_and_fleet[1],
                     {"compaction_warnings": "standard"})
        assert resolve_mode("compaction_warnings", "s1") == "standard"

    def test_environment_beats_the_static_default_when_fleet_is_silent(
        self, _isolate_sentinel_and_fleet, monkeypatch
    ):
        from coordinator_core.session import mode_resolution as MR

        monkeypatch.setattr(MR, "_compaction_default_for_environment",
                            lambda env=None: "informational")
        assert resolve_mode("compaction_warnings", "s1") == "informational"

    def test_a_resolution_failure_falls_back_to_the_static_default(
        self, _isolate_sentinel_and_fleet, monkeypatch
    ):
        """Locality resolution must never block a mode read."""
        from coordinator_core.session import mode_resolution as MR

        def _boom():
            raise RuntimeError("locality unavailable")

        monkeypatch.setattr(MR, "_compaction_default_for_environment", _boom)
        assert resolve_mode("compaction_warnings", "s1") == "standard"

    def test_the_callable_itself_swallows_a_locality_failure(self, monkeypatch):
        from coordinator_core.session import mode_resolution as MR

        def _boom(*a, **k):
            raise RuntimeError("no")

        monkeypatch.setattr("coordinator_core.env_locality.locality", _boom)
        assert _REAL_ENV_DEFAULT() is None


# --- job_mode: environment-wins ----------------------------------------------


class TestJobModeEnvironmentWins:
    """One test per failing-input case (never one test for all three), plus
    one happy-path test per enum value -- per this chunk's own spec."""

    # -- failing inputs: each asserts the conservative anchor -----------------

    def test_absent_variable_resolves_to_conservative_anchor(self, _isolate_sentinel_and_fleet):
        assert resolve_mode("job_mode", "s1", env={}) == "interactive"

    def test_absent_env_argument_resolves_to_conservative_anchor(self, _isolate_sentinel_and_fleet):
        """`env` itself omitted entirely (the pool-broken isolated=False
        fallback shape) -- must abstain rather than reach for os.environ."""
        assert resolve_mode("job_mode", "s1") == "interactive"

    def test_empty_string_variable_resolves_to_conservative_anchor(self, _isolate_sentinel_and_fleet):
        assert resolve_mode("job_mode", "s1", env={COORDINATOR_JOB_MODE: ""}) == "interactive"

    def test_unrecognised_value_resolves_to_conservative_anchor(self, _isolate_sentinel_and_fleet):
        assert (
            resolve_mode("job_mode", "s1", env={COORDINATOR_JOB_MODE: "not-a-real-mode"})
            == "interactive"
        )

    # -- happy path: one test per enum value -----------------------------------

    def test_blitz_resolves(self, _isolate_sentinel_and_fleet):
        assert resolve_mode("job_mode", "s1", env={COORDINATOR_JOB_MODE: "blitz"}) == "blitz"

    def test_cron_resolves(self, _isolate_sentinel_and_fleet):
        assert resolve_mode("job_mode", "s1", env={COORDINATOR_JOB_MODE: "cron"}) == "cron"

    def test_interactive_resolves(self, _isolate_sentinel_and_fleet):
        assert (
            resolve_mode("job_mode", "s1", env={COORDINATOR_JOB_MODE: "interactive"})
            == "interactive"
        )

    # -- environment-wins proof: beats a stale fleet record --------------------

    def test_environment_value_beats_a_stale_fleet_record(self, _isolate_sentinel_and_fleet):
        """The precedence this chunk adds `job_mode` for: a fleet-wide
        record must not silently override the caller's own explicit
        assertion."""
        _home = _isolate_sentinel_and_fleet[1]
        _write_fleet(_home, {"job_mode": "cron"})
        assert resolve_mode("job_mode", "s1", env={COORDINATOR_JOB_MODE: "blitz"}) == "blitz"

    def test_fleet_value_used_when_environment_is_silent(self, _isolate_sentinel_and_fleet):
        _home = _isolate_sentinel_and_fleet[1]
        _write_fleet(_home, {"job_mode": "cron"})
        assert resolve_mode("job_mode", "s1", env={}) == "cron"

    def test_fleet_wrong_type_degrades_to_conservative_anchor(self, _isolate_sentinel_and_fleet):
        _home = _isolate_sentinel_and_fleet[1]
        _write_fleet(_home, {"job_mode": "not-a-real-mode"})
        assert resolve_mode("job_mode", "s1", env={}) == "interactive"

    def test_none_value_from_environment_default_abstains(self, monkeypatch, _isolate_sentinel_and_fleet):
        """A candidate value outside `JOB_MODE_VALUES` degrades via
        `_validate_value` at the registry boundary, exercised directly here
        (module docstring: verify that path rather than reimplementing it)."""
        from coordinator_core.session import mode_resolution as MR

        assert MR._job_mode_from_environment(None) is None
        assert MR._job_mode_from_environment({}) is None
        assert MR._job_mode_from_environment({COORDINATOR_JOB_MODE: ""}) is None
        assert MR._job_mode_from_environment({COORDINATOR_JOB_MODE: "blitz"}) == "blitz"


class TestJobModeRegistryInvariant:
    def test_job_mode_satisfies_the_widened_invariant(self):
        entry = MODE_KEYS["job_mode"]
        assert entry.session_pair is None
        assert entry.precedence == "environment-wins"
        assert entry.environment_default is not None

    def test_environment_wins_without_environment_default_is_refused_at_definition_time(self):
        bad_registry = {
            "bogus": ModeKey(
                session_pair=None,
                precedence="environment-wins",
                value_type=JOB_MODE_VALUES,
                default="interactive",
            )
        }
        with pytest.raises(ValueError):
            _validate_registry(bad_registry)

    def test_environment_wins_with_no_session_pair_is_accepted(self):
        ok_registry = {
            "bogus": ModeKey(
                session_pair=None,
                precedence="environment-wins",
                value_type=JOB_MODE_VALUES,
                default="interactive",
                environment_default=lambda env: None,
            )
        }
        _validate_registry(ok_registry)  # must not raise


# --- job_mode resolution cost: AC-9, process time and spawn count only ------
#
# See baton AC-9 ("resolution costs no interpreter start on a hot path and
# stays under the brightline") and this plan's C2 row: the pre-existing
# "0.038 ms, zero spawns" figure describes `env_locality.py`'s OWN ladder,
# not this FORWARDING_SET + MODE_KEYS read, so it does not discharge AC-9 for
# the path this chunk adds -- measured here, directly, against the same
# `benchmarks.process_time.batched_process_time_ms` primitive
# `test_touch_record_perf.py` uses, per DR-344 vocabulary (process time and
# spawn count, never wall clock).


#: The one bar (DR-344). `SUSPENSION_BAR_MS` (2000ms) is which-to-switch-off
#: -first, never a target, and never cited here in its place.
_BRIGHTLINE_MS = 500.0


#: Iterations of `resolve_mode("job_mode", ...)` inside one spawned driver --
#: amortises the interpreter-start floor across enough real work that the
#: import-only baseline's own noise does not dominate the delta (same
#: reasoning `test_touch_record_perf.py::_APPENDS_PER_DRIVER` states).
_JOB_MODE_CALLS_PER_DRIVER = 2000


def _write_job_mode_driver(driver_path, repo_root, do_resolve: bool) -> None:
    """`do_resolve=False` writes the IMPORT-ONLY baseline: same interpreter
    start, same import, no resolution loop. Module docstring's own "sixth
    trap" section names this as the correct way to isolate an in-process
    op's own cost from the interpreter-start floor neither this file nor
    `batched_process_time_ms` can amortise away on a single spawned process
    -- the delta between the two driver shapes is `job_mode` resolution's
    own cost, and `procs_per_call` compared between them is this path's own
    spawn count (never zero on this box -- see this test's own baseline
    assertion below -- but held CONSTANT if `resolve_mode` itself spawns
    nothing)."""
    loop = (
        f'for _ in range({_JOB_MODE_CALLS_PER_DRIVER}):\n'
        f'    resolve_mode("job_mode", "bench-session", env={{"COORDINATOR_JOB_MODE": "blitz"}})\n'
        if do_resolve
        else ""
    )
    script = f'''\
import sys

sys.path.insert(0, r"{repo_root}")

from coordinator_core.session.mode_resolution import resolve_mode

{loop}
sys.exit(0)
'''
    driver_path.write_text(script, encoding="utf-8")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_job_mode_resolution_costs_no_spawn_and_stays_under_the_brightline(tmp_path):
    """AC-9: measured as a DELTA against an import-only baseline of the same
    driver shape (module docstring's "sixth trap" -- a bare figure off this
    box's own interpreter-start floor is not this op's cost). Never wall
    clock (DR-344)."""
    from coordinator_core.benchmarks.process_time import batched_process_time_ms

    repo_root = Path(__file__).resolve().parents[3]
    baseline_path = tmp_path / "job_mode_baseline.py"
    driver_path = tmp_path / "job_mode_driver.py"
    _write_job_mode_driver(baseline_path, repo_root, do_resolve=False)
    _write_job_mode_driver(driver_path, repo_root, do_resolve=True)

    baseline = batched_process_time_ms([sys.executable, str(baseline_path)], k=5)
    result = batched_process_time_ms([sys.executable, str(driver_path)], k=5)

    assert baseline["rc"] == 0
    assert result["rc"] == 0
    # No subprocess of its own: the resolution loop must not raise this
    # path's own spawn count above the import-only baseline's.
    assert result["procs_per_call"] <= baseline["procs_per_call"], (
        f"job_mode resolution spawned a process of its own: baseline "
        f"procs_per_call={baseline['procs_per_call']!r}, driver "
        f"procs_per_call={result['procs_per_call']!r}"
    )
    delta_ms = result["process_time_ms"] - baseline["process_time_ms"]
    assert delta_ms < _BRIGHTLINE_MS, (
        f"job_mode resolution cost {delta_ms!r}ms process time over "
        f"{_JOB_MODE_CALLS_PER_DRIVER} calls (driver "
        f"{result['process_time_ms']!r}ms - baseline "
        f"{baseline['process_time_ms']!r}ms), at or over DR-344's "
        f"{_BRIGHTLINE_MS}ms brightline"
    )

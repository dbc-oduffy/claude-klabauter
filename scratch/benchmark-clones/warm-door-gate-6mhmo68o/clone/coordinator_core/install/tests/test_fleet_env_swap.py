"""
coordinator_core.install.tests.test_fleet_env_swap — C2/C3 of the junction-
publication plan: `_swap_in_new_env` retargets the `env_root` junction
instead of renaming a real directory, `_sweep_orphaned_swap_dirs` never
sweeps the currently-published generation, and `ensure_fleet_env` repairs a
torn publish (C3) with one `create_junction` call rather than a full rebuild.

Purpose: proves the defect this plan exists to fix is actually fixed, not
merely relocated. The old rename-swap failed `os.rename` outright under any
plain-open reader handle (WinError 5, measured — see `fleet_env.py`'s module
docstring); `test_ac4_...` below reproduces exactly that reader shape against
the NEW mechanism and asserts the swap succeeds, the held handle keeps
reading the OLD generation, and a fresh read gets the NEW one — the AC4
quadruple this whole plan is testing.

No platform gate is needed on the AC4 test: `coordinator_core.install.junction`
already branches nt (`_winapi.CreateJunction`) vs posix (`os.symlink`)
internally, and the one place OS behaviour genuinely differs for THIS test —
whether `shutil.rmtree` of a directory containing an open file handle
succeeds immediately (posix) or fails and defers (nt, WinError sharing
violation) — is exercised identically on both platforms by asserting
reclaim only AFTER an explicit `_sweep_orphaned_swap_dirs` call following
handle-close, which is a no-op if posix already reclaimed it inline.

Spec backlink: docs/plans/2026-08-20-the-fleet-env-publishes-through-a-juncti.md § C2, C3

Negative-spec:
    - Does NOT touch the real fleet environment or spawn `uv` — every test
      builds synthetic generation directories under `tmp_path` and, where a
      health probe is needed, points a fake `bin/python` at `sys.executable`
      via `LOCK_PYTHON_MINOR`/`_FLEET_ENV_IMPORT_PROBES` monkeypatches (same
      pattern as `test_fleet_env_healthy_minor_check.py`).
    - Does NOT exercise the C4 cutover from a real pre-junction directory —
      `test_swap_refuses_when_env_root_is_a_real_directory` proves
      `_swap_in_new_env` refuses that case rather than silently mis-handling
      it; C4 owns the actual cutover.
    - Does NOT re-test `_fleet_env_healthy`'s minor/import-probe contract —
      that is `test_fleet_env_healthy_minor_check.py`'s surface; this file
      only reuses the same fixture shape to build a "healthy" generation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coordinator_core.install import fleet_env, junction


def _current_minor_string() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _stub_health_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `_fleet_env_healthy` pass for any generation built by
    `_make_healthy_generation` — mirrors
    `test_fleet_env_healthy_minor_check.py`'s fixture, minus its
    `_is_windows_shell` stub: `_env_python_path`'s real (unstubbed) branch
    is used deliberately here so the fake interpreter lands at the exact
    path `is_executable` accepts on this platform (on nt that means a
    `Scripts/python.exe`-suffixed symlink — a bare `python` symlink reads as
    non-executable under nt's PATHEXT-based check)."""
    monkeypatch.setattr(fleet_env, "LOCK_PYTHON_MINOR", _current_minor_string())
    monkeypatch.setattr(fleet_env, "_FLEET_ENV_IMPORT_PROBES", ())


def _make_healthy_generation(base_dir: Path, name: str) -> Path:
    gen = base_dir / name
    python_bin = fleet_env._env_python_path(gen)
    python_bin.parent.mkdir(parents=True)
    python_bin.symlink_to(Path(sys.executable))
    return gen


def _write_marker(dir_path: Path, content: str) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "marker.txt").write_text(content, encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------
# C2 — _swap_in_new_env publishes by retargeting the junction.
# --------------------------------------------------------------------------


def test_first_ever_publish_creates_junction_with_no_prior_to_restore(tmp_path):
    """No prior publish (env_root does not exist at all) — the common case
    on a brand-new box. Nothing to restore on failure; the happy path just
    creates the junction."""
    env_root = tmp_path / "fleet-env"
    gen_new = tmp_path / "fleet-env.gen-new"
    gen_new.mkdir()

    fleet_env._swap_in_new_env(env_root, gen_new)

    assert junction.is_junction(env_root)
    assert junction.junction_target(env_root).resolve() == gen_new.resolve()


def test_retarget_replaces_prior_generation(tmp_path):
    env_root = tmp_path / "fleet-env"
    gen_old = tmp_path / "fleet-env.gen-old"
    gen_new = tmp_path / "fleet-env.gen-new"
    gen_old.mkdir()
    gen_new.mkdir()
    junction.create_junction(env_root, gen_old)

    fleet_env._swap_in_new_env(env_root, gen_new)

    assert junction.is_junction(env_root)
    assert junction.junction_target(env_root).resolve() == gen_new.resolve()
    # Reclaimed once nothing holds it open.
    assert not gen_old.exists()


def test_swap_refuses_when_env_root_is_a_real_directory(tmp_path):
    """Pre-junction real-directory layout (today's shape, before C4's
    cutover has run) is not this function's problem to solve — it must
    refuse loudly rather than silently mis-handle a case C4 owns."""
    env_root = tmp_path / "fleet-env"
    env_root.mkdir()
    gen_new = tmp_path / "fleet-env.gen-new"
    gen_new.mkdir()

    with pytest.raises(fleet_env.FleetEnvError):
        fleet_env._swap_in_new_env(env_root, gen_new)


# --------------------------------------------------------------------------
# AC4 — the test that matters: a real open reader handle across a full swap.
# --------------------------------------------------------------------------


def test_ac4_open_reader_handle_survives_full_swap(tmp_path):
    env_root = tmp_path / "fleet-env"
    gen_old = tmp_path / "fleet-env.gen-old"
    gen_new = tmp_path / "fleet-env.gen-new"
    _write_marker(gen_old, "OLD-CONTENT")
    _write_marker(gen_new, "NEW-CONTENT")

    junction.create_junction(env_root, gen_old)

    held_handle = open(gen_old / "marker.txt", "r", encoding="utf-8")
    try:
        # (a) the swap itself must succeed — the exact operation the old
        # rename-swap raised WinError 5 on under this reader shape.
        fleet_env._swap_in_new_env(env_root, gen_new)

        # (b) the still-open handle keeps reading the OLD content.
        held_handle.seek(0)
        assert held_handle.read() == "OLD-CONTENT"

        # (c) a fresh open through env_root reads the NEW content.
        assert (env_root / "marker.txt").read_text(encoding="utf-8") == "NEW-CONTENT"
    finally:
        held_handle.close()

    # (d)/(f) once the handle closes, the old generation reclaims. On posix
    # this already happened inline during the swap; on nt the swap's own
    # reclaim attempt was deferred (sharing violation while the handle was
    # open), so an explicit sweep (what the next rebuild would run anyway)
    # finishes it. Either way, this assertion is the same on both platforms.
    fleet_env._sweep_orphaned_swap_dirs(env_root, assume_build_lock_held=True)
    assert not gen_old.exists()


# --------------------------------------------------------------------------
# AC5 — restore-on-failure guard.
# --------------------------------------------------------------------------


def test_ac5_restore_guard_repoints_at_previous_generation_and_reraises(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    env_root = tmp_path / "fleet-env"
    gen_old = tmp_path / "fleet-env.gen-old"
    gen_new = tmp_path / "fleet-env.gen-new"
    gen_old.mkdir()
    gen_new.mkdir()
    junction.create_junction(env_root, gen_old)

    original_create = junction.create_junction
    calls = {"n": 0}

    def _flaky_create(link, target):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated create_junction failure")
        return original_create(link, target)

    monkeypatch.setattr(junction, "create_junction", _flaky_create)

    with pytest.raises(OSError):
        fleet_env._swap_in_new_env(env_root, gen_new)

    assert calls["n"] == 2, "expected exactly one failed attempt then one restore attempt"
    assert junction.is_junction(env_root)
    assert junction.junction_target(env_root).resolve() == gen_old.resolve()


def test_ac5_competing_publisher_planting_our_target_is_treated_as_benign(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Slice-A review finding 1: C3's lock-free self-repair (or a second
    legitimate publisher) can retarget `env_root` at OUR `build_dir` in the
    window between `remove_junction` and this function's own
    `create_junction` call. That is not a failure — it means someone else
    already finished the publish we were doing — so the swap must report
    success (env_root ends up a junction at build_dir) rather than
    attempting a restore that would undo a publish that already landed."""
    env_root = tmp_path / "fleet-env"
    gen_old = tmp_path / "fleet-env.gen-old"
    gen_new = tmp_path / "fleet-env.gen-new"
    gen_old.mkdir()
    gen_new.mkdir()
    junction.create_junction(env_root, gen_old)

    original_create = junction.create_junction

    def _competing_publisher_plants_our_target(link, target):
        # Simulate a second process's create_junction landing first, at
        # exactly the target we were about to publish — then our own call
        # observes "already exists".
        original_create(link, gen_new)
        raise OSError("simulated: name already exists (competing publisher)")

    monkeypatch.setattr(junction, "create_junction", _competing_publisher_plants_our_target)

    fleet_env._swap_in_new_env(env_root, gen_new)  # must not raise

    assert junction.is_junction(env_root)
    assert junction.junction_target(env_root).resolve() == gen_new.resolve()


def test_ac5_restore_itself_failing_raises_combined_cause_never_masked(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Slice-A review finding 1's WARN: if the restore attempt back to the
    previous generation ALSO fails, that second exception must not silently
    supersede the first — both causes are named explicitly in one raised
    error, not left to implicit `__context__` chaining `ensure_fleet_env`'s
    wrapper would not surface."""
    env_root = tmp_path / "fleet-env"
    gen_old = tmp_path / "fleet-env.gen-old"
    gen_new = tmp_path / "fleet-env.gen-new"
    gen_old.mkdir()
    gen_new.mkdir()
    junction.create_junction(env_root, gen_old)

    def _always_fails(link, target):
        raise OSError(f"simulated failure targeting {target}")

    monkeypatch.setattr(junction, "create_junction", _always_fails)

    with pytest.raises(fleet_env.FleetEnvError) as excinfo:
        fleet_env._swap_in_new_env(env_root, gen_new)

    message = str(excinfo.value)
    assert str(gen_new) in message or "gen-new" in message
    assert str(gen_old) in message or "gen-old" in message


def test_env_root_never_left_absent_under_a_racing_repair(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end shape of the slice-A finding: a legitimate publish racing
    a competing junction-plant at the same name either succeeds outright or
    fails with both causes named — and `env_root` is never left absent
    either way; it always resolves to SOME healthy generation."""
    env_root = tmp_path / "fleet-env"
    gen_old = tmp_path / "fleet-env.gen-old"
    gen_new = tmp_path / "fleet-env.gen-new"
    gen_old.mkdir()
    gen_new.mkdir()
    junction.create_junction(env_root, gen_old)

    original_create = junction.create_junction

    def _competing_publisher_wins_the_name(link, target):
        original_create(link, gen_new)
        raise OSError("simulated: name already exists (competing publisher)")

    monkeypatch.setattr(junction, "create_junction", _competing_publisher_wins_the_name)

    fleet_env._swap_in_new_env(env_root, gen_new)

    assert junction.is_junction(env_root)
    assert junction.junction_target(env_root) is not None


# --------------------------------------------------------------------------
# C2 — _sweep_orphaned_swap_dirs never sweeps the published generation.
# --------------------------------------------------------------------------


def test_sweep_excludes_the_currently_published_generation(tmp_path):
    env_root = tmp_path / "fleet-env"
    gen_live = tmp_path / "fleet-env.gen-live"
    gen_orphan = tmp_path / "fleet-env.gen-orphan"
    gen_live.mkdir()
    gen_orphan.mkdir()
    junction.create_junction(env_root, gen_live)

    fleet_env._sweep_orphaned_swap_dirs(env_root, assume_build_lock_held=True)

    assert gen_live.exists()
    assert junction.is_junction(env_root)
    assert not gen_orphan.exists()


def test_sweep_is_a_noop_when_env_root_has_no_generation_siblings(tmp_path):
    env_root = tmp_path / "fleet-env"
    gen_live = tmp_path / "fleet-env.gen-live"
    gen_live.mkdir()
    junction.create_junction(env_root, gen_live)

    fleet_env._sweep_orphaned_swap_dirs(env_root, assume_build_lock_held=True)  # must not raise

    assert gen_live.exists()


# --------------------------------------------------------------------------
# C3 — torn-publish detection and self-repair.
# --------------------------------------------------------------------------


def test_env_root_absent_true_when_nothing_at_the_name(tmp_path):
    assert fleet_env._env_root_absent(tmp_path / "fleet-env") is True


def test_env_root_absent_false_for_a_junction(tmp_path):
    env_root = tmp_path / "fleet-env"
    gen = tmp_path / "fleet-env.gen-a"
    gen.mkdir()
    junction.create_junction(env_root, gen)
    assert fleet_env._env_root_absent(env_root) is False


def test_env_root_absent_false_for_a_real_directory(tmp_path):
    env_root = tmp_path / "fleet-env"
    env_root.mkdir()
    assert fleet_env._env_root_absent(env_root) is False


def test_find_torn_publish_generation_returns_healthy_candidate(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    _stub_health_probe(monkeypatch)
    env_root = tmp_path / "fleet-env"
    gen = _make_healthy_generation(tmp_path, "fleet-env.gen-abc")

    found = fleet_env._find_torn_publish_generation(env_root)

    assert found is not None
    assert found.resolve() == gen.resolve()


def test_find_torn_publish_generation_none_when_no_candidate_is_healthy(tmp_path):
    env_root = tmp_path / "fleet-env"
    unhealthy = tmp_path / "fleet-env.gen-broken"
    unhealthy.mkdir()  # no bin/python at all -> never healthy

    assert fleet_env._find_torn_publish_generation(env_root) is None


def test_ensure_fleet_env_repairs_torn_publish_without_rebuilding(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """C3's whole point: a torn publish costs one create_junction call, not
    a `uv sync`. `_provision_uv_environment` is stubbed to fail the test if
    called at all."""
    _stub_health_probe(monkeypatch)
    env_root = tmp_path / "fleet-env"
    _make_healthy_generation(tmp_path, "fleet-env.gen-abc")

    monkeypatch.setattr(fleet_env, "resolve_environment_root", lambda **_: env_root)

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("torn-publish repair must not provision a fresh environment")

    monkeypatch.setattr(fleet_env, "_provision_uv_environment", _must_not_run)

    status = fleet_env.ensure_fleet_env(
        settings_home_factory=lambda: tmp_path / "settings-home"
    )

    assert status == "ready"
    assert junction.is_junction(env_root)


def test_check_only_does_not_repair_torn_publish(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """`check_only=True`'s contract is no mutation at all — repair is a
    disk mutation (creates a junction), so it must not run under check_only,
    even though the repair itself is cheap."""
    _stub_health_probe(monkeypatch)
    env_root = tmp_path / "fleet-env"
    _make_healthy_generation(tmp_path, "fleet-env.gen-abc")

    monkeypatch.setattr(fleet_env, "resolve_environment_root", lambda **_: env_root)

    status = fleet_env.ensure_fleet_env(
        check_only=True, settings_home_factory=lambda: tmp_path / "settings-home"
    )

    assert status == "would-rebuild"
    assert not junction.is_junction(env_root)
    assert not env_root.exists()

"""
coordinator_core.install.tests.test_fleet_env_cutover — C4 of the junction-
publication plan: `_cutover_to_junction_layout` moves `env_root` from
today's real-directory layout to the junction layout `_swap_in_new_env`
(C2/C3) requires, exactly once per environment root.

Purpose: proves the one-time bootstrapping move is safe under the same
transient-reader-handle shape the rest of the plan measures (WinError 5,
`PermissionError` with `winerror == 5` under `nt`), that it never loops
unboundedly or forces the rename, and that a failure partway through never
leaves `env_root` absent.

Spec backlink: docs/plans/2026-08-20-the-fleet-env-publishes-through-a-juncti.md § C4

Negative-spec:
    - Does NOT touch the real fleet environment or spawn `uv` — every test
      builds synthetic directories under `tmp_path`.
    - Does NOT re-test `_swap_in_new_env`'s publish mechanics
      (`test_fleet_env_swap.py`'s surface) — this file only exercises the
      real-directory -> junction bootstrapping move that mechanism cannot
      perform on itself.
    - Does NOT re-run the N=70 background-reader load probe the plan's retry
      budget was measured against — that probe is itself real load on a
      50-70-session box and must not be re-run casually (see
      `fleet_env._CUTOVER_RETRY_BUDGET_SECS`'s docstring).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from coordinator_core.install import fleet_env, junction


def _write_marker(dir_path: Path, content: str) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "marker.txt").write_text(content, encoding="utf-8", newline="\n")


def _permission_error(winerror: "int | None") -> PermissionError:
    exc = PermissionError("simulated transient reader-handle rename failure")
    if winerror is not None:
        exc.winerror = winerror  # type: ignore[attr-defined]
    return exc


# --------------------------------------------------------------------------
# Idempotence — already a junction is a no-op (AC7 re-runs safely).
# --------------------------------------------------------------------------


def test_already_junction_is_a_noop(tmp_path):
    env_root = tmp_path / "fleet-env"
    gen = tmp_path / "fleet-env.gen-existing"
    _write_marker(gen, "CONTENT")
    junction.create_junction(env_root, gen)

    outcome = fleet_env._cutover_to_junction_layout(env_root)

    assert outcome.status == "already-junction"
    assert outcome.generation.resolve() == gen.resolve()
    assert junction.junction_target(env_root).resolve() == gen.resolve()
    assert (env_root / "marker.txt").read_text(encoding="utf-8") == "CONTENT"


def test_reruns_after_a_successful_cutover_are_still_a_noop(tmp_path):
    env_root = tmp_path / "fleet-env"
    _write_marker(env_root, "CONTENT")

    first = fleet_env._cutover_to_junction_layout(env_root)
    assert first.status == "cutover"

    second = fleet_env._cutover_to_junction_layout(env_root)

    assert second.status == "already-junction"
    assert second.generation.resolve() == first.generation.resolve()


# --------------------------------------------------------------------------
# Real-directory cutover succeeds and reads through afterwards.
# --------------------------------------------------------------------------


def test_real_directory_cutover_succeeds_and_reads_through(tmp_path):
    env_root = tmp_path / "fleet-env"
    _write_marker(env_root, "REAL-DIR-CONTENT")

    outcome = fleet_env._cutover_to_junction_layout(env_root)

    assert outcome.status == "cutover"
    assert junction.is_junction(env_root)
    assert outcome.generation is not None
    assert outcome.generation.name.startswith("fleet-env.gen-")
    assert (env_root / "marker.txt").read_text(encoding="utf-8") == "REAL-DIR-CONTENT"
    assert (outcome.generation / "marker.txt").read_text(encoding="utf-8") == "REAL-DIR-CONTENT"


def test_absent_env_root_raises_with_nothing_to_cut_over(tmp_path):
    env_root = tmp_path / "fleet-env"

    with pytest.raises(fleet_env.FleetEnvError):
        fleet_env._cutover_to_junction_layout(env_root)

    assert not env_root.exists()


# --------------------------------------------------------------------------
# Retry-exhausted path: named remediation, no mutation.
# --------------------------------------------------------------------------


def test_retry_exhausted_raises_named_remediation_and_mutates_nothing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    env_root = tmp_path / "fleet-env"
    _write_marker(env_root, "UNTOUCHED")

    monkeypatch.setattr(os, "name", "nt", raising=False)

    def _always_blocked(*_args, **_kwargs):
        raise _permission_error(5)

    monkeypatch.setattr(fleet_env.os, "rename", _always_blocked)

    with pytest.raises(fleet_env.FleetEnvCutoverBlocked) as excinfo:
        fleet_env._cutover_to_junction_layout(env_root, retry_budget_secs=0.05)

    message = str(excinfo.value)
    assert "fleet-env-cutover.py" in message
    assert "importing" in message

    # Nothing mutated: env_root is still the same real directory.
    assert env_root.is_dir()
    assert not junction.is_junction(env_root)
    assert (env_root / "marker.txt").read_text(encoding="utf-8") == "UNTOUCHED"
    # No orphaned generation sibling left behind by the failed attempt.
    siblings = [p.name for p in tmp_path.iterdir()]
    assert not any(name.startswith("fleet-env.gen-") for name in siblings)


def test_retry_gives_up_within_the_stated_budget(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Must not loop past the budget looking for a lucky window."""
    env_root = tmp_path / "fleet-env"
    _write_marker(env_root, "CONTENT")

    monkeypatch.setattr(os, "name", "nt", raising=False)
    monkeypatch.setattr(
        fleet_env.os, "rename", lambda *_a, **_k: (_ for _ in ()).throw(_permission_error(5))
    )

    start = time.monotonic()
    with pytest.raises(fleet_env.FleetEnvCutoverBlocked):
        fleet_env._cutover_to_junction_layout(env_root, retry_budget_secs=0.2)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0  # generous ceiling well above the 0.2s budget


def test_non_transient_permission_error_is_not_retried(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A PermissionError that is NOT the WinError-5 reader-handle shape (a
    real permission problem, or any nt error code other than 5) must
    propagate immediately, not be silently retried for the whole budget."""
    env_root = tmp_path / "fleet-env"
    _write_marker(env_root, "CONTENT")

    monkeypatch.setattr(os, "name", "nt", raising=False)
    calls = {"n": 0}

    def _wrong_code(*_args, **_kwargs):
        calls["n"] += 1
        raise _permission_error(32)  # sharing violation, not WinError 5

    monkeypatch.setattr(fleet_env.os, "rename", _wrong_code)

    with pytest.raises(PermissionError):
        fleet_env._cutover_to_junction_layout(env_root, retry_budget_secs=10.0)

    assert calls["n"] == 1, "a non-transient error must not be retried"


# --------------------------------------------------------------------------
# Restore-on-create-failure: env_root must never be left absent.
# --------------------------------------------------------------------------


def test_restore_on_create_junction_failure_leaves_env_root_readable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    env_root = tmp_path / "fleet-env"
    _write_marker(env_root, "RESTORE-ME")

    monkeypatch.setattr(
        junction,
        "create_junction",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("simulated create_junction failure")),
    )

    with pytest.raises(OSError):
        fleet_env._cutover_to_junction_layout(env_root)

    # Restored: env_root is a real, readable directory again — not a
    # junction, not absent.
    assert env_root.is_dir()
    assert not junction.is_junction(env_root)
    assert (env_root / "marker.txt").read_text(encoding="utf-8") == "RESTORE-ME"


def test_restore_under_load_goes_through_bounded_retry_not_a_bare_rename(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """The restore-on-failure path must not use a bare `os.rename` — the
    tree it renames back is the SAME tree the vacate-rename just moved, so
    it can still carry an open reader handle and hit the identical
    transient WinError-5 shape. Proves the restore actually retries (not
    just that it eventually succeeds) by making it fail transiently once,
    then succeed."""
    env_root = tmp_path / "fleet-env"
    _write_marker(env_root, "RESTORE-UNDER-LOAD")

    monkeypatch.setattr(os, "name", "nt", raising=False)
    monkeypatch.setattr(
        junction,
        "create_junction",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("simulated create_junction failure")),
    )

    real_rename = os.rename
    restore_calls = {"n": 0}

    def _rename_vacate_then_flaky_restore(src, dst, *args, **kwargs):
        # First call is the vacate (env_root -> generation): let it through.
        # Every subsequent call is a restore attempt (generation -> env_root):
        # fail once with the transient shape, then succeed.
        if str(dst).endswith(env_root.name) or Path(dst) == env_root:
            restore_calls["n"] += 1
            if restore_calls["n"] == 1:
                raise _permission_error(5)
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(fleet_env.os, "rename", _rename_vacate_then_flaky_restore)

    with pytest.raises(OSError):
        fleet_env._cutover_to_junction_layout(env_root, retry_budget_secs=1.0)

    assert restore_calls["n"] >= 2, "restore must have retried the transient failure, not given up on the first try"
    assert env_root.is_dir()
    assert not junction.is_junction(env_root)
    assert (env_root / "marker.txt").read_text(encoding="utf-8") == "RESTORE-UNDER-LOAD"


def test_restore_retry_exhausted_names_generation_dir_and_absent_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Genuinely unrecoverable case: vacate succeeds, create_junction fails,
    and the restore's own bounded retry ALSO exhausts. env_root is left
    ABSENT — the resulting FleetEnvError must name that state and the
    generation directory the environment actually lives at, not a bare
    traceback."""
    env_root = tmp_path / "fleet-env"
    _write_marker(env_root, "UNRECOVERABLE")

    monkeypatch.setattr(os, "name", "nt", raising=False)
    monkeypatch.setattr(
        junction,
        "create_junction",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("simulated create_junction failure")),
    )

    real_rename = os.rename
    vacated = {"done": False}

    def _rename_vacate_then_always_blocked_restore(src, dst, *args, **kwargs):
        if not vacated["done"]:
            vacated["done"] = True
            return real_rename(src, dst, *args, **kwargs)
        raise _permission_error(5)

    monkeypatch.setattr(fleet_env.os, "rename", _rename_vacate_then_always_blocked_restore)

    with pytest.raises(fleet_env.FleetEnvError) as excinfo:
        fleet_env._cutover_to_junction_layout(env_root, retry_budget_secs=0.05)

    message = str(excinfo.value)
    assert str(env_root) in message
    assert "ABSENT" in message
    # The generation directory the environment now actually lives at must
    # be named so an operator can find it.
    siblings = [p for p in tmp_path.iterdir() if p.name.startswith("fleet-env.gen-")]
    assert len(siblings) == 1
    assert str(siblings[0]) in message
    assert (siblings[0] / "marker.txt").read_text(encoding="utf-8") == "UNRECOVERABLE"

    # The one unacceptable outcome, but at least it's diagnosable: env_root
    # itself carries no entry at all.
    assert not junction.is_junction(env_root)
    assert not env_root.is_dir()

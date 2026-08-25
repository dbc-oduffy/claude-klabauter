"""C0 (docs/plans/2026-08-16-registry-read-stops-costing-a-process.md): a
re-run of install must bring an already-installed `<settings-home>/bin/`
resolver family current, idempotently, and safely under concurrent access.

Root-cause finding this module encodes as regression coverage (see the
plan's C0 body): `_install_one`'s force-overwrite classification block IS
reached on every `_install_bin_resolvers` call — it is not gated behind a
first-install-only phase and the copier itself is not the defect (Anti-scope:
"do not fix the overwrite policy"). The staleness this plan was drafted
against was un-run install, not a mechanism gap — `test_refresh_repairs_a_stale_destination_family_wide`
and `test_second_consecutive_run_is_a_byte_level_noop` below are the direct
evidence for that finding, exercised against `_install_bin_resolvers`
(the same function `substrate.run` Step 3 calls) rather than a live
`~/.coordinator-claude-settings`, per this chunk's "no live install" scope
line.

Also covers the concurrency fix landed alongside this file: `_install_one`'s
force-overwrite branch now writes via `atomic_write_bytes` (same-directory
mkstemp + os.replace) instead of a bare `shutil.copyfile`, and
`_install_bin_resolvers`'s ml/ch/ml_explicit and platform-localize write
loops each hold `coordinator_core.locked_write.held_lock` on `bin_dst` for
the duration of the write loop (`_write_agent_helper_forwarders`'s own
Step 3b lock was already covered by `test_forwarder_write_lock_and_venv_swap_sweep.py`
— this file does not re-test that one).

Negative-spec: this module never targets a real `~/.coordinator-claude-settings`
or `~/.claude` — every fixture writes under `tmp_path`; `COORDINATOR_LOCK_ROOT`
isolates the lock sidecar directory the same way, per the documented test-only
escape hatch (`locked_write.py`'s own docstring), never the operator's real one.
"""
from __future__ import annotations

import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from coordinator_core.install import substrate
from coordinator_core.install._shared import atomic_write_bytes
from coordinator_core.install.substrate import (
    _CH_FAMILY_FILES,
    _install_bin_resolvers,
    _install_one,
    _load_bin_templates_manifest,
    _resolve_bin_templates_manifest_root,
)
from coordinator_core.locked_write import held_lock

# `_install_bin_resolvers` (exercised throughout this module, mirroring
# `test_install_agent_helper_execution_and_idempotency.py`'s own tiering
# decision for the same call) reaches `_emit_and_verify_ps1_forwarders`,
# which spawns a real `powershell.exe` execution-policy probe on Windows --
# a real external-process spawn (spawn ratchet Rule 2), so this file is
# cadence-tiered like its sibling rather than left on the fast/default gate.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _isolate_lock_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("COORDINATOR_LOCK_ROOT", str(tmp_path / "lock-root"))


def _run_install(tmp_path: Path, monkeypatch, bin_dst: Path, *, seed: str = "v1") -> None:
    ml_bin = tmp_path / "ml_bin"
    ch_bin = tmp_path / "ch_bin"

    bin_manifest = _load_bin_templates_manifest(_resolve_bin_templates_manifest_root())
    for entry in bin_manifest.install_bin_resolvers_entries():
        _write(ml_bin / entry.name, f"ml-source-content::{entry.name}::{seed}\n")
    for f, _exec_bit in _CH_FAMILY_FILES:
        _write(ch_bin / f, f"ch-source-content::{f}::{seed}\n")

    monkeypatch.setenv("MAKIMA_ROOT", str(_REPO_ROOT))

    _install_bin_resolvers(
        ml_bin, ch_bin, bin_dst,
        check_only=False,
        python3_cmd_resolved_bin=sys.executable,
    )


def _snapshot(bin_dst: Path) -> "dict[str, bytes]":
    return {
        p.name: p.read_bytes()
        for p in bin_dst.iterdir()
        if p.is_file()
    }


class TestRootCauseIsUnRunInstallNotAGatedOrUnreachedRefresh:
    """AC1/root_cause evidence: the force-overwrite refresh mechanism IS
    reached on a re-run and DOES repair a stale destination, family-wide —
    i.e. the copier is not the defect (Anti-scope)."""

    def test_first_pass_writes_the_static_bin_family(self, tmp_path, monkeypatch):
        _isolate_lock_root(monkeypatch, tmp_path)
        bin_dst = tmp_path / "bin_dst"
        bin_dst.mkdir()
        _run_install(tmp_path, monkeypatch, bin_dst, seed="v1")

        bin_manifest = _load_bin_templates_manifest(_resolve_bin_templates_manifest_root())
        for entry in bin_manifest.install_bin_resolvers_entries():
            dst = bin_dst / entry.name
            assert dst.is_file(), f"{entry.name} not installed on first pass"
            assert "::v1" in dst.read_text(encoding="utf-8")

    def test_refresh_repairs_a_stale_destination_family_wide(self, tmp_path, monkeypatch):
        _isolate_lock_root(monkeypatch, tmp_path)
        bin_dst = tmp_path / "bin_dst"
        bin_dst.mkdir()
        _run_install(tmp_path, monkeypatch, bin_dst, seed="v1")
        before = _snapshot(bin_dst)

        # Simulate a stale box: the templates moved on (seed="v2"); a re-run
        # over the SAME bin_dst, with no intervening uninstall/reset, must
        # bring every family member current -- this is the refresh step
        # itself, reached on a plain re-run, gated behind nothing.
        _run_install(tmp_path, monkeypatch, bin_dst, seed="v2")
        after = _snapshot(bin_dst)

        bin_manifest = _load_bin_templates_manifest(_resolve_bin_templates_manifest_root())
        changed = 0
        for entry in bin_manifest.install_bin_resolvers_entries():
            name = entry.name
            assert "::v2" in after[name].decode("utf-8"), (
                f"{name} still stale after refresh -- refresh step was not reached"
            )
            if before[name] != after[name]:
                changed += 1
        assert changed > 0, "expected at least one file to differ v1 -> v2"


class TestSecondConsecutiveRunIsAByteLevelNoop:
    """AC2: idempotence. A second consecutive run over unchanged sources
    writes nothing -- verified both by a byte-level snapshot comparison of
    `bin_dst` and by proving the write primitive itself is never invoked."""

    def test_byte_level_snapshot_unchanged(self, tmp_path, monkeypatch):
        _isolate_lock_root(monkeypatch, tmp_path)
        bin_dst = tmp_path / "bin_dst"
        bin_dst.mkdir()
        _run_install(tmp_path, monkeypatch, bin_dst, seed="v1")
        before = _snapshot(bin_dst)

        _run_install(tmp_path, monkeypatch, bin_dst, seed="v1")
        after = _snapshot(bin_dst)

        assert before == after
        assert set(before) == set(after)

    def test_second_run_never_calls_the_write_primitive(self, tmp_path, monkeypatch):
        _isolate_lock_root(monkeypatch, tmp_path)
        bin_dst = tmp_path / "bin_dst"
        bin_dst.mkdir()
        _run_install(tmp_path, monkeypatch, bin_dst, seed="v1")

        calls: "list[Path]" = []
        real_atomic_write_bytes = substrate.atomic_write_bytes

        def _spy(target, data, **kwargs):
            calls.append(Path(target))
            return real_atomic_write_bytes(target, data, **kwargs)

        monkeypatch.setattr(substrate, "atomic_write_bytes", _spy)
        _run_install(tmp_path, monkeypatch, bin_dst, seed="v1")

        assert calls == [], (
            f"second identical-content run invoked the write primitive for: {calls} "
            "-- not a no-op"
        )


class TestForceOverwriteRoutesThroughAtomicWriteNotShutilCopyfile:
    """C0 concurrency requirement: `_install_one`'s force-overwrite path
    (cold-create AND content-differs) must go through `atomic_write_bytes`
    (same-dir mkstemp + os.replace), never a bare `shutil.copyfile` racing a
    concurrent reader of the destination."""

    def test_cold_create_never_calls_shutil_copyfile(self, tmp_path, monkeypatch):
        src = tmp_path / "src.cmd"
        dst = tmp_path / "dst.cmd"
        src.write_text("payload\n", encoding="utf-8")

        def _boom(*_a, **_k):
            raise AssertionError("shutil.copyfile must not be called for a force-overwrite install")

        monkeypatch.setattr(substrate.shutil, "copyfile", _boom)
        _install_one(src, dst, False, "machine-local", False, force_overwrite=True)
        assert dst.read_text(encoding="utf-8") == "payload\n"

    def test_content_diff_overwrite_never_calls_shutil_copyfile(self, tmp_path, monkeypatch):
        src = tmp_path / "src.cmd"
        dst = tmp_path / "dst.cmd"
        src.write_text("new-payload\n", encoding="utf-8")
        dst.write_text("old-payload\n", encoding="utf-8")

        def _boom(*_a, **_k):
            raise AssertionError("shutil.copyfile must not be called for a force-overwrite install")

        monkeypatch.setattr(substrate.shutil, "copyfile", _boom)
        _install_one(src, dst, False, "machine-local", False, force_overwrite=True)
        assert dst.read_text(encoding="utf-8") == "new-payload\n"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "exec bit is meaningless on Windows (§ macOS verification item 2 / "
            "this plan's own text) -- os.chmod there does not set POSIX exec "
            "bits at all, so this precondition cannot even be established; "
            "genuine coverage of this property is the macOS-only AC5m pass."
        ),
    )
    def test_atomic_replace_preserves_exec_bit_across_a_refresh(self, tmp_path, monkeypatch):
        """macOS parity (§ macOS verification item 2): a refresh must not
        drop the exec bit off a forwarder it rewrites."""
        src = tmp_path / "forwarder"
        dst = tmp_path / "forwarder_dst"
        src.write_text("#!/bin/sh\necho new\n", encoding="utf-8")
        dst.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
        dst.chmod(dst.stat().st_mode | 0o111)
        assert dst.stat().st_mode & 0o111

        _install_one(src, dst, True, "machine-local", False, force_overwrite=True)

        assert dst.read_text(encoding="utf-8") == "#!/bin/sh\necho new\n"
        assert dst.stat().st_mode & 0o111, "exec bit lost across atomic refresh"


class TestConcurrentInstallLeavesTheFamilyByteCompleteAndConsistent:
    """C0 concurrency requirement: two processes (modelled here as two
    threads, each running the real `_install_bin_resolvers` write loops
    under its own OS-level `held_lock` acquisition) racing the same
    `<settings-home>/bin` must leave every file byte-complete -- never a
    torn/partial write -- and the family internally consistent (every
    member from exactly one of the two racing template generations, not a
    mix within a single file)."""

    def test_two_racing_installs_leave_every_file_byte_complete(self, tmp_path, monkeypatch):
        _isolate_lock_root(monkeypatch, tmp_path)
        bin_dst = tmp_path / "bin_dst"
        bin_dst.mkdir()
        # Seed once so both racing runs are force-overwrite passes (the
        # branch this fix targets), not cold-creates.
        _run_install(tmp_path, monkeypatch, bin_dst, seed="v0")

        # threading.local-free: each thread gets its own tmp source tree and
        # its own MAKIMA_ROOT monkeypatch is process-global (fine -- same
        # value in both), but its own ml_bin/ch_bin content so the two racing
        # generations are distinguishable in the final result.
        bin_manifest = _load_bin_templates_manifest(_resolve_bin_templates_manifest_root())
        os.environ["MAKIMA_ROOT"] = str(_REPO_ROOT)

        def _race(seed: str, ml_bin: Path, ch_bin: Path) -> None:
            for entry in bin_manifest.install_bin_resolvers_entries():
                _write(ml_bin / entry.name, f"ml-source-content::{entry.name}::{seed}\n")
            for f, _exec_bit in _CH_FAMILY_FILES:
                _write(ch_bin / f, f"ch-source-content::{f}::{seed}\n")
            _install_bin_resolvers(
                ml_bin, ch_bin, bin_dst,
                check_only=False,
                python3_cmd_resolved_bin=sys.executable,
            )

        errors: "list[BaseException]" = []

        def _worker(seed: str, ml_bin: Path, ch_bin: Path) -> None:
            try:
                _race(seed, ml_bin, ch_bin)
            except BaseException as exc:  # noqa: BLE001 -- surfaced to the test thread below
                errors.append(exc)

        t1 = threading.Thread(
            target=_worker, args=("A", tmp_path / "ml_bin_A", tmp_path / "ch_bin_A"),
        )
        t2 = threading.Thread(
            target=_worker, args=("B", tmp_path / "ml_bin_B", tmp_path / "ch_bin_B"),
        )
        t1.start()
        t2.start()
        t1.join(timeout=30.0)
        t2.join(timeout=30.0)
        assert not t1.is_alive() and not t2.is_alive(), "a racing install pass hung"
        assert not errors, f"racing install pass(es) raised: {errors}"

        # Every ml/ch family member must be present, and each one's content
        # must be a COMPLETE, single-generation write -- either entirely "A"
        # or entirely "B", never a torn mix of both (the TOCTOU/non-atomic
        # write this fix closes would show up here as a file matching
        # neither expected string, or as one whose bytes don't round-trip
        # cleanly as UTF-8 text at all).
        for entry in bin_manifest.install_bin_resolvers_entries():
            dst = bin_dst / entry.name
            assert dst.is_file(), f"{entry.name} missing after racing installs"
            text = dst.read_text(encoding="utf-8")
            assert text in (
                f"ml-source-content::{entry.name}::A\n",
                f"ml-source-content::{entry.name}::B\n",
            ), f"{entry.name} left in a torn/inconsistent state: {text!r}"
        for f, _exec_bit in _CH_FAMILY_FILES:
            dst = bin_dst / f
            assert dst.is_file(), f"{f} missing after racing installs"
            text = dst.read_text(encoding="utf-8")
            assert text in (
                f"ch-source-content::{f}::A\n",
                f"ch-source-content::{f}::B\n",
            ), f"{f} left in a torn/inconsistent state: {text!r}"

    def test_bin_family_write_loop_serialises_against_an_externally_held_lock(
        self, tmp_path, monkeypatch,
    ):
        """Direct proof the ml/ch/ml_explicit write loop actually acquires
        `held_lock` on `bin_dst` (not merely that racing output happens to
        look right) -- mirrors
        `test_forwarder_write_lock_and_venv_swap_sweep.py`'s existing proof
        for the Step 3b forwarder loop, for the ml/ch/ml_explicit loop this
        chunk adds a lock to."""
        _isolate_lock_root(monkeypatch, tmp_path)
        bin_dst = tmp_path / "bin_dst"
        bin_dst.mkdir()
        _run_install(tmp_path, monkeypatch, bin_dst, seed="v0")

        with held_lock(bin_dst, holder_label="test-holder", timeout=5.0):
            result: "dict[str, object]" = {}

            def _call():
                try:
                    _run_install(tmp_path, monkeypatch, bin_dst, seed="v1")
                except Exception as exc:  # noqa: BLE001
                    result["error"] = exc

            t = threading.Thread(target=_call)
            t.start()
            t.join(timeout=3.0)
            # The writer thread must still be blocked behind this held lock.
            assert t.is_alive()
            bin_manifest = _load_bin_templates_manifest(_resolve_bin_templates_manifest_root())
            sample = bin_manifest.install_bin_resolvers_entries()[0]
            assert "::v0" in (bin_dst / sample.name).read_text(encoding="utf-8"), (
                "install proceeded past the ml/ch write loop while an external "
                "holder still held the bin_dst lock"
            )

        t.join(timeout=15.0)
        assert not t.is_alive()
        assert "error" not in result

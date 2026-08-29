"""C4 (docs/plans/2026-08-16-registry-read-stops-costing-a-process.md): prove
the bake (C2) and the family refresh (C0) against a REAL install, not only
`tmp_path` -- AC1, AC2, AC5, AC6 jointly, live.

Direct precedent: `state/lessons/2026-08-15-simulating-the-fresh-install-
condition-h-8bf3476b2152.yaml` -- "a simulation of the condition does not
discharge [a 'validated live' AC]: clone it for real ... and read the
resolved VALUES rather than the success flag." `test_bin_family_refresh.py`
(C0's own coverage) proves the mechanism against `tmp_path`; this module
proves the SAME `_install_bin_resolvers` call against this operator's real
`<settings-home>/bin` -- the actual destination the plan's AC1/AC2/AC6
language names -- and reads observed byte counts, not a boolean.

LIVE-MUTATION SAFETY (negative-spec, required reading before editing this
file): every test below writes into the REAL `<settings-home>/bin`, shared
with every other active session on this box (CLAUDE.md § Load norm: 50-70
concurrent LLMs average). This module adds NO locking, backup, or copy
mechanism of its own -- safety rests entirely on C0's landed
`_install_bin_resolvers`, which already wraps its ml/ch/ml_explicit and
platform-localize write loops in `coordinator_core.locked_write.held_lock`
on `bin_dst`, and on `_install_one`'s force-overwrite path already routing
through `atomic_write_bytes` (same-directory mkstemp + `os.replace`, atomic
on both Windows and POSIX) instead of a bare `shutil.copyfile`. This module
only EXERCISES that mechanism against the real destination; it never adds a
second one. Two consequences that follow directly:

  1. A concurrent peer session's OWN install/refresh (e.g. a SessionStart
     drift sweep, or another operator running `coordinator:install`) is safe
     to race against these tests for the same reason two racing installs are
     safe in `test_bin_family_refresh.py`'s
     `TestConcurrentInstallLeavesTheFamilyByteCompleteAndConsistent` --
     `held_lock` serialises writers, and every reader (including this
     module's own post-write verification reads) observes either the old or
     the fully-written new content, never a torn write.
  2. AC2's byte-level before/after comparison is ITSELF racy in the small
     window between the "after first run" snapshot and the "after second
     run" snapshot: a peer's own refresh landing in that window is a genuine
     content change this module did not cause and cannot distinguish from a
     bug in the idempotence property under test. `test_second_run_is_a_
     byte_level_noop_on_the_static_family` below narrows the snapshot to
     only the files `_install_bin_resolvers` itself writes (never the full
     391-entry `bin/` tree, most of which -- the derived agent-helper
     forwarders and their `.ps1` twins -- this module does not assert
     no-op-ness over) precisely to shrink that race window and its blast
     radius; it does not close the race. If a run here observes a file this
     module wrote change underneath it between snapshots, that is reported
     as evidence of a live peer race, not silently retried or asserted away.

Negative-spec: this module never persists `COORDINATOR_SETTINGS_HOME` --
Anti-scope. `settings_home()` is read via its normal, unmodified precedence
(no env override set here), which is exactly what already resolves to this
operator's real settings home on this box.

macOS parity (AC5m) is NOT exercised here and cannot be from a Windows
session -- see § macOS verification in the plan; this module discharges AC5
(Windows) only. The debt-backlog row citing that procedure is tracked
outside this file (see the chunk's completion note).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core import machine_resolver
from coordinator_core._settings_home import settings_home
from coordinator_core.launchable import resolve_launchable
from coordinator_core.win_portability import is_executable, no_console_creationflags
from coordinator_core.install.substrate import (
    _CH_FAMILY_FILES,
    _install_bin_resolvers,
    _load_bin_templates_manifest,
    _resolve_baked_python_bin,
    _resolve_bin_templates_manifest_root,
)

# `_install_bin_resolvers` formerly reached `_emit_and_verify_ps1_forwarders`,
# which spawned a real `powershell.exe` execution-policy probe on Windows --
# that function is deleted 2026-08-29 (docs/plans/2026-08-26-every-forwarder-
# that-can-reach-the-door-does.md C12, DR-365: the `.ps1` leg is condemned
# outright). This module's own "dump is an accepted verb" check still spawns
# `machine-local.cmd` for real -- a genuine OS-process spawn (spawn ratchet
# Rule 2) independent of that deletion, and reason enough on its own for the
# cadence tiering below, mirroring `test_bin_family_refresh.py`'s tiering note.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
    # `real_home` opts out of `conftest.py::_quarantine_real_home` -- its own
    # docstring says "read-only oracles only" because a quarantined write is
    # normally the SAFE default. This module is the deliberate, plan-
    # authorized exception: C4's whole purpose is proving a live write
    # against the real `<settings-home>/bin`, and the write is safe not
    # because it is read-only but because C0's `held_lock` + `atomic_write_
    # bytes` make it safe to race a peer session -- see this module's own
    # negative-spec above for the full argument.
    pytest.mark.real_home,
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_NO_CONSOLE = no_console_creationflags()

_FIVE_STATIC_SHIM_NAMES = (
    "machine-local.cmd",
    "claude-home.cmd",
    "coordinator-settings-home.cmd",
    "platform-localize.cmd",
    "resolve-coordinator-clone.cmd",
)


def _resolve_real_doe_bin_templates() -> "Path | None":
    """The real `templates/bin/` directory this box's install would copy
    from -- resolved the same way an operator's `repos.doe_claude` registry
    key resolves it, never a fixture. Returns ``None`` when this box has no
    DoE-claude checkout registered/present (the module's tests then skip
    with that reason rather than fail -- an environment gap, not a defect)."""
    doe_root_raw = machine_resolver.registry_get("repos.doe_claude")
    if not doe_root_raw:
        return None
    candidate = Path(doe_root_raw) / "coordinator" / "templates" / "bin"
    return candidate if candidate.is_dir() else None


def _resolve_real_ch_bin() -> Path:
    """`<claude_klabauter_root>/coordinator/lib/claude-home` -- same resolution
    `run()` performs (`ch_bin = claude_klabauter_lib / "claude-home"`), rooted at this
    checkout since this test module lives inside claude-klabauter itself."""
    return _REPO_ROOT / "coordinator" / "lib" / "claude-home"


def _static_family_dest_names() -> "list[str]":
    manifest = _load_bin_templates_manifest(_resolve_bin_templates_manifest_root())
    names = [e.name for e in manifest.install_bin_resolvers_entries()]
    names += [f for f, _exec_bit in _CH_FAMILY_FILES]
    return names


def _snapshot_static_family(bin_dst: Path) -> "dict[str, tuple[int, str]]":
    """(size, sha256) per file this module's own live-install call writes --
    a byte-level snapshot restricted to the static family (see module
    docstring's negative-spec on why the full `bin/` tree is not snapshotted
    here)."""
    snap: "dict[str, tuple[int, str]]" = {}
    for name in _static_family_dest_names():
        p = bin_dst / name
        if not p.is_file():
            continue
        data = p.read_bytes()
        snap[name] = (len(data), hashlib.sha256(data).hexdigest())
    return snap


def _skip_reason_if_unavailable() -> "str | None":
    if _resolve_real_doe_bin_templates() is None:
        return (
            "no real DoE-claude templates/bin/ resolvable on this box "
            "(repos.doe_claude registry key absent, or the checkout lacks "
            "coordinator/templates/bin/) -- environment gap, not a test failure"
        )
    if not _resolve_real_ch_bin().is_dir():
        return f"no real claude-home family source at {_resolve_real_ch_bin()}"
    return None


def _run_real_install_once() -> "tuple[Path, dict]":
    """One live pass of `_install_bin_resolvers` against this operator's
    real `<settings-home>/bin`, real DoE templates, and real
    `coordinator/lib/claude-home` -- the exact call `run()` Step 3 makes.
    Returns `(bin_dst, observed_counts)` where `observed_counts` records the
    raw values this chunk's brief asks for, never a pass/fail flag alone."""
    ml_bin = _resolve_real_doe_bin_templates()
    ch_bin = _resolve_real_ch_bin()
    bin_dst = settings_home() / "bin"
    python3_cmd_resolved_bin = _resolve_baked_python_bin()

    _install_bin_resolvers(
        ml_bin, ch_bin, bin_dst,
        check_only=False,
        python3_cmd_resolved_bin=python3_cmd_resolved_bin,
    )

    unbaked = 0
    for name in _FIVE_STATIC_SHIM_NAMES:
        p = bin_dst / name
        if p.is_file():
            unbaked += p.read_bytes().count(b"__PYTHON_BIN__")
    observed = {
        "unbaked_shim_token_count": unbaked,
        "python_bin_sidecar_present": (bin_dst / ".python-bin").is_file(),
    }
    return bin_dst, observed


class TestLiveBakeAndRefresh:
    """AC1, AC6: a real install pass against this operator's actual
    `<settings-home>/bin` bakes all five static shims and writes the durable
    `.python-bin` sidecar -- read off disk, not asserted as a success flag."""

    def test_five_shims_bake_to_zero_unbaked_tokens_and_sidecar_present(self):
        skip = _skip_reason_if_unavailable()
        if skip:
            pytest.skip(skip)

        bin_dst, observed = _run_real_install_once()

        if sys.platform == "win32":
            assert observed["unbaked_shim_token_count"] == 0, (
                f"expected 0 unbaked __PYTHON_BIN__ occurrences across the five "
                f"static shims after a live install pass, observed "
                f"{observed['unbaked_shim_token_count']} at {bin_dst}"
            )
        else:
            # § macOS verification item 1: there is no .cmd rung at all on
            # POSIX, so the bake-count assertion is meaningless there --
            # AC5m tracks this platform's own divergent surfaces separately
            # and is NOT closeable by this Windows-authored branch.
            pytest.skip(
                "non-Windows host: the five .cmd shims this assertion covers "
                "do not exist here -- AC5m is a separate, POSIX-only pass"
            )

        assert observed["python_bin_sidecar_present"], (
            f"<settings-home>/bin/.python-bin absent after a live install pass "
            f"at {bin_dst} -- the durable half of AC6 did not land"
        )


class TestLiveMachineLocalDumpVerbIsAccepted:
    """AC1: `machine-local dump` is an accepted verb on this box's real,
    freshly-refreshed installed CLI -- invoked for real, output parsed, not
    merely a nonzero-exit check."""

    def test_dump_returns_the_registry_as_json(self):
        skip = _skip_reason_if_unavailable()
        if skip:
            pytest.skip(skip)

        bin_dst, _observed = _run_real_install_once()
        machine_local_bin = bin_dst / "machine-local"
        assert is_executable(machine_local_bin), (
            f"{machine_local_bin} not executable after a live install pass"
        )

        argv = [*resolve_launchable(str(machine_local_bin)), "dump"]
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=30, **_NO_CONSOLE,
        )
        assert proc.returncode == 0, (
            f"`machine-local dump` exited {proc.returncode} -- stderr: {proc.stderr!r}"
        )
        payload = json.loads(proc.stdout)
        assert isinstance(payload, dict) and payload, (
            f"`machine-local dump` returned an empty/non-dict payload: {payload!r}"
        )


class TestSecondConsecutiveRunIsAByteLevelNoopOnTheStaticFamily:
    """AC2: idempotence, proven live -- a second consecutive
    `_install_bin_resolvers` pass over the same real destination, with no
    intervening template change, writes nothing to the static family this
    module snapshots. See module docstring's negative-spec for why this is
    scoped to the static family rather than the full `bin/` tree, and for
    the residual race this narrowing does not close."""

    def test_static_family_byte_identical_across_a_second_pass(self):
        skip = _skip_reason_if_unavailable()
        if skip:
            pytest.skip(skip)

        bin_dst, _observed = _run_real_install_once()
        after_first = _snapshot_static_family(bin_dst)

        _run_real_install_once()
        after_second = _snapshot_static_family(bin_dst)

        assert after_first == after_second, (
            "static bin family changed across a second consecutive live "
            "install pass with no intervening template edit -- either a "
            "genuine idempotence regression, or (see this module's "
            "negative-spec) a concurrent peer session's own refresh landed "
            "in the snapshot window; re-run in isolation to distinguish "
            "the two before treating this as a defect"
        )

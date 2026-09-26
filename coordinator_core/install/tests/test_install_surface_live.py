"""C4 (docs/plans/2026-08-16-registry-read-stops-costing-a-process.md): prove
the bake (C2) and the family refresh (C0) against REAL install content, not
only `tmp_path` -- AC1, AC2, AC5, AC6 jointly, live.

Direct precedent: `state/lessons/2026-08-15-simulating-the-fresh-install-
condition-h-8bf3476b2152.yaml` -- "a simulation of the condition does not
discharge [a 'validated live' AC]: clone it for real ... and read the
resolved VALUES rather than the success flag." `test_bin_family_refresh.py`
(C0's own coverage) proves the mechanism against `tmp_path`; this module
reads the REAL observed values off this operator's actual `<settings-home>/
bin` where a check can do so without writing, and exercises
`_install_bin_resolvers` against a COPY of that real content where a check
must actually run the installer -- never against the live destination
itself.

NEVER-WRITES-LIVE SAFETY (negative-spec, required reading before editing
this file): this module never writes into the REAL `<settings-home>/bin`,
full stop -- PM ruling 2026-09-18. That directory is shared live with every
other active session on this box (CLAUDE.md § Load norm: 50-70 concurrent
LLMs average), and an earlier version of this module was the likely writer
behind an unexplained settings-home mutation observed at 16:12 on
2026-09-18. Two consequences follow directly:

  1. Checks that only need to OBSERVE the real install (AC1/AC6's baked-shim
     token count and `.python-bin` sidecar presence, AC1's `machine-local
     dump` verb) read whatever the real `<settings-home>/bin` already
     contains. They never call `_install_bin_resolvers` or any other writer
     against that real path, and they skip with a named reason when the real
     install is absent rather than installing one to make the read possible.
  2. The one check that genuinely needs to RUN the installer twice to prove
     byte-level idempotence (AC2) does so against a `tmp_path` copy of the
     real `<settings-home>/bin`, made via `shutil.copytree` before either
     pass. The INPUT is still real install content -- the same static-family
     bytes this operator's actual bin directory carries -- but every write
     `_install_bin_resolvers` performs lands in that copy, never in the real
     tree. This discharges the AC's actual intent (prove the mechanism
     against real install content, reading resolved values rather than a
     success flag) without reintroducing the live-write hazard the AC's
     literal "write live" language created.

Negative-spec: this module never persists `COORDINATOR_SETTINGS_HOME` --
Anti-scope. `settings_home()` is read via its normal, unmodified precedence
(no env override set here), which is exactly what already resolves to this
operator's real settings home on this box, and is used ONLY to locate what
to read (or, for the idempotence check, what to copy) -- never as a write
destination.

macOS parity (AC5m) is NOT exercised here and cannot be from a Windows
session -- see § macOS verification in the plan; this module discharges AC5
(Windows) only. The debt-backlog row citing that procedure is tracked
outside this file (see the chunk's completion note).
"""
from __future__ import annotations

import hashlib
import json
import shutil
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

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
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
    doe_root_raw = machine_resolver.registry_get("repos.doe_claude")
    if not doe_root_raw:
        return None
    candidate = Path(doe_root_raw) / "coordinator" / "templates" / "bin"
    return candidate if candidate.is_dir() else None


def _resolve_real_ch_bin() -> Path:
    return _REPO_ROOT / "coordinator" / "lib" / "claude-home"


def _static_family_dest_names() -> "list[str]":
    manifest = _load_bin_templates_manifest(_resolve_bin_templates_manifest_root())
    names = [e.name for e in manifest.install_bin_resolvers_entries()]
    names += [f for f, _exec_bit in _CH_FAMILY_FILES]
    return names


def _snapshot_static_family(bin_dst: Path) -> "dict[str, tuple[int, str]]":
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


def _skip_reason_if_real_bin_absent(bin_dst: Path) -> "str | None":
    if not bin_dst.is_dir():
        return (
            f"no real install found at {bin_dst} -- this operator has not "
            "installed the bin family on this box, so there is nothing to "
            "read -- environment gap, not a test failure"
        )
    return None


def _run_install_against(bin_dst: Path) -> None:
    ml_bin = _resolve_real_doe_bin_templates()
    ch_bin = _resolve_real_ch_bin()
    python3_cmd_resolved_bin = _resolve_baked_python_bin()

    _install_bin_resolvers(
        ml_bin, ch_bin, bin_dst,
        check_only=False,
        python3_cmd_resolved_bin=python3_cmd_resolved_bin,
    )


class TestLiveBakeAndRefresh:

    def test_five_shims_bake_to_zero_unbaked_tokens_and_sidecar_present(self):
        bin_dst = settings_home() / "bin"
        skip = _skip_reason_if_real_bin_absent(bin_dst)
        if skip:
            pytest.skip(skip)

        if sys.platform == "win32":
            unbaked = 0
            for name in _FIVE_STATIC_SHIM_NAMES:
                p = bin_dst / name
                if p.is_file():
                    unbaked += p.read_bytes().count(b"__PYTHON_BIN__")
            assert unbaked == 0, (
                f"expected 0 unbaked __PYTHON_BIN__ occurrences across the five "
                f"static shims in the real install, observed {unbaked} at {bin_dst}"
            )
        else:
            pytest.skip(
                "non-Windows host: the five .cmd shims this assertion covers "
                "do not exist here -- AC5m is a separate, POSIX-only pass"
            )

        assert (bin_dst / ".python-bin").is_file(), (
            f"<settings-home>/bin/.python-bin absent from the real install "
            f"at {bin_dst} -- the durable half of AC6 has not landed there"
        )


class TestLiveMachineLocalDumpVerbIsAccepted:

    def test_dump_returns_the_registry_as_json(self):
        bin_dst = settings_home() / "bin"
        skip = _skip_reason_if_real_bin_absent(bin_dst)
        if skip:
            pytest.skip(skip)

        machine_local_bin = bin_dst / "machine-local"
        if not machine_local_bin.exists():
            pytest.skip(
                f"{machine_local_bin} absent from the real install -- "
                "environment gap, not a test failure"
            )
        assert is_executable(machine_local_bin), (
            f"{machine_local_bin} not executable in the real install"
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

    def test_static_family_byte_identical_across_a_second_pass(self, tmp_path):
        skip = _skip_reason_if_unavailable()
        if skip:
            pytest.skip(skip)

        real_bin_dst = settings_home() / "bin"
        skip = _skip_reason_if_real_bin_absent(real_bin_dst)
        if skip:
            pytest.skip(skip)

        copy_bin_dst = tmp_path / "bin"
        shutil.copytree(real_bin_dst, copy_bin_dst)

        _run_install_against(copy_bin_dst)
        after_first = _snapshot_static_family(copy_bin_dst)

        _run_install_against(copy_bin_dst)
        after_second = _snapshot_static_family(copy_bin_dst)

        assert after_first == after_second, (
            "static bin family changed across a second consecutive install "
            "pass against a tmp_path copy of the real install, with no "
            "intervening template edit -- a genuine idempotence regression"
        )

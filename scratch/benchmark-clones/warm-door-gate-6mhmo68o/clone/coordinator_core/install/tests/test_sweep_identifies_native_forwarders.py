"""Coverage for C4a: teach `_sweep_orphaned_agent_helpers` to identify a
native (`.exe`) forwarder image via the install-side manifest, rather than a
content marker it cannot carry.

See `docs/dispatch-briefs/2026-08-26-every-forwarder-that-can-reach-the-door-
does/C4a.md` and `_NATIVE_FORWARDER_MANIFEST_NAME`'s docstring in
`coordinator_core/install/substrate.py` for the identification-mechanism
choice (manifest over resource/version stamp) and its rationale.
"""
from __future__ import annotations

import json

from coordinator_core.install import substrate
from coordinator_core.install.substrate import (
    _native_forwarder_manifest_path,
    _read_native_forwarder_manifest,
    _sweep_orphaned_agent_helpers,
    _write_native_forwarder_manifest,
)


def test_write_then_read_native_forwarder_manifest_round_trips(tmp_path):
    _write_native_forwarder_manifest(tmp_path, {"foo-cli", "bar-cli"})

    assert _read_native_forwarder_manifest(tmp_path) == {"foo-cli", "bar-cli"}


def test_read_native_forwarder_manifest_absent_returns_empty_set(tmp_path):
    assert _read_native_forwarder_manifest(tmp_path) == set()


def test_read_native_forwarder_manifest_malformed_returns_empty_set(tmp_path):
    _native_forwarder_manifest_path(tmp_path).write_text("not json", encoding="utf-8")

    assert _read_native_forwarder_manifest(tmp_path) == set()


def test_write_native_forwarder_manifest_overwrites_not_appends(tmp_path):
    _write_native_forwarder_manifest(tmp_path, {"old-cli"})
    _write_native_forwarder_manifest(tmp_path, {"new-cli"})

    assert _read_native_forwarder_manifest(tmp_path) == {"new-cli"}


def test_manifest_file_itself_is_excluded_from_the_sweep(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setattr(
        substrate.tempfile, "gettempdir", lambda: str(tmp_path / "_unrelated-temp-root")
    )
    _write_native_forwarder_manifest(tmp_path, {"retired-native"})
    manifest_path = _native_forwarder_manifest_path(tmp_path)
    assert manifest_path.name.startswith("_")

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)

    assert manifest_path.exists(), "the manifest side-file must never be swept itself"


def test_sweep_removes_manifest_named_orphan_without_reading_it_as_text(monkeypatch, tmp_path):
    """A native `.exe` image is binary and would raise `UnicodeDecodeError`
    on `read_text()` -- before this chunk that exception fell through the
    sweep's own except-continue and left the orphan on disk forever. The
    manifest match must identify and sweep it WITHOUT ever attempting that
    read."""
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setattr(
        substrate.tempfile, "gettempdir", lambda: str(tmp_path / "_unrelated-temp-root")
    )
    orphan = tmp_path / "retired-native.exe"
    orphan.write_bytes(b"\x4d\x5a\x00\x01\xff\xfe\x00binary-not-utf8\x80\x81")
    _write_native_forwarder_manifest(tmp_path, {"retired-native.exe"})

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)

    assert not orphan.exists(), "manifest-identified native orphan must be swept"


def test_sweep_leaves_binary_file_absent_from_manifest_alone(tmp_path):
    """A binary file the manifest does not name is not ours -- it must
    survive, matching the marker branches' "opted out" posture for a
    hand-authored decoy."""
    decoy = tmp_path / "unrelated-tool.exe"
    decoy.write_bytes(b"\x4d\x5a\x00\x01\xff\xfe\x00not-ours\x80\x81")

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)

    assert decoy.exists()


def test_sweep_protects_manifest_named_entry_still_in_this_runs_write_set(tmp_path):
    """Condition 2 (absence from this run's complete write set) still gates
    a manifest match -- a currently-valid native forwarder must survive even
    though its name is manifest-listed, exactly as a marker-carrying
    currently-valid `.cmd` survives via `protected_names`."""
    name = "current-native.exe"
    kept = tmp_path / name
    kept.write_bytes(b"\x4d\x5a\x00\x01currently-valid\x80\x81")
    _write_native_forwarder_manifest(tmp_path, {name})

    _sweep_orphaned_agent_helpers(tmp_path, {name: name}, {}, False)

    assert kept.exists(), "a name in this run's agent_helper_target_map must stay protected"


def test_sweep_check_only_reports_manifest_named_orphan(tmp_path, capsys):
    orphan = tmp_path / "retired-native.exe"
    orphan.write_bytes(b"\x4d\x5a\x00\x01\xff\xfe\x00binary\x80\x81")
    _write_native_forwarder_manifest(tmp_path, {"retired-native.exe"})

    try:
        _sweep_orphaned_agent_helpers(tmp_path, {}, {}, True)
        raised = False
    except substrate.SubstrateFatalError:
        raised = True

    assert raised, "check_only must fail fatally when an orphan is found, matching existing behavior"
    assert orphan.exists(), "check_only must never delete"
    assert "retired-native.exe" in capsys.readouterr().out

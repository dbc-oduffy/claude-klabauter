"""C5: `_read_native_forwarder_manifest` collapses four distinct states --
absent, unreadable, malformed, and genuinely-empty -- into one empty set,
making "nothing installed" indistinguishable from "this read could not say".
This module pins the tri-state read (`_read_native_forwarder_manifest_state`)
and its two teeth: `_union_native_forwarder_manifest` must not truncate the
manifest on an unmeasured read, and the reporting surface
(`check_settings_home`, via `_is_door_owned_forwarder_slot` and the
manifest-state read beside it) must surface "unmeasured" rather than
rendering it as ordinary non-ownership.

Spec backlink: docs/plans/2026-09-11-session-identity-residue-memo-send-sent.md,
task C5.
"""
from __future__ import annotations

from coordinator_core.install.settings_home_report import _is_door_owned_forwarder_slot
from coordinator_core.install.substrate import (
    NativeForwarderManifestRead,
    _native_forwarder_manifest_path,
    _read_native_forwarder_manifest,
    _read_native_forwarder_manifest_state,
    _union_native_forwarder_manifest,
    _write_native_forwarder_manifest,
)


def test_absent_manifest_is_measured_empty(tmp_path):
    """The normal pre-C5 state -- no manifest ever written -- is
    `measured=True`, never confused with a read that failed."""
    state = _read_native_forwarder_manifest_state(tmp_path)

    assert state == NativeForwarderManifestRead(frozenset(), True)
    assert _read_native_forwarder_manifest(tmp_path) == set()


def test_valid_empty_manifest_is_measured_empty(tmp_path):
    _write_native_forwarder_manifest(tmp_path, set())

    state = _read_native_forwarder_manifest_state(tmp_path)

    assert state == NativeForwarderManifestRead(frozenset(), True)


def test_valid_nonempty_manifest_is_measured(tmp_path):
    _write_native_forwarder_manifest(tmp_path, {"cross-repo-memo"})

    state = _read_native_forwarder_manifest_state(tmp_path)

    assert state == NativeForwarderManifestRead(frozenset({"cross-repo-memo"}), True)


def test_malformed_json_is_unmeasured(tmp_path):
    path = _native_forwarder_manifest_path(tmp_path)
    path.write_text("not json{{{", encoding="utf-8")

    state = _read_native_forwarder_manifest_state(tmp_path)

    assert state.measured is False
    assert state.names == frozenset()
    # The plain set-returning reader still collapses to empty for its
    # existing set-only callers.
    assert _read_native_forwarder_manifest(tmp_path) == set()


def test_names_field_wrong_shape_is_unmeasured(tmp_path):
    path = _native_forwarder_manifest_path(tmp_path)
    path.write_text('{"names": "not-a-list"}', encoding="utf-8")

    state = _read_native_forwarder_manifest_state(tmp_path)

    assert state.measured is False


def test_unreadable_existing_path_is_unmeasured(tmp_path):
    """A manifest path that exists but cannot be read as a file (e.g. it is
    a directory) is unmeasured, not "absent"."""
    path = _native_forwarder_manifest_path(tmp_path)
    path.mkdir()

    state = _read_native_forwarder_manifest_state(tmp_path)

    assert state.measured is False


def test_union_skips_write_on_unmeasured_read_rather_than_truncating(tmp_path):
    """The defect this row exists to fix: naively treating an unreadable
    manifest as empty and unioning into it would OVERWRITE the manifest
    with only this run's partial write set, silently dropping every
    previously-recorded name a corrupted-but-recoverable manifest still
    named. The fix is to write nothing at all on an unmeasured read."""
    path = _native_forwarder_manifest_path(tmp_path)
    path.write_text("not json{{{", encoding="utf-8")

    _union_native_forwarder_manifest(tmp_path, {"newly-written-cli"})

    # The manifest file is untouched -- still malformed, not overwritten
    # with a truncated valid one.
    assert path.read_text(encoding="utf-8") == "not json{{{"


def test_union_still_unions_on_a_measured_read(tmp_path):
    _write_native_forwarder_manifest(tmp_path, {"already-recorded-cli"})

    _union_native_forwarder_manifest(tmp_path, {"newly-written-cli"})

    assert _read_native_forwarder_manifest(tmp_path) == {
        "already-recorded-cli",
        "newly-written-cli",
    }


def test_door_owned_slot_unaffected_by_manifest_shape_signature(tmp_path, monkeypatch):
    """`_is_door_owned_forwarder_slot` keeps its pre-existing boolean-only
    return (other callers rely on it, e.g.
    `test_report_reads_a_cut_over_bin_correctly.py`); this row exposes
    "unmeasured" via `_read_native_forwarder_manifest_state` beside it,
    never by changing this predicate's contract. A malformed manifest
    still yields a plain `False` here -- correctly conservative -- while
    the state read on the same directory reports `measured=False`."""
    from coordinator_core.install import settings_home_report

    bin_dir = tmp_path
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / "cross-repo-memo"
    path.write_text("stub", encoding="utf-8")

    manifest_path = _native_forwarder_manifest_path(bin_dir)
    manifest_path.write_text("not json{{{", encoding="utf-8")

    monkeypatch.setattr(settings_home_report, "is_door_installed", lambda d: True)
    monkeypatch.setattr(settings_home_report, "is_native_image", lambda p: True)

    door_owned = _is_door_owned_forwarder_slot("cross-repo-memo", path, bin_dir)
    state = _read_native_forwarder_manifest_state(bin_dir)

    assert door_owned is False
    assert state.measured is False


def test_door_owned_slot_true_on_a_clean_manifest_match(tmp_path, monkeypatch):
    from coordinator_core.install import settings_home_report

    bin_dir = tmp_path
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / "cross-repo-memo"
    path.write_text("stub", encoding="utf-8")

    _write_native_forwarder_manifest(bin_dir, {"cross-repo-memo"})

    monkeypatch.setattr(settings_home_report, "is_door_installed", lambda d: True)
    monkeypatch.setattr(settings_home_report, "is_native_image", lambda p: True)

    door_owned = _is_door_owned_forwarder_slot("cross-repo-memo", path, bin_dir)
    state = _read_native_forwarder_manifest_state(bin_dir)

    assert door_owned is True
    assert state.measured is True


def test_check_settings_home_surfaces_manifest_unmeasured(tmp_path, monkeypatch):
    """End-to-end through the reporting surface: an unmeasured manifest for
    a name this check cannot otherwise confirm door-owned lands in
    `native_forwarder_manifest_unmeasured`, not silently folded into
    `forwarder_unverified` alone."""
    from coordinator_core.install import settings_home_report

    settings_home_path = tmp_path / "settings-home"
    bin_dir = settings_home_path / "bin"
    bin_dir.mkdir(parents=True)
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)

    path = bin_dir / "cross-repo-memo"
    path.write_bytes(b"\x7fELF-not-a-forwarder-body")
    manifest_path = _native_forwarder_manifest_path(bin_dir)
    manifest_path.write_text("not json{{{", encoding="utf-8")

    monkeypatch.setattr(
        settings_home_report,
        "expected_forwarders",
        lambda claude_klabauter_root: {"cross-repo-memo": "cross-repo-memo"},
    )
    monkeypatch.setattr(
        settings_home_report,
        "resolve_engine_root_for_install",
        lambda: type("R", (), {"root": None})(),
    )
    monkeypatch.setattr(settings_home_report, "is_door_installed", lambda d: True)
    monkeypatch.setattr(settings_home_report, "is_native_image", lambda p: False)
    monkeypatch.setattr(settings_home_report, "forwarder_body_is_ours", lambda p, t: False)
    monkeypatch.setattr(
        settings_home_report, "_byte_copied_body_matches_source", lambda n, p, r: False
    )

    report = settings_home_report.check_settings_home(settings_home_path, claude_klabauter_root)

    assert "cross-repo-memo" in report.native_forwarder_manifest_unmeasured
    assert "cross-repo-memo" in report.forwarder_unverified

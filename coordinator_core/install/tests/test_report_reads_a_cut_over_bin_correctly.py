"""The settings-home report tells the truth about a bin/ full of native images.

TWO WAYS THE SAME REPORT LIED, both measured on this box after the 372-name
cutover landed, and both the same defect underneath: a check written when
`coordinator-invoke` was the only door-owned name, still asking that
question of a bin/ where 371 more names are door-owned too.

  - `bin/ forwarders: 2/376 verified`. A cut-over name's body IS the door
    image, so `forwarder_body_is_ours` -- which looks for the Python
    forwarder marker -- says no, correctly. The report read that as
    corruption for every name but the bare one.
  - every deliberately-imageless name reported permanently stale. The
    currency audit was handed all 376 names, and at that layer "no image
    installed" is indistinguishable from "image is a build behind".

Both fail `complete`, so a correct install reported itself broken -- and a
red that is always red is a red nobody reads, which is worse than no check.
The population fix was measured by claude-klabauter-b0 (the residual stale set
was EXACTLY the two predicates' complement, verified empty in both
directions); this file is its guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.install import door_install, settings_home_report
from coordinator_core.warm.door import build as door_build

_MACH_O = b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01"


@pytest.fixture
def bin_dir(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    return d


def _write_manifest(bin_dir: Path, names: list[str]) -> None:
    (bin_dir / "_native-forwarder-manifest.json").write_text(
        json.dumps({"names": names}), encoding="utf-8"
    )


# --- the audit population -------------------------------------------------


def test_a_process_replacing_name_is_never_audited_for_currency(bin_dir):
    """`claude-doe` is refused a door image on purpose, so asking whether its
    image is current has no answer but "stale", forever."""
    names = settings_home_report._names_the_installer_gives_an_image(
        ["blocked", "claude-doe"], bin_dir
    )
    assert names == ["blocked"]


def test_a_name_the_engine_carries_no_script_for_is_not_audited(bin_dir, tmp_path):
    """The publish-excluded population -- repo-side tools deliberately not
    carried into the published engine. The engine root comes from the door's
    own sidecar, which is the root the installed images were built against."""
    engine = tmp_path / "engine"
    (engine / "coordinator" / "bin").mkdir(parents=True)
    (engine / "coordinator" / "bin" / "blocked.py").write_text("x", encoding="utf-8")
    (bin_dir / door_build.SIDECAR_FILENAME).write_text(str(engine), encoding="utf-8")

    names = settings_home_report._names_the_installer_gives_an_image(
        ["blocked", "publish"], bin_dir
    )

    assert "blocked" in names
    assert "publish" not in names


def test_an_unreadable_sidecar_drops_only_the_engine_leg(bin_dir):
    """No sidecar means the engine question cannot be ASKED -- so it is not
    answered "exempt". The warm-servable filter still applies and everything
    else audits as before, which is the direction that cannot hide a stale
    image."""
    names = settings_home_report._names_the_installer_gives_an_image(
        ["blocked", "publish", "claude-doe"], bin_dir
    )
    assert names == ["blocked", "publish"]


# --- the door-owned predicate ---------------------------------------------


def test_a_manifested_native_image_reads_as_door_owned(bin_dir, monkeypatch):
    """The 371 names the 2026-09-02 cutover installed. Their bodies are
    images because that is what a cut-over name's body IS."""
    monkeypatch.setattr(settings_home_report, "is_door_installed", lambda d: True)
    path = bin_dir / "blocked"
    path.write_bytes(_MACH_O)
    _write_manifest(bin_dir, ["blocked"])

    assert settings_home_report._is_door_owned_forwarder_slot("blocked", path, bin_dir)


def test_a_manifested_name_whose_image_became_garbage_still_fails(bin_dir, monkeypatch):
    """Manifest membership alone would exempt a name whose image was later
    replaced by something that is not an image -- exactly the corruption the
    original `installed_name == BARE_FORWARDER_NAME` gate existed to keep
    catching."""
    monkeypatch.setattr(settings_home_report, "is_door_installed", lambda d: True)
    path = bin_dir / "blocked"
    path.write_text("garbage, not a real forwarder body", encoding="utf-8")
    _write_manifest(bin_dir, ["blocked"])

    assert not settings_home_report._is_door_owned_forwarder_slot("blocked", path, bin_dir)


def test_an_unmanifested_binary_is_not_exempted(bin_dir, monkeypatch):
    """The other half: magic bytes alone would exempt any binary someone
    dropped into `bin/`. Both conditions, never either."""
    monkeypatch.setattr(settings_home_report, "is_door_installed", lambda d: True)
    path = bin_dir / "blocked"
    path.write_bytes(_MACH_O)
    _write_manifest(bin_dir, ["something-else"])

    assert not settings_home_report._is_door_owned_forwarder_slot("blocked", path, bin_dir)


def test_nothing_is_door_owned_when_no_door_is_installed(bin_dir, monkeypatch):
    """A manifest is a record of what an install once wrote. With the door
    itself gone, the images it names are leftovers, not a healthy cutover."""
    monkeypatch.setattr(settings_home_report, "is_door_installed", lambda d: False)
    path = bin_dir / "blocked"
    path.write_bytes(_MACH_O)
    _write_manifest(bin_dir, ["blocked"])

    assert not settings_home_report._is_door_owned_forwarder_slot("blocked", path, bin_dir)


def test_is_native_image_is_false_for_an_absent_path(tmp_path):
    """The predicate answers "is it an image", never "does it exist" -- its
    callers already distinguish absent from present, and conflating the two
    here would report a missing forwarder as a corrupt one."""
    assert not door_install.is_native_image(tmp_path / "nothing-here")

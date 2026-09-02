"""The installed argv[0] door images must track the door this install landed,
that door must track the sources this tree ships, and the completeness check
must go red when either stops being true -- and ONLY then.

TWO ORACLES, NOT THE PREBUILT (2026-09-02). This file was written against
`_PREBUILT_DOOR_EXE` on a Windows box. Only `door.exe` is committed: the POSIX
`door` is `.gitignore`d at `.gitignore:223`, because a POSIX image bakes an
absolute interpreter path at compile time and one box's binary is wrong on any
other. So on POSIX the prebuilt oracle answered "no readable prebuilt" on a
fresh clone, or answered off whatever stale local build artifact a developer's
tree carried -- and these tests passed either way, because the fixture read the
same file the code did. Measured on machine-b: an install that had just built
all 387 images from current source reported all 387 "a build behind" against a
10-day-old ignored `door`, with a remediation (re-run the installer) that
reproduced the verdict exactly. The per-slot reference is now the installed
door (`_reference_image_bytes`) and the door's own currency is its recorded
source fingerprint (`_current_source_fingerprint`); both hold on every platform.

WHAT WENT WRONG (2026-09-01). A full `scripts/setup.py` run exited 0 and
printed `PASS [settings-home] bin/ forwarders: 384/384 verified` on a box
where 373 of 374 installed `.exe` images were hardlinks to one orphaned
inode carrying the pre-`bc604470c3` door build -- the build without the
mode-gated stdin read. Every invocation of `cross-repo-memo` hung. Two
independent defects produced that, and this file pins both:

  1. `substrate._install_bin_resolvers` fed the native door leg
     `claude_klabauter_root_resolved`, the live claude-klabauter checkout, which carries no
     engine stamp and never will. `_write_native_door_forwarder` rejected
     it for every name and fell through to the Python forwarder, so no
     `.exe` was ever written or refreshed. See `_door_engine_root`.
  2. Nothing anywhere compared an installed image to the prebuilt. The
     forwarder count, `verify_installed_provenance`, and the native
     manifest are all satisfied by a self-consistent stale install -- a
     check whose success signal is satisfied by the failure it exists to
     catch.

DISCRIMINATION IS THE POINT, NOT COVERAGE. Each test below asserts BOTH
polarities against the same fixture: current bytes must pass, divergent
bytes must fail. A test that only asserts the red case cannot tell a
working currency check from one that reports everything stale.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from coordinator_core.install import door_install, settings_home_report, substrate


def _plant(bin_dst: Path, name: str, payload: bytes) -> Path:
    dest = door_install.named_forwarder_path(bin_dst, name)
    dest.write_bytes(payload)
    return dest


def _plant_door(bin_dst: Path, payload: bytes, *, sources: "dict | None" = None) -> Path:
    """Install a door image plus a self-consistent provenance sidecar at
    `bin_dst`. `sources` defaults to THIS tree's real door-source fingerprint,
    which is what makes the pair read `ok` -- pass a mutated mapping for the
    build-behind polarity."""
    dest = bin_dst / door_install.DOOR_INSTALLED_NAME
    dest.write_bytes(payload)
    door_install.installed_provenance_path(bin_dst).write_text(
        json.dumps(
            {
                "image_sha256": hashlib.sha256(payload).hexdigest(),
                "sources": door_install._current_source_fingerprint()
                if sources is None
                else sources,
            }
        ),
        encoding="utf-8",
    )
    return dest


@pytest.fixture()
def prebuilt_bytes() -> bytes:
    """The bytes every argv[0] slot is measured against.

    NOT `_PREBUILT_DOOR_EXE`, despite the name this fixture keeps for its
    callers. `coordinator_core/warm/door/door` is `.gitignore`d (only the
    Windows `door.exe` is committed), so on POSIX this fixture used to read
    either nothing at all or whatever stale local build artifact the developer's
    tree happened to carry -- and every assertion below then agreed with that
    artifact rather than with the installer. The reference is the door the
    install lands at `DOOR_INSTALLED_NAME`; see `door_install.
    _reference_image_bytes`. A synthetic payload is enough, but it must lead
    with real native magic: `audit_installed_image_currency` skips any slot that
    is not a compiled image, so a payload without it would be classified as a
    Python forwarder and never reach the comparison under test.
    """
    return door_install.NATIVE_IMAGE_MAGIC[0] + b"door image bytes for this box" * 8


def test_audit_discriminates_current_from_stale(tmp_path, prebuilt_bytes):
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    _plant_door(bin_dst, prebuilt_bytes)
    _plant(bin_dst, "current-one", prebuilt_bytes)
    _plant(bin_dst, "current-two", prebuilt_bytes)
    # Same length, different bytes: the size short-circuit must not be the
    # only discriminator, or a same-size rebuild reads as current.
    _plant(bin_dst, "stale-same-size", prebuilt_bytes[:4] + b"\x00" + prebuilt_bytes[5:])
    _plant(bin_dst, "stale-short", door_install.NATIVE_IMAGE_MAGIC[0] + b"short")

    audit = door_install.audit_installed_image_currency(
        bin_dst,
        ["current-one", "current-two", "stale-same-size", "stale-short", "never-installed"],
    )

    assert audit.current == ["current-one", "current-two"]
    assert audit.stale == ["stale-same-size", "stale-short"]
    # An absent slot appears in neither list -- `check_settings_home`'s own
    # forwarder-missing leg reports it, off a different artifact.
    assert "never-installed" not in audit.current + audit.stale


def test_audit_reads_one_inode_once(tmp_path, prebuilt_bytes, monkeypatch):
    """The slots are hardlinks to a single image by construction. Hashing per
    name would read the same image once per name -- 373 reads on the measured
    box, whose load norm is 50-70 concurrent sessions."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    _plant_door(bin_dst, prebuilt_bytes)
    first = _plant(bin_dst, "a", prebuilt_bytes)
    names = ["a"]
    for name in ("b", "c", "d"):
        link = door_install.named_forwarder_path(bin_dst, name)
        try:
            os.link(first, link)
        except OSError:
            pytest.skip("filesystem does not support hardlinks")
        names.append(name)

    reads: list[Path] = []
    real_read = Path.read_bytes

    def counting_read(self):
        reads.append(Path(self))
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read)
    audit = door_install.audit_installed_image_currency(bin_dst, names)

    assert audit.stale == [] and audit.current == names
    # One read for the reference door, one for the shared inode. Never one
    # per name.
    assert len(reads) == 2, reads


def test_provenance_verdict_separates_currency_from_self_consistency(tmp_path, prebuilt_bytes):
    """A stale install's exe and sidecar agree with each other perfectly --
    that agreement is what `ok` used to certify, and it is exactly what a
    build-behind box satisfies."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    # BOTH PAIRS ARE INTERNALLY PERFECT. What separates them is whether the
    # sources the sidecar records are the sources this tree ships -- the only
    # currency question POSIX can answer, since it ships no prebuilt to compare
    # an image against (see the `prebuilt_bytes` fixture).
    _plant_door(bin_dst, prebuilt_bytes)
    assert door_install.verify_installed_provenance(bin_dst).status == "ok"

    drifted = dict(door_install._current_source_fingerprint())
    drifted["door_posix.c"] = "0" * 64
    _plant_door(bin_dst, prebuilt_bytes[:4] + b"\x00" + prebuilt_bytes[5:], sources=drifted)
    verdict = door_install.verify_installed_provenance(bin_dst)
    assert verdict.status == "stale", verdict
    assert "build behind" in verdict.detail
    assert "door_posix.c" in verdict.detail


def test_report_goes_red_only_when_an_image_diverges(tmp_path, prebuilt_bytes, monkeypatch):
    """The regression proper: the completeness line must not read PASS while
    an installed image is a build behind."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    monkeypatch.setattr(
        settings_home_report, "expected_forwarders", lambda _root: {"cross-repo-memo": "x"}
    )
    monkeypatch.setattr(
        settings_home_report, "forwarder_body_is_ours", lambda _p, _t: True
    )
    _plant_door(bin_dst, prebuilt_bytes)

    _plant(bin_dst, "cross-repo-memo", prebuilt_bytes)
    report = settings_home_report.check_settings_home(tmp_path, Path("."))
    assert report.door_image_stale == []
    assert not any(
        "build behind" in line for line in settings_home_report.format_report_lines(report)
    )

    _plant(bin_dst, "cross-repo-memo", prebuilt_bytes[:4] + b"\x00" + prebuilt_bytes[5:])
    report = settings_home_report.check_settings_home(tmp_path, Path("."))
    assert report.door_image_stale == ["cross-repo-memo"]
    assert any(
        "build behind" in line for line in settings_home_report.format_report_lines(report)
    )


def test_door_leg_never_installs_from_the_live_claude_klabauter_checkout(monkeypatch):
    """`_door_engine_root` must answer with the install resolver's root, and
    the live checkout is never that answer -- `engine_root_for_install`'s own
    negative spec. Passing the checkout through is what silently disabled the
    whole native leg."""
    from coordinator_core.install import engine_root_for_install
    from coordinator_core.warm.engine_root import is_engine_root

    published = Path("/published/engine")
    monkeypatch.setattr(
        engine_root_for_install,
        "resolve_engine_root_for_install",
        lambda: engine_root_for_install.InstallEngineRoot(
            kind="published", root=published, remediation=None
        ),
    )
    assert substrate._door_engine_root() == published

    monkeypatch.setattr(
        engine_root_for_install,
        "resolve_engine_root_for_install",
        lambda: engine_root_for_install.InstallEngineRoot(
            kind="none", root=None, remediation="do the thing"
        ),
    )
    assert substrate._door_engine_root() is None

    # The shape that made the live checkout look installable to nobody: it is
    # not a stamped engine root, so feeding it to the door leg can only ever
    # produce the silent 382-name fallthrough.
    assert not is_engine_root(Path(__file__).resolve().parents[3])


def test_a_cut_over_name_counts_as_present_without_a_python_body(tmp_path, prebuilt_bytes, monkeypatch):
    """`remove_superseded_python_forwarders` deletes the Python body on a
    successful cutover -- deliberately, under ONE ENTRYPOINT PER PLATFORM.
    Reading the body first therefore scores a correctly-installed box as
    missing every name it just installed: `17/384 verified` on a box whose
    369 images were all current (2026-09-01, the first run after the door
    leg was rewired)."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    monkeypatch.setattr(
        settings_home_report, "expected_forwarders", lambda _root: {"cross-repo-memo": "x"}
    )
    # No Python body at bin/cross-repo-memo -- only the native image.
    _plant_door(bin_dst, prebuilt_bytes)
    _plant(bin_dst, "cross-repo-memo", prebuilt_bytes)

    report = settings_home_report.check_settings_home(tmp_path, Path("."))

    assert report.forwarder_missing == []
    assert report.forwarder_door_owned == ["cross-repo-memo"]
    assert report.forwarder_present == 1
    assert report.forwarder_expected == 1


def test_an_unanswerable_currency_question_is_not_reported_as_ok(tmp_path, monkeypatch):
    """The P1 the reviewer found, pinned: an unreadable prebuilt used to fall
    through to `ok`, so a caller could not tell "verified current" from "could
    not look" -- while `check_settings_home`'s currency leg FAILED loudly on
    the identical condition. Two sibling gates, opposite verdicts, one cause,
    inside the very change that exists to stop that shape."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    payload = b"whatever this box installed"
    _plant_door(bin_dst, payload)

    def _unreadable() -> "dict[str, str]":
        raise OSError("door sources unreadable in this checkout")

    monkeypatch.setattr(door_install, "_current_source_fingerprint", _unreadable)

    verdict = door_install.verify_installed_provenance(bin_dst)

    assert verdict.status == "unverifiable", verdict
    assert "could not be checked" in verdict.detail


def test_no_installed_door_is_unanswerable_not_every_slot_stale(tmp_path):
    """A `bin/` with no door holds no images this install placed, so there is
    nothing to measure -- and measuring its Python forwarders against a door
    binary anyway is how the check came to report all 387 slots "a build
    behind" on a box that had just built every one of them from current source
    (machine-b, 2026-09-02). The absent door is reported on its own artifact,
    not restated once per slot."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    _plant(bin_dst, "cross-repo-memo", b"#!/usr/bin/env python3\n")

    with pytest.raises(door_install.DoorInstallError) as excinfo:
        door_install.audit_installed_image_currency(bin_dst, ["cross-repo-memo"])

    assert "no installed door" in str(excinfo.value)


def test_a_current_posix_door_is_not_rebuilt(tmp_path, monkeypatch):
    """`install_named_forwarder` calls `install_door` once per name, and the
    POSIX build has no `/Brepro` equivalent -- so rebuilding unconditionally
    manufactured the split the currency audit then reported: 371 slots at one
    sha, the bare name at another, same sources, same box, three minutes apart
    (machine-b, 2026-09-02). A door already current for this tree's sources is
    left alone."""
    if os.name == "nt":
        pytest.skip("the POSIX build branch is not reached on Windows")
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    _plant_door(bin_dst, b"already current for these sources")

    monkeypatch.setattr(door_install, "is_engine_root", lambda _root: True)
    builds: list[Path] = []
    monkeypatch.setattr(
        door_install,
        "_sweep_displaced_images",
        lambda _bin: None,
        raising=False,
    )

    def _explode(*_a, **_kw):
        builds.append(bin_dst)
        raise AssertionError("rebuilt a door that was already current")

    import coordinator_core.install.door_install_posix_build as posix_build

    monkeypatch.setattr(posix_build, "build_or_advise", _explode)

    door_install.install_door(bin_dst, tmp_path / "engine")

    assert builds == []

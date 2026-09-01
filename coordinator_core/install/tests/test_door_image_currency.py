"""The installed argv[0] door images must track the committed prebuilt, and
the completeness check must go red when they don't.

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


@pytest.fixture()
def prebuilt_bytes() -> bytes:
    return door_install._PREBUILT_DOOR_EXE.read_bytes()


def test_audit_discriminates_current_from_stale_and_missing(tmp_path, prebuilt_bytes):
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    _plant(bin_dst, "current-one", prebuilt_bytes)
    _plant(bin_dst, "current-two", prebuilt_bytes)
    # Same length, different bytes: the size short-circuit must not be the
    # only discriminator, or a same-size rebuild reads as current.
    _plant(bin_dst, "stale-same-size", b"\x00" + prebuilt_bytes[1:])
    _plant(bin_dst, "stale-short", b"not a door")

    audit = door_install.audit_installed_image_currency(
        bin_dst,
        ["current-one", "current-two", "stale-same-size", "stale-short", "never-installed"],
    )

    assert audit.current == ["current-one", "current-two"]
    assert audit.stale == ["stale-same-size", "stale-short"]
    assert audit.missing == ["never-installed"]


def test_audit_reads_one_inode_once(tmp_path, prebuilt_bytes, monkeypatch):
    """The slots are hardlinks to a single image by construction. Hashing per
    name would read the same image once per name -- 373 reads on the measured
    box, whose load norm is 50-70 concurrent sessions."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
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
    # One read for the prebuilt, one for the shared inode. Never one per name.
    assert len(reads) == 2, reads


def test_audit_exempts_static_family_slots(tmp_path):
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    _plant(bin_dst, "claude-home", b"a static family owns this slot")

    assert door_install.audit_installed_image_currency(
        bin_dst, ["claude-home"]
    ).stale == ["claude-home"]
    assert door_install.audit_installed_image_currency(
        bin_dst, ["claude-home"], exempt_names={"claude-home"}
    ) == door_install.ImageCurrencyAudit(current=[], stale=[], missing=[])


def test_provenance_verdict_separates_currency_from_self_consistency(tmp_path, prebuilt_bytes):
    """A stale install's exe and sidecar agree with each other perfectly --
    that agreement is what `ok` used to certify, and it is exactly what a
    build-behind box satisfies."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    def write_pair(payload: bytes) -> None:
        (bin_dst / door_install.DOOR_INSTALLED_NAME).write_bytes(payload)
        door_install.installed_provenance_path(bin_dst).write_text(
            json.dumps({"image_sha256": hashlib.sha256(payload).hexdigest()}),
            encoding="utf-8",
        )

    write_pair(prebuilt_bytes)
    assert door_install.verify_installed_provenance(bin_dst).status == "ok"

    write_pair(b"\x00" + prebuilt_bytes[1:])
    verdict = door_install.verify_installed_provenance(bin_dst)
    assert verdict.status == "stale", verdict
    assert "build behind" in verdict.detail


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

    _plant(bin_dst, "cross-repo-memo", prebuilt_bytes)
    report = settings_home_report.check_settings_home(tmp_path, Path("."))
    assert report.door_image_stale == []
    assert not any(
        "build behind" in line for line in settings_home_report.format_report_lines(report)
    )

    _plant(bin_dst, "cross-repo-memo", b"\x00" + prebuilt_bytes[1:])
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
    (bin_dst / door_install.DOOR_INSTALLED_NAME).write_bytes(payload)
    door_install.installed_provenance_path(bin_dst).write_text(
        json.dumps({"image_sha256": hashlib.sha256(payload).hexdigest()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        door_install, "_PREBUILT_DOOR_EXE", tmp_path / "no-prebuilt-for-this-platform"
    )

    verdict = door_install.verify_installed_provenance(bin_dst)

    assert verdict.status == "unverifiable", verdict
    assert "could not be checked" in verdict.detail

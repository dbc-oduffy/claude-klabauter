"""Regression tests for `coordinator_core.install.door_serving_census`.

Spec backlink: docs/plans/2026-08-30-twenty-one-bin-names-reach-the-door-or-are-thoroughly-dead.md, chunk C4

Scope: `_classify` and `build_census`'s bucket logic, exercised against
constructed fixture trees rather than the live repo/engine — the four
buckets are the entire contract this module exists to get right, and a
constructed fixture can hold exactly one name in each state without
depending on today's live population (which changes as C3/C5 land).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from coordinator_core.install import door_serving_census as dsc


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def _install_image(settings_home_bin: Path, name: str) -> Path:
    """Build the image fixture THIS platform's installer would actually write
    (`door_install.named_forwarder_path`), plus the POSIX-only manifest entry
    that distinguishes a door image from the static-family shim sharing the
    extensionless slot.

    Not a convenience: the pre-2026-09-18 fixtures wrote `<name>.exe` on every
    platform, which is why a `.exe`-only `_installed_image_names` stayed green on
    Linux while returning the empty set in production -- the suite never
    constructed the shape POSIX installs. Tests that assert an image exists go
    through here; the platform-specific spellings are pinned explicitly by the
    skip-gated pair below.
    """
    from coordinator_core.install import door_install

    image = door_install.named_forwarder_path(settings_home_bin, name)
    _touch(image)
    if sys.platform != "win32":
        manifest = settings_home_bin / "_native-forwarder-manifest.json"
        try:
            names = json.loads(manifest.read_text(encoding="utf-8")).get("names", [])
        except (OSError, ValueError):
            names = []
        if name not in names:
            names.append(name)
        manifest.write_text(json.dumps({"names": names}), encoding="utf-8")
    return image


def test_serves_when_image_and_engine_resolution_both_present(tmp_path):
    generator = tmp_path / "generator" / "coordinator" / "bin"
    engine = tmp_path / "engine" / "coordinator" / "bin"
    settings_home_bin = tmp_path / "settings-home" / "bin"

    _touch(generator / "static-check.py")
    _touch(engine / "static-check.py")
    _install_image(settings_home_bin, "static-check")

    rows = dsc.build_census(
        generator_bin_dir=generator,
        engine_bin_dir=engine,
        settings_home_bin=settings_home_bin,
    )
    row = next(r for r in rows if r.name == "static-check")
    assert row.bucket == dsc.SERVES


def test_defect_when_image_exists_but_engine_cannot_resolve(tmp_path):
    generator = tmp_path / "generator" / "coordinator" / "bin"
    engine = tmp_path / "engine" / "coordinator" / "bin"
    settings_home_bin = tmp_path / "settings-home" / "bin"

    engine.mkdir(parents=True, exist_ok=True)
    _install_image(settings_home_bin, "coordinator-write-review-trail")

    rows = dsc.build_census(
        generator_bin_dir=generator,
        engine_bin_dir=engine,
        settings_home_bin=settings_home_bin,
    )
    row = next(r for r in rows if r.name == "coordinator-write-review-trail")
    assert row.bucket == dsc.DEFECT


def test_deliberate_no_image_for_a_publisher_side_name(tmp_path):
    """This repo's own `coordinator/bin/` carries the name, the engine does
    not, and there is no image — the publisher-side/renamed population
    `launcher_is_installable` already carves out. Must never read as a
    gap."""
    generator = tmp_path / "generator" / "coordinator" / "bin"
    engine = tmp_path / "engine" / "coordinator" / "bin"
    settings_home_bin = tmp_path / "settings-home" / "bin"

    _touch(generator / "publish.py")
    engine.mkdir(parents=True, exist_ok=True)
    settings_home_bin.mkdir(parents=True, exist_ok=True)

    rows = dsc.build_census(
        generator_bin_dir=generator,
        engine_bin_dir=engine,
        settings_home_bin=settings_home_bin,
    )
    row = next(r for r in rows if r.name == "publish")
    assert row.bucket == dsc.DELIBERATE_NO_IMAGE


def test_pending_cutover_when_engine_resolves_but_no_image_installed_yet(tmp_path):
    generator = tmp_path / "generator" / "coordinator" / "bin"
    engine = tmp_path / "engine" / "coordinator" / "bin"
    settings_home_bin = tmp_path / "settings-home" / "bin"

    _touch(generator / "chunk-commits")
    _touch(engine / "chunk-commits")
    settings_home_bin.mkdir(parents=True, exist_ok=True)

    rows = dsc.build_census(
        generator_bin_dir=generator,
        engine_bin_dir=engine,
        settings_home_bin=settings_home_bin,
    )
    row = next(r for r in rows if r.name == "chunk-commits")
    assert row.bucket == dsc.PENDING_CUTOVER


def test_static_family_bare_name_is_pending_not_deliberate(tmp_path, monkeypatch):
    """A name absent from BOTH `coordinator/bin/` trees but present as a
    bare entry in `_static_bin_family_names()` (the six static-family
    shims' pre-cutover shape) must land in PENDING_CUTOVER, never
    DELIBERATE_NO_IMAGE — merging the two is the exact failure the origin
    plan names as "swallowing (d) into (c)"."""
    generator = tmp_path / "generator" / "coordinator" / "bin"
    engine = tmp_path / "engine" / "coordinator" / "bin"
    settings_home_bin = tmp_path / "settings-home" / "bin"
    generator.mkdir(parents=True, exist_ok=True)
    engine.mkdir(parents=True, exist_ok=True)
    settings_home_bin.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        dsc,
        "_static_bin_family_names",
        lambda: frozenset({"machine-local", "machine-local.cmd", "_machine_local.py"}),
    )

    rows = dsc.build_census(
        generator_bin_dir=generator,
        engine_bin_dir=engine,
        settings_home_bin=settings_home_bin,
    )
    row = next(r for r in rows if r.name == "machine-local")
    assert row.bucket == dsc.PENDING_CUTOVER


def test_extensionless_engine_candidate_resolves_same_as_dotpy(tmp_path):
    """The two-candidate resolution order: `.py` first, extensionless
    fallback second — matching `invoke_from_argv._resolve_entrypoint_script`
    exactly, not a single-suffix rule."""
    engine = tmp_path / "coordinator" / "bin"
    _touch(engine / "with-suite-mutex")
    assert dsc._resolves_two_candidate(engine, "with-suite-mutex") is True
    assert dsc._resolves_two_candidate(engine, "nonexistent-name") is False


def test_render_census_reports_four_buckets_separately():
    rows = [
        dsc.CensusRow("a", dsc.SERVES, True, True, True),
        dsc.CensusRow("b", dsc.DEFECT, True, False, False),
        dsc.CensusRow("c", dsc.DELIBERATE_NO_IMAGE, False, False, True),
        dsc.CensusRow("d", dsc.PENDING_CUTOVER, False, True, False),
    ]
    out = dsc.render_census(rows)
    for bucket in (dsc.SERVES, dsc.DEFECT, dsc.DELIBERATE_NO_IMAGE, dsc.PENDING_CUTOVER):
        assert bucket in out
    assert "total=4" in out


def test_main_exits_nonzero_when_a_defect_is_present(tmp_path, monkeypatch):
    generator = tmp_path / "generator" / "coordinator" / "bin"
    engine = tmp_path / "engine" / "coordinator" / "bin"
    settings_home_bin = tmp_path / "settings-home" / "bin"
    generator.mkdir(parents=True, exist_ok=True)
    engine.mkdir(parents=True, exist_ok=True)
    _install_image(settings_home_bin, "coordinator-write-review-trail")

    monkeypatch.setattr(dsc, "DEFAULT_GENERATOR_BIN_DIR", generator)
    monkeypatch.setattr(dsc, "_settings_home_root", lambda: settings_home_bin.parent)

    class _FakeInstallEngineRoot:
        root = engine.parent.parent  # engine_root such that engine_root/coordinator/bin == engine

    monkeypatch.setattr(
        dsc, "resolve_engine_root_for_install", lambda: _FakeInstallEngineRoot()
    )

    exit_code = dsc.main([])
    assert exit_code == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX installs the image extensionless")
def test_posix_extensionless_image_named_by_the_manifest_serves(tmp_path):
    """The shape POSIX actually installs: an EXTENSIONLESS image, identified as
    a door image by the producer's own `_native-forwarder-manifest.json`. A
    `.exe`-only read returns the empty set here, so SERVES and DEFECT are
    structurally unreachable and the CLI cannot fail on the defect it exists to
    detect."""
    generator = tmp_path / "generator" / "coordinator" / "bin"
    engine = tmp_path / "engine" / "coordinator" / "bin"
    settings_home_bin = tmp_path / "settings-home" / "bin"

    _touch(generator / "static-check.py")
    _touch(engine / "static-check.py")
    _touch(settings_home_bin / "static-check")
    (settings_home_bin / "_native-forwarder-manifest.json").write_text(
        json.dumps({"names": ["static-check"]}), encoding="utf-8"
    )

    rows = dsc.build_census(
        generator_bin_dir=generator,
        engine_bin_dir=engine,
        settings_home_bin=settings_home_bin,
    )
    row = next(r for r in rows if r.name == "static-check")
    assert row.has_image, "the extensionless POSIX image must count as an image"
    assert row.bucket == dsc.SERVES


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only shared extensionless slot")
def test_posix_extensionless_shim_absent_from_the_manifest_is_not_an_image(tmp_path):
    """The reason the POSIX arm reads the manifest instead of testing the
    suffix: on POSIX the extensionless slot is SHARED with the static-family
    Python/shell shims (`named_forwarder_path`'s docstring). A bare suffix test
    calls each of those an image -- measured: 3 invented DEFECT rows on this
    box's live settings home."""
    generator = tmp_path / "generator" / "coordinator" / "bin"
    engine = tmp_path / "engine" / "coordinator" / "bin"
    settings_home_bin = tmp_path / "settings-home" / "bin"
    engine.mkdir(parents=True, exist_ok=True)
    generator.mkdir(parents=True, exist_ok=True)

    _touch(settings_home_bin / "machine-local")
    (settings_home_bin / "_native-forwarder-manifest.json").write_text(
        json.dumps({"names": []}), encoding="utf-8"
    )

    assert dsc._installed_image_names(settings_home_bin) == set(), (
        "a static-family shim in the shared extensionless slot is not a door image"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only manifest arm")
def test_posix_manifest_naming_an_absent_slot_reports_no_image(tmp_path):
    """Presence is intersected with the manifest, never trusted from it: a
    manifest naming a slot nothing occupies must never report a false SERVES."""
    settings_home_bin = tmp_path / "settings-home" / "bin"
    settings_home_bin.mkdir(parents=True, exist_ok=True)
    (settings_home_bin / "_native-forwarder-manifest.json").write_text(
        json.dumps({"names": ["never-installed"]}), encoding="utf-8"
    )
    assert dsc._installed_image_names(settings_home_bin) == set()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only manifest arm")
def test_posix_absent_or_unparsable_manifest_degrades_to_no_images(tmp_path):
    settings_home_bin = tmp_path / "settings-home" / "bin"
    _touch(settings_home_bin / "static-check")
    assert dsc._installed_image_names(settings_home_bin) == set()
    (settings_home_bin / "_native-forwarder-manifest.json").write_text(
        "not json", encoding="utf-8"
    )
    assert dsc._installed_image_names(settings_home_bin) == set()


def test_probe_resolves_this_platforms_own_image_spelling(tmp_path, capsys, monkeypatch):
    """`_probe` hardcoding `<name>.exe` reports "no installed image at ..." for
    every name on POSIX. It must dial `named_forwarder_path`, the single source
    of truth for the installed spelling.

    The spawn itself is stubbed -- a zero-byte fixture is not a runnable image on
    any platform, and what is under test is WHICH path `_probe` dials.
    """
    settings_home_bin = tmp_path / "settings-home" / "bin"
    image = _install_image(settings_home_bin, "static-check")
    spawned: "list[str]" = []

    class _Result:
        stdout = "usage: static-check\n"
        stderr = ""
        returncode = 0

    def _fake_run(cmd, **kwargs):
        spawned.append(cmd[0])
        return _Result()

    monkeypatch.setattr(dsc.subprocess, "run", _fake_run)

    rc = dsc._probe("static-check", settings_home_bin=settings_home_bin)
    captured = capsys.readouterr()

    assert "no installed image" not in captured.err, (
        f"the image exists at {image} under this platform's own spelling:\n{captured.err}"
    )
    assert spawned == [str(image)]
    assert rc == 0


def test_pytest_modules_in_generator_bin_are_not_census_candidates(tmp_path):
    """`coordinator/bin/` holds this tree's own pytest modules; none is a door
    image basename, and left in they report as PENDING_CUTOVER gaps."""
    generator = tmp_path / "generator" / "coordinator" / "bin"
    _touch(generator / "publish.py")
    _touch(generator / "conftest.py")
    _touch(generator / "test_publish_reaches_the_door.py")

    assert dsc._generator_bin_names(generator) == {"publish"}


def test_the_windows_arm_reads_the_suffix_on_every_host(monkeypatch, tmp_path):
    """The Windows branch, driven on POSIX too rather than excused with a skip.

    `sys.platform` is read at call time, so telling the function it is on Windows
    is an honest simulation that fabricates nothing on disk: it must then read
    `<name>.exe` from the suffix and consult no manifest. This is the SOLE pin on
    the Windows arm, on every host: the covered branch reads no platform-varying
    primitive other than `sys.platform` (no `os.name`, no PATHEXT, no case
    folding), so a native win32 run and this one exercise byte-identical code
    and the skip-gated twin it replaced was a permanent never-run-here test.
    The skip-unless-win32 pattern still earns its place on the arms that touch
    real filesystem semantics -- `named_forwarder_path` spelling, case folding.
    """
    # The native
    # skip-gated twin asserting this same proposition over this same fixture was
    # deleted; simulation is a total substitute for a suffix comparison.
    settings_home_bin = tmp_path / "settings-home" / "bin"
    _touch(settings_home_bin / "static-check.exe")
    _touch(settings_home_bin / "machine-local")

    monkeypatch.setattr(dsc.sys, "platform", "win32")
    assert dsc._installed_image_names(settings_home_bin) == {"static-check"}

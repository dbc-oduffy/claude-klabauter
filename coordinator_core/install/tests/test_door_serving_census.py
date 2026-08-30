"""Regression tests for `coordinator_core.install.door_serving_census`.

Spec backlink: docs/plans/2026-08-30-twenty-one-bin-names-reach-the-door-or-are-thoroughly-dead.md, chunk C4

Scope: `_classify` and `build_census`'s bucket logic, exercised against
constructed fixture trees rather than the live repo/engine — the four
buckets are the entire contract this module exists to get right, and a
constructed fixture can hold exactly one name in each state without
depending on today's live population (which changes as C3/C5 land).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.install import door_serving_census as dsc


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def test_serves_when_image_and_engine_resolution_both_present(tmp_path):
    generator = tmp_path / "generator" / "coordinator" / "bin"
    engine = tmp_path / "engine" / "coordinator" / "bin"
    settings_home_bin = tmp_path / "settings-home" / "bin"

    _touch(generator / "static-check.py")
    _touch(engine / "static-check.py")
    _touch(settings_home_bin / "static-check.exe")

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
    _touch(settings_home_bin / "coordinator-write-review-trail.exe")

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
    _touch(settings_home_bin / "coordinator-write-review-trail.exe")

    monkeypatch.setattr(dsc, "DEFAULT_GENERATOR_BIN_DIR", generator)
    monkeypatch.setattr(dsc, "_settings_home_root", lambda: settings_home_bin.parent)

    class _FakeInstallEngineRoot:
        root = engine.parent.parent  # engine_root such that engine_root/coordinator/bin == engine

    monkeypatch.setattr(
        dsc, "resolve_engine_root_for_install", lambda: _FakeInstallEngineRoot()
    )

    exit_code = dsc.main([])
    assert exit_code == 1

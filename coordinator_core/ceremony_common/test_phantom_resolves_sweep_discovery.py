
from __future__ import annotations

from pathlib import Path

from coordinator_core.ceremony_common.phantom_resolves_sweep import (
    discover_brief_defining_packages,
    discover_consumes_manifest_modules,
)


def test_discover_brief_defining_packages_finds_a_newly_planted_package(tmp_path: Path) -> None:
    fake_root = tmp_path / "coordinator_core"
    pkg_dir = fake_root / "fourth_ceremony_assemble"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text(
        'def brief(*, decisions=None):\n    return {"directives": [], "judgment_points": []}\n'
    )
    found = discover_brief_defining_packages(fake_root)
    assert "fourth_ceremony_assemble" in found
    assert found["fourth_ceremony_assemble"] == pkg_dir / "__init__.py"


def test_discover_brief_defining_packages_does_not_false_fire_on_a_helper_named_brief_like(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "coordinator_core"
    pkg_dir = fake_root / "not_an_assembler"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text(
        "def _brief_helper():\n    return {}\n\ndef briefing():\n    return {}\n"
    )
    found = discover_brief_defining_packages(fake_root)
    assert "not_an_assembler" not in found


def test_discover_consumes_manifest_modules_finds_a_newly_planted_manifest(tmp_path: Path) -> None:
    fake_root = tmp_path / "coordinator_core"
    pkg_dir = fake_root / "fourth_ceremony_assemble"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "brief.py").write_text(
        'CONSUMES_MANIFEST: tuple = ("new-cli-added-with-the-fourth-package",)\n'
    )
    found = discover_consumes_manifest_modules(fake_root)
    assert "fourth_ceremony_assemble" in found
    assert found["fourth_ceremony_assemble"] == pkg_dir / "brief.py"


def test_discover_consumes_manifest_modules_does_not_false_fire_on_a_reference_in_prose(
    tmp_path: Path,
) -> None:
    """A module that only MENTIONS `CONSUMES_MANIFEST` in a comment/
    docstring (as many `_CLI_DISPATCH`-shaped packages do, cross-
    referencing the ceremony packages' own manifest by name) must not be
    mistaken for a module that DEFINES one -- the regex is anchored to a
    module-level assignment (`^CONSUMES_MANIFEST\\s*[:=]`), never a bare
    substring match."""
    fake_root = tmp_path / "coordinator_core"
    pkg_dir = fake_root / "mentions_only"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text(
        '"""See CONSUMES_MANIFEST in workday_complete for the pattern this module follows."""\n'
        "_CLI_DISPATCH = {}\n"
    )
    found = discover_consumes_manifest_modules(fake_root)
    assert "mentions_only" not in found


def test_this_repos_live_discovery_matches_the_eleven_known_brief_packages() -> None:
    found = discover_brief_defining_packages()
    assert set(found) == {
        "backlog_grind_assemble",
        "baton_assemble",
        "consolidate_assemble",
        "learn_lessons_assemble",
        "merge_assemble",
        "orient_assemble",
        "pickup_assemble",
        "review_assemble",
        "workday_complete",
        "workstream_complete",
        "workweek_complete",
        "plan_assemble",
        "quick_wrap_assemble",
        "roadmap_planning_assemble",
        "sprint_planning_assemble",
    }

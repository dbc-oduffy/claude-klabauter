"""coordinator_core.ceremony_common.test_phantom_resolves_sweep_discovery —
proves the auto-discovery primitives `phantom_resolves_sweep.discover_
brief_defining_packages`/`discover_consumes_manifest_modules` actually pick
up a NEW package, not just the three/eleven that happen to exist in this
repo today.

Why this file exists: `test_argv_prog_slot_contract.py`'s manifest union
and `test_phantom_resolves_id_sweep.py`'s package registry are both now
auto-discovery-driven rather than hand-imported — the generalization this
guard-pair dispatch asked for. Proving that generalization actually bites
on a package added AFTER today means constructing a synthetic
`coordinator_core/`-shaped tree under `tmp_path` (never the real repo
tree — this repo's own discovered set is exercised live by the two
consumer test files) and confirming discovery finds the planted package.

Spec backlink: cross-repo/inbox/2026-07-27-… "Generalize seam guards
fleet-wide" dispatch (example-doctrine-repo, 2026-07-27), red-proof requirement.
"""

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
    """Regression pin against THIS repo's real tree (not a synthetic one):
    the 2026-07-27 generalization pass's own inventory found 11 `brief(`-
    defining packages. If this count changes, `test_phantom_resolves_id_
    sweep.py`'s three-bucket registration test (providers/verified-empty/
    deferred-allowlist) will independently fail by name for whichever
    package is new and unregistered -- this pin exists so a silent count
    DROP (a package's `brief(` disappearing without the corresponding
    sweep/allowlist entry being removed too) is equally visible, which the
    other test alone would not catch."""
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
    }

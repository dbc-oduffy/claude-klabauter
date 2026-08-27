"""Tests for coordinator_core.install.scaffold_structure -- Port D native
canonical-structure scaffold.

Covers AC D1-D6: manifest parse of eager dir + eager file-with-template
entries, idempotent no-clobber, README from readme: field with "# <dir>"
heading, .gitkeep only-when-empty, template copy, --dry-run mutates nothing,
manifest resolved relative to manifest-root not --root, declared-template
fatal-live vs warn-dry-run, and --root backslash normalization (coverage M3).
PLUS sentinel.probe_p12's amber-iff-would-create-≥1 mapping (signature parity,
graceful-absent on sibling_bin_dir=None / manifest-not-locatable, AC D2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.install.scaffold_structure import (
    WRITE_SURFACE,
    ScaffoldError,
    locate_manifest,
    main,
    parse_manifest,
    scaffold_canonical_structure,
)
from coordinator_core.install.write_surface import ShapedClause, validate

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

SIMPLE_MANIFEST = """\
entries:
  - path: state/lessons/
    creation: eager
    schema: null
    gitkeep: false
    readme: >
      Lessons captured during a session.

      Second paragraph.
  - path: state/scratch/
    creation: eager
    schema: null
    gitkeep: true
    readme: null
  - path: state/deferred/
    creation: lazy
    schema: null
    gitkeep: false
    readme: some deferred dir, not eager
  - path: docs/TEMPLATE.md
    creation: eager
    schema: null
    gitkeep: false
    readme: null
    template: templates/TEMPLATE.md
  - path: docs/no-template.md
    creation: eager
    schema: null
    gitkeep: false
    readme: null
"""


def _write_manifest(manifest_root: Path, text: str = SIMPLE_MANIFEST) -> None:
    manifest_root.mkdir(parents=True, exist_ok=True)
    (manifest_root / "canonical-structure.yaml").write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# parse_manifest
# --------------------------------------------------------------------------


def test_parse_manifest_selects_only_eager_dir_and_templated_file_entries():
    entries = parse_manifest(SIMPLE_MANIFEST).entries
    paths = {e.path for e in entries}
    # eager dir entries included
    assert "state/lessons/" in paths
    assert "state/scratch/" in paths
    # non-eager (creation: lazy) dir excluded
    assert "state/deferred/" not in paths
    # eager file WITH template included
    assert "docs/TEMPLATE.md" in paths
    # eager file WITHOUT template excluded (no creation vector — not dir, no template)
    assert "docs/no-template.md" not in paths


def test_parse_manifest_readme_block_scalar_preserves_paragraphs():
    entries = parse_manifest(SIMPLE_MANIFEST).entries
    lessons = next(e for e in entries if e.path == "state/lessons/")
    assert "Lessons captured during a session." in lessons.readme
    assert "Second paragraph." in lessons.readme


def test_parse_manifest_gitkeep_flag():
    entries = parse_manifest(SIMPLE_MANIFEST).entries
    scratch = next(e for e in entries if e.path == "state/scratch/")
    assert scratch.gitkeep is True
    lessons = next(e for e in entries if e.path == "state/lessons/")
    assert lessons.gitkeep is False


def test_parse_manifest_template_field():
    entries = parse_manifest(SIMPLE_MANIFEST).entries
    tmpl = next(e for e in entries if e.path == "docs/TEMPLATE.md")
    assert tmpl.template == "templates/TEMPLATE.md"


# --------------------------------------------------------------------------
# parse_manifest -- dropped (eager, no creation vector) reporting
# --------------------------------------------------------------------------


def test_parse_manifest_reports_templateless_eager_entry_as_dropped():
    """docs/no-template.md is `creation: eager`, not a dir, no template --
    this is the silent-drop defect: must now be reported, not just excluded
    from `.entries`."""
    parsed = parse_manifest(SIMPLE_MANIFEST)
    dropped_paths = {path for path, _reason in parsed.dropped}
    assert "docs/no-template.md" in dropped_paths
    reason = next(reason for path, reason in parsed.dropped if path == "docs/no-template.md")
    assert reason  # non-empty, human-readable


def test_parse_manifest_template_backed_eager_entry_not_dropped():
    parsed = parse_manifest(SIMPLE_MANIFEST)
    dropped_paths = {path for path, _reason in parsed.dropped}
    assert "docs/TEMPLATE.md" not in dropped_paths


def test_parse_manifest_dir_entry_not_dropped():
    parsed = parse_manifest(SIMPLE_MANIFEST)
    dropped_paths = {path for path, _reason in parsed.dropped}
    assert "state/lessons/" not in dropped_paths
    assert "state/scratch/" not in dropped_paths


def test_parse_manifest_non_eager_templateless_entry_not_reported_as_dropped():
    """state/deferred/ is `creation: lazy` and templateless -- it was never
    a creation candidate, so reporting it as dropped would be noise."""
    parsed = parse_manifest(SIMPLE_MANIFEST)
    dropped_paths = {path for path, _reason in parsed.dropped}
    assert "state/deferred/" not in dropped_paths


# --------------------------------------------------------------------------
# parse_manifest -- produced_by: satisfied-elsewhere (DR-116)
# --------------------------------------------------------------------------

PRODUCED_BY_MANIFEST = """\
entries:
  - path: CLAUDE.md
    creation: eager
    schema: null
    gitkeep: false
    readme: null
    produced_by: /coordinator:repo-setup phase 2 (template)
  - path: docs/no-template.md
    creation: eager
    schema: null
    gitkeep: false
    readme: null
  - path: state/lessons/
    creation: eager
    schema: null
    gitkeep: false
    readme: null
  - path: docs/both.md
    creation: eager
    schema: null
    gitkeep: false
    readme: null
    template: templates/both.md
    produced_by: /coordinator:repo-setup phase 3
  - path: docs/lazy-produced.md
    creation: lazy
    schema: null
    gitkeep: false
    readme: null
    produced_by: /coordinator:repo-setup phase 9
  - path: docs/empty-produced.md
    creation: eager
    schema: null
    gitkeep: false
    readme: null
    produced_by:
  - path: state/dir-with-producer/
    creation: eager
    schema: null
    gitkeep: false
    readme: null
    produced_by: /coordinator:repo-setup phase 4
"""


def test_parse_manifest_produced_by_entry_is_satisfied_elsewhere_not_dropped_not_created():
    parsed = parse_manifest(PRODUCED_BY_MANIFEST)
    entry_paths = {e.path for e in parsed.entries}
    dropped_paths = {path for path, _reason in parsed.dropped}
    satisfied_paths = {path for path, _producer in parsed.satisfied_elsewhere}

    assert "CLAUDE.md" not in entry_paths
    assert "CLAUDE.md" not in dropped_paths
    assert "CLAUDE.md" in satisfied_paths
    producer = next(p for path, p in parsed.satisfied_elsewhere if path == "CLAUDE.md")
    assert producer == "/coordinator:repo-setup phase 2 (template)"


def test_parse_manifest_genuine_orphan_still_dropped_regression_d6fa361d():
    """An eager entry with neither template: nor produced_by: is still
    reported as a dropped orphan -- regression guard on d6fa361d."""
    parsed = parse_manifest(PRODUCED_BY_MANIFEST)
    dropped_paths = {path for path, _reason in parsed.dropped}
    satisfied_paths = {path for path, _producer in parsed.satisfied_elsewhere}
    assert "docs/no-template.md" in dropped_paths
    assert "docs/no-template.md" not in satisfied_paths


def test_parse_manifest_directory_entry_unaffected_by_produced_by_logic():
    parsed = parse_manifest(PRODUCED_BY_MANIFEST)
    entry_paths = {e.path for e in parsed.entries}
    dropped_paths = {path for path, _reason in parsed.dropped}
    satisfied_paths = {path for path, _producer in parsed.satisfied_elsewhere}
    assert "state/lessons/" in entry_paths
    assert "state/lessons/" not in dropped_paths
    assert "state/lessons/" not in satisfied_paths


def test_parse_manifest_both_template_and_produced_by_template_wins():
    """Precedence decision (documented in parse_manifest's docstring):
    template-backed creation wins over produced_by when both are present --
    it is the concrete capability this module owns and can execute."""
    parsed = parse_manifest(PRODUCED_BY_MANIFEST)
    entry_paths = {e.path for e in parsed.entries}
    dropped_paths = {path for path, _reason in parsed.dropped}
    satisfied_paths = {path for path, _producer in parsed.satisfied_elsewhere}
    assert "docs/both.md" in entry_paths
    assert "docs/both.md" not in dropped_paths
    assert "docs/both.md" not in satisfied_paths
    both_entry = next(e for e in parsed.entries if e.path == "docs/both.md")
    assert both_entry.template == "templates/both.md"
    assert both_entry.produced_by == "/coordinator:repo-setup phase 3"


def test_parse_manifest_directory_entry_with_produced_by_dir_wins():
    """Precedence decision (documented in parse_manifest's docstring): a
    directory entry wins over produced_by when both are present, same as
    template-backed creation -- the directory is a concrete capability this
    module owns and can execute, produced_by is discarded."""
    parsed = parse_manifest(PRODUCED_BY_MANIFEST)
    entry_paths = {e.path for e in parsed.entries}
    dropped_paths = {path for path, _reason in parsed.dropped}
    satisfied_paths = {path for path, _producer in parsed.satisfied_elsewhere}
    assert "state/dir-with-producer/" in entry_paths
    assert "state/dir-with-producer/" not in dropped_paths
    assert "state/dir-with-producer/" not in satisfied_paths
    dir_entry = next(e for e in parsed.entries if e.path == "state/dir-with-producer/")
    assert dir_entry.produced_by == "/coordinator:repo-setup phase 4"


def test_parse_manifest_non_eager_produced_by_not_reported_anywhere():
    parsed = parse_manifest(PRODUCED_BY_MANIFEST)
    entry_paths = {e.path for e in parsed.entries}
    dropped_paths = {path for path, _reason in parsed.dropped}
    satisfied_paths = {path for path, _producer in parsed.satisfied_elsewhere}
    assert "docs/lazy-produced.md" not in entry_paths
    assert "docs/lazy-produced.md" not in dropped_paths
    assert "docs/lazy-produced.md" not in satisfied_paths


def test_parse_manifest_empty_produced_by_treated_as_absent():
    """An empty/null produced_by: is ABSENT, not satisfied-elsewhere -- the
    entry falls through to the genuine-orphan (dropped) case."""
    parsed = parse_manifest(PRODUCED_BY_MANIFEST)
    dropped_paths = {path for path, _reason in parsed.dropped}
    satisfied_paths = {path for path, _producer in parsed.satisfied_elsewhere}
    assert "docs/empty-produced.md" in dropped_paths
    assert "docs/empty-produced.md" not in satisfied_paths


def test_parse_manifest_no_produced_by_anywhere_behaves_as_today():
    """Requirement 5 pin: a manifest with no produced_by: anywhere (the
    SIMPLE_MANIFEST fixture) behaves exactly as it did before this change --
    empty satisfied_elsewhere, unchanged entries/dropped."""
    parsed = parse_manifest(SIMPLE_MANIFEST)
    assert parsed.satisfied_elsewhere == []
    paths = {e.path for e in parsed.entries}
    assert paths == {"state/lessons/", "state/scratch/", "docs/TEMPLATE.md"}
    dropped_paths = {path for path, _reason in parsed.dropped}
    assert dropped_paths == {"docs/no-template.md"}


def test_satisfied_elsewhere_reaches_caller_visible_result(tmp_path: Path):
    """The satisfied-elsewhere channel reaches ScaffoldResult, distinct from
    dropped_entries and from created/skipped counts, in both modes."""
    root = tmp_path / "root"
    root.mkdir()
    manifest_root = tmp_path / "manifest_root"
    _write_manifest(manifest_root, PRODUCED_BY_MANIFEST)
    (manifest_root / "templates").mkdir(parents=True)
    (manifest_root / "templates" / "both.md").write_text("both body\n", encoding="utf-8")

    dry_result = scaffold_canonical_structure(root, manifest_root, dry_run=True)
    satisfied_paths = {path for path, _producer in dry_result.satisfied_elsewhere}
    assert "CLAUDE.md" in satisfied_paths
    dropped_paths = {path for path, _reason in dry_result.dropped_entries}
    assert "CLAUDE.md" not in dropped_paths
    assert any("CLAUDE.md" in line for line in dry_result.lines)
    assert not (root / "CLAUDE.md").exists()

    live_result = scaffold_canonical_structure(root, manifest_root, dry_run=False)
    assert not (root / "CLAUDE.md").exists()
    live_satisfied_paths = {path for path, _producer in live_result.satisfied_elsewhere}
    assert "CLAUDE.md" in live_satisfied_paths


# --------------------------------------------------------------------------
# locate_manifest
# --------------------------------------------------------------------------


def test_locate_manifest_found(tmp_path: Path):
    _write_manifest(tmp_path)
    manifest = locate_manifest(tmp_path)
    assert manifest == tmp_path / "canonical-structure.yaml"


def test_locate_manifest_absent_raises(tmp_path: Path):
    with pytest.raises(ScaffoldError, match="manifest not found"):
        locate_manifest(tmp_path)


# --------------------------------------------------------------------------
# scaffold_canonical_structure -- live mode
# --------------------------------------------------------------------------


def _template_manifest_root(tmp_path: Path) -> Path:
    manifest_root = tmp_path / "manifest_root"
    _write_manifest(manifest_root)
    (manifest_root / "templates").mkdir(parents=True)
    (manifest_root / "templates" / "TEMPLATE.md").write_text("template body\n", encoding="utf-8")
    return manifest_root


def test_live_creates_dirs_readme_gitkeep_and_template_file(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    manifest_root = _template_manifest_root(tmp_path)

    result = scaffold_canonical_structure(root, manifest_root, dry_run=False)

    assert (root / "state" / "lessons").is_dir()
    readme = (root / "state" / "lessons" / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# state/lessons\n\n")
    assert "Lessons captured during a session." in readme

    assert (root / "state" / "scratch").is_dir()
    assert (root / "state" / "scratch" / ".gitkeep").is_file()
    # gitkeep dirs do NOT get a README even with readme text present
    assert not (root / "state" / "scratch" / "README.md").exists()

    assert (root / "docs" / "TEMPLATE.md").read_text(encoding="utf-8") == "template body\n"

    assert result.created_dirs == 2
    assert result.created_readmes == 1
    assert result.created_gitkeeps == 1
    assert result.created_files == 1
    assert result.would_create_count() == 5


def test_live_is_idempotent_second_run_no_clobber(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    manifest_root = _template_manifest_root(tmp_path)

    scaffold_canonical_structure(root, manifest_root, dry_run=False)
    # Mutate the README so a second run must NOT clobber it.
    readme_path = root / "state" / "lessons" / "README.md"
    readme_path.write_text("hand-edited content\n", encoding="utf-8")

    second = scaffold_canonical_structure(root, manifest_root, dry_run=False)

    assert readme_path.read_text(encoding="utf-8") == "hand-edited content\n"
    assert second.created_dirs == 0
    assert second.created_readmes == 0
    assert second.created_gitkeeps == 0
    assert second.created_files == 0
    assert second.would_create_count() == 0
    assert second.skipped > 0


def test_gitkeep_not_recreated_once_dir_has_content(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    manifest_root = _template_manifest_root(tmp_path)

    scaffold_canonical_structure(root, manifest_root, dry_run=False)
    gitkeep = root / "state" / "scratch" / ".gitkeep"
    gitkeep.unlink()
    (root / "state" / "scratch" / "real-file.yaml").write_text("x\n", encoding="utf-8")

    result = scaffold_canonical_structure(root, manifest_root, dry_run=False)

    assert not gitkeep.exists()
    assert result.created_gitkeeps == 0


def test_declared_template_missing_is_fatal_on_live_run(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    manifest_root = tmp_path / "manifest_root"
    _write_manifest(manifest_root)
    # No templates/ dir created -- template is declared but absent on disk.

    with pytest.raises(ScaffoldError, match="declared template not found"):
        scaffold_canonical_structure(root, manifest_root, dry_run=False)


# --------------------------------------------------------------------------
# scaffold_canonical_structure -- dry-run mode
# --------------------------------------------------------------------------


def test_dry_run_mutates_nothing(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    manifest_root = _template_manifest_root(tmp_path)

    result = scaffold_canonical_structure(root, manifest_root, dry_run=True)

    assert not (root / "state").exists()
    assert not (root / "docs").exists()
    assert result.created_dirs == 2
    assert result.created_readmes == 1
    assert result.created_gitkeeps == 1
    assert result.created_files == 1
    assert result.would_create_count() == 5


def test_dry_run_after_live_run_reports_zero_would_create(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    manifest_root = _template_manifest_root(tmp_path)

    scaffold_canonical_structure(root, manifest_root, dry_run=False)
    result = scaffold_canonical_structure(root, manifest_root, dry_run=True)

    assert result.would_create_count() == 0


def test_dry_run_missing_template_warns_and_continues_not_fatal(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    manifest_root = tmp_path / "manifest_root"
    _write_manifest(manifest_root)
    # No templates/ dir -- template declared but absent.

    result = scaffold_canonical_structure(root, manifest_root, dry_run=True)

    # Missing-template entry must NOT count toward would_create (AC D2 / F7).
    assert result.created_files == 0
    assert any("declared template not found" in line for line in result.lines)


def test_would_create_excludes_missing_template_entries(tmp_path: Path):
    """AC D2 (F7): would-create count excludes entries whose template is
    missing -- the missing-template case is an error-continue, not a
    would-create, and must not trigger the probe_p12 amber threshold."""
    root = tmp_path / "root"
    root.mkdir()
    manifest_root = tmp_path / "manifest_root"
    dir_only_manifest = """\
entries:
  - path: docs/orphan.md
    creation: eager
    schema: null
    gitkeep: false
    readme: null
    template: templates/orphan.md
"""
    _write_manifest(manifest_root, dir_only_manifest)

    result = scaffold_canonical_structure(root, manifest_root, dry_run=True)

    assert result.would_create_count() == 0


# --------------------------------------------------------------------------
# --root backslash normalization (coverage M3, Windows Git-Bash/MSYS)
# --------------------------------------------------------------------------


def test_root_backslash_normalization(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    manifest_root = _template_manifest_root(tmp_path)

    # Simulate a Git-Bash/MSYS caller passing a backslash-separated root.
    backslash_root = str(root).replace("/", "\\")

    result = scaffold_canonical_structure(backslash_root, manifest_root, dry_run=False)

    assert (root / "state" / "lessons").is_dir()
    assert result.created_dirs == 2


# --------------------------------------------------------------------------
# Root existence guard
# --------------------------------------------------------------------------


def test_root_does_not_exist_raises(tmp_path: Path):
    manifest_root = _template_manifest_root(tmp_path)
    missing_root = tmp_path / "does-not-exist"

    with pytest.raises(ScaffoldError, match="root dir does not exist"):
        scaffold_canonical_structure(missing_root, manifest_root, dry_run=False)


def test_dropped_entries_reach_caller_visible_result(tmp_path: Path):
    """AC: the drop is counted and reported on ScaffoldResult, in both
    dry-run and live mode, and is not conflated with created/skipped."""
    root = tmp_path / "root"
    root.mkdir()
    manifest_root = _template_manifest_root(tmp_path)

    dry_result = scaffold_canonical_structure(root, manifest_root, dry_run=True)
    assert dry_result.dropped_entries == [
        (
            "docs/no-template.md",
            "creation: eager but neither a directory (no trailing '/') nor "
            "template-backed (no template: value) -- manifest/parser "
            "disagreement, not created",
        )
    ]
    assert any("docs/no-template.md" in line for line in dry_result.lines)
    # Not conflated with would-create.
    assert dry_result.would_create_count() == 5

    live_result = scaffold_canonical_structure(root, manifest_root, dry_run=False)
    assert len(live_result.dropped_entries) == 1
    assert live_result.dropped_entries[0][0] == "docs/no-template.md"
    assert any("docs/no-template.md" in line for line in live_result.lines)
    assert not (root / "docs" / "no-template.md").exists()


def test_manifest_not_locatable_raises(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    empty_manifest_root = tmp_path / "empty_manifest_root"
    empty_manifest_root.mkdir()

    with pytest.raises(ScaffoldError, match="manifest not found"):
        scaffold_canonical_structure(root, empty_manifest_root, dry_run=True)


# --------------------------------------------------------------------------
# sentinel.probe_p12 -- result -> ProbeNote mapping (signature UNCHANGED)
# --------------------------------------------------------------------------


def test_probe_p12_sibling_bin_dir_none_is_graceful_absent(tmp_path: Path):
    from coordinator_core.plugin_health.sentinel import probe_p12

    assert probe_p12(None, tmp_path) == []


def test_probe_p12_manifest_not_locatable_is_graceful_absent(tmp_path: Path):
    from coordinator_core.plugin_health.sentinel import probe_p12

    claude_home = tmp_path / "home"
    claude_home.mkdir()
    # sibling_bin_dir.parent resolves to a dir with no canonical-structure.yaml.
    sibling_bin_dir = tmp_path / "coordinator" / "bin"
    sibling_bin_dir.mkdir(parents=True)

    assert probe_p12(sibling_bin_dir, claude_home) == []


def test_probe_p12_amber_when_would_create_gte_one(tmp_path: Path):
    from coordinator_core.plugin_health.sentinel import probe_p12

    claude_home = tmp_path / "home"
    claude_home.mkdir()
    coordinator_root = tmp_path / "coordinator"
    sibling_bin_dir = coordinator_root / "bin"
    sibling_bin_dir.mkdir(parents=True)
    _write_manifest(coordinator_root)

    notes = probe_p12(sibling_bin_dir, claude_home)

    assert len(notes) == 1
    assert notes[0].id == "P-12"
    assert notes[0].severity == "amber"


def test_probe_p12_empty_when_would_create_zero(tmp_path: Path):
    from coordinator_core.plugin_health.sentinel import probe_p12

    claude_home = tmp_path / "home"
    claude_home.mkdir()
    coordinator_root = tmp_path / "coordinator"
    sibling_bin_dir = coordinator_root / "bin"
    sibling_bin_dir.mkdir(parents=True)
    _write_manifest(coordinator_root)
    (coordinator_root / "templates").mkdir(parents=True)
    (coordinator_root / "templates" / "TEMPLATE.md").write_text("template body\n", encoding="utf-8")

    # Pre-scaffold so a second (probe) dry-run reports zero would-create.
    scaffold_canonical_structure(claude_home, coordinator_root, dry_run=False)

    notes = probe_p12(sibling_bin_dir, claude_home)

    assert notes == []


# --------------------------------------------------------------------------
# main() -- standalone CLI entry (python3 -m coordinator_core.install.scaffold_structure)
# --------------------------------------------------------------------------


def test_main_dry_run_returns_zero_and_creates_nothing(tmp_path: Path, capsys):
    root = tmp_path / "root"
    root.mkdir()
    manifest_root = _template_manifest_root(tmp_path)

    exit_code = main(["--root", str(root), "--manifest-root", str(manifest_root), "--dry-run"])

    assert exit_code == 0
    assert not (root / "state").exists()
    assert not (root / "docs").exists()
    out = capsys.readouterr().out
    assert "would create" in out


def test_main_live_run_returns_zero_and_scaffolds(tmp_path: Path, capsys):
    root = tmp_path / "root"
    root.mkdir()
    manifest_root = _template_manifest_root(tmp_path)

    exit_code = main(["--root", str(root), "--manifest-root", str(manifest_root)])

    assert exit_code == 0
    assert (root / "state" / "lessons").is_dir()
    out = capsys.readouterr().out
    assert "scaffold complete" in out


def test_main_manifest_absent_returns_one(tmp_path: Path, capsys):
    root = tmp_path / "root"
    root.mkdir()
    empty_manifest_root = tmp_path / "empty_manifest_root"
    empty_manifest_root.mkdir()

    exit_code = main(["--root", str(root), "--manifest-root", str(empty_manifest_root)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "manifest not found" in err


def test_probe_p12_unexpected_exception_maps_to_absent(tmp_path: Path, monkeypatch):
    from coordinator_core.plugin_health import sentinel as sentinel_mod
    from coordinator_core.install import scaffold_structure as scaffold_mod

    def _raise(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(scaffold_mod, "scaffold_canonical_structure", _raise)

    claude_home = tmp_path / "home"
    claude_home.mkdir()
    sibling_bin_dir = tmp_path / "coordinator" / "bin"
    sibling_bin_dir.mkdir(parents=True)

    notes = sentinel_mod.probe_p12(sibling_bin_dir, claude_home)

    assert notes == []


class TestWriteSurfaceDeclaration:
    """AC coverage for scaffold_structure.WRITE_SURFACE (spec:
    docs/plans/2026-08-06-writer-declared-write-surface-manifest.md, chunk
    C3e). Asserts shape only -- never today's canonical-structure.yaml
    contents -- so this stays green across an unrelated manifest edit
    (machine-independence)."""

    def test_declaration_is_valid(self) -> None:
        assert validate(WRITE_SURFACE) == ()

    def test_declaration_names_the_writer_and_module(self) -> None:
        assert WRITE_SURFACE.writer_id == "scaffold-structure"
        assert WRITE_SURFACE.source_module == "coordinator_core.install.scaffold_structure"

    def test_surface_is_four_shaped_clauses_not_a_static_list(self) -> None:
        assert len(WRITE_SURFACE.clauses) == 4
        for clause in WRITE_SURFACE.clauses:
            assert isinstance(clause, ShapedClause)

    def test_all_clauses_discover_via_parse_manifest(self) -> None:
        for clause in WRITE_SURFACE.clauses:
            assert "parse_manifest" in clause.discovered_by

    def test_clauses_distinguish_dir_gitkeep_readme_and_file_entries(self) -> None:
        discovered_by = [clause.discovered_by for clause in WRITE_SURFACE.clauses]
        assert any("_scaffold_dir_entry" in d and "gitkeep" not in d.lower() and "readme" not in d.lower()
                    for d in discovered_by)
        assert any("gitkeep" in d.lower() for d in discovered_by)
        assert any("readme" in d.lower() for d in discovered_by)
        assert any("_scaffold_file_entry" in d for d in discovered_by)

    def test_all_entries_are_file_path_kind(self) -> None:
        for clause in WRITE_SURFACE.clauses:
            assert clause.entry_template.kind == "file-path"

    def test_entry_templates_carry_placeholders_not_resolved_paths(self) -> None:
        for clause in WRITE_SURFACE.clauses:
            assert "<manifest-declared" in (clause.entry_template.path or "")

    def test_declaration_is_independent_of_current_manifest_contents(
        self, tmp_path: Path
    ) -> None:
        """Parsing an arbitrary manifest must not mutate WRITE_SURFACE --
        the declaration is a static shape description, not a snapshot of
        whatever canonical-structure.yaml happens to say right now."""
        before = WRITE_SURFACE
        parse_manifest(SIMPLE_MANIFEST)
        assert WRITE_SURFACE is before


# --------------------------------------------------------------------------
# resolution journal wiring (C6)
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _journal_env(tmp_path, monkeypatch):
    from coordinator_core.install import resolution_journal as rj

    journal_path = tmp_path / "journal" / "resolution-journal.jsonl"
    monkeypatch.setenv(rj.RESOLUTION_JOURNAL_ENV_VAR, str(journal_path))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    return journal_path


class TestResolutionJournalWiring:
    def test_live_run_journals_resolved_entries_for_every_clause(self, tmp_path: Path) -> None:
        from coordinator_core.install import resolution_journal as rj
        from coordinator_core.install.receipt import derive_receipt_entries

        root = tmp_path / "root"
        root.mkdir()
        manifest_root = tmp_path / "manifest_root"
        _write_manifest(manifest_root, SIMPLE_MANIFEST)
        (manifest_root / "templates").mkdir(parents=True)
        (manifest_root / "templates" / "TEMPLATE.md").write_text("tmpl body\n", encoding="utf-8")

        scaffold_canonical_structure(root, manifest_root, dry_run=False)

        journal = rj.read_journal()
        assert "scaffold-structure" in journal
        resolutions = journal["scaffold-structure"]
        # All four clauses reported (this run examined every entry kind).
        assert set(resolutions) == {0, 1, 2, 3}

        dir_paths = {e.path for e in resolutions[0].entries}
        assert "state/lessons/" in dir_paths
        assert "state/scratch/" in dir_paths

        gitkeep_paths = {e.path for e in resolutions[1].entries}
        assert "state/scratch/.gitkeep" in gitkeep_paths

        readme_paths = {e.path for e in resolutions[2].entries}
        assert "state/lessons/README.md" in readme_paths
        # state/scratch/ is gitkeep: true -> never gets a README, even
        # though readme is null there anyway; assert the narrower real
        # gate (readme_text and not entry.gitkeep) held.
        assert "state/scratch/README.md" not in readme_paths

        file_paths = {e.path for e in resolutions[3].entries}
        assert "docs/TEMPLATE.md" in file_paths

        # Round-trip: derive_receipt_entries can consume what we journaled
        # for this writer without raising.
        receipt_entries = derive_receipt_entries(WRITE_SURFACE, resolutions)
        assert any(e.path == "docs/TEMPLATE.md" for e in receipt_entries)

    def test_dry_run_never_journals(self, tmp_path: Path) -> None:
        from coordinator_core.install import resolution_journal as rj

        root = tmp_path / "root"
        root.mkdir()
        manifest_root = tmp_path / "manifest_root"
        _write_manifest(manifest_root, SIMPLE_MANIFEST)
        (manifest_root / "templates").mkdir(parents=True)
        (manifest_root / "templates" / "TEMPLATE.md").write_text("tmpl body\n", encoding="utf-8")

        scaffold_canonical_structure(root, manifest_root, dry_run=True)

        assert rj.read_journal() == {}

    def test_skip_exists_branch_still_journals_the_real_on_disk_entry(self, tmp_path: Path) -> None:
        """A second live run, where everything already exists, must still
        journal the concrete entries -- they are real on-disk facts, not a
        phantom write."""
        from coordinator_core.install import resolution_journal as rj

        root = tmp_path / "root"
        root.mkdir()
        manifest_root = tmp_path / "manifest_root"
        _write_manifest(manifest_root, SIMPLE_MANIFEST)
        (manifest_root / "templates").mkdir(parents=True)
        (manifest_root / "templates" / "TEMPLATE.md").write_text("tmpl body\n", encoding="utf-8")

        scaffold_canonical_structure(root, manifest_root, dry_run=False)
        rj.clear_journal()

        scaffold_canonical_structure(root, manifest_root, dry_run=False)

        journal = rj.read_journal()
        resolutions = journal["scaffold-structure"]
        file_paths = {e.path for e in resolutions[3].entries}
        assert "docs/TEMPLATE.md" in file_paths

    def test_missing_declared_template_live_never_journals_the_file_clause(
        self, tmp_path: Path
    ) -> None:
        """A live run that raises ScaffoldError on a missing declared
        template must not leave a phantom entry in the journal for that
        run -- record_resolution for clause 3 is never reached."""
        from coordinator_core.install import resolution_journal as rj

        root = tmp_path / "root"
        root.mkdir()
        manifest_root = tmp_path / "manifest_root"
        _write_manifest(manifest_root, SIMPLE_MANIFEST)
        # Deliberately do NOT create templates/TEMPLATE.md.

        with pytest.raises(ScaffoldError):
            scaffold_canonical_structure(root, manifest_root, dry_run=False)

        assert rj.read_journal() == {}
        assert len(WRITE_SURFACE.clauses) == 4

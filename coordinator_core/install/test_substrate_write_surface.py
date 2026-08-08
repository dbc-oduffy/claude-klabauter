"""Tests for `substrate.WRITE_SURFACE`.

Spec backlink: docs/plans/2026-08-06-writer-declared-write-surface-manifest.md,
chunk C3c (`install-substrate`), corrected by chunk C3c2, extended by the
`<settings-home>/bin/` + overwrite-backup-tree follow-on dispatch closing
`state/debt-backlog/2026-08-06-write-surface-declarations-must-live-wit-e49b9cfd8ad1.yaml`.

Purpose: proves the declaration carries exactly the twenty-eight
survey-pinned clauses — C3c/C3c2's original six (two distinct `os-env-var`
Windows-PATH call sites, one `delete`-effect AppX-stub clause, one SHAPED
agent-helper forwarder-triple clause, two SHAPED percolation clauses), the
prior follow-on's nine (clauses 7-15: three SHAPED `<settings-home>/bin/`
static-manifest-driven family clauses `ml_family`/`ml_explicit`/
`platform_localize`, two STATIC hand-maintained family clauses
`ch_family`/`rm_family`, the installer's own provenance-ledger clause, two
`delete`-effect clauses `_sweep_orphaned_agent_helpers`/
`_prune_orphaned_static_bin_names`, and the `_careful_write` disposable
backup-tree clause), a further dispatch's eleven (clauses 16-26): the
`<settings-home>/machine-local/` seeding family, `settings-manifest.md`,
the `concerns[]` structured-key merge, and the `_c10a_steps` whoami/venv
group (tree copy, legacy-dir delete, compat-pointer creation,
`coordinator.whoami_src` key, legacy-venv delete), clause 27 (`_fnm_step`'s
brew/curl third-party `fnm` installer leg, declared via the stated-reason
escape hatch), and the ps1-launcher-class plan's closing clause 28
(`_write_ps1_policy_status`'s `<settings-home>/ps1-policy-gate-status.json`
durable AC13 status file) — never the flattened misreading an earlier
estimate produced, and never varying with what a runtime-read manifest
happens to list today.

Negative spec — this module does NOT:
  - validate `WRITE_SURFACE` against `write_surface.validate()` (C4's
    emission-op concern, not this writer's);
  - assert on `_write_strategy_for`'s literal force/careful/refuse values —
    that mechanism is expressed via the percolation/backup clauses' free-text
    `reason`, not a dedicated field (`WriteSurfaceEntry` has none), so this
    module only asserts that `reason` is populated and non-generic, not on
    its exact wording;
  - enumerate today's `SETUP_TEMPLATE_FILES`/`SETUP_TEMPLATE_HOOK_FILES`/
    `ML_FAMILY_FILES`/`ML_EXPLICIT_FILES`/`PLATFORM_LOCALIZE_FILES`
    filenames — those clauses are SHAPED specifically so this test stays
    independent of what the relevant manifest lists at any given time.
"""
from __future__ import annotations

from coordinator_core.install import substrate as target
from coordinator_core.install.write_surface import ShapedClause, StaticClause


def test_write_surface_identity_and_clause_count():
    declaration = target.WRITE_SURFACE
    assert declaration.writer_id == "install-substrate"
    assert declaration.source_module == "coordinator_core.install.substrate"
    assert len(declaration.clauses) == 28


def test_two_distinct_os_env_var_clauses_for_windows_path():
    clause_a, clause_b = target.WRITE_SURFACE.clauses[0], target.WRITE_SURFACE.clauses[1]
    for clause in (clause_a, clause_b):
        assert isinstance(clause, StaticClause)
        assert len(clause.entries) == 1
        assert clause.entries[0].kind == "os-env-var"
        assert clause.entries[0].key == "PATH"
    assert clause_a.entries[0].reason != clause_b.entries[0].reason
    assert "_percolation_and_path_steps" in clause_a.entries[0].reason
    assert "_windows_health_steps" in clause_b.entries[0].reason


def test_appx_stub_removal_clause_is_delete_not_write():
    clause = target.WRITE_SURFACE.clauses[2]
    assert isinstance(clause, StaticClause)
    assert clause.effect == "delete"
    assert len(clause.entries) == 2
    for entry in clause.entries:
        assert entry.kind == "file-path"
        assert entry.effect == "delete"
        assert "WindowsApps" in entry.path


def test_agent_helper_forwarder_triple_clause_is_shaped_not_flattened():
    """Widened by the ps1-launcher-class plan's C4: the `.py`/`.cmd` pair
    became a `.py`/`.cmd`/`.ps1` triple off the SAME discovery mechanism —
    still one clause, not a 28th."""
    clause = target.WRITE_SURFACE.clauses[3]
    assert isinstance(clause, ShapedClause)
    assert clause.discovered_by == "_derive_agent_helper_target_map"
    assert clause.entry_template.kind == "file-path"
    assert ".ps1" in clause.entry_template.path
    assert ".cmd" in clause.entry_template.path
    assert ".py" in clause.entry_template.path


def test_percolation_clauses_are_shaped_and_manifest_independent():
    """Both percolation clauses must be SHAPED (not a static filename list)
    so this test — and the declaration — never varies with what
    `setup-templates-manifest.py` happens to list today."""
    files_clause, hook_clause = target.WRITE_SURFACE.clauses[4], target.WRITE_SURFACE.clauses[5]
    for clause in (files_clause, hook_clause):
        assert isinstance(clause, ShapedClause)
        assert clause.entry_template.kind == "file-path"
        assert ".claude/setup" in clause.entry_template.path
        # No hardcoded template filename anywhere in the declaration —
        # only a placeholder segment.
        assert "<relative-" in clause.entry_template.path


def test_percolation_clauses_use_distinct_manifest_attrs():
    files_clause, hook_clause = target.WRITE_SURFACE.clauses[4], target.WRITE_SURFACE.clauses[5]
    assert target._MANIFEST_ATTRS[0] in files_clause.discovered_by
    assert target._MANIFEST_ATTRS[2] in hook_clause.discovered_by
    assert files_clause.discovered_by != hook_clause.discovered_by


def test_percolation_clauses_express_write_strategy_via_reason():
    """`write_strategy` (force/careful/refuse) has no dedicated field on
    `WriteSurfaceEntry` — it is expressed via the free-text `reason`
    field, and the two clauses' reasons must differ (force_overwrite is
    unconditional for hook files, conditional for template files)."""
    files_clause, hook_clause = target.WRITE_SURFACE.clauses[4], target.WRITE_SURFACE.clauses[5]
    assert files_clause.entry_template.reason
    assert hook_clause.entry_template.reason
    assert files_clause.entry_template.reason != hook_clause.entry_template.reason
    assert "force_overwrite" in hook_clause.entry_template.reason


def test_bin_family_shaped_clauses_key_off_manifest_group_attrs():
    """Clauses 7-9 — the three static-manifest-driven `<settings-home>/bin/`
    families (`ml_family`, `ml_explicit`, `platform_localize`) — must each
    be SHAPED and each `discovered_by` must name its own
    `_BIN_TEMPLATE_MANIFEST_GROUP_ATTRS` entry, recomputed here rather than
    hardcoded so a future attr rename in the manifest constant alone keeps
    this test honest."""
    ml_family, ml_explicit, platform_localize = target.WRITE_SURFACE.clauses[6:9]
    for clause in (ml_family, ml_explicit, platform_localize):
        assert isinstance(clause, ShapedClause)
        assert clause.entry_template.kind == "file-path"
        assert "<settings-home>/bin/" in clause.entry_template.path
    assert target._BIN_TEMPLATE_MANIFEST_GROUP_ATTRS[0] in ml_family.discovered_by
    assert target._BIN_TEMPLATE_MANIFEST_GROUP_ATTRS[1] in ml_explicit.discovered_by
    assert target._BIN_TEMPLATE_MANIFEST_GROUP_ATTRS[2] in platform_localize.discovered_by
    discovered_bys = {ml_family.discovered_by, ml_explicit.discovered_by, platform_localize.discovered_by}
    assert len(discovered_bys) == 3


def test_ch_family_and_rm_family_static_clauses_recomputed_from_source_constants():
    """Clauses 10-11 are STATIC (fixed, hand-maintained lists), unlike
    clauses 7-9's manifest-driven SHAPED form — each recomputed here from
    the same `_CH_FAMILY_FILES`/`_RM_FAMILY_FILES` constants the writer
    itself reads, so a future edit to either constant alone keeps this test
    honest rather than silently staling the declaration."""
    ch_clause, rm_clause = target.WRITE_SURFACE.clauses[9], target.WRITE_SURFACE.clauses[10]
    assert isinstance(ch_clause, StaticClause)
    assert isinstance(rm_clause, StaticClause)

    ch_expected = {f"<settings-home>/bin/{name}" for name, _exec_bit in target._CH_FAMILY_FILES}
    assert {e.path for e in ch_clause.entries} == ch_expected
    for entry in ch_clause.entries:
        assert entry.kind == "file-path"

    rm_expected = {f"<settings-home>/bin/{name}" for name in target._RM_FAMILY_FILES}
    assert {e.path for e in rm_clause.entries} == rm_expected
    for entry in rm_clause.entries:
        assert entry.kind == "file-path"


def test_bin_manifest_provenance_ledger_clause_uses_filename_constant():
    """Clause 12 — the installer's own provenance ledger — must path itself
    off `_BIN_MANIFEST_FILENAME` (the same constant `_write_bin_manifest`/
    `_read_bin_manifest` read), not a restated literal."""
    clause = target.WRITE_SURFACE.clauses[11]
    assert isinstance(clause, StaticClause)
    assert len(clause.entries) == 1
    entry = clause.entries[0]
    assert entry.kind == "file-path"
    assert entry.path == f"<settings-home>/bin/{target._BIN_MANIFEST_FILENAME}"


def test_sweep_and_prune_delete_clauses_are_shaped_delete_and_distinct():
    """Clauses 13-14 — `_sweep_orphaned_agent_helpers` (marker-provenance)
    and `_prune_orphaned_static_bin_names` (previous-manifest diff) — are
    two DIFFERENT delete mechanisms and must stay two distinct clauses,
    both `effect="delete"` at both the clause and entry level, both
    SHAPED (the eligible name set is discovered per run)."""
    sweep_clause, prune_clause = target.WRITE_SURFACE.clauses[12], target.WRITE_SURFACE.clauses[13]
    for clause in (sweep_clause, prune_clause):
        assert isinstance(clause, ShapedClause)
        assert clause.effect == "delete"
        assert clause.entry_template.effect == "delete"
        assert clause.entry_template.kind == "file-path"
    assert sweep_clause.discovered_by != prune_clause.discovered_by
    assert "_sweep_orphaned_agent_helpers" in sweep_clause.discovered_by
    assert "_prune_orphaned_static_bin_names" in prune_clause.discovered_by


def test_careful_write_backup_tree_clause_uses_backup_subdir_constant():
    """Clause 15 — the `_careful_write` disposable backup tree — must be
    SHAPED (timestamp + relative-path segment are runtime-computed) and
    path itself off `_OVERWRITE_BACKUP_SUBDIR`, the same constant
    `_careful_write_backup_path` joins into its own return value."""
    clause = target.WRITE_SURFACE.clauses[14]
    assert isinstance(clause, ShapedClause)
    assert clause.discovered_by == "_careful_write_backup_path"
    entry = clause.entry_template
    assert entry.kind == "file-path"
    assert f".claude/{target._OVERWRITE_BACKUP_SUBDIR}/" in entry.path
    assert "<relative-" in entry.path
    assert "pre-install-<TIMESTAMP>.bak" in entry.path


def test_tracked_ml_files_clause_recomputed_from_source_constant():
    """Clause 16 — the four tracked machine-local template files — is
    STATIC and recomputed from `_TRACKED_ML_FILES` (the same constant `run`
    reads at both its check-only probe and its real seed loop)."""
    clause = target.WRITE_SURFACE.clauses[15]
    assert isinstance(clause, StaticClause)
    expected = {f"<settings-home>/machine-local/{name}" for name in target._TRACKED_ML_FILES}
    assert {e.path for e in clause.entries} == expected
    for entry in clause.entries:
        assert entry.kind == "file-path"
        assert entry.reason


def test_ml_concern_baseline_clauses_use_source_constants():
    """Clauses 17-19 — unreal.toml, registry.toml (seed leg), hardware.toml —
    each a single-entry STATIC clause pathed off its own source constant."""
    unreal_clause, registry_seed_clause, hardware_clause = target.WRITE_SURFACE.clauses[16:19]
    for clause, name in (
        (unreal_clause, target._ML_UNREAL_TOML_NAME),
        (registry_seed_clause, target._ML_REGISTRY_TOML_NAME),
        (hardware_clause, target._ML_HARDWARE_TOML_NAME),
    ):
        assert isinstance(clause, StaticClause)
        assert len(clause.entries) == 1
        entry = clause.entries[0]
        assert entry.kind == "file-path"
        assert entry.path == f"<settings-home>/machine-local/{name}"
        assert "seed-if-absent" in entry.reason


def test_settings_manifest_clause_uses_source_constant():
    """Clause 20 — settings-manifest.md, installed via `_install_one` with
    `force_overwrite=False` (preserve-on-diff)."""
    clause = target.WRITE_SURFACE.clauses[19]
    assert isinstance(clause, StaticClause)
    assert len(clause.entries) == 1
    entry = clause.entries[0]
    assert entry.kind == "file-path"
    assert entry.path == f"<settings-home>/{target._SETTINGS_MANIFEST_FILENAME}"


def test_concerns_structured_key_clause_is_structured_file_key_not_file_path():
    """Clause 21 — `_register_hardware_concern`'s in-place merge of
    "hardware" into registry.toml's `concerns` array — must be
    `structured-file-key`, distinct from clause 18's whole-file seed."""
    clause = target.WRITE_SURFACE.clauses[20]
    assert isinstance(clause, StaticClause)
    assert len(clause.entries) == 1
    entry = clause.entries[0]
    assert entry.kind == "structured-file-key"
    assert entry.key == "concerns[]=hardware"
    assert entry.path == f"<settings-home>/machine-local/{target._ML_REGISTRY_TOML_NAME}"


def test_whoami_tree_copy_clause_is_shaped():
    """Clause 22 — the coordinator-whoami/ tree copy — must be SHAPED
    (`_iter_whoami_files` discovers the file set per run)."""
    clause = target.WRITE_SURFACE.clauses[21]
    assert isinstance(clause, ShapedClause)
    assert "_iter_whoami_files" in clause.discovered_by
    assert clause.entry_template.kind == "file-path"
    assert f"<settings-home>/{target._WHOAMI_DIRNAME}/" in clause.entry_template.path


def test_legacy_whoami_delete_and_compat_pointer_clauses_are_distinct():
    """Clauses 23-24 — the legacy coordinator-whoami dir delete and its
    replacement compat pointer — share a path but are distinct effects
    (delete vs. write), never collapsed into one clause."""
    delete_clause, pointer_clause = target.WRITE_SURFACE.clauses[22], target.WRITE_SURFACE.clauses[23]
    assert isinstance(delete_clause, StaticClause)
    assert isinstance(pointer_clause, StaticClause)
    assert delete_clause.effect == "delete"
    assert pointer_clause.effect == "write"
    expected_path = f"<install-base>/.claude/{target._WHOAMI_DIRNAME}"
    assert delete_clause.entries[0].path == expected_path
    assert delete_clause.entries[0].effect == "delete"
    assert pointer_clause.entries[0].path == expected_path
    assert pointer_clause.entries[0].effect == "write"


def test_whoami_src_machine_local_key_clause():
    """Clause 25 — the `coordinator.whoami_src` machine-local key."""
    clause = target.WRITE_SURFACE.clauses[24]
    assert isinstance(clause, StaticClause)
    assert len(clause.entries) == 1
    entry = clause.entries[0]
    assert entry.kind == "machine-local-key"
    assert entry.key == "coordinator.whoami_src"


def test_legacy_venv_delete_clause_is_distinct_from_ensure_venv_surface():
    """Clause 26 — the legacy `.coordinator-venv` delete leg is substrate's
    own surface, distinct from `ensure_venv`'s declared current-venv
    surface (creation, interpreter-pin key, build-lock sidecar) — this
    module never re-declares that."""
    clause = target.WRITE_SURFACE.clauses[25]
    assert isinstance(clause, StaticClause)
    assert clause.effect == "delete"
    assert len(clause.entries) == 1
    entry = clause.entries[0]
    assert entry.kind == "file-path"
    assert entry.effect == "delete"
    assert entry.path == f"<install-base>/.claude/{target._LEGACY_VENV_DIRNAME}"


def test_no_new_clause_restates_a_bin_dir_or_ensure_venv_surface():
    """Sanity guard: none of clauses 16-27 accidentally re-declare the
    `<settings-home>/bin/` surface (clauses 4/7-14) or the CURRENT
    `<settings-home>/.coordinator-venv` tree (ensure_venv's own surface)."""
    for clause in target.WRITE_SURFACE.clauses[15:]:
        entries = clause.entries if isinstance(clause, StaticClause) else (clause.entry_template,)
        for entry in entries:
            if entry.path:
                assert "<settings-home>/bin/" not in entry.path
                assert "<settings-home>/.coordinator-venv" not in entry.path


def test_fnm_step_clause_uses_stated_reason_escape_hatch():
    """Clause 27 — `_fnm_step`'s brew/curl third-party `fnm` installer leg
    — must be declared (not omitted for lacking an honest kind), via the
    stated-reason escape hatch: `kind="file-path"` (least-dishonest choice)
    with a `reason` that names both installer paths, the guard, and that
    the actual on-disk footprint is not enumerable from here."""
    clause = target.WRITE_SURFACE.clauses[26]
    assert isinstance(clause, StaticClause)
    assert len(clause.entries) == 1
    entry = clause.entries[0]
    assert entry.kind == "file-path"
    assert entry.effect == "write"
    assert "brew install fnm" in entry.reason
    assert "fnm.vercel.app" in entry.reason
    # Review: coordinator:code-reviewer — restored (P2): undisclosed
    # removal, unrelated to C4's stated scope; substrate.py's fnm reason
    # text still contains both substrings verbatim.
    assert "_refuse_machine_mutation" in entry.reason
    assert "cannot enumerate" in entry.reason


def test_ps1_policy_status_clause_is_static_and_beside_bin_dir():
    """Clause 28 (ps1-launcher-class plan C4, AC13) — the durable `.ps1`
    execution-policy status file, `<settings-home>/ps1-policy-gate-
    status.json`. STATIC (one named file, one call site), and deliberately
    NOT under `<settings-home>/bin/` — it is a status record about that
    directory's `.ps1` contents (clause 4), not another entry in it."""
    clause = target.WRITE_SURFACE.clauses[27]
    assert isinstance(clause, StaticClause)
    assert len(clause.entries) == 1
    entry = clause.entries[0]
    assert entry.kind == "file-path"
    assert entry.path == f"<settings-home>/{target._PS1_POLICY_STATUS_FILENAME}"
    assert "<settings-home>/bin/" not in entry.path
    assert "GREEN and RED both write" in entry.reason

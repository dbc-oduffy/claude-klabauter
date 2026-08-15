"""Tests for `coordinator_core.install.substrate`'s ten `ShapedClause`
resolution-journal wiring (C5 of
docs/research/2026-08-06-install-receipt-persistence-design.md).

Purpose: prove each shaped-clause write site journals the CONCRETE entries
it actually resolved to (never a template placeholder, never a phantom
write that was skipped/refused), that a clause whose mechanism never fired
this run leaves NO journal row (distinguishable from a legitimate
empty-tuple "resolved to nothing"), and that `read_journal()`'s output for
each clause round-trips through `receipt.derive_receipt_entries` without
raising `ClauseResolutionMismatchError`/`UnresolvedShapedClauseError`.

Negative spec — this file does NOT:
  - exercise `maximalist.py`'s run-start clear / run-end persist wiring
    (C4, a separate chunk);
  - re-test `resolution_journal.py`'s own round-trip/tolerant-reader
    behaviour (`test_resolution_journal.py`, C1) — only that substrate.py's
    write sites call it correctly, with the right clause index and the
    right concrete entries.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.install import resolution_journal
from coordinator_core.install import substrate
from coordinator_core.install.receipt import derive_receipt_entries
from coordinator_core.install.write_surface import ShapedClause, WriteSurfaceEntry

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SHAPED_CLAUSE_INDICES = (
    substrate._CLAUSE_AGENT_HELPER_FORWARDERS,
    substrate._CLAUSE_SETUP_FILES,
    substrate._CLAUSE_SETUP_HOOK_FILES,
    substrate._CLAUSE_ML_FAMILY,
    substrate._CLAUSE_ML_EXPLICIT,
    substrate._CLAUSE_PLATFORM_LOCALIZE,
    substrate._CLAUSE_ORPHAN_SWEEP,
    substrate._CLAUSE_PRUNE_ORPHANED_STATIC,
    substrate._CLAUSE_CAREFUL_BACKUP,
    substrate._CLAUSE_WHOAMI_COPY,
)


@pytest.fixture(autouse=True)
def _journal_env(tmp_path, monkeypatch):
    """Run-scoped journal under `tmp_path`, and `COORDINATOR_DISABLE_
    MACHINE_MUTATION` explicitly unset — this suite's conftest arms it
    suite-wide, which would silently no-op every write site under test
    here (both substrate's own writes AND the journal append that mirrors
    them)."""
    journal_path = tmp_path / "journal" / "resolution-journal.jsonl"
    monkeypatch.setenv(resolution_journal.RESOLUTION_JOURNAL_ENV_VAR, str(journal_path))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    return journal_path


def test_clause_indices_point_at_the_declared_shaped_clauses():
    """Sanity-pins the `_CLAUSE_*` constants against `WRITE_SURFACE.clauses`
    itself — every one of the ten must resolve to an actual `ShapedClause`
    at that position, never a `StaticClause` or the wrong shaped clause."""
    assert len(_SHAPED_CLAUSE_INDICES) == 10
    for idx in _SHAPED_CLAUSE_INDICES:
        clause = substrate.WRITE_SURFACE.clauses[idx]
        assert isinstance(clause, ShapedClause), (
            f"clause index {idx} is {type(clause).__name__}, expected ShapedClause"
        )


# Review: coordinator:code-reviewer (2026-08-06, rcpt-R3-writer-wiring) — the
# type-only assertion above cannot catch pointing at the WRONG ShapedClause
# (e.g. index 12 resolving to some other shaped clause than "orphan sweep").
# Each constant's own comment names the clause it is meant to reach
# (`substrate.py`'s "Clause N —" markers); pin identity against
# `discovered_by`, the one field on `ShapedClause` that is unique per clause
# and stable under `WRITE_SURFACE.clauses` reordering (the failure mode this
# guards against), unlike position alone.
_EXPECTED_DISCOVERED_BY = {
    "_CLAUSE_AGENT_HELPER_FORWARDERS": "_derive_agent_helper_target_map",
    "_CLAUSE_SETUP_FILES": "_load_setup_template_manifest (SETUP_TEMPLATE_FILES)",
    "_CLAUSE_SETUP_HOOK_FILES": "_load_setup_template_manifest (SETUP_TEMPLATE_HOOK_FILES)",
    "_CLAUSE_ORPHAN_SWEEP": "_sweep_orphaned_agent_helpers (marker-provenance orphan sweep)",
    "_CLAUSE_PRUNE_ORPHANED_STATIC": "_prune_orphaned_static_bin_names (previous-manifest diff)",
    "_CLAUSE_CAREFUL_BACKUP": "_careful_write_backup_path",
    "_CLAUSE_WHOAMI_COPY": "_iter_whoami_files (_c10a_copy_one)",
}
"""Manifest-driven ML_FAMILY/ML_EXPLICIT/PLATFORM_LOCALIZE clauses (7, 8, 9)
are covered by identity below via `_BIN_TEMPLATE_MANIFEST_GROUP_ATTRS`
rather than a literal string here, since their `discovered_by` is itself
f-string-interpolated from that same constant in `substrate.py` — asserting
against the constant, not a duplicated literal, keeps the two from silently
drifting apart."""


def test_clause_indices_point_at_the_correct_shaped_clause_by_identity():
    """Strengthens the type-only check above to an identity check: each
    `_CLAUSE_*` constant must land on the SPECIFIC `ShapedClause` its name
    describes, not merely on *a* `ShapedClause`. A future reordering of
    `WRITE_SURFACE.clauses` that moves a clause without updating its index
    constant now fails loudly here instead of silently filing journal
    entries under the wrong clause."""
    for const_name, expected_discovered_by in _EXPECTED_DISCOVERED_BY.items():
        idx = getattr(substrate, const_name)
        clause = substrate.WRITE_SURFACE.clauses[idx]
        assert isinstance(clause, ShapedClause), (
            f"{const_name} (index {idx}) is {type(clause).__name__}, expected ShapedClause"
        )
        assert clause.discovered_by == expected_discovered_by, (
            f"{const_name} (index {idx}) points at discovered_by="
            f"{clause.discovered_by!r}, expected {expected_discovered_by!r}"
        )

    for const_name, group_attr_index in (
        ("_CLAUSE_ML_FAMILY", 0),
        ("_CLAUSE_ML_EXPLICIT", 1),
        ("_CLAUSE_PLATFORM_LOCALIZE", 2),
    ):
        idx = getattr(substrate, const_name)
        clause = substrate.WRITE_SURFACE.clauses[idx]
        assert isinstance(clause, ShapedClause), (
            f"{const_name} (index {idx}) is {type(clause).__name__}, expected ShapedClause"
        )
        expected = (
            "_load_bin_templates_manifest "
            f"({substrate._BIN_TEMPLATE_MANIFEST_GROUP_ATTRS[group_attr_index]})"
        )
        assert clause.discovered_by == expected, (
            f"{const_name} (index {idx}) points at discovered_by="
            f"{clause.discovered_by!r}, expected {expected!r}"
        )


# --- _sweep_orphaned_agent_helpers (clause _CLAUSE_ORPHAN_SWEEP) -----------


def test_orphan_sweep_journals_only_actually_deleted_entries(tmp_path):
    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()
    orphan = bin_dst / "stale-helper"
    orphan.write_text(f"# {substrate._AGENT_FORWARDER_MARKER}\n", encoding="utf-8")
    kept = bin_dst / "machine-local"  # a reserved/static name, never swept
    kept.write_text(f"# {substrate._AGENT_FORWARDER_MARKER}\n", encoding="utf-8")

    substrate._sweep_orphaned_agent_helpers(bin_dst, {}, {}, check_only=False)

    assert not orphan.exists()
    assert kept.exists()
    journal = resolution_journal.read_journal()
    resolution = journal[substrate._WRITER_ID][substrate._CLAUSE_ORPHAN_SWEEP]
    assert resolution.entries == (
        WriteSurfaceEntry(kind="file-path", path=str(orphan), effect="delete"),
    )


def test_orphan_sweep_journals_empty_tuple_when_nothing_orphaned(tmp_path):
    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()

    substrate._sweep_orphaned_agent_helpers(bin_dst, {}, {}, check_only=False)

    journal = resolution_journal.read_journal()
    assert journal[substrate._WRITER_ID][substrate._CLAUSE_ORPHAN_SWEEP].entries == ()


def test_orphan_sweep_never_got_there_leaves_no_journal_row(tmp_path):
    """`bin_dst` doesn't exist yet — the sweep never determined anything,
    which must be distinguishable from a genuine empty resolution: no row
    at all, not a present-but-empty one."""
    bin_dst = tmp_path / "does-not-exist"

    substrate._sweep_orphaned_agent_helpers(bin_dst, {}, {}, check_only=False)

    journal = resolution_journal.read_journal()
    assert substrate._WRITER_ID not in journal


def test_orphan_sweep_check_only_never_journals(tmp_path):
    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()
    orphan = bin_dst / "stale-helper"
    orphan.write_text(f"# {substrate._AGENT_FORWARDER_MARKER}\n", encoding="utf-8")

    with pytest.raises(substrate.SubstrateFatalError):
        substrate._sweep_orphaned_agent_helpers(bin_dst, {}, {}, check_only=True)

    assert orphan.exists()  # check-only never deletes
    journal = resolution_journal.read_journal()
    assert substrate._WRITER_ID not in journal


def test_orphan_sweep_guard_refused_journals_nothing_phantom(tmp_path, monkeypatch):
    """When the disable-mutation guard refuses the delete, the SAME guard
    refuses the journal append (both delegate to the same
    `_refuse_machine_mutation`) — no phantom row ever claims a deletion
    that never happened."""
    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()
    orphan = bin_dst / "stale-helper"
    orphan.write_text(f"# {substrate._AGENT_FORWARDER_MARKER}\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")

    substrate._sweep_orphaned_agent_helpers(bin_dst, {}, {}, check_only=False)

    assert orphan.exists()  # refused, never deleted
    journal = resolution_journal.read_journal()
    assert substrate._WRITER_ID not in journal


# --- _prune_orphaned_static_bin_names (clause _CLAUSE_PRUNE_ORPHANED_STATIC)


def test_prune_orphaned_static_names_journals_only_actual_deletes(tmp_path):
    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()
    stale = bin_dst / "retired-tool"
    stale.write_text("stale\n", encoding="utf-8")
    substrate._write_bin_manifest(bin_dst, frozenset({"retired-tool", "kept-tool"}))
    kept = bin_dst / "kept-tool"
    kept.write_text("kept\n", encoding="utf-8")

    substrate._prune_orphaned_static_bin_names(bin_dst, frozenset({"kept-tool"}), check_only=False)

    assert not stale.exists()
    assert kept.exists()
    journal = resolution_journal.read_journal()
    resolution = journal[substrate._WRITER_ID][substrate._CLAUSE_PRUNE_ORPHANED_STATIC]
    assert resolution.entries == (
        WriteSurfaceEntry(kind="file-path", path=str(stale), effect="delete"),
    )


def test_prune_orphaned_static_names_never_got_there_leaves_no_row(tmp_path):
    bin_dst = tmp_path / "does-not-exist"

    substrate._prune_orphaned_static_bin_names(bin_dst, frozenset(), check_only=False)

    journal = resolution_journal.read_journal()
    assert substrate._WRITER_ID not in journal


# --- _percolation_and_path_steps (clauses SETUP_FILES/SETUP_HOOK_FILES/CAREFUL_BACKUP)


def _git(*args, cwd):
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_percolation_journals_setup_files_hook_files_and_careful_backup(tmp_path):
    setup_src = tmp_path / "src"
    setup_src.mkdir()
    (setup_src / "tracked.sh").write_text("fresh\n", encoding="utf-8")

    install_base = tmp_path / "install_base"
    setup_dest = install_base / ".claude" / "setup"
    setup_dest.mkdir(parents=True)
    (setup_dest / "tracked.sh").write_text("stale\n", encoding="utf-8")

    _git("init", cwd=setup_dest)
    _git("config", "user.email", "t@example.com", cwd=setup_dest)
    _git("config", "user.name", "T", cwd=setup_dest)
    _git("add", "tracked.sh", cwd=setup_dest)
    _git("commit", "-m", "seed", cwd=setup_dest)

    bin_dst = tmp_path / "bin_dst"

    substrate._percolation_and_path_steps(
        setup_src, ["tracked.sh"], [], [], str(install_base), bin_dst, check_only=False,
    )

    # careful write actually happened: content replaced, backup on disk.
    assert (setup_dest / "tracked.sh").read_text(encoding="utf-8") == "fresh\n"

    journal = resolution_journal.read_journal()
    per_clause = journal[substrate._WRITER_ID]

    setup_files_res = per_clause[substrate._CLAUSE_SETUP_FILES]
    assert setup_files_res.entries == (
        WriteSurfaceEntry(kind="file-path", path=str(setup_dest / "tracked.sh")),
    )

    hook_files_res = per_clause[substrate._CLAUSE_SETUP_HOOK_FILES]
    assert hook_files_res.entries == ()

    backup_res = per_clause[substrate._CLAUSE_CAREFUL_BACKUP]
    assert len(backup_res.entries) == 1
    backup_entry = backup_res.entries[0]
    assert backup_entry.kind == "file-path"
    assert ".pre-install-" in backup_entry.path
    assert Path(backup_entry.path).is_file()


def test_percolation_check_only_never_journals(tmp_path):
    setup_src = tmp_path / "src"
    setup_src.mkdir()
    (setup_src / "f.txt").write_text("x\n", encoding="utf-8")
    install_base = tmp_path / "install_base"
    bin_dst = tmp_path / "bin_dst"

    with pytest.raises(substrate.SubstrateFatalError):
        substrate._percolation_and_path_steps(
            setup_src, ["f.txt"], [], [], str(install_base), bin_dst, check_only=True,
        )

    journal = resolution_journal.read_journal()
    assert substrate._WRITER_ID not in journal


# --- _c10a_steps whoami copy (clause _CLAUSE_WHOAMI_COPY) -------------------


def _c10a_common(tmp_path):
    install_base = tmp_path / "install_base"
    settings_home = tmp_path / "settings_home"
    settings_home.mkdir(parents=True)
    plugin_root = tmp_path / "plugin_root"
    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()
    return install_base, settings_home, plugin_root, bin_dst


def test_c10a_whoami_no_source_journals_empty_tuple(tmp_path):
    install_base, settings_home, plugin_root, bin_dst = _c10a_common(tmp_path)

    substrate._c10a_steps(str(install_base), settings_home, plugin_root, bin_dst, check_only=False)

    journal = resolution_journal.read_journal()
    assert journal[substrate._WRITER_ID][substrate._CLAUSE_WHOAMI_COPY].entries == ()


def test_c10a_whoami_dest_already_populated_journals_empty_tuple(tmp_path):
    install_base, settings_home, plugin_root, bin_dst = _c10a_common(tmp_path)
    (plugin_root / "whoami").mkdir(parents=True)
    (plugin_root / "whoami" / "a.txt").write_text("a\n", encoding="utf-8")
    dst_whoami = settings_home / substrate._WHOAMI_DIRNAME
    dst_whoami.mkdir(parents=True)
    (dst_whoami / "already-here.txt").write_text("already\n", encoding="utf-8")

    substrate._c10a_steps(str(install_base), settings_home, plugin_root, bin_dst, check_only=False)

    journal = resolution_journal.read_journal()
    assert journal[substrate._WRITER_ID][substrate._CLAUSE_WHOAMI_COPY].entries == ()


def test_c10a_whoami_copy_journals_the_copied_files(tmp_path):
    install_base, settings_home, plugin_root, bin_dst = _c10a_common(tmp_path)
    src_whoami = plugin_root / "whoami"
    src_whoami.mkdir(parents=True)
    (src_whoami / "a.txt").write_text("a\n", encoding="utf-8")
    (src_whoami / "sub").mkdir()
    (src_whoami / "sub" / "b.txt").write_text("b\n", encoding="utf-8")

    substrate._c10a_steps(str(install_base), settings_home, plugin_root, bin_dst, check_only=False)

    dst_whoami = settings_home / substrate._WHOAMI_DIRNAME
    journal = resolution_journal.read_journal()
    resolution = journal[substrate._WRITER_ID][substrate._CLAUSE_WHOAMI_COPY]
    got_paths = {e.path for e in resolution.entries}
    assert got_paths == {
        str(dst_whoami / "a.txt"),
        str(dst_whoami / "sub" / "b.txt"),
    }
    assert all(e.kind == "file-path" for e in resolution.entries)


def test_c10a_whoami_check_only_never_journals(tmp_path):
    install_base, settings_home, plugin_root, bin_dst = _c10a_common(tmp_path)
    src_whoami = plugin_root / "whoami"
    src_whoami.mkdir(parents=True)
    (src_whoami / "a.txt").write_text("a\n", encoding="utf-8")

    substrate._c10a_steps(str(install_base), settings_home, plugin_root, bin_dst, check_only=True)

    journal = resolution_journal.read_journal()
    assert substrate._WRITER_ID not in journal


# --- _install_bin_resolvers (clauses AGENT_HELPER_FORWARDERS/ML_FAMILY/ML_EXPLICIT/PLATFORM_LOCALIZE)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_install_bin_resolvers_journals_all_four_shaped_clauses(tmp_path, monkeypatch):
    ml_bin = tmp_path / "ml_bin"
    ch_bin = tmp_path / "ch_bin"
    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()

    bin_manifest = substrate._load_bin_templates_manifest(
        substrate._resolve_bin_templates_manifest_root()
    )
    for entry in bin_manifest.install_bin_resolvers_entries():
        _write(ml_bin / entry.name, f"src::{entry.name}\n")
    for f, _exec_bit in substrate._CH_FAMILY_FILES:
        _write(ch_bin / f, f"ch-src::{f}\n")

    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(_REPO_ROOT))

    substrate._install_bin_resolvers(
        ml_bin, ch_bin, bin_dst,
        check_only=False,
        python3_cmd_resolved_bin="/usr/bin/python3",
    )

    journal = resolution_journal.read_journal()
    per_clause = journal[substrate._WRITER_ID]

    ml_family_paths = {e.path for e in per_clause[substrate._CLAUSE_ML_FAMILY].entries}
    assert ml_family_paths == {str(bin_dst / e.name) for e in bin_manifest.ml_family}

    ml_explicit_paths = {e.path for e in per_clause[substrate._CLAUSE_ML_EXPLICIT].entries}
    assert ml_explicit_paths == {str(bin_dst / e.name) for e in bin_manifest.ml_explicit}

    platform_localize_paths = {e.path for e in per_clause[substrate._CLAUSE_PLATFORM_LOCALIZE].entries}
    assert platform_localize_paths == {str(bin_dst / e.name) for e in bin_manifest.platform_localize}

    agent_bin = Path(_REPO_ROOT) / "coordinator" / "bin"
    agent_map = substrate._derive_agent_helper_target_map(agent_bin)
    agent_cmd_map = substrate._resolve_agent_cmd_dest_collisions(agent_map)
    expected_agent_paths = {str(bin_dst / f) for f in agent_map} | {
        str(bin_dst / cmd) for cmd in agent_cmd_map.values()
    }
    agent_forwarder_paths = {e.path for e in per_clause[substrate._CLAUSE_AGENT_HELPER_FORWARDERS].entries}
    assert agent_forwarder_paths == expected_agent_paths


def test_install_bin_resolvers_check_only_never_journals(tmp_path, monkeypatch):
    ml_bin = tmp_path / "ml_bin"
    ch_bin = tmp_path / "ch_bin"
    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()
    bin_manifest = substrate._load_bin_templates_manifest(
        substrate._resolve_bin_templates_manifest_root()
    )
    for entry in bin_manifest.install_bin_resolvers_entries():
        _write(ml_bin / entry.name, f"src::{entry.name}\n")
        _write(bin_dst / entry.name, f"src::{entry.name}\n")  # up to date -> check passes
    for f, _exec_bit in substrate._CH_FAMILY_FILES:
        _write(ch_bin / f, f"ch-src::{f}\n")
        _write(bin_dst / f, f"ch-src::{f}\n")

    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(_REPO_ROOT))

    # check_only mode: agent-helper forwarders will be "absent" (stale) and
    # raise SubstrateFatalError from _write_agent_forwarder's check branch —
    # that is fine, we only care that nothing was journaled before it did.
    with pytest.raises(substrate.SubstrateFatalError):
        substrate._install_bin_resolvers(
            ml_bin, ch_bin, bin_dst,
            check_only=True,
            python3_cmd_resolved_bin="/usr/bin/python3",
        )

    journal = resolution_journal.read_journal()
    assert substrate._WRITER_ID not in journal


# --- round-trip through receipt.derive_receipt_entries ---------------------


def test_orphan_sweep_resolution_round_trips_through_derive_receipt_entries(tmp_path):
    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()
    orphan = bin_dst / "stale-helper"
    orphan.write_text(f"# {substrate._AGENT_FORWARDER_MARKER}\n", encoding="utf-8")

    substrate._sweep_orphaned_agent_helpers(bin_dst, {}, {}, check_only=False)

    journal = resolution_journal.read_journal()
    resolutions = journal[substrate._WRITER_ID]

    # derive_receipt_entries needs a resolution for EVERY ShapedClause in
    # WRITE_SURFACE — supply empty resolutions for the nine this test didn't
    # exercise, proving read_journal()'s per-clause dict is exactly the
    # shape derive_receipt_entries expects (never crashes on the one clause
    # this test actually populated).
    from coordinator_core.install.receipt import ClauseResolution

    full_resolutions = {idx: ClauseResolution(entries=()) for idx in _SHAPED_CLAUSE_INDICES}
    full_resolutions.update(resolutions)

    entries = derive_receipt_entries(substrate.WRITE_SURFACE, full_resolutions)
    delete_entries = [e for e in entries if e.path == str(orphan)]
    assert len(delete_entries) == 1
    assert delete_entries[0].effect == "delete"

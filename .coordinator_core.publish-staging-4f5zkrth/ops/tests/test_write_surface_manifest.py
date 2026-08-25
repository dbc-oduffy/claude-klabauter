"""
coordinator_core.ops.tests.test_write_surface_manifest — coverage for the
"write_surface.emit_manifest" op.

Purpose: pins the C4 emission op — that it is reachable ONLY through
`register_op`/`dispatch_message` (no parallel dispatch path), that collection DISCOVERS
declaring modules (never a hand-maintained writer roster — that roster went stale within
hours, see the module's own docstring), that `undeclared` and `duplicate_writer_id` are
representable (not merely detected), and that `effect="delete"` entries survive emission
rather than being filtered.

Spec backlink: pln-writer-declared-write-surface-49d3bd, chunk C4
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import coordinator_core.ops  # noqa: F401 — populates _REGISTRY (write_surface.emit_manifest)
from coordinator_core.ipc import dispatch_message, get_op_handler
from coordinator_core.ops import write_surface_manifest as wsm

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _run(coro):
    return asyncio.run(coro)


def _dispatch(method="write_surface.emit_manifest", params=None, id_=1):
    msg = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    return _run(dispatch_message(msg))


def _independently_count_declaring_modules() -> int:
    """Independent count of every module on disk exporting a module-level
    `WRITE_SURFACE`, computed at test time by re-deriving the FILE LIST (independently of
    `_candidate_modules`'s own directory walk) and checking each file with the real
    `wsm._declares_write_surface` predicate — NOT a re-implementation of that predicate.
    # Review: code-reviewer — a copy of the predicate shares the implementation's blind
    # spot and would agree at the same wrong count; independence must come from the file
    # list, not the scan rule.
    """

    count = 0
    for root_rel in wsm._SCAN_ROOTS:
        root = _REPO_ROOT / root_rel
        if not root.is_dir():
            continue
        for module_path in root.glob("*.py"):
            if module_path.name == "__init__.py" or module_path.name.startswith("test_"):
                continue
            if wsm._declares_write_surface(module_path):
                count += 1
    return count


def test_registered_via_register_op_and_reachable_through_dispatch():
    # Registration is the whole point of this chunk — assert the handler is actually
    # findable in the registry, not merely defined as a bare function nobody wired up.
    assert get_op_handler("write_surface.emit_manifest") is not None

    resp = _dispatch()
    assert "error" not in resp, resp
    result = resp["result"]
    assert result["schema_version"] == 1
    assert isinstance(result["entries"], list)
    assert result["entries"], "expected at least one manifest entry"


def test_manifest_writer_count_matches_modules_on_disk_declaring_write_surface():
    """The regression this dispatch exists to prevent: a hand-maintained writer roster
    going stale while modules keep declaring on disk. Collection must DISCOVER declaring
    modules, so the number of distinct declaring source modules represented in the
    manifest must equal the number found by an independent on-disk scan — not a
    hardcoded count, which would just be a second stale roster."""
    manifest = wsm.build_manifest()
    declared_or_empty = {
        e["source_module"]
        for e in manifest["entries"]
        if e["status"] in ("declared", "declared-empty")
    }
    expected_count = _independently_count_declaring_modules()
    assert expected_count > 9, (
        "sanity floor: the whole point of this fix is that more than the old 9-writer "
        f"roster declares on disk; independent scan found only {expected_count}"
    )
    assert len(declared_or_empty) == expected_count, (
        f"manifest represents {len(declared_or_empty)} declaring source modules, "
        f"independent on-disk scan found {expected_count} — collection is not "
        "discovering every WRITE_SURFACE-declaring module"
    )


def test_every_discovered_candidate_contributes_at_least_one_entry():
    # source_module in a declared entry is the writer's OWN dotted declaration (decl.
    # source_module), not the on-disk relative path used for bookkeeping — tie discovery
    # to emission via each candidate's own writer_id instead.
    manifest = wsm.build_manifest()
    writer_ids_present = {e["writer_id"] for e in manifest["entries"]}

    for module_path in wsm._candidate_modules(_REPO_ROOT):
        module = wsm._load_candidate(_REPO_ROOT, module_path)
        decl = getattr(module, "WRITE_SURFACE")
        assert decl.writer_id in writer_ids_present, (
            f"{module_path} declares writer_id {decl.writer_id!r} but no manifest "
            "entry carries it"
        )


def test_undeclared_module_is_representable_and_does_not_block_emission(monkeypatch):
    real_load = wsm._load_candidate

    def _boom(repo_root, module_path):
        if module_path.name == "configure_git.py":
            raise ImportError("simulated missing declaration")
        return real_load(repo_root, module_path)

    monkeypatch.setattr(wsm, "_load_candidate", _boom)

    manifest = wsm.build_manifest()
    undeclared = [
        e
        for e in manifest["entries"]
        if e["status"] == "undeclared" and e["source_module"].endswith("configure_git.py")
    ]
    assert len(undeclared) == 1
    assert "simulated missing declaration" in undeclared[0]["reason"]

    # Loudness must not block: every other candidate module still emitted entries.
    # Review: code-reviewer — a `>=` bound tolerates a silent partial drop (it would still
    # pass if some other candidate's entries went missing, as long as not enough of them
    # dropped to cross the threshold). Tighten to an exact expected-writer-set comparison
    # so any silent partial drop is caught.
    other_writer_ids = {
        e["writer_id"]
        for e in manifest["entries"]
        if not e["source_module"].endswith("configure_git.py")
    }
    candidates = wsm._candidate_modules(_REPO_ROOT)
    expected_writer_ids = set()
    for module_path in candidates:
        if module_path.name == "configure_git.py":
            continue
        module = wsm._load_candidate(_REPO_ROOT, module_path)
        expected_writer_ids.add(getattr(module, "WRITE_SURFACE").writer_id)
    assert expected_writer_ids <= other_writer_ids, (
        f"candidates missing from emitted entries: {expected_writer_ids - other_writer_ids}"
    )


def test_discover_declarations_matches_manifest_writer_roster():
    """The public seam (`discover_declarations`) must agree with
    `build_manifest`'s own scan on which writer_ids exist — both call the
    same `_candidate_modules`/`_load_candidate` primitives."""
    manifest = wsm.build_manifest()
    manifest_writer_ids = {e["writer_id"] for e in manifest["entries"]}

    declarations, failures = wsm.discover_declarations(_REPO_ROOT)
    assert failures == []
    assert set(declarations.keys()) <= manifest_writer_ids
    for writer_id, decl in declarations.items():
        assert decl.writer_id == writer_id


def test_discover_declarations_reports_import_failure_as_a_loud_failure_not_a_silent_drop(monkeypatch):
    """Review: code-reviewer (P2) -- a failed candidate must surface via the
    `failures` return, never vanish from both `declarations` and
    `failures`."""
    real_load = wsm._load_candidate

    def _boom(repo_root, module_path):
        if module_path.name == "configure_git.py":
            raise ImportError("simulated missing declaration")
        return real_load(repo_root, module_path)

    monkeypatch.setattr(wsm, "_load_candidate", _boom)

    declarations, failures = wsm.discover_declarations(_REPO_ROOT)
    failure_hints = [hint for hint, _reason in failures]
    matching = [h for h in failure_hints if h.endswith("configure_git.py")]
    assert len(matching) == 1
    assert not any(d.source_module.endswith("configure_git.py") for d in declarations.values())


def test_duplicate_writer_id_is_flagged_not_silently_collapsed():
    # Exercises the duplicate-bookkeeping logic directly against two synthetic decls
    # sharing a writer_id — no real on-disk fixture module needed.
    from coordinator_core.install.write_surface import WriteSurfaceDeclaration

    decl_a = WriteSurfaceDeclaration(writer_id="dup-writer", source_module="mod_a", clauses=())
    decl_b = WriteSurfaceDeclaration(writer_id="dup-writer", source_module="mod_b", clauses=())

    entries: list = []
    seen_writer_ids: dict = {}
    for rel, decl in (("mod_a.py", decl_a), ("mod_b.py", decl_b)):
        if decl.writer_id in seen_writer_ids:
            entries.append(
                wsm._duplicate_writer_id_entry(decl.writer_id, rel, seen_writer_ids[decl.writer_id])
            )
        else:
            seen_writer_ids[decl.writer_id] = rel
        entries.extend(wsm._flatten_declaration(decl))

    duplicate_entries = [e for e in entries if e["status"] == "duplicate_writer_id"]
    assert len(duplicate_entries) == 1
    assert duplicate_entries[0]["writer_id"] == "dup-writer"
    assert duplicate_entries[0]["source_module"] == "mod_b.py"
    assert "mod_a.py" in duplicate_entries[0]["reason"]

    # Both modules' own declared-empty entries still emitted — not collapsed away.
    declared_empty = [e for e in entries if e["status"] == "declared-empty"]
    assert {e["source_module"] for e in declared_empty} == {"mod_a", "mod_b"}


def test_delete_effect_entries_survive_emission():
    manifest = wsm.build_manifest()
    delete_entries = [e for e in manifest["entries"] if e.get("effect") == "delete"]
    assert delete_entries, "expected install-substrate's AppX-stub delete clause to emit"
    assert any(e["writer_id"] == "install-substrate" for e in delete_entries)


def test_shaped_entries_carry_normalized_discovered_by():
    manifest = wsm.build_manifest()
    shaped = [e for e in manifest["entries"] if e["form"] == "shaped"]
    assert shaped, "expected at least one ShapedClause-declared entry"
    for entry in shaped:
        assert entry["discovered_by"]
        assert entry["discovered_by_normalized"] == " ".join(
            entry["discovered_by"].casefold().split()
        )


def test_hyphenated_script_writer_is_loaded_via_file_location_fallback():
    # coordinator/bin/seed-marketplace-enabledplugins.py cannot be reached by
    # importlib.import_module (hyphenated filename, no package) — pin that the generic
    # dotted-import-then-file-location-fallback path still reaches it and emits its
    # declared entries.
    manifest = wsm.build_manifest()
    entries = [
        e for e in manifest["entries"] if e["writer_id"] == "seed-marketplace-enabledplugins"
    ]
    assert entries
    assert all(e["status"] in ("declared", "declared-empty") for e in entries)


def test_dotted_module_path_none_for_non_identifier_filename():
    module_path = (
        _REPO_ROOT / "coordinator" / "bin" / "seed-marketplace-enabledplugins.py"
    )
    assert wsm._dotted_module_path(_REPO_ROOT, module_path) is None


def test_declares_write_surface_finds_binding_nested_in_if(tmp_path):
    # Review: code-reviewer — a `WRITE_SURFACE` bound inside a module-level `if` (e.g. a
    # platform branch) is a child of the `If` node, not of `tree.body`; the scan must
    # still find it.
    module_path = tmp_path / "nested_if_writer.py"
    module_path.write_text(
        "import sys\n"
        "if sys.platform == 'win32':\n"
        "    WRITE_SURFACE = object()\n"
        "else:\n"
        "    WRITE_SURFACE = object()\n"
    )
    assert wsm._declares_write_surface(module_path) is True


def test_declares_write_surface_finds_binding_nested_in_try(tmp_path):
    # Same nesting concern for an import-fallback `try`/`except` pattern.
    module_path = tmp_path / "nested_try_writer.py"
    module_path.write_text(
        "try:\n"
        "    from somewhere import WRITE_SURFACE\n"
        "except ImportError:\n"
        "    WRITE_SURFACE = object()\n"
    )
    assert wsm._declares_write_surface(module_path) is True


def test_declares_write_surface_ignores_binding_nested_in_function(tmp_path):
    # A `WRITE_SURFACE` bound inside a function body is not a module-level declaration —
    # the scan should not match it.
    module_path = tmp_path / "function_scoped_writer.py"
    module_path.write_text(
        "def build():\n"
        "    WRITE_SURFACE = object()\n"
        "    return WRITE_SURFACE\n"
    )
    assert wsm._declares_write_surface(module_path) is False


def test_dotted_module_path_present_for_identifier_filename():
    module_path = _REPO_ROOT / "coordinator_core" / "ops" / "configure_git.py"
    assert (
        wsm._dotted_module_path(_REPO_ROOT, module_path)
        == "coordinator_core.ops.configure_git"
    )

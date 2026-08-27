"""
coordinator_core.tests.test_repo_identity_gate_extraction — C1 extraction
test: `compute_repo_identity_gate` moved out of `coordinator_core.pickup_assemble`
into the lean `coordinator_core.repo_identity_gate` module.

Spec backlink: `pln-a-ceremony-must-not-be-able-to-5e9421` § C1.

This suite does NOT re-derive `compute_repo_identity_gate`'s own verdict-ladder
behaviour — that is exhaustively covered at
`coordinator_core/pickup_assemble/tests/test_repo_identity_gate.py`, which
keeps passing unchanged against the re-export (proving parity by continuing
to exercise the same call paths through the old import site). This file
instead asserts the SHAPE of the extraction itself:

  1. The lean module is importable standalone and produces identical
     objects to the ones `pickup_assemble` re-exports (same functions, same
     verdict-string constants) — an extraction, not a fork.
  2. Each of the five direct importers named in the C1 chunk body now
     imports from the lean module, not from `pickup_assemble` — the actual
     defect this chunk exists to close (a re-export alone does not save a
     caller that bypassed `repo_identity.resolve_checked_repo_root`).
  3. Importing the lean module alone does not drag in `pickup_assemble`'s
     own heavy dependency chain (e.g. `yaml`, `coordinator_core.dag`) —
     which is the entire cold-start saving this chunk exists to bank.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()

_DIRECT_IMPORTER_PATHS = [
    _PROJECT_ROOT / "coordinator_core" / "baton_assemble" / "apply.py",
    _PROJECT_ROOT / "coordinator_core" / "workstream_complete" / "apply.py",
    _PROJECT_ROOT / "coordinator_core" / "execute_plan_assemble" / "close_out_and_stamp.py",
    _PROJECT_ROOT / "coordinator_core" / "session_ledger" / "aggregate_chain_loe.py",
    _PROJECT_ROOT / "coordinator_core" / "write_guards" / "guard_doctrine_surface_edits.py",
]


class TestLeanModuleIsTheSourceOfTruth:
    def test_lean_module_importable_standalone(self):
        module = importlib.import_module("coordinator_core.repo_identity_gate")
        assert hasattr(module, "compute_repo_identity_gate")
        assert hasattr(module, "_repo_identity_plausible_cwd")
        assert module._REPO_IDENTITY_MATCH == "MATCH"
        assert module._REPO_IDENTITY_MISMATCH == "MISMATCH"
        assert module._REPO_IDENTITY_UNRESOLVED == "UNRESOLVED"

    def test_pickup_assemble_reexports_identical_objects(self):
        lean = importlib.import_module("coordinator_core.repo_identity_gate")
        legacy = importlib.import_module("coordinator_core.pickup_assemble")

        # Extraction, not a rename/fork: the re-export must be the SAME
        # object, not a re-implementation that happens to match today.
        assert legacy.compute_repo_identity_gate is lean.compute_repo_identity_gate
        assert legacy._repo_identity_plausible_cwd is lean._repo_identity_plausible_cwd
        assert legacy._REPO_IDENTITY_MATCH == lean._REPO_IDENTITY_MATCH
        assert legacy._REPO_IDENTITY_MISMATCH == lean._REPO_IDENTITY_MISMATCH
        assert legacy._REPO_IDENTITY_UNRESOLVED == lean._REPO_IDENTITY_UNRESOLVED


class TestFiveDirectImportersRepointed:
    """staff-eng Finding 10: the re-export alone does not save these five --
    each bypassed `repo_identity.resolve_checked_repo_root` and imported
    `compute_repo_identity_gate` directly from `pickup_assemble`. Verified
    against the source text on disk, not a stale/cached import graph."""

    @pytest.mark.parametrize("path", _DIRECT_IMPORTER_PATHS, ids=lambda p: p.name)
    def test_importer_no_longer_names_pickup_assemble_for_the_gate(self, path):
        text = path.read_text(encoding="utf-8")
        assert not re.search(
            r"from coordinator_core\.pickup_assemble import \(?[^)\n]*compute_repo_identity_gate",
            text,
            re.DOTALL,
        ), f"{path} still imports compute_repo_identity_gate from pickup_assemble"

    @pytest.mark.parametrize("path", _DIRECT_IMPORTER_PATHS, ids=lambda p: p.name)
    def test_importer_now_names_the_lean_module(self, path):
        text = path.read_text(encoding="utf-8")
        assert re.search(
            r"from coordinator_core\.repo_identity_gate import \(?[\s\S]{0,120}?compute_repo_identity_gate",
            text,
        ), f"{path} does not import compute_repo_identity_gate from the lean module"


@pytest.mark.cadence
@pytest.mark.spawns_process
class TestLeanImportDoesNotDragPickupAssemble:
    """The whole point of the extraction: importing the gate must not cost
    the 10k-line module's own dependency chain (per its own inline comment,
    ~360ms, dragging `subagent_sandbox` -> `yaml`)."""

    def test_importing_lean_module_in_a_fresh_interpreter_skips_pickup_assemble(self):
        import subprocess

        from coordinator_core.win_portability import no_console_creationflags

        probe = (
            "import sys; "
            "import coordinator_core.repo_identity_gate; "
            "print('pickup_assemble_loaded=' + str('coordinator_core.pickup_assemble' in sys.modules))"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            **no_console_creationflags(),
        )
        assert result.returncode == 0, result.stderr
        assert "pickup_assemble_loaded=False" in result.stdout

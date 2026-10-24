"""mise-prep-run declares the residue files it writes through recording_declared_writes."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from coordinator_core import cli_entry
from coordinator_core.ops.workflow_bind import _workflow_bind_args
from coordinator_core.ops.workflow_validate import _workflow_validate

_MODULE_PATH = Path(__file__).resolve().parents[1] / "mise-prep-run.py"
_spec = importlib.util.spec_from_file_location("mise_prep_run_declares", _MODULE_PATH)
mpr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mpr)

_BAD = {"SPINE": {"status": "DEFECT", "kind": "spine-absent"}}


def _plan(tmp_path: Path) -> str:
    rel = "docs/plans/p0.md"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntitle: p0\n---\n\nbody\n", encoding="utf-8")
    return rel


def _stub(monkeypatch, validate_ok: bool) -> None:
    def fake(repo_root, op, params):
        if op in ("plan.seam_record", "plan.seam_check"):
            return {"verdict": "CLEAN", "set": list(params["plans"]), "per_plan": {}, "findings": []}
        if op == "plan.mise_prep_probe":
            return {
                "gate": {
                    "verdict": "NOT-PREPPED",
                    "classes": _BAD,
                    "withheld_rows": [],
                    "stamp": {"state": None},
                },
                "census": None,
                "upgrade": None,
                "process_ms": 0.0,
            }
        if op == "workflow.bind_args":
            return _workflow_bind_args(params, repo_root)
        if op == "workflow.validate":
            if validate_ok:
                return _workflow_validate(params, repo_root)
            return {"ok": False, "findings": [{"message": "refused"}]}
        raise AssertionError(f"unexpected call to {op}")

    monkeypatch.setattr(mpr, "_invoke", fake)


def _capture_records(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []
    monkeypatch.setattr(
        cli_entry, "_record", lambda declared, cwd, max_paths=None: seen.append(list(declared))
    )
    return seen


def _residue_files(tmp_path: Path) -> list[Path]:
    root = tmp_path / "state" / "mise-workflows"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def test_an_emission_records_exactly_the_seam_note_and_script_it_wrote(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    _stub(monkeypatch, validate_ok=True)
    seen = _capture_records(monkeypatch)

    rc = mpr.main([plan, "--repo-root", str(tmp_path), "--dry-run"])

    assert rc == mpr.EXIT_NOT_ALL_CERTIFIED
    written = {
        p.resolve() for p in _residue_files(tmp_path) if not p.name.endswith(".emitted.json")
    }
    assert any(p.suffix == ".mjs" for p in written)
    assert len(seen) == 1
    assert {Path(d).resolve() for d in seen[0]} == written


def test_a_validate_refusal_leaves_no_file_behind(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    _stub(monkeypatch, validate_ok=False)
    _capture_records(monkeypatch)

    rc = mpr.main([plan, "--repo-root", str(tmp_path), "--dry-run"])

    assert rc != mpr.EXIT_OK
    assert _residue_files(tmp_path) == []


def test_no_declaration_is_recorded_outside_a_residue_emission(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    _stub(monkeypatch, validate_ok=True)
    seen = _capture_records(monkeypatch)
    monkeypatch.setattr(mpr, "_emit_residue", lambda *a, **k: ([], 0))

    mpr.main([plan, "--repo-root", str(tmp_path), "--dry-run"])

    assert seen == []
    assert _residue_files(tmp_path) == []


@pytest.mark.parametrize("name", ["_emit_residue", "_write_residue"])
def test_both_residue_entry_points_exist(name):
    assert callable(getattr(mpr, name))

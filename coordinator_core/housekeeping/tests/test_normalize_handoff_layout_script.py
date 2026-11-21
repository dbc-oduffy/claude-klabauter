"""scripts/normalize_handoff_layout.py: dry-run moves nothing, --apply canonicalizes, rerun is a no-op."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "normalize_handoff_layout.py"


def _load():
    spec = importlib.util.spec_from_file_location("normalize_handoff_layout_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _baton(root: Path, rel: str, state: str = "ready_to_fire", claimed: str | None = None) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = f"---\ndeployment_state: {state}\n"
    if claimed is not None:
        fm += f"claimed_by: {claimed}\n"
    p.write_text(fm + "---\nbody\n", encoding="utf-8")


def test_fleet_iterates_registry_dedupes_and_reports_missing(tmp_path, monkeypatch, capsys):
    mod = _load()
    a, b, ghost = tmp_path / "a", tmp_path / "b", tmp_path / "ghost"
    _baton(a, "state/handoffs/2026-10-01-x.md")
    _baton(b, "state/handoffs/2026-10-02-y.md", "shipped")
    monkeypatch.setattr(mod._fleet_names, "_registry_paths", lambda: [str(a), str(b), str(a), str(ghost)])
    assert mod.main([]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["repo"] for r in out["repos"]] == sorted(str(p) for p in (a, b, ghost))
    by = {Path(r["repo"]).name: r for r in out["repos"]}
    assert by["ghost"]["missing"] is True
    assert by["a"]["flat"] == 1 and by["b"]["terminal"] == 1
    assert out["moves"] == 2 and out["dry_run"] is True
    assert (a / "state/handoffs/2026-10-01-x.md").exists()


def test_fleet_with_empty_registry_refuses_naming_repos(monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod._fleet_names, "_registry_paths", lambda: [])
    assert mod.main([]) == 2
    assert "repos.*" in capsys.readouterr().err


def test_apply_never_overwrites_an_existing_destination(tmp_path, monkeypatch):
    mod = _load()
    _baton(tmp_path, "state/handoffs/2026-10-01-x.md")
    plan = mod.plan_layout(str(tmp_path))
    assert len(plan.moves) == 1
    _baton(tmp_path, "state/handoffs/2026-10/2026-10-01-x.md")  # appears after planning
    result = mod._apply(tmp_path, plan)
    assert result["moved"] == 0 and result["refused_collision"] == ["state/handoffs/2026-10-01-x.md"]
    assert (tmp_path / "state/handoffs/2026-10-01-x.md").exists()


def test_docstring_names_the_commands():
    doc = _load().__doc__
    for needle in ("--repo <repo-root>", "--repo <repo-root> --apply", "normalize_handoff_layout.py --apply"):
        assert needle in doc

"""scripts/normalize_handoff_layout.py: dry-run moves nothing, --apply canonicalizes, rerun is a no-op."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
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


def _fixture(root: Path) -> None:
    _baton(root, "state/handoffs/2026-10-01-flat-live.md")
    _baton(root, "state/handoffs/2026-10-02-flat-done.md", "shipped")
    _baton(root, "state/handoffs/2026-10-04-flat-claimed.md", claimed="sess-1")
    _baton(root, "state/handoffs/2026-10/2026-10-06-month-live.md")
    _baton(root, "state/handoffs/2026-10/2026-10-07-month-done.md", "closed")
    _baton(root, "state/handoffs/2026-10-09-perdir/2026-10-09-perdir.md")
    (root / "state/handoffs/mixed").mkdir()
    (root / "state/handoffs/mixed/a.md").write_text("x", encoding="utf-8")
    (root / "state/handoffs/mixed/b.md").write_text("y", encoding="utf-8")
    _baton(root, "archive/handoffs/2026-09-01-archived-flat.md", "shipped")


def _tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_SCRIPT), *args], capture_output=True, text=True, timeout=60)


def test_dry_run_moves_nothing_and_reports_counts(tmp_path):
    _fixture(tmp_path)
    before = _tree(tmp_path)
    proc = _run("--repo", str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert _tree(tmp_path) == before
    assert report["dry_run"] is True
    assert report["flat"] == 3
    assert report["month"] == 2
    assert report["per_handoff_dir"] == 2
    assert report["terminal"] == 3
    assert report["skipped_live_or_claimed"] == 1
    assert report["unclassified_dirs"] == ["state/handoffs/mixed"]
    assert report["moves"] > 0


def test_apply_canonicalizes_and_rerun_is_noop(tmp_path):
    _fixture(tmp_path)
    first = json.loads(_run("--repo", str(tmp_path), "--apply").stdout)
    assert first["dry_run"] is False and first["moved"] == first["moves"] > 0
    names = _tree(tmp_path)
    assert "state/handoffs/2026-10/2026-10-01-flat-live.md" in names
    assert "archive/handoffs/2026-10/2026-10-02-flat-done.md" in names
    assert "archive/handoffs/2026-10/2026-10-07-month-done.md" in names
    assert "archive/handoffs/2026-09/2026-09-01-archived-flat.md" in names
    assert "state/handoffs/2026-10/2026-10-09-perdir.md" in names
    assert "state/handoffs/2026-10-04-flat-claimed.md" in names  # claimed live baton untouched
    assert "state/handoffs/mixed/a.md" in names  # unclassified dir untouched
    assert not (tmp_path / "state/handoffs/2026-10-09-perdir").exists()
    second = json.loads(_run("--repo", str(tmp_path), "--apply").stdout)
    assert second["moves"] == 0 and second["moved"] == 0
    assert _tree(tmp_path) == names


def test_missing_handoff_tree_is_reported_not_failed(tmp_path):
    proc = _run("--repo", str(tmp_path))
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["missing"] is True


def test_repo_not_a_directory_exits_2(tmp_path):
    proc = _run("--repo", str(tmp_path / "nope"))
    assert proc.returncode == 2


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

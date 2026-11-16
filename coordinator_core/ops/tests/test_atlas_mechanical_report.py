"""update_docs.atlas_mechanical_report: the op writes produce()'s report and replies with path and counts."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from types import SimpleNamespace

from coordinator_core.architecture_survey import mechanical_report as mr
from coordinator_core.architecture_survey.tests.test_mechanical_report import EDGES, ROWS, _atlas
from coordinator_core.ops import atlas_mechanical_report as mod
from pathlib import Path
import pytest


def _stub_structure(monkeypatch, structure) -> None:
    """Replace the live rag fetch and the repo-resident citation checker with clean fixtures."""
    monkeypatch.setattr(mr, "_fetch_structure", lambda root, run_dir: structure)
    clean = SimpleNamespace(run=lambda root, atlas: SimpleNamespace(findings=[]))
    monkeypatch.setattr(mr, "load_checker", lambda root: clean)


def test_default_call_writes_report_and_returns_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    structure = _atlas(tmp_path, ROWS, EDGES)
    _stub_structure(monkeypatch, structure)
    reply = asyncio.run(mod._handler({"repo_root": str(tmp_path)}))
    expected = tmp_path.resolve() / "scratch" / "atlas-mechanical" / f"{date.today().isoformat()}-report.json"
    assert reply["exit_code"] == 0
    assert reply["report_path"] == str(expected)
    report = json.loads(expected.read_text(encoding="utf-8"))
    assert reply["pages_changed"] == len(report["pages_changed"])
    assert reply["dead_paths"] == len(report["dead_paths"])
    assert reply["tokens"] == report["tokens"]["narrative_payload"]


def test_out_param_redirects_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_structure(monkeypatch, _atlas(tmp_path, ROWS, EDGES))
    target = tmp_path / "elsewhere" / "r.json"
    reply = mod._run({"repo_root": str(tmp_path), "out": str(target)})
    assert reply["report_path"] == str(target)
    assert target.is_file()
    assert not (tmp_path / "scratch" / "atlas-mechanical" / f"{date.today().isoformat()}-report.json").exists()


def test_missing_repo_root_is_refused(tmp_path: Path) -> None:
    reply = mod._run({"repo_root": str(tmp_path / "absent")})
    assert reply == {"exit_code": 2, "error": "not-a-git-repo"}

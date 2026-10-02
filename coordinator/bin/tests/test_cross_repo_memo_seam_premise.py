"""The premise check locates `scoped_to.seam` in the receiver's artifact instead of calling it pinned."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import sys

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli():
    sys.path.insert(0, str(_BIN_DIR))
    loader = importlib.machinery.SourceFileLoader("cross_repo_memo_seam", str(_BIN_DIR / "cross-repo-memo.py"))
    spec = importlib.util.spec_from_loader("cross_repo_memo_seam", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _run(cli, tmp_path, scoped_to, capsys):
    cli._run_scoped_premise_checks("recv-em", str(tmp_path), "ask", scoped_to)
    return capsys.readouterr().out


def test_seam_found_reports_line_and_demands_reading(tmp_path, capsys):
    (tmp_path / "PIPELINE.md").write_text("intro\n## Phase 6: Tail - Close Out the Run\nbody\n", encoding="utf-8")
    out = _run(_load_cli(), tmp_path, {"artifact": "PIPELINE.md", "seam": "Phase 6: Tail - Close Out the Run / Baton disposition"}, capsys)
    assert "PIPELINE.md:2" in out
    assert "locating is not reading" in out
    assert "no automated oracle" not in out


def test_seam_absent_from_artifact_is_named(tmp_path, capsys):
    (tmp_path / "PIPELINE.md").write_text("nothing relevant\n", encoding="utf-8")
    out = _run(_load_cli(), tmp_path, {"artifact": "PIPELINE.md", "seam": "Phase 9"}, capsys)
    assert "NOT FOUND in PIPELINE.md" in out


def test_seam_without_artifact_is_not_called_pinned(tmp_path, capsys):
    out = _run(_load_cli(), tmp_path, {"seam": "Phase 6"}, capsys)
    assert "NOT checked" in out
    assert "seam Phase 6: pinned" not in out

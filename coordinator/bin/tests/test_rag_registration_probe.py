"""rag-registration probe: silent when example-retrieval-repo is absent, one defect line per unregistered repo."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

from coordinator_core.install import rag_fleet_registration as rfr

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "workday_start_health_probes_under_test_rr", _BIN_DIR / "workday-start-health-probes.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("probe spawned a subprocess")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)


def test_absent_rag_is_silent_exit_zero(monkeypatch, capsys):
    _no_spawn(monkeypatch)
    monkeypatch.setattr(rfr, "detect_example_retrieval_repo", lambda: None)
    assert _mod.main(["rag-registration"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_unregistered_repos_each_get_a_defect_line(monkeypatch, capsys, tmp_path):
    _no_spawn(monkeypatch)
    rag = rfr.RagInstall(root=tmp_path, interpreter="py")
    monkeypatch.setattr(rfr, "detect_example_retrieval_repo", lambda: rag)
    monkeypatch.setattr(rfr, "unregistered_repos", lambda r: [("a", tmp_path / "a"), ("b", tmp_path / "b")])
    assert _mod.main(["rag-registration"]) == 1
    lines = capsys.readouterr().err.strip().splitlines()
    assert lines == [rfr.remedy_line("a"), rfr.remedy_line("b")]
    assert all("fix: python scripts/setup.py" in ln for ln in lines)


def test_all_registered_is_silent_exit_zero(monkeypatch, capsys, tmp_path):
    rag = rfr.RagInstall(root=tmp_path, interpreter="py")
    monkeypatch.setattr(rfr, "detect_example_retrieval_repo", lambda: rag)
    monkeypatch.setattr(rfr, "unregistered_repos", lambda r: [])
    assert _mod.main(["rag-registration"]) == 0
    assert capsys.readouterr().err == ""


def test_unknown_argument_refused(capsys):
    assert _mod.main(["rag-registration", "--bogus"]) == 2


def test_listed_in_usage_and_subcommands(capsys):
    assert "rag-registration" in _mod._SUBCOMMANDS
    _mod.main([])
    assert "rag-registration" in capsys.readouterr().err

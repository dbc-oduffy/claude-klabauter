"""A missing `--` separator yields a terse fact + usage line, not the help wall."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib

import pytest

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent
_SYNTAX = 'coordinator-safe-commit [--body-file F] "<subject>" -- <path>...'


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_safe_commit", str(_BIN_DIR / "coordinator-safe-commit.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_safe_commit", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def test_missing_separator_prints_fact_then_usage_line(monkeypatch, tmp_path, capsys):
    mod = _load_cli_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.py").write_text("x")
    with pytest.raises(SystemExit) as ei:
        mod.main(["my subject", "a.py", "b.py"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert "`--` separator" in lines[0]
    assert _SYNTAX in err
    assert len(lines) <= 3
    assert 'Try: coordinator-safe-commit "my subject" -- a.py b.py' in err
    assert "DR-344" not in err and "killed" not in err


def test_missing_separator_with_nonexistent_paths_offers_no_rewrite(monkeypatch, tmp_path, capsys):
    mod = _load_cli_module()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        mod.main(["my subject", "nope.py"])
    err = capsys.readouterr().err
    assert _SYNTAX in err
    assert "Try:" not in err
    assert len(err.splitlines()) == 2

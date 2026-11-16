"""CLI seam tests for coordinator/bin/update-docs-bookkeeping.py: argv parsing, --help, and
the exit-code contract (1 usage, 2 op failure)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import pytest

_PATH = Path(__file__).resolve().parents[1] / "update-docs-bookkeeping.py"
_spec = importlib.util.spec_from_file_location("update_docs_bookkeeping_cli", _PATH)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


def test_parse_maps_flags_to_op_params(tmp_path: Path) -> None:
    params, repo = cli._parse_argv(["--write", "--rebaseline", "--ack", "--repo", str(tmp_path)])
    assert params == {"write": True, "rebaseline": True, "ack": True}
    assert repo == tmp_path


def test_default_parse_sets_no_write() -> None:
    params, _ = cli._parse_argv([])
    assert params == {}


def test_help_exits_zero_with_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0
    assert "Usage:" in capsys.readouterr().out


def test_unknown_flag_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--bogus"]) == 1
    assert "usage error" in capsys.readouterr().err


def test_op_failure_exits_two(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def boom():
        raise RuntimeError("engine gone")

    monkeypatch.setattr(cli, "_engine", boom)
    assert cli.main([]) == 2
    assert "engine gone" in capsys.readouterr().err


def test_success_prints_the_op_result_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        cli, "_engine", lambda: (lambda params, repo_root: {"inventory": {}, "index_regen": {}}, lambda r: r)
    )
    assert cli.main([]) == 0
    assert capsys.readouterr().out.strip() == '{"inventory": {}, "index_regen": {}}'

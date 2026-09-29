"""The over-cap summary refusal and the unregistered-sender warning (coordinator-content-repo #111)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import sys

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli():
    sys.path.insert(0, str(_BIN_DIR))
    loader = importlib.machinery.SourceFileLoader("cross_repo_memo_w5c", str(_BIN_DIR / "cross-repo-memo.py"))
    spec = importlib.util.spec_from_loader("cross_repo_memo_w5c", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _Refusal(RuntimeError):
    op_stderr = "fleet op setup error: memo.send: summary is 130 chars, cap is 120 — shorten it"
    result = None


def test_over_cap_refusal_reaches_stdout_with_actual_length(capsys):
    cli = _load_cli()
    cli._echo_summary_cap_refusal_to_stdout(_Refusal("refused"))
    out = capsys.readouterr().out
    assert "summary is 130 chars, cap is 120" in out


def test_unrelated_refusal_prints_nothing_on_stdout(capsys):
    cli = _load_cli()
    cli._echo_summary_cap_refusal_to_stdout(RuntimeError("collision"))
    assert capsys.readouterr().out == ""

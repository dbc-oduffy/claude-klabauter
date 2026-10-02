"""`coordinator-safe-commit --help` names the file-borne pathspec route and its subagent limitation."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import pathlib

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _usage_text() -> str:
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_safe_commit", str(_BIN_DIR / "coordinator-safe-commit.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_safe_commit", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    stream = io.StringIO()
    mod.usage(stream)
    return stream.getvalue()


def test_usage_names_the_params_file_route():
    text = _usage_text()
    assert "coordinator-invoke ceremony.commit_v2 --params-file <file>" in text


def test_usage_states_a_subagent_is_denied_on_that_route():
    text = " ".join(_usage_text().split())
    assert "A subagent is denied on that route" in text

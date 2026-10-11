"""The send path prints the bare-path warning for neither-repo paths only."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "cross_repo_memo_citation_warning", str(_BIN_DIR / "cross-repo-memo.py")
    )
    spec = importlib.util.spec_from_loader("cross_repo_memo_citation_warning", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def test_ambiguous_only_prints_nothing(capsys):
    mod = _load_cli_module()
    mod._print_citation_warning({"citations_ambiguous": ["docs/b.md"]})
    assert capsys.readouterr().out == ""


def test_unresolved_prints_new_text(capsys):
    mod = _load_cli_module()
    mod._print_citation_warning({"citations_unresolved": ["docs/n.md"]})
    out = capsys.readouterr().out
    assert "bare paths left unqualified (in neither repo): docs/n.md" in out
    assert "ambiguous" not in out

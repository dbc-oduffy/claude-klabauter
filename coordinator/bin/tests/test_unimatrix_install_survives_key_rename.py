"""The publish repo-key rename must not turn test_unimatrix_install.py into a SyntaxError.

The rename rewrites the authoring repo key to the published one wherever it appears as a literal.
A call that passes both keys as keywords collapses to a repeated keyword (SyntaxError) in the
mirror (round 5 failed this way on `claude_klabauter=..., claude_klabauter=...`). The authoring key
is therefore built by concatenation in the source.
"""
import ast
from pathlib import Path

_AUTHORING = "project_" + "claude-klabauter"
_PUBLISHED = "claude_" + "klabauter"
_SRC = Path(__file__).resolve().with_name("test_unimatrix_install.py")


def test_renamed_source_parses_without_repeated_keywords():
    text = _SRC.read_text(encoding="utf-8")
    assert f"{_AUTHORING}=" not in text, "authoring key as a keyword collapses into the published key on rename"
    tree = ast.parse(text.replace(_AUTHORING, _PUBLISHED))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            names = [k.arg for k in node.keywords if k.arg]
            assert len(names) == len(set(names)), f"repeated keyword after rename at line {node.lineno}"

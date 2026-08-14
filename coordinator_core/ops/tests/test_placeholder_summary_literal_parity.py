"""
coordinator_core.ops.tests.test_placeholder_summary_literal_parity

Pins `handoff_normalize._PLACEHOLDER_SUMMARIES`'s spinoff literal
byte-identical to `coordinator/bin/coordinator-doc-new.py::_scaffold_spinoff`'s
`placeholder_summary` local. The two copies exist because `coordinator/bin/`
scripts import FROM `coordinator_core`, never the reverse (see
`handoff_normalize.py`'s comment above `_PLACEHOLDER_SUMMARIES`), so the
constant cannot be hoisted to a shared module -- duplication is the accepted
shape, not the defect.

What breaks if this test goes RED: the two literals have drifted. Either
`handoff_normalize`'s absence-detection stops recognizing
`coordinator-doc-new.py`-scaffolded spinoff placeholders (they silently land
in `state/` uncorrected, un-backfilled, with no other test catching it), or
the scaffold's placeholder text changed and normalize's copy is now stale.
Fix the drifted literal -- do NOT delete this test to make it pass.

`coordinator-doc-new.py` is a script (hyphenated filename, not on the import
path, and executes `main()` only under `__name__ == "__main__"`). Rather than
import it -- which would pull in its full argparse/CLI surface for a single
string constant -- this test parses its AST and extracts the `placeholder_summary`
local from `_scaffold_spinoff` as a literal `ast.Constant` (or a single-part
f-string with no interpolation, which is how the source currently spells it).
An extraction that requires anything more than "the module parses, and this
one assignment's RHS is a constant string" fails loudly rather than silently
approximating -- a brittle regex over the source text was rejected as worse
than no test.
"""

from __future__ import annotations

import ast
from pathlib import Path

from coordinator_core.ops.handoff_normalize import _PLACEHOLDER_SUMMARIES

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCAFFOLD_SCRIPT = _REPO_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"


def _extract_spinoff_placeholder_summary_literal() -> str:
    """AST-extract the `placeholder_summary` local from `_scaffold_spinoff`.

    Walks the parsed module for a function named `_scaffold_spinoff`, then
    for a top-level `Assign` inside it whose single target is the name
    `placeholder_summary`, whose RHS is either a plain string `Constant` or
    an f-string (`JoinedStr`) with exactly one `Constant` part (i.e. no
    actual interpolation -- matching the source's `f"PLACEHOLDER..."` with
    no `{}` fields). Anything else (multiple assignments, interpolated
    f-string, missing function) raises -- this test must not silently pass
    on a source shape it doesn't understand.
    """
    source = _SCAFFOLD_SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_SCAFFOLD_SCRIPT))

    func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_scaffold_spinoff":
            func_node = node
            break
    if func_node is None:
        raise AssertionError(
            "coordinator-doc-new.py: _scaffold_spinoff not found -- "
            "the scaffold function was renamed or removed; update this "
            "extraction alongside the rename"
        )

    matches: list[str] = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if node.targets[0].id != "placeholder_summary":
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            matches.append(value.value)
        elif isinstance(value, ast.JoinedStr) and len(value.values) == 1:
            part = value.values[0]
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                matches.append(part.value)
            else:
                raise AssertionError(
                    "coordinator-doc-new.py: _scaffold_spinoff's "
                    "placeholder_summary is now an interpolated f-string -- "
                    "this extraction only handles a literal-only f-string"
                )
        else:
            raise AssertionError(
                "coordinator-doc-new.py: _scaffold_spinoff's "
                "placeholder_summary RHS is not a plain string literal -- "
                "update this extraction to match the new shape"
            )

    if len(matches) != 1:
        raise AssertionError(
            "coordinator-doc-new.py: expected exactly one "
            f"`placeholder_summary = ...` assignment in _scaffold_spinoff, "
            f"found {len(matches)}"
        )
    return matches[0]


def test_spinoff_placeholder_summary_literal_stays_byte_identical():
    """
    `handoff_normalize._PLACEHOLDER_SUMMARIES` must contain
    `coordinator-doc-new.py::_scaffold_spinoff`'s `placeholder_summary`
    literal byte-for-byte. If this fails, one side's literal was edited
    without the other -- fix the drifted copy, don't delete this test.
    """
    scaffold_literal = _extract_spinoff_placeholder_summary_literal()
    assert scaffold_literal in _PLACEHOLDER_SUMMARIES, (
        "handoff_normalize._PLACEHOLDER_SUMMARIES no longer contains "
        f"coordinator-doc-new.py's spinoff placeholder_summary literal "
        f"({scaffold_literal!r}); the two copies have drifted -- see this "
        "test's module docstring for the duplication rationale."
    )

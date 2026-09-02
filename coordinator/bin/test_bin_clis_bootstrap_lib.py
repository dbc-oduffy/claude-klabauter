"""Every `coordinator/bin/*.py` importing a `lib/` sibling by bare name must
bootstrap `lib` first.

`coordinator/bin/lib/__init__.py` is the ONE place `coordinator/bin/lib` is put
on `sys.path` (see its docstring — 273 scattered preambles were collapsed into
it precisely so no module body mutates interpreter global state inside the warm
server ~50 sessions share). The cost of that centralisation is a prelude every
CLI has to remember:

    import lib  # noqa: F401 — bootstraps coordinator/bin/lib onto sys.path
    from cc_invoke import require_dispatch_engine_on_path

Omitting it is silent at author time and silent at import time. It raises
`ModuleNotFoundError: No module named 'cc_invoke'` only when the branch holding
the import actually executes — so a CLI whose bare-name import sits inside
`main()` past an argument check ships broken and stays broken until someone runs
that exact path.

That is not hypothetical. `write-identity-file.py` carried the defect in both
the authoring tree and the published mirror; it is invisible on any box that
already has `~/.claude/coordinator-identity.yaml`, because the values the step
would write are already there. It bites only a genuinely fresh install — the one
case nobody re-runs. Ten CLIs in this directory were missing the prelude when
this test was written.

A prelude repeated by hand across dozens of peers will be omitted again; this
test is the artifact that makes the omission loud instead of the operator
remembering. Static (AST) rather than import-based on purpose: importing each
module would not execute a function-scoped import, which is exactly where the
original defect lived.

Escape hatch: a module doing its own `sys.path` work is left alone — it has
taken the problem on explicitly, and this test does not adjudicate that choice.
"""

from __future__ import annotations

import ast
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
LIB_DIR = BIN_DIR / "lib"

PRELUDE = "import lib  # noqa: F401 — bootstraps coordinator/bin/lib onto sys.path"


def _sibling_module_names() -> set[str]:
    """Bare names importable only because `lib/__init__` inserted its own dir."""
    return {p.stem for p in LIB_DIR.glob("*.py")} - {"__init__"}


def _bare_name_importers(source: str, siblings: set[str]) -> list[tuple[str, int]]:
    """`from <lib sibling> import ...` statements, as (module, lineno) pairs.

    Absolute imports only (`level == 0`): a relative import resolves through the
    package machinery and never needs the bare-name path entry.
    """
    tree = ast.parse(source)
    return [
        (node.module, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module in siblings
    ]


def _bootstraps_lib(source: str) -> bool:
    tree = ast.parse(source)
    return any(
        isinstance(node, ast.Import) and any(alias.name == "lib" for alias in node.names)
        for node in ast.walk(tree)
    )


def test_every_bare_name_lib_importer_bootstraps_lib_first():
    siblings = _sibling_module_names()
    assert siblings, f"no lib siblings discovered under {LIB_DIR} — test is inert"

    offenders: list[str] = []
    for path in sorted(BIN_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            importers = _bare_name_importers(source, siblings)
        except SyntaxError:
            continue
        if not importers:
            continue
        if "sys.path" in source or _bootstraps_lib(source):
            continue
        module, lineno = importers[0]
        offenders.append(f"{path.name}:{lineno} imports '{module}' by bare name")

    assert not offenders, (
        "coordinator/bin CLI(s) import a lib/ sibling by bare name without "
        "bootstrapping lib first — each raises ModuleNotFoundError the moment "
        "that import executes:\n  "
        + "\n  ".join(offenders)
        + f"\n\nAdd this line above the bare-name import:\n    {PRELUDE}"
    )


def test_the_check_catches_a_planted_omission():
    """Proof the gate above can fail — a green suite must mean the tree is clean,
    never that the detector stopped detecting."""
    siblings = _sibling_module_names()
    sibling = sorted(siblings)[0]
    planted = f"def main():\n    from {sibling} import something\n"

    assert _bare_name_importers(planted, siblings)
    assert not _bootstraps_lib(planted)

    repaired = f"def main():\n    {PRELUDE}\n    from {sibling} import something\n"
    assert _bootstraps_lib(repaired)

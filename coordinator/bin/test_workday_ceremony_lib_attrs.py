"""Every `wc.<name>` a bin script reads must exist on `workday_ceremony_lib`.

Trap: these scripts bind the lib lazily, so a stale attribute only raises at
the call site, deep inside a ceremony after side effects (sync-main) have run.
"""
import ast
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(_BIN / "lib"))

import workday_ceremony_lib  # noqa: E402


def _wc_attrs(script: Path) -> set:
    tree = ast.parse(script.read_text(encoding="utf-8"))
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in ("wc", "_wc")
    }


def test_every_wc_attribute_exists():
    scripts = [
        p for p in _BIN.glob("*.py")
        if not p.name.startswith("test_") and "workday_ceremony_lib as" in p.read_text(encoding="utf-8")
    ]
    assert scripts, "no bin script imports workday_ceremony_lib -- glob is stale"
    missing = {
        (p.name, attr)
        for p in scripts
        for attr in _wc_attrs(p)
        if not hasattr(workday_ceremony_lib, attr)
    }
    assert not missing, sorted(missing)

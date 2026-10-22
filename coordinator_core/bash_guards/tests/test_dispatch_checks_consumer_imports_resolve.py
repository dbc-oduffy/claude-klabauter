"""Pin: every name a production module imports from bash_guards.dispatch_checks resolves there.

Covers `from coordinator_core.bash_guards.dispatch_checks import name` (top-level or
function-local) and `dispatch_checks.<name>` attribute reads after
`from coordinator_core.bash_guards import dispatch_checks`. A facade that drops a re-export
fails here, naming the consumer file and the missing name.
"""

from __future__ import annotations

import ast
from pathlib import Path

import coordinator_core
from coordinator_core.bash_guards import dispatch_checks

_FACADE = "coordinator_core.bash_guards.dispatch_checks"
_PACKAGE = "coordinator_core.bash_guards"
_NEEDLES = ("bash_guards.dispatch_checks import", "bash_guards import")


def _consumer_files() -> list[Path]:
    root = Path(coordinator_core.__file__).parent
    out = []
    for path in root.rglob("*.py"):
        if "tests" in path.relative_to(root).parts:
            continue
        if path.resolve() == Path(dispatch_checks.__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(n in text for n in _NEEDLES):
            out.append(path)
    return sorted(out)


def _referenced_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    module_aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level:
            continue
        if node.module == _FACADE:
            names.extend(a.name for a in node.names if a.name != "*")
        elif node.module == _PACKAGE:
            for a in node.names:
                if a.name == "dispatch_checks":
                    module_aliases.add(a.asname or a.name)
    if module_aliases:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in module_aliases
            ):
                names.append(node.attr)
    return names


def test_consumers_are_found():
    assert _consumer_files(), "no consumer of dispatch_checks found; the scan is vacuous"


def test_every_imported_name_resolves_on_the_facade():
    root = Path(coordinator_core.__file__).parent
    missing = [
        f"{path.relative_to(root).as_posix()}: {name}"
        for path in _consumer_files()
        for name in _referenced_names(path)
        if not hasattr(dispatch_checks, name)
    ]
    assert not missing, "dispatch_checks does not export:\n" + "\n".join(missing)

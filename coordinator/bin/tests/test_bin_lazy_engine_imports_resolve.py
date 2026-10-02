"""Every engine import a coordinator/bin CLI defers into a function body names a
module that exists.

A CLI can be present, registered and dispatchable while one subcommand cannot
run: its `from coordinator_core.<mod> import ...` sits inside the handler, so
nothing resolves it until a ceremony fires that subcommand. A retirement that
deletes `<mod>` without draining that caller leaves a live directive that
fails closed on ModuleNotFoundError at the first ceremony run. The
dispatchability and directive-id checks cannot see this; this test resolves
the import itself.

Static resolution against the tree, never an import: a module resolves when
`<mod>.py` or `<mod>/__init__.py` exists under the repo root. A name imported
from a package resolves when it is a submodule file or its text appears in the
package's `__init__.py`. No subcommand runs, nothing is imported, no spawn.
Platform- and addon-gated imports need no allowlist: the gate decides whether
the import runs, not whether the module exists in the tree.

Indentation is the lazy-import signal: an indented engine import is inside a
function, a class, or a module-level `if`/`try`, all of which run only on some
paths. Module-level unindented imports are already proven by importing the CLI.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BIN = _REPO_ROOT / "coordinator" / "bin"

_FROM_IMPORT = re.compile(
    r"^[ \t]+from[ \t]+(coordinator_core(?:\.\w+)*)[ \t]+import[ \t]+(\([^)]*\)|[^\n]+)",
    re.M,
)
_PLAIN_IMPORT = re.compile(r"^[ \t]+import[ \t]+(coordinator_core(?:\.\w+)*)", re.M)


def _module_file(root: Path, module: str) -> Path | None:
    base = root.joinpath(*module.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _imported_names(clause: str) -> list[str]:
    names = []
    for line in clause.strip("()").splitlines():
        line = line.split("#", 1)[0]
        for part in line.split(","):
            name = part.strip().split(" as ", 1)[0].strip()
            if name and name != "*" and name.isidentifier():
                names.append(name)
    return names


def find_unresolved_lazy_imports(root: Path, files: list[Path]) -> list[str]:
    """`<file>:<line> <target>` for each indented engine import naming nothing."""
    unresolved = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        if "coordinator_core" not in text:
            continue
        rel = path.relative_to(root).as_posix()
        for match in _FROM_IMPORT.finditer(text):
            module, clause = match.group(1), match.group(2)
            line = text.count("\n", 0, match.start()) + 1
            target = _module_file(root, module)
            if target is None:
                unresolved.append(f"{rel}:{line} {module}")
                continue
            if target.name != "__init__.py":
                continue
            init_text = target.read_text(encoding="utf-8")
            for name in _imported_names(clause):
                if _module_file(root, f"{module}.{name}") is None and name not in init_text:
                    unresolved.append(f"{rel}:{line} {module}.{name}")
        for match in _PLAIN_IMPORT.finditer(text):
            if _module_file(root, match.group(1)) is None:
                line = text.count("\n", 0, match.start()) + 1
                unresolved.append(f"{rel}:{line} {match.group(1)}")
    return unresolved


def _bin_sources() -> list[Path]:
    return sorted(
        p for p in _BIN.rglob("*.py")
        if "tests" not in p.parts and "__pycache__" not in p.parts
    )


def test_every_lazy_engine_import_in_bin_resolves():
    unresolved = find_unresolved_lazy_imports(_REPO_ROOT, _bin_sources())
    assert unresolved == [], (
        "coordinator/bin defers an engine import to a module or name that does not "
        "exist; the subcommand fails on ModuleNotFoundError/ImportError when a "
        "ceremony fires it. Repoint or delete the caller:\n  " + "\n  ".join(unresolved)
    )


def test_a_lazy_import_of_a_deleted_module_is_reported(tmp_path):
    pkg = tmp_path / "coordinator_core" / "ops"
    pkg.mkdir(parents=True)
    (tmp_path / "coordinator_core" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("LIVE = 1\n", encoding="utf-8")
    (pkg / "kept.py").write_text("", encoding="utf-8")
    cli = tmp_path / "coordinator" / "bin" / "probe-cli.py"
    cli.parent.mkdir(parents=True)
    cli.write_text(
        "from coordinator_core.ops import kept\n"
        "def run():\n"
        "    from coordinator_core.ops.kept import x\n"
        "    from coordinator_core.ops.retired_scan import y\n"
        "    from coordinator_core.ops import (\n"
        "        kept,  # submodule\n"
        "        LIVE,\n"
        "        retired_name as alias,\n"
        "    )\n"
        "    import coordinator_core.ops.gone\n",
        encoding="utf-8",
    )

    assert find_unresolved_lazy_imports(tmp_path, [cli]) == [
        "coordinator/bin/probe-cli.py:4 coordinator_core.ops.retired_scan",
        "coordinator/bin/probe-cli.py:5 coordinator_core.ops.retired_name",
        "coordinator/bin/probe-cli.py:10 coordinator_core.ops.gone",
    ]

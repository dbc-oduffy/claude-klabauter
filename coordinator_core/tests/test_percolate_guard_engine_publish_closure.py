"""The percolate guard engine ships to the engine-root mirror as a named carve-in
under the whole-entry `percolate` deny; this pins that the carve-in is exactly
`guards.py`'s import closure and reaches no scrub-table module."""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORE = _REPO_ROOT / "coordinator_core"
_DECLARATIONS = _REPO_ROOT / "setup" / "publish-allowlist-declarations.yaml"
_PORTABLE = _REPO_ROOT / "setup" / "publish-targets.portable"
_PKG = "coordinator_core.percolate"
_ROW = "claude-klabauter"
_FORBIDDEN = {
    "codename_provenance_seed",
    "depersonalize",
    "identity_permit",
    "content_strip_inventory",
}
_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _declared_admit() -> set[str]:
    rows = yaml.safe_load(_DECLARATIONS.read_text(encoding="utf-8"))["rows"]
    for entry in rows[_ROW]["deny"]:
        if isinstance(entry, dict) and entry["name"] == "percolate":
            return {
                _PKG if f == "__init__.py" else f"{_PKG}.{Path(f).stem}"
                for f in entry.get("admit") or []
            }
    raise AssertionError("percolate is no longer a deny entry on the engine row")


def _field7() -> list[str]:
    for line in _PORTABLE.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{_ROW}|"):
            return [e for e in line.split("|")[6].split(",") if e]
    raise AssertionError("engine row missing from publish-targets.portable")


def _module_file(mod: str) -> Path | None:
    base = _REPO_ROOT.joinpath(*mod.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def _imports(path: Path, mod: str, *, module_level: bool) -> set[str]:
    """Dotted `coordinator_core.*` module names imported by `path`: only the
    module-level ones when `module_level`, else every import in the file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    pkg_parts = mod.split(".") if path.name == "__init__.py" else mod.split(".")[:-1]
    found: set[str] = set()

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if module_level and isinstance(child, _FUNC_NODES):
                continue
            if isinstance(child, ast.Import):
                found.update(a.name for a in child.names)
            elif isinstance(child, ast.ImportFrom):
                if child.level:
                    base = pkg_parts[: len(pkg_parts) - (child.level - 1)]
                    target = ".".join(base + ([child.module] if child.module else []))
                else:
                    target = child.module or ""
                found.add(target)
                found.update(f"{target}.{a.name}" for a in child.names if a.name != "*")
            visit(child)

    visit(tree)
    return {m for m in found if m == "coordinator_core" or m.startswith("coordinator_core.")}


def _closure(root: str) -> set[str]:
    seen: set[str] = set()
    stack = [root]
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        path = _module_file(mod)
        if path is None:
            continue  # `from pkg import name` where name is not a module
        seen.add(mod)
        stack.extend(_imports(path, mod, module_level=True))
        parent = mod.rsplit(".", 1)[0]
        if "." in mod:
            stack.append(parent)  # importing a submodule runs its package __init__
    return seen


def test_admitted_percolate_set_is_exactly_the_guard_closure_modules():
    admitted = _declared_admit()
    assert len(admitted) == 8
    percolate_closure = {m for m in _closure(f"{_PKG}.guards") if m.startswith(_PKG)}
    assert percolate_closure == admitted


def test_field7_carries_exactly_the_admitted_percolate_entries_and_not_the_bare_name():
    entries = _field7()
    nested = {e for e in entries if e.startswith("percolate/")}
    assert nested == {
        "percolate/__init__.py" if m == _PKG else f"percolate/{m.rsplit('.', 1)[1]}.py"
        for m in _declared_admit()
    }
    assert "percolate" not in entries
    assert not any(e.startswith("!percolate") for e in entries)


def test_guard_closure_outside_percolate_ships_on_the_engine_row():
    entries = set(_field7())
    for mod in sorted(_closure(f"{_PKG}.guards")):
        if mod.startswith(_PKG) or mod == "coordinator_core":
            continue
        top = mod.split(".")[1]
        assert top in entries or f"{top}.py" in entries, f"{mod} is not published"


def test_admitted_modules_never_import_a_scrub_table_or_an_unadmitted_percolate_module():
    admitted = _declared_admit()
    for mod in sorted(admitted):
        path = _module_file(mod)
        assert path is not None, mod
        for imported in _imports(path, mod, module_level=False):
            if imported.startswith(_PKG + ".") or imported == _PKG:
                # `from coordinator_core.percolate import name` yields both the
                # package and `package.name`; only a real module is a hazard.
                if _module_file(imported) is None:
                    continue
                assert imported == _PKG or imported in admitted, f"{mod} imports {imported}"
            assert imported.rsplit(".", 1)[-1] not in _FORBIDDEN, f"{mod} imports {imported}"


def test_the_scrub_tables_are_not_admitted():
    admitted_stems = {m.rsplit(".", 1)[-1] for m in _declared_admit()}
    assert not (_FORBIDDEN & admitted_stems)

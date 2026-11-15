"""Render the three generated op-catalog files for a landing without importing op modules.

`render_for_landing(root, changed)` re-reads only the op modules in the landing set by AST,
patches their `description`/`summary` over the root's committed `op-catalog.json`, and renders
all three files through `emit_op_catalog.render_contracts`.

Traps: never use `emit_op_catalog._REPO`/`OUTPUTS` (they point at the engine install, not
`root`); the base catalog is HEAD's blob (linked worktrees included), never the worktree file, which
may carry a peer's uncommitted edit; the file is read only when HEAD holds no catalog. A change beyond a docstring (op set, class, timeout, schema) is refused.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from coordinator_core.contract.emit_op_catalog import render_contracts
from coordinator_core.contract.op_contract import _summary

GENERATED = (
    "coordinator_core/contract/op-catalog.json",
    "coordinator_core/contract/mcp-tools.json",
    "docs/reference/op-catalog.md",
)
_RENDER_KEYS = ("op-catalog.json", "mcp-tools.json", "op-catalog.md")
_REGISTRY_MAP = "coordinator_core/ops/_registry_map.py"
_REGEN_REQUIRED = frozenset(
    {
        _REGISTRY_MAP,
        "coordinator_core/ipc.py",
        "coordinator_core/authz/classification.py",
        "coordinator_core/contract/emit_op_catalog.py",
        "coordinator_core/contract/op_contract.py",
    }
)
_REGEN_COMMAND = "python -m coordinator_core.contract.emit_op_catalog --write"


class RenderRefused(Exception):
    """A correct render is impossible from the landing set; `path` names the file."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def _norm(p: str) -> str:
    p = p.replace("\\", "/")
    return p[2:] if p.startswith("./") else p


def _is_register_op(node: ast.expr) -> bool:
    if isinstance(node, ast.Call):
        node = node.func
    return (isinstance(node, ast.Name) and node.id == "register_op") or (
        isinstance(node, ast.Attribute) and node.attr == "register_op"
    )


def _str_arg(call: ast.Call, consts: Dict[str, str]) -> Optional[str]:
    """The op-name argument: a string literal, or a module-level `NAME = "literal"` constant."""
    if not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name):
        return consts.get(arg.id)
    return None


def _str_consts(tree: ast.Module) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            out[node.targets[0].id] = node.value.value
    return out


def _op_module_map(root: Path) -> Dict[str, str]:
    path = root / _REGISTRY_MAP
    try:
        tree = ast.parse(path.read_bytes())
    except (OSError, SyntaxError, ValueError) as exc:
        raise RenderRefused(_REGISTRY_MAP, f"unreadable: {exc}. {_regen_hint()}") from exc
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if isinstance(target, ast.Name) and target.id == "OP_MODULE_MAP" and isinstance(node.value, ast.Dict):
            out: Dict[str, str] = {}
            for k, v in zip(node.value.keys, node.value.values):
                if (
                    isinstance(k, ast.Constant)
                    and isinstance(v, ast.Constant)
                    and isinstance(k.value, str)
                    and isinstance(v.value, str)
                ):
                    out[k.value] = v.value
            return out
    raise RenderRefused(_REGISTRY_MAP, f"OP_MODULE_MAP literal not found. {_regen_hint()}")


def _regen_hint() -> str:
    return f"run `{_REGEN_COMMAND}` and include the three generated files in the run's writes"


def _handlers(tree: ast.Module) -> Dict[str, Optional[ast.AST]]:
    """op name -> handler def node (None when registered but not statically locatable)."""
    consts = _str_consts(tree)
    defs: Dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs[node.name] = node
    found: Dict[str, Optional[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and _is_register_op(dec):
                    name = _str_arg(dec, consts)
                    if name is not None:
                        found[name] = node
        elif isinstance(node, ast.Call):
            if _is_register_op(node) and isinstance(node.func, (ast.Name, ast.Attribute)):
                name = _str_arg(node, consts)
                if name is None or name in found:
                    continue
                fn = node.args[1] if len(node.args) > 1 else None
                found[name] = defs.get(fn.id) if isinstance(fn, ast.Name) else None
            elif isinstance(node.func, ast.Call) and _is_register_op(node.func):
                name = _str_arg(node.func, consts)
                fn = node.args[0] if node.args else None
                if name is not None and name not in found:
                    found[name] = defs.get(fn.id) if isinstance(fn, ast.Name) else None
    return found


def _describe(op: str, handler: ast.AST, module_doc: Optional[str]) -> str:
    """`op_contract.describe_doc`'s rule over AST docstrings."""
    doc = ast.get_docstring(handler, clean=True)  # type: ignore[arg-type]
    if doc is None or "module docstring" in doc or "\n" not in doc.strip():
        if module_doc:
            doc = f"{doc}\n\n{module_doc}" if doc else module_doc
    return doc or f"{op}: handler has no docstring"


def _head_catalog(root: Path) -> Tuple[str, bytes]:
    rel = GENERATED[0]
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.git.git_state import head_blobs

    common = resolve_git_common_dir(root)
    if common.is_dir():
        entry = head_blobs(root, [rel]).get(rel)
        if entry is not None:
            obj = read_object(common, entry[1])
            if obj is not None and obj[0] == "blob":
                return rel, obj[1]
    try:
        return rel, (root / rel).read_bytes()
    except OSError as exc:
        raise RenderRefused(rel, f"base catalog unreadable at HEAD or on disk: {exc}") from exc


def render_for_landing(root: Path, changed: List[str]) -> Optional[Dict[str, str]]:
    """Return {repo-relative generated path: text}, or None when no changed path is an op module.

    Raises RenderRefused when a correct render is impossible from the landing set.
    """
    root = Path(root)
    paths: Set[str] = {_norm(p) for p in changed}
    beyond = sorted(paths & _REGEN_REQUIRED)
    if beyond:
        raise RenderRefused(
            beyond[0], f"changes beyond a docstring patch; {_regen_hint()}"
        )
    if not any(p.startswith("coordinator_core/") and p.endswith(".py") for p in paths):
        return None

    op_map = _op_module_map(root)
    triggered: Dict[str, str] = {}
    by_module: Dict[str, List[str]] = {}
    for op, module in op_map.items():
        by_module.setdefault(module, []).append(op)
    for module in by_module:
        base = module.replace(".", "/")
        triggered[base + ".py"] = module
        triggered[base + "/__init__.py"] = module
    hits = sorted(p for p in paths if p in triggered)
    if not hits:
        return None

    cat_path, cat_bytes = _head_catalog(root)
    try:
        catalog = json.loads(cat_bytes.decode("utf-8"))
        contracts: List[dict] = catalog["ops"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RenderRefused(cat_path, f"base catalog unparsable: {exc}") from exc
    by_name = {c["name"]: c for c in contracts}

    for rel in hits:
        module = triggered[rel]
        try:
            tree = ast.parse((root / rel).read_bytes())
        except (OSError, SyntaxError, ValueError) as exc:
            raise RenderRefused(rel, f"cannot parse: {exc}") from exc
        module_doc = ast.get_docstring(tree, clean=True)
        handlers = _handlers(tree)
        for op in sorted(by_module[module]):
            handler = handlers.get(op)
            if handler is None:
                raise RenderRefused(
                    rel, f"handler for op {op!r} is not statically locatable in this file"
                )
            entry = by_name.get(op)
            if entry is None or entry.get("module") != module:
                raise RenderRefused(
                    rel, f"op {op!r} is absent from or moved in the committed catalog; {_regen_hint()}"
                )
            description = _describe(op, handler, module_doc)
            entry["description"] = description
            entry["summary"] = _summary(description)

    rendered = render_contracts(contracts)
    return {gen: rendered[key] for gen, key in zip(GENERATED, _RENDER_KEYS)}

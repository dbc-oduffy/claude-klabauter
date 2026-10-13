"""Guard: no engine source writes a pre-push hook or spawns `git lfs install`.

Walks coordinator_core/, coordinator/, scripts/ (skipping tests dirs, test_*.py,
__pycache__) with a byte prefilter before ast.parse. Predicates:
(a) list/tuple literal with adjacent string constants "lfs", "install";
(b) "pre-push" as a `/` path-join operand, an open/write_text/os.replace
    argument, or a module-level constant assignment;
(c) HookDisposition(hook_name="pre-push", action="replace");
(d) a string literal ending "/pre-push" or containing "git lfs install".
A retire entry (action="remove", hook_name inline) passes, and so does a
docstring: the negative-spec that records a removal names what was removed.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("coordinator_core", "coordinator", "scripts")
_WRITE_CALLS = {"open", "write_text", "write_bytes", "replace", "rename"}


def _str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _contains_pre_push(node: ast.AST) -> bool:
    return any(_str(n) == "pre-push" for n in ast.walk(node))


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            if _str(body[0].value) is not None:
                ids.add(id(body[0].value))
    return ids


def find_hits(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    docstrings = _docstring_ids(tree)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            vals = [_str(e) for e in node.elts]
            for a, b in zip(vals, vals[1:]):
                if a == "lfs" and b == "install":
                    hits.append((node.lineno, "spawns git lfs install"))
        elif _str(node) is not None and id(node) not in docstrings and (
            node.value.endswith("/pre-push") or "git lfs install" in node.value
        ):
            hits.append((node.lineno, "pre-push path or shell lfs install literal"))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if _str(node.left) == "pre-push" or _str(node.right) == "pre-push":
                hits.append((node.lineno, "joins a pre-push hook path"))
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name in _WRITE_CALLS and any(_contains_pre_push(a) for a in node.args):
                hits.append((node.lineno, "writes a pre-push hook"))
            if name == "HookDisposition":
                kw = {k.arg: _str(k.value) for k in node.keywords}
                if kw.get("hook_name") == "pre-push" and kw.get("action") == "replace":
                    hits.append((node.lineno, "replace-disposition for pre-push"))
    for stmt in tree.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
            if _str(stmt.value) == "pre-push":
                hits.append((stmt.lineno, "pre-push bound to a module constant"))
    return hits


def _sources():
    for d in _SCAN_DIRS:
        for p in (_ROOT / d).rglob("*.py"):
            parts = p.relative_to(_ROOT).parts
            if "tests" in parts or "__pycache__" in parts or p.name.startswith("test_"):
                continue
            yield p


def test_no_engine_source_writes_pre_push_or_runs_lfs_install():
    found = []
    for p in _sources():
        raw = p.read_bytes()
        if b"lfs" not in raw and b"pre-push" not in raw:
            continue
        for line, fact in find_hits(raw.decode("utf-8", "replace")):
            found.append(f"{p.relative_to(_ROOT).as_posix()}:{line}: {fact}")
    assert not found, "\n".join(found)


def test_predicate_a_fires():
    assert find_hits('import subprocess\nsubprocess.run(["git", "lfs", "install"])\n')


def test_predicate_b_fires():
    assert find_hits('p = hooks / "pre-push"\n')
    assert find_hits('open(os.path.join(d, "pre-push"), "w")\n')
    assert find_hits('HOOK = "pre-push"\n')


def test_docstring_naming_the_removal_does_not_fire():
    assert not find_hits('"""git lfs install was removed."""\n')
    assert not find_hits('def f():\n    """Never writes .git/hooks/pre-push."""\n')


def test_string_literal_forms_fire():
    assert find_hits('p = ".git/hooks/pre-push"\n')
    assert find_hits('os.system("git lfs install")\n')


def test_predicate_c_fires():
    src = 'HookDisposition(hook_name="pre-push", action="replace")\n'
    assert find_hits(src)


def test_retire_entry_shape_passes():
    src = 'HookDisposition(hook_name="pre-push", action="remove")\n'
    assert not find_hits(src)

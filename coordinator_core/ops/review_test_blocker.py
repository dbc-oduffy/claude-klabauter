"""The failing-test blocker: one failing node id plus its message, as a halt's gap line.

Zero spawns; the only I/O is an mtime-cached read of a failing node's module to detect
designed_red. The JS fragment in `ask_compose` mirrors `from_reverify`,
`own_diff_blockers` and `gap_line` (the halt is built in the emitted script); the constants are
what Python imports. `TestBlocker`/`from_reverify`/`own_diff_blockers` are the reference the JS
must match.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, FrozenSet, List, Mapping, Optional, Tuple

TEST_BLOCKER = "failing_test"
FAILING_DETAIL_FIELD = "failing_detail"
OWN_DIFF_PATHS_FIELD = "review_declared_paths"
_MAX_MESSAGE = 300


@dataclass(frozen=True)
class TestBlocker:
    __test__ = False

    node_id: str
    message: str = ""

    def gap_line(self) -> str:
        """`<node_id>: <first line of message, at most 300 chars>`."""
        lines = self.message.strip().splitlines()
        return f"{self.node_id}: {lines[0].strip()[:_MAX_MESSAGE] if lines else ''}"


def _detail(result: Mapping[str, Any]) -> dict:
    """`failing_detail` (a list of {node_id, message}) as {node_id: message}."""
    raw = result.get(FAILING_DETAIL_FIELD)
    out: dict = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, Mapping) and item.get("node_id"):
                msg = item.get("message")
                out[str(item["node_id"])] = "" if msg is None else str(msg)
    return out


def _is_designed_red_expr(node: ast.AST) -> bool:
    """True for `pytest.mark.designed_red` / `mark.designed_red`, bare or called, or a list/tuple holding one."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return any(_is_designed_red_expr(e) for e in node.elts)
    if isinstance(node, ast.Call):
        node = node.func
    return isinstance(node, ast.Attribute) and node.attr == "designed_red"


@lru_cache(maxsize=256)
def _marked_scopes(path: str, mtime_ns: int) -> Tuple[bool, FrozenSet[str]]:
    """(module-level mark, names of functions/classes decorated designed_red) for one file.

    Keyed on mtime so an edited module is re-read; an unreadable or unparsable file is unmarked.
    """
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return False, frozenset()
    module_marked = False
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and node in tree.body:
            if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
                module_marked = module_marked or _is_designed_red_expr(node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if any(_is_designed_red_expr(d) for d in node.decorator_list):
                names.add(node.name)
    return module_marked, frozenset(names)


def is_designed_red(node_id: str, root: Optional[str] = None) -> bool:
    """True when the node's module sets `pytestmark` to designed_red, or the node's function or an
    enclosing class carries the decorator. The file part resolves against `root` (default cwd).
    """
    file_part, *scopes = node_id.split("::")
    path = Path(root or ".") / _norm_path(file_part)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return False
    module_marked, names = _marked_scopes(str(path), mtime)
    if module_marked:
        return True
    return any(s.split("[", 1)[0] in names for s in scopes)


def from_reverify(result: Mapping[str, Any], root: Optional[str] = None) -> List[TestBlocker]:
    """Blockers for `failing` minus `pre_existing`, `sibling_owned`, `foreign_red` (a {node: paths}
    map; its keys), `kept` and designed_red nodes, sorted by node id.

    Messages come from `failing_detail`; a node without detail has an empty message. A designed_red
    node is a worklist entry, never a gate (docs/reference/test-tiers.md); `root` locates its module.
    """
    ids = result.get("failing") or []
    if isinstance(ids, str):
        ids = [ids]
    excluded = (
        set(result.get("pre_existing") or [])
        | set(result.get("sibling_owned") or [])
        | set(result.get("foreign_red") or [])
        | set(result.get("kept") or [])
    )
    detail = _detail(result)
    return [
        TestBlocker(n, detail.get(n, ""))
        for n in sorted({str(i) for i in ids} - excluded)
        if not is_designed_red(n, root)
    ]


def _norm_path(p: str) -> str:
    p = str(p).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def own_diff_blockers(
    result: Mapping[str, Any], own_paths, root: Optional[str] = None
) -> List[TestBlocker]:
    """`from_reverify` blockers whose node id's file part is in `own_paths`.

    The file part is the text before the first `::`; both sides are normalised to `/`
    separators with a leading `./` stripped.
    """
    own = {_norm_path(p) for p in own_paths or []}
    return [b for b in from_reverify(result, root) if _norm_path(b.node_id.split("::", 1)[0]) in own]

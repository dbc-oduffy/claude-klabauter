"""Select the test modules that name an edited path as a string literal.

A structural tripwire selects its subject by reading it (a literal such as
`"run_pass.py"` in an expected-files list), so import-based and file-path
based test mapping both miss it. `literal_namers` finds such modules from the
bytes of the target repo's own tracked test modules.

Negative spec: zero spawns, no git verb (the index is parsed in process), no
writes, no ast -- one compiled bytes regex per file, then a suffix test on
each captured literal.

Match rule: a module names edited path `p` iff it holds a quoted literal
`lit` that, after `\\` -> `/` and a leading `./` stripped, equals `p` or is a
trailing component-suffix of it, and `lit` is not a bare generic basename
(`__init__.py`, `__main__.py`, `conftest.py`).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Collection, Dict, List, Sequence

from coordinator_core.git import git_index

#: Read bound is the index size plus this slack: one read call, no fstat. A
#: worktree file grown past it since staging is truncated.
_GROWTH_SLACK = 65536
_GENERIC_BASENAMES  = frozenset({"__init__.py", "__main__.py", "conftest.py"})


def _norm(text: str) -> str:
    text = re.sub(r"[\\/]+", "/", text)
    return text[2:] if text.startswith("./") else text


def _is_test_module(path: str) -> bool:
    if not path.endswith(".py"):
        return False
    base = path.rpartition("/")[2]
    return base.startswith("test_") or base.endswith("_test.py")


def _compile(named: Sequence[str]):
    bases = sorted({_norm(p).rsplit("/", 1)[-1] for p in named if p}, key=len, reverse=True)
    if not bases:
        return None
    needles = tuple(b.encode("utf-8") for b in bases)
    alt = b"|".join(re.escape(n) for n in needles)
    return needles, re.compile(rb"""["']([^"'\r\n]*?(?:""" + alt + rb"""))["']""")


def _match(data: bytes, normed: Sequence[str], rx) -> List[str]:
    if rx is None:
        return []
    needles, literal = rx
    if not any(n in data for n in needles):
        return []
    hits = set()
    for m in literal.finditer(data):
        lit = _norm(m.group(1).decode("utf-8", "replace"))
        if "/" not in lit and lit in _GENERIC_BASENAMES:
            continue
        for orig, p in normed:
            if p == lit or p.endswith("/" + lit):
                hits.add(orig)
    return sorted(hits)


def names_in(data: bytes, named: Sequence[str]) -> List[str]:
    """Sorted members of `named` that a string literal in `data` names."""
    normed = [(p, _norm(p)) for p in named if p]
    return _match(data, normed, _compile(named))


def literal_namers(
    repo_root: Path, named: Sequence[str], *, exclude: Collection[str] = ()
) -> Dict[str, List[str]]:
    """Map each tracked test module to the sorted `named` paths it names.

    Candidates are index paths matching `test_*.py` / `*_test.py`, minus
    `exclude`. An unreadable or absent index yields `{}`.
    """
    normed = [(p, _norm(p)) for p in named if p]
    rx = _compile(named)
    if rx is None:
        return {}
    try:
        index = git_index.parse_index_identity(repo_root)
    except (git_index.IndexParseError, OSError, ValueError):
        return {}
    skip = {_norm(e) for e in exclude}
    root = Path(repo_root)
    out: Dict[str, List[str]] = {}
    base = str(root) + "/"
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    for path, entry in index.items():
        if not _is_test_module(path) or path in skip:
            continue
        try:
            fd = os.open(base + path, flags)
            try:
                data = os.read(fd, entry.size + _GROWTH_SLACK)
            finally:
                os.close(fd)
        except OSError:
            continue
        hits = _match(data, normed, rx)
        if hits:
            out[path] = hits
    return out

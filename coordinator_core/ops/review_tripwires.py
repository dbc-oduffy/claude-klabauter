"""Select the test modules that name an edited path as a string literal.

A structural tripwire selects its subject by reading it (a literal such as
`"run_pass.py"` in an expected-files list), so import-based and file-path
based test mapping both miss it. `literal_namers` finds such modules by
scanning the bytes of the target repo's test modules.

Negative spec: zero spawns, no git verb, no index read, no ast, no hashing, no
writes. One pass, no state, so a cold call costs the same as a warm one.

Trap: walk roots are `pyproject.toml` `[tool.pytest.ini_options] testpaths`;
a repo declaring none (or configuring them elsewhere) is walked from its root,
bounded only by that tree's size. Bytes past 4MiB of a module are not scanned.

Match rule: a module names edited path `p` iff it holds a quoted literal
`lit` that, after `\\` -> `/` and a leading `./` stripped, equals `p` or is a
trailing component-suffix of it, and `lit` is not a bare generic basename
(`__init__.py`, `__main__.py`, `conftest.py`).
"""

from __future__ import annotations

import fnmatch
import os
import re
import tomllib
from pathlib import Path
from typing import Collection, Dict, List, Sequence, Tuple

_GENERIC_BASENAMES = frozenset({"__init__.py", "__main__.py", "conftest.py"})
_ALWAYS_SKIPPED_DIRS = frozenset({"__pycache__", "node_modules"})
_CHUNK = 65536
_READ_CAP = 4 * 1024 * 1024
_MAX_LITERAL = 260
_QUOTES = (b'"', b"'")


def _norm(text: str) -> str:
    text = re.sub(r"[\\/]+", "/", text)
    return text[2:] if text.startswith("./") else text


def _is_test_name(name: str) -> bool:
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _literal_around(data: bytes, hit: int, width: int) -> str | None:
    """The same-line quoted literal holding the bytes at `hit`, or None."""
    line_start = data.rfind(b"\n", 0, hit) + 1
    open_at = max(data.rfind(q, line_start, hit) for q in _QUOTES)
    if open_at < 0:
        return None
    line_end = data.find(b"\n", hit + width)
    if line_end < 0:
        line_end = len(data)
    close_at = data.find(data[open_at : open_at + 1], hit + width, line_end)
    if close_at < 0 or close_at - open_at - 1 > _MAX_LITERAL:
        return None
    return data[open_at + 1 : close_at].decode("utf-8", "replace")


def _names(data: bytes, by_base: Dict[bytes, List[Tuple[str, str]]]) -> List[str]:
    hits = set()
    for base, paths in by_base.items():
        at = data.find(base)
        while at >= 0:
            raw = _literal_around(data, at, len(base))
            if raw is not None:
                lit = _norm(raw)
                if lit and not ("/" not in lit and lit in _GENERIC_BASENAMES):
                    for orig, p in paths:
                        if p == lit or p.endswith("/" + lit):
                            hits.add(orig)
            at = data.find(base, at + 1)
    return sorted(hits)


def _by_basename(named: Sequence[str]) -> Dict[bytes, List[Tuple[str, str]]]:
    by_base: Dict[bytes, List[Tuple[str, str]]] = {}
    for orig in named:
        if not orig:
            continue
        p = _norm(orig)
        by_base.setdefault(p.rsplit("/", 1)[-1].encode("utf-8"), []).append((orig, p))
    return by_base


def names_in(data: bytes, named: Sequence[str]) -> List[str]:
    """Sorted members of `named` that a string literal in `data` names."""
    return _names(data, _by_basename(named))


def _pytest_config(root: Path) -> Tuple[List[str], List[str]]:
    try:
        with open(root / "pyproject.toml", "rb") as fh:
            ini = tomllib.load(fh).get("tool", {}).get("pytest", {}).get("ini_options", {})
    except (OSError, ValueError, AttributeError):
        return [], []

    def strs(value) -> List[str]:
        if isinstance(value, str):
            value = value.split()
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    return strs(ini.get("testpaths")), strs(ini.get("norecursedirs"))


def _walk_roots(testpaths: List[str]) -> List[str]:
    rels = sorted({_norm(t).strip("/") for t in testpaths if t.strip()}) or [""]
    if "" in rels:
        return [""]
    return [r for r in rels if not any(r.startswith(o + "/") for o in rels if o != r)]


def _read(path: str, flags: int) -> bytes | None:
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        data = os.read(fd, _CHUNK)
        if len(data) == _CHUNK:
            parts = [data]
            total = len(data)
            while total < _READ_CAP:
                more = os.read(fd, _CHUNK)
                if not more:
                    break
                parts.append(more)
                total += len(more)
                if len(more) < _CHUNK:
                    break
            data = b"".join(parts)
        return data
    except OSError:
        return None
    finally:
        os.close(fd)


def literal_namers(
    repo_root: Path, named: Sequence[str], *, exclude: Collection[str] = ()
) -> Dict[str, List[str]]:
    """Map each test module to the sorted `named` paths its bytes name.

    Candidates are `test_*.py` / `*_test.py` files under the repo's pytest
    testpaths, minus `exclude`. An unreadable root or module is skipped.
    """
    by_base = _by_basename(named)
    if not by_base:
        return {}
    root = str(repo_root).replace("\\", "/").rstrip("/")
    testpaths, norecurse = _pytest_config(Path(repo_root))
    skip = {_norm(e) for e in exclude}
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    out: Dict[str, List[str]] = {}
    stack = [(f"{root}/{r}" if r else root, r) for r in _walk_roots(testpaths)]
    while stack:
        abs_dir, rel_dir = stack.pop()
        try:
            with os.scandir(abs_dir) as it:
                entries = list(it)
        except OSError:
            continue
        for de in entries:
            name = de.name
            try:
                if de.is_dir(follow_symlinks=False):
                    if (
                        name.startswith(".")
                        or name in _ALWAYS_SKIPPED_DIRS
                        or any(fnmatch.fnmatch(name, pat) for pat in norecurse)
                    ):
                        continue
                    stack.append(
                        (f"{abs_dir}/{name}", f"{rel_dir}/{name}" if rel_dir else name)
                    )
                    continue
                if not _is_test_name(name) or not de.is_file():
                    continue
            except OSError:
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if rel in skip:
                continue
            data = _read(f"{abs_dir}/{name}", flags)
            if data is None:
                continue
            hits = _names(data, by_base)
            if hits:
                out[rel] = hits
    return out

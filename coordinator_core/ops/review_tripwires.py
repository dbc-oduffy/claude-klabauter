"""Select the test modules that name an edited path as a string literal.

A structural tripwire selects its subject by reading it (a literal such as
`"run_pass.py"` in an expected-files list), so import-based and file-path
based test mapping both miss it. `literal_namers` finds such modules by
scanning the bytes of the target repo's tracked test modules.

Negative spec: zero spawns, no git verb, no directory walk, no ast, no hashing,
no writes. One in-process index parse, no state, so a cold call costs the same
as a warm one.

Trap: candidates are the git index's tracked paths filtered by
`pyproject.toml` `[tool.pytest.ini_options] testpaths` (none declared: the
whole tree); the bound is the tracked set, never the disk. An untracked module
is never opened. An unreadable index raises `TripwireScanUnavailable`; an
absent one (unborn repo) is the empty set. Bytes past 4MiB of a module are not
scanned.

Match rule: a module names edited path `p` iff it holds a quoted literal
`lit` that, after `\\` -> `/` and a leading `./` stripped, equals `p` or is a
trailing component-suffix of it, and `lit` is not a bare generic basename
(`__init__.py`, `__main__.py`, `conftest.py`).
"""

from __future__ import annotations

import fnmatch
import functools
import os
import re
import tomllib
from pathlib import Path
from typing import Collection, Dict, List, Sequence, Tuple

from coordinator_core.git import git_state


class TripwireScanUnavailable(Exception):
    """The tracked-file index could not be read, so no tripwire set can be claimed complete."""


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


_SEPARATORS = b"\"'\\/\n"
_SEP_TABLE = bytes.maketrans(_SEPARATORS, b"/" * len(_SEPARATORS))


class _Bases(Dict[bytes, List[Tuple[str, str]]]):
    """`basename -> [(orig, normalised)]` plus, when every basename is separator-free, the
    probe `_names` uses to visit only the quote-closed occurrences of the basenames."""

    needles: Tuple[Tuple[bytes, int], ...] = ()
    max_len = 0

    def arm(self) -> "_Bases":
        """Needles are each basename's own extension plus a quote, so a named `.md` or `.json`
        beside the `.py` files does not disable the probe."""
        keys = list(self)
        if not keys or any(not k or b"." not in k or any(c in k for c in _SEPARATORS) for k in keys):
            return self
        exts = sorted({k[k.rfind(b".") :] for k in keys})
        self.needles = tuple((e + q, len(e)) for e in exts for q in _QUOTES)
        self.max_len = max(len(k) for k in keys)
        return self


def _names(data: bytes, by_base: Dict[bytes, List[Tuple[str, str]]]) -> List[str]:
    """A match needs a literal whose final component is the basename, so the only
    occurrences that can match are those closed by a quote right after the basename."""
    needles = getattr(by_base, "needles", ())
    if needles:
        window = by_base.max_len + 1
        spots = []
        for needle, sl in needles:
            at = data.find(needle)
            while at >= 0:
                end = at + sl
                token = data[max(0, end - window) : end].translate(_SEP_TABLE).rpartition(b"/")[2]
                if token in by_base:
                    spots.append((end - len(token), token))
                at = data.find(needle, at + 1)
    else:
        spots = []
        for base in by_base:
            at = data.find(base)
            while at >= 0:
                spots.append((at, base))
                at = data.find(base, at + 1)
    hits = set()
    for at, base in spots:
        raw = _literal_around(data, at, len(base))
        if raw is not None:
            lit = _norm(raw)
            if lit and not ("/" not in lit and lit in _GENERIC_BASENAMES):
                for orig, p in by_base[base]:
                    if p == lit or p.endswith("/" + lit):
                        hits.add(orig)
    return sorted(hits)


def _by_basename(named: Sequence[str]) -> Dict[bytes, List[Tuple[str, str]]]:
    by_base = _Bases()
    for orig in named:
        if not orig:
            continue
        p = _norm(orig)
        by_base.setdefault(p.rsplit("/", 1)[-1].encode("utf-8"), []).append((orig, p))
    return by_base.arm()


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


def _tracked_test_bytes(repo_root: Path, exclude: Collection[str]):
    """Yield (rel, bytes) for each readable candidate test module, sorted by path."""
    root = str(repo_root).replace("\\", "/").rstrip("/")
    testpaths, norecurse = _pytest_config(Path(repo_root))
    skip = {_norm(e) for e in exclude}
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    try:
        tracked = git_state.read_index_paths(repo_root)
    except (git_state.IndexParseError, OSError, ValueError) as exc:
        raise TripwireScanUnavailable(str(exc)) from exc
    roots = _walk_roots(testpaths)
    dir_ok: Dict[str, bool] = {}

    def dir_admitted(parent: str) -> bool:
        ok = dir_ok.get(parent)
        if ok is None:
            ok = dir_ok[parent] = any(not r or parent == r or parent.startswith(r + "/") for r in roots) and not any(
                d.startswith(".")
                or d in _ALWAYS_SKIPPED_DIRS
                or any(fnmatch.fnmatch(d, pat) for pat in norecurse)
                for d in (parent.split("/") if parent else ())
            )
        return ok

    for rel in sorted(tracked):
        parent, _, name = rel.rpartition("/")
        if not _is_test_name(name) or rel in skip or not dir_admitted(parent):
            continue
        data = _read(f"{root}/{rel}", flags)
        if data is not None:
            yield rel, data


def literal_namers(
    repo_root: Path, named: Sequence[str], *, exclude: Collection[str] = ()
) -> Dict[str, List[str]]:
    """Map each test module to the sorted `named` paths its bytes name.

    Candidates are tracked `test_*.py` / `*_test.py` files under the repo's
    pytest testpaths, minus `exclude`. An unreadable module is skipped.
    Raises `TripwireScanUnavailable` when the index cannot be read.
    """
    by_base = _by_basename(named)
    if not by_base:
        return {}
    out: Dict[str, List[str]] = {}
    for rel, data in _tracked_test_bytes(repo_root, exclude):
        hits = _names(data, by_base)
        if hits:
            out[rel] = hits
    return out


_WALK_PRIMITIVES = (b"rglob(", b"os.walk(", b".glob(", b"iterdir(", b"ls-files")
_LITERAL_RE = re.compile(rb"""(["'])([^"'\n]{1,%d})\1""" % _MAX_LITERAL)


@functools.lru_cache(maxsize=8)
def _walk_probe(created: Tuple[str, ...]):
    """One regex per own directory of `created`, matching that directory's components
    joined by separators anywhere in a module; a literal equal to the directory always
    matches it, so a module matching none holds no such literal. None: not probe-able."""
    dirs = {_norm(c).rpartition("/")[0] for c in created if c} - {"", "."}
    if not dirs or not all(d.isascii() for d in dirs):
        return None
    return tuple(
        re.compile(rb"[\\/]+".join(re.escape(part.encode()) for part in d.split("/"))) for d in sorted(dirs)
    )


def walked_created(data: bytes, created: Sequence[str]) -> List[str]:
    """The `created` paths a test module's bytes walk: the bytes hold a walk primitive
    (`rglob(`, `os.walk(`, `.glob(`, `iterdir(`, `ls-files`) and a quoted literal that,
    normalised, equals the path's own directory. Shallower ancestors never count: a
    repo-root literal such as the package name would select nearly every walker.
    "", "." never count."""
    probe = _walk_probe(tuple(created))
    if probe is not None and not any(rx.search(data) for rx in probe):
        return []
    if not any(w in data for w in _WALK_PRIMITIVES):
        return []
    lits = {
        _norm(m.group(2).decode("utf-8", "replace")).rstrip("/") for m in _LITERAL_RE.finditer(data)
    } - {"", "."}
    return [c for c in created if c and _norm(c).rpartition("/")[0] in lits]


def tree_scanners(
    repo_root: Path, created: Sequence[str], *, exclude: Collection[str] = ()
) -> List[str]:
    """Sorted tracked test modules that walk the own directory of a created path
    (`walked_created`). Same candidate set and failure mode as `literal_namers`."""
    paths = [c for c in created if c]
    if not paths:
        return []
    return sorted(rel for rel, data in _tracked_test_bytes(repo_root, exclude) if walked_created(data, paths))


def scan_tracked(
    repo_root: Path, named: Sequence[str], created: Sequence[str], *, exclude: Collection[str] = ()
) -> List[str]:
    """Sorted union of `literal_namers(named)` and `tree_scanners(created)` from ONE read
    of each candidate module. Same candidate set and failure mode as either alone."""
    by_base = _by_basename(named)
    paths = [c for c in created if c]
    found = set()
    if by_base or paths:
        for rel, data in _tracked_test_bytes(repo_root, exclude):
            if (by_base and _names(data, by_base)) or (paths and walked_created(data, paths)):
                found.add(rel)
    return sorted(found)

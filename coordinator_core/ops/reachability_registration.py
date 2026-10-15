"""coordinator_core.ops.reachability_registration — hook, bin script and op registration resolver.

Entries: a public file directly under `hooks/` or `bin/` (any extension), and a `.py` file adding
`@register_op("<name>")`. Verdicts read only the diff's `added` map, the shared `read_index` list,
ancestor index files and named wiring surfaces, never the whole tree and never a spawn.

Negative-spec:
  - A test file is never a referrer, and neither is a same-stem sibling of a bin script (a shim).
  - A hook is `undecidable` when the repo holds no hook wiring surface: the wiring lives elsewhere.
  - An op is `undecidable` when the repo holds no op registry surface, for the same reason.
  - A private stem (`_x`, `__init__`) is `undecidable`: it is infrastructure, not an entry point.
  - `conftest.py` is never a hook or bin entry.
  - A miss is `unreachable` only for a file the range created (`new`); a pre-existing hook or bin
    script that misses stays `undecidable`, because its wiring may live in another repo.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from coordinator_core.ops.reachability_docs import _INDEX_NAMES

_OP_DECL = re.compile(r"""^[ \t]*@register_op\(\s*['"]([^'"]+)['"]""", re.M)
_HOOK_FILE = re.compile(r"(^|/)hooks/[^/]+$")
_BIN_FILE = re.compile(r"(^|/)bin/[^/]+$")
_SHIM_EXT = (".cmd", ".ps1", ".bat")
_OP_REGISTRY_TAILS = ("ops/__init__.py", "hooks/__init__.py")

# What a kind answers when no wiring surface or added line names a PRE-EXISTING file. The HEAD
# oracle (every tracked member of the kind, `added={}`) sets each: a kind with over 5% false
# `unreachable` is `undecidable`. Oracle at HEAD: hook 27/127 miss (wired from hooks.json in another
# repo), cli 505/522 miss (invoked from skills outside this repo), op 1/433 miss (a research sandbox
# file). A file the range created has no such history, so its miss is trusted as `unreachable`.
_NO_REFERENCE = {"hook": "undecidable", "cli": "undecidable", "op": "unreachable"}


def registration_entries(rel: str, text: str) -> list[tuple[str, str]]:
    """[(kind, symbol)] for kind in ("cli", "hook", "op"); [] when rel is none of them."""
    out: list[tuple[str, str]] = []
    stem = Path(rel).stem
    if os.path.basename(rel) == "conftest.py":
        pass
    elif _HOOK_FILE.search(rel) and not rel.endswith(_SHIM_EXT):
        out.append(("hook", stem))
    elif _BIN_FILE.search(rel) and not rel.endswith(_SHIM_EXT):
        out.append(("cli", stem))
    if rel.endswith(".py"):
        out += [("op", n) for n in dict.fromkeys(_OP_DECL.findall(text))]
    return out


def _read(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _referrer_text(added: dict[str, list[str]], rel: str, shim_siblings: bool) -> str:
    from coordinator_core.ops.review_reachability import _TEST_RE

    own = (Path(rel).parent.as_posix(), Path(rel).stem)
    return "\n".join(
        "\n".join(lines)
        for r, lines in added.items()
        if r != rel
        and not _TEST_RE.search(r)
        and not (shim_siblings and (Path(r).parent.as_posix(), Path(r).stem) == own)
    )


def _ancestor_dirs(rel: str) -> list[str]:
    parts = Path(rel).parent.parts
    return ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]


def _word(token: str, extra: str = "") -> re.Pattern:
    return re.compile(r"(?<![\w" + extra + r"])" + re.escape(token) + r"(?![\w" + extra + r"])")


def registration_verdicts(
    root: Path,
    found: list[tuple[str, str, str]],
    added: dict[str, list[str]],
    index: list[str],
    *,
    new: frozenset[str] = frozenset(),
) -> dict[tuple[str, str, str], str]:
    """{(kind, symbol, rel): verdict}. `new` is the set of paths the range created."""
    from coordinator_core.ops.review_reachability import _MANIFESTS, _TEST_RE

    live = [p for p in index if not _TEST_RE.search(p)]
    hook_surfaces = [p for p in live if p.endswith("hooks/__init__.py") or os.path.basename(p) == "hooks.json"]
    op_surfaces = [p for p in live if p.endswith(_OP_REGISTRY_TAILS) or os.path.basename(p) == "_registry_map.py"]
    cache: dict[str, str] = {}
    files: dict[str, str] = {}

    def text_of(rels: list[str]) -> str:
        for r in rels:
            if r not in files:
                files[r] = _read(root, r)
        return "\n".join(files[r] for r in dict.fromkeys(rels))

    def surface(key: str, rels: list[str]) -> str:
        if key not in cache:
            cache[key] = text_of(rels)
        return cache[key]

    out: dict[tuple[str, str, str], str] = {}
    for kind, sym, rel in found:
        key = (kind, sym, rel)
        if kind not in _NO_REFERENCE:
            out[key] = "undecidable"
            continue
        miss = "unreachable" if rel in new else _NO_REFERENCE[kind]
        if kind in ("hook", "cli"):
            if sym.startswith("_"):
                out[key] = "undecidable"
                continue
            pat = _word(sym, "-")
            refs = _referrer_text(added, rel, shim_siblings=True)
            if kind == "hook":
                local = [f"{d}/{n}".lstrip("/") for d in _ancestor_dirs(rel) for n in ("hooks.json", "hooks/__init__.py")]
                surfaces = list(dict.fromkeys(hook_surfaces + [p for p in local if (root / p).is_file()]))
                if not surfaces:
                    out[key] = "undecidable"
                    continue
                text = text_of(surfaces)
            else:
                indexes = [f"{d}/{n}".lstrip("/") for d in _ancestor_dirs(rel) for n in _INDEX_NAMES]
                text = text_of(list(_MANIFESTS) + indexes)
            out[key] = "reachable" if pat.search(refs) or pat.search(text) else miss
        else:
            dotted = Path(rel).with_suffix("").as_posix().replace("/", ".")
            if not op_surfaces:
                out[key] = "undecidable"
                continue
            by_module = _word(dotted)
            by_name = _word(sym)
            if by_module.search(surface("op", op_surfaces + hook_surfaces)) or by_name.search(
                _referrer_text(added, rel, shim_siblings=False)
            ):
                out[key] = "reachable"
            else:
                out[key] = miss
    return out

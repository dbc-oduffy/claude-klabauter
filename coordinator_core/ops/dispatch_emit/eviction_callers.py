"""Removed definitions and the callers an eviction would strand in the remaining tree.

`removed_definitions` names the top-level definitions a reset drops from a `.py`; `stranded_callers`
finds the tree files that still use them. The candidate set is every planned `.py` plus one
`git grep -l -w -F` over the removed names, never an import graph; an AST read confirms each. A
caller that cannot be judged returns `UNJUDGEABLE`, so the eviction over-holds and never under-holds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

UNJUDGEABLE = "<unjudgeable>"


def _norm(path: str) -> str:
    return str(path).replace("\\", "/")


def removed_definitions(before: bytes, after: Optional[bytes]) -> set[str]:
    """Top-level names defined in `before` and absent from `after` (None: the file is gone, so all
    of them). An `after` that does not parse defines nothing, so every name counts as removed."""
    from coordinator_core.git.checkpoint_hunks import _definitions

    names, _params = _definitions(before.decode("utf-8", "replace"))
    if after is None:
        return names
    kept, _params = _definitions(after.decode("utf-8", "replace"))
    return names - kept


def _module_aliases(producer: str, caller: str) -> list[str]:
    """Dotted module names under which `caller` may import `producer`: the repo-root path and each
    suffix of two or more components (a sub-tree on `sys.path`), plus the bare leaf for a caller
    in the producer's own directory."""
    parts = producer[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return []
    aliases = [".".join(parts[i:]) for i in range(len(parts) - 1)]
    if len(parts) == 1 or caller.rpartition("/")[0] == "/".join(parts[:-1]):
        aliases.append(parts[-1])
    return aliases


def _grep_candidates(root: Path, names: set[str]) -> set[str]:
    """Tracked `.py` paths whose worktree text holds any of `names` as a whole word: ONE git
    process, patterns on stdin so the argv stays small."""
    from coordinator_core.git.run import run_git

    res = run_git(
        ["-C", str(root), "grep", "-l", "-z", "-w", "-F", "-f", "-", "--", "*.py"],
        input="\n".join(sorted(names)).encode("utf-8"),
    )
    if res.returncode not in (0, 1):  # 1: no match
        raise RuntimeError(f"git grep exit {res.returncode}: {res.stderr.strip()[:200]}")
    return {_norm(p) for p in res.stdout.split("\0") if p}


def _judge(root: Path, work: dict[str, set[str]], after: dict[str, Optional[bytes]]) -> dict[str, list[tuple[str, str]]]:
    from coordinator_core.git.checkpoint_hunks import uses_new_definition

    names = set().union(*work.values())
    candidates = {p for p in after if p.endswith(".py")} | _grep_candidates(root, names)
    out: dict[str, list[tuple[str, str]]] = {}
    for caller in sorted(candidates):
        if caller in after:
            data = after[caller]
        else:
            try:
                data = (root / caller).read_bytes()
            except OSError:
                data = None
        if data is None:
            continue
        text = data.decode("utf-8", "replace")
        package = caller.rpartition("/")[0].replace("/", ".") or None
        for producer, removed in work.items():
            leaf = producer[:-3].rpartition("/")[2]
            if leaf == "__init__":
                leaf = producer[:-3].rpartition("/")[0].rpartition("/")[2]
            if caller == producer or leaf not in text or not any(n in text for n in removed):
                continue
            for module in _module_aliases(producer, caller):
                why = uses_new_definition(text, module, removed, {}, package, star=True)
                if why:
                    out.setdefault(producer, []).append((caller, why))
                    break
    return out


def stranded_callers(
    root: Path, removed: dict[str, set[str]], after: dict[str, Optional[bytes]],
) -> dict[str, list[tuple[str, str]]]:
    """`{producer: [(caller path, why)]}` for each producer in `removed` (`{path: removed names}`)
    whose removed name a `.py` still uses after the eviction.

    `after` maps every path the eviction plans to its post-eviction bytes (None: the path is
    removed). A planned caller is judged on those bytes, so a caller whose reset still names a
    removed symbol is returned like any other; every other caller is judged on its tree bytes, clean
    committed or dirty. A path outside the Python tree and a producer with no removed names are
    skipped. Any failure returns `{producer: [(UNJUDGEABLE, reason)]}` for every producer, so the
    eviction refuses. One git process at most, none when no names are removed. Never raises."""
    work = {_norm(p): set(n) for p, n in removed.items() if n and _norm(p).endswith(".py")}
    if not work:
        return {}
    try:
        return _judge(Path(root), work, {_norm(k): v for k, v in after.items()})
    except Exception as exc:  # noqa: BLE001 -- unjudgeable: the eviction refuses
        return {p: [(UNJUDGEABLE, f"{type(exc).__name__}: {exc}"[:200])] for p in work}

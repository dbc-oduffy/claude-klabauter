"""Always-run guards: the repo-declared guard list and the per-file hit comparison behind it.

A repo lists repo-wide guard test nodes under ``always_run_guards`` in coordinator.local.md. The
land gate adds them to every scoped selection and judges a guard that is red at HEAD by whether the
tree names a touched file more often than HEAD does, never by the guard's verdict.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping, Sequence

from coordinator_core.ops.dispatch_emit.invariant_measured_paths import existing_invariants

GUARDS_KEY = "always_run_guards"
GUARD_HITS_KEY = "guard_hits"  # retest record: {node: {"tree": {path: n}, "head": {path: n}}}


def declared_guards(root: Path) -> list[str]:
    """Guard node ids declared at ``root``, files that exist only. Absent file or key -> []."""
    from coordinator_core.ops.doc_content_verify import _read_local_md_list

    return existing_invariants(Path(root), _read_local_md_list(Path(root), GUARDS_KEY))


_KEY_LINE = re.compile(r"^([A-Za-z_][\w\-]*)\s*:")
_NEAR_MISS_RATIO = 0.7


def config_problems(root: Path) -> list[str]:
    """One sentence per defect in ``root``'s guard declaration, each naming the bad key or id.

    Defects: a top-level frontmatter key that is a near-miss of ``GUARDS_KEY`` (it selects nothing),
    and a listed guard id whose test file does not exist (``declared_guards`` drops it silently).
    Absent file -> []. Only the guard-list key family is judged; other keys are never inspected.
    """
    from difflib import SequenceMatcher

    from coordinator_core.ops.doc_content_verify import _read_local_md_list
    from coordinator_core.resolve_validation_cmd import _extract_frontmatter

    root = Path(root)
    local_md = root / "coordinator.local.md"
    if not local_md.is_file():
        return []
    problems: list[str] = []
    seen: set[str] = set()
    for line in _extract_frontmatter(local_md).split("\n"):
        match = _KEY_LINE.match(line)
        key = match.group(1) if match else ""
        if not key or key == GUARDS_KEY or key in seen:
            continue
        seen.add(key)
        if SequenceMatcher(None, key.lower(), GUARDS_KEY).ratio() >= _NEAR_MISS_RATIO:
            problems.append(
                f"coordinator.local.md key '{key}' is not '{GUARDS_KEY}'; it selects no guard, rename it"
            )
    listed = _read_local_md_list(root, GUARDS_KEY)
    resolved = set(existing_invariants(root, listed))
    for node in listed:
        if node not in resolved:
            problems.append(
                f"{GUARDS_KEY} id '{node}' names no test file at {node.split('::')[0]}; it runs nothing, "
                "fix or remove it"
            )
    return problems


def touched_hits(text: str, touched: Sequence[str]) -> dict[str, int]:
    """Per touched repo-relative path, how many times ``text`` names it (backslashes read as ``/``).

    A path counts only on a path boundary: ``a/b.py`` is not found in ``a/b.pyc``, ``a/b.py/x`` or
    ``xa/b.py``. A leading directory (``/repo/a/b.py``) still counts.
    """
    body = text.replace("\\", "/")
    hits: dict[str, int] = {}
    for path in touched:
        norm = path.replace("\\", "/")
        pattern = r"(?<![\w.\-])" + re.escape(norm) + r"(?![\w\-/]|\.\w)"
        hits[norm] = len(re.findall(pattern, body))
    return hits


def new_hits(head: Mapping[str, int], tree: Mapping[str, int]) -> list[str]:
    """Touched paths whose tree count exceeds the HEAD count (missing HEAD count is 0).

    Trap: pytest truncates long assertion text, so counts are a lower bound, and a guard whose
    message truncates identically on both sides can mask a new hit. A guard must print one line
    per hit to stdout, untruncated.
    """
    return [p for p, n in tree.items() if n > head.get(p, 0)]

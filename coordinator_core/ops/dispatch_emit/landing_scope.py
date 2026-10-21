"""Pure allow-list of the paths a run's terminal commit may land.

Stdlib only and no IO: terminal_commit has a cold-import pin.
"""
from __future__ import annotations

from typing import Iterable, Sequence

FOREIGN_EXCLUDED_KEY = "foreign_excluded"


def _norm(path: object) -> str:
    text = str(path or "").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def own_landing_set(
    admitted: Sequence[str],
    *,
    declared: Iterable[str],
    declared_prefixes: Iterable[str],
    own_artifacts: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Split ``admitted`` into ``(kept, foreign)``.

    A path is own when it is in ``declared`` or ``own_artifacts``, or sits under a declared
    prefix (segment-bounded: ``src/foo`` does not admit ``src/foobar/x``). ``kept`` preserves
    admitted order; ``foreign`` is sorted and deduplicated. ``own_artifacts`` is the run's own
    record graph, never ``run_touched.touched_paths``, which admits undeclared dirt.
    """
    own = {_norm(p) for p in declared} | {_norm(p) for p in own_artifacts}
    own.discard("")
    unders = []
    for prefix in declared_prefixes:
        base = _norm(prefix)
        if base:
            unders.append((base.rstrip("/"), base if base.endswith("/") else base + "/"))
    kept: list[str] = []
    foreign: set[str] = set()
    for raw in admitted:
        path = _norm(raw)
        if path in own or any(path == base or path.startswith(under) for base, under in unders):
            kept.append(raw)
        else:
            foreign.add(path)
    return kept, sorted(foreign)

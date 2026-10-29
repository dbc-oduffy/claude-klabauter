"""Split a sizing that carries a research block and file writes into a research doc and a build doc.

The research doc keeps the `research` block and drops `writes`; the build doc keeps the writes and drops
`research` (the same result as `_research_sizing.drop_research_block`, applied to a copy). The gate mints
the pair as `ask_fanout` siblings: research `ready`, build `after` the research run.

Negative-spec: pure; reads and writes no file, spawns no process, mutates neither input.
"""

from __future__ import annotations

import copy
from typing import Mapping, Optional, Sequence

_NOTEBOOKLM = "notebooklm"


def split_research_build(
    sizing: Mapping, sizing_rel: str, writes: Sequence[str]
) -> tuple[dict, dict]:
    """Return `(research_sizing_doc, build_sizing_doc)` as deep copies of `sizing`.

    Research doc: `research` kept, `writes` removed. Build doc: `research` removed, `writes` set to the
    non-empty entries of `writes`. `sizing_rel` is only named in the ValueError for a sizing with no
    research block, which has nothing to split."""
    research = sizing.get("research")
    if not (isinstance(research, Mapping) and research):
        raise ValueError(f"sizing {sizing_rel}: no research block to split")
    research_doc = copy.deepcopy(dict(sizing))
    research_doc.pop("writes", None)
    build_doc = copy.deepcopy(dict(sizing))
    build_doc.pop("research", None)
    build_doc["writes"] = [str(w).strip() for w in writes if str(w).strip()]
    return research_doc, build_doc


def notebooklm_targets_or_halt(research: Mapping) -> tuple[list[dict], Optional[str]]:
    """Return `(targets, halt_reason)`; exactly one side is meaningful.

    A research block whose `sources` omit notebooklm, or that already carries notebooklm targets, returns
    its `targets` unchanged with `None`. A notebooklm source with no notebooklm target derives one target
    per `questions` entry (the scout's topic areas, de-duplicated, in order) appended to the existing
    targets. With no questions either, returns `([], reason)` naming the missing targets.

    Trap: only `questions` count as topic areas; a bare `intent` is not split into targets."""
    targets = [dict(t) for t in research.get("targets") or () if isinstance(t, Mapping)]
    if _NOTEBOOKLM not in (research.get("sources") or ()):
        return targets, None
    if any(t.get("source") == _NOTEBOOKLM and t.get("ref") for t in targets):
        return targets, None
    seen: set[str] = set()
    derived = []
    for q in research.get("questions") or ():
        ref = str(q).strip()
        if ref and ref not in seen:
            seen.add(ref)
            derived.append({"source": _NOTEBOOKLM, "ref": ref})
    if derived:
        return targets + derived, None
    return [], (
        "research names a notebooklm source but no notebooklm targets and no topic areas to derive "
        "them from: supply research.targets entries {source: notebooklm, ref: <notebook>}"
    )

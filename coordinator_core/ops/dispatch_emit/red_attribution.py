"""Own-path attribution of a red: does a failing node belong to the run's footprint, or does its failure
text name only paths outside it (a peer's dirt)? Shared by the land_run targeted retest and the review
reverify so the two never diverge. Pure text matching over captured output; no spawn."""

from __future__ import annotations

import re
from typing import Mapping, Sequence


def _posix(paths: Sequence[str]) -> list[str]:
    return [str(p).replace("\\", "/") for p in paths if p]


def _names_any(body: str, own_paths: Sequence[str]) -> bool:
    text = body.replace("\\", "/")
    return any(p in text for p in own_paths)


def names_own_path(log_text: str, nodes: Sequence[str], write_set: Sequence[str]) -> set[str]:
    """The ``nodes`` whose failure text in ``log_text`` (their ``___ name ___`` section, else the
    ``FAILED``/``ERROR`` summary line) names a ``write_set`` path, or that sit in a ``write_set`` test
    file: the run's own red, not a peer's dirt."""
    own_paths = _posix(write_set)
    if not nodes or not own_paths:
        return set()
    text = log_text.replace("\\", "/")
    out: set[str] = set()
    for node in nodes:
        if node.split("::")[0] in own_paths:
            out.add(node)
            continue
        name = re.escape(re.split(r"\[", node.split("::")[-1])[0])
        section = re.search(
            rf"^_{{2,}} (?:\S+\.)?{name}(?:\[.*?\])? _{{2,}}$(.*?)(?=^_{{2,}} .+ _{{2,}}$|^={{3,}} )",
            text, flags=re.MULTILINE | re.DOTALL,
        )
        body = section.group(1) if section else "".join(
            ln for ln in text.splitlines(keepends=True) if ln.startswith(("FAILED ", "ERROR ")) and node in ln
        )
        if any(p in body for p in own_paths):
            out.add(node)
    return out


def names_own_path_in_output(
    output: Mapping[str, str] | None, nodes: Sequence[str], write_set: Sequence[str],
) -> set[str]:
    """As ``names_own_path`` for per-node failure text (``{node id: text}``): a node in a ``write_set``
    test file, or whose own text names a ``write_set`` path, is the run's own red."""
    own_paths = _posix(write_set)
    if not nodes or not own_paths:
        return set()
    return {
        n for n in nodes
        if n.split("::")[0].replace("\\", "/") in own_paths or _names_any(str((output or {}).get(n) or ""), own_paths)
    }

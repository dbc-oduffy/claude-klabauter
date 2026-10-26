"""Importability judge for the tree a halt leaves behind.

In process, zero spawns. Judges the run's own changed `.py` files and their importers inside the
run's write set; the caller evicts what this reports (`halt_tree_evict`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence


def broken_paths(
    root: Path, own_changed: Sequence[str], write_set: Sequence[str]
) -> dict[str, str]:
    """Return `{repo-relative posix path: rendered finding}` for every path left unimportable.

    `own_changed` are the paths the run owns (never a peer's); `write_set` bounds the importers
    examined. The result may name a write-set path the run does not own: the caller reports it and
    never evicts it. An empty dict means the tree is importable.
    """
    raise NotImplementedError("broken_paths: implemented by C1")

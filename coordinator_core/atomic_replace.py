"""
coordinator_core.atomic_replace — the one atomic same-directory-mkstemp +
os.replace primitive, relocated here (verbatim body) from
``coordinator_core.install._shared`` (C1, `docs/plans/
2026-09-11-state-writers-claim-through-one-seam.md`).

Purpose: `coordinator_core.session.claimed_write` (the claiming seam, D1)
needs this primitive, and a seam reached from hook and guard processes must
not pull in `coordinator_core.install`'s module-top imports (`subprocess`,
`coordinator_core.machine_resolver`, `coordinator_core._settings_home`) — see
that module's docstring for why those are too heavy for a hook-process import
path. This leaf module imports stdlib only, sibling to `atomic_append.py`
(the one append primitive), so the two live at the same level and neither
seam has to reach into `install/` for its write mechanics.

`coordinator_core.install._shared` re-exports `atomic_write_bytes` BY NAME
from here, so every existing `from coordinator_core.install._shared import
atomic_write_bytes` caller (`receipt.py`, `substrate.py`,
`ops/fleet/capability_index.py`, the install tests) keeps working unchanged
— this is a relocation, not a behaviour change, for every install caller.

Negative-spec (RAG-bait):
    This module does NOT decide whether a write targets `state/`, does not
    claim anything, and carries no `declare_write` call — that is the seam's
    job (`session/claimed_write.py`), which delegates its `replace_bytes`
    entry point to `atomic_write_bytes` here and adds no copy of the
    mkstemp/os.replace mechanics.

    `atomic_write_bytes` preserves an EXISTING target's mode (`preserve_mode`,
    default True) — POSIX `mkstemp` creates the new tempfile 0600, which
    would otherwise silently narrow the replaced file's permissions. A
    target that does not yet exist is created at `mkstemp`'s mode (0600 on
    POSIX). `os.replace` also replaces a symlink itself rather than writing
    through it, and breaks hardlinks — both are residuals of the underlying
    primitive, not worked around here.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Union

__all__ = ["atomic_write_bytes"]


def atomic_write_bytes(
    target: Union[str, Path], data: bytes, *, preserve_mode: bool = True
) -> None:
    """Byte-level atomic-write primitive — a doctrine PORT of
    :func:`coordinator_core.install.gen_settings_hooks._atomic_write_json`'s
    write mechanics, not a simplified variant (C6, the Staff Engineer F4/D8 "quartet
    reuse — doctrine, not functions"). Same shape: a same-DIRECTORY
    ``tempfile.mkstemp`` (guarantees ``os.replace`` lands on the same
    filesystem, so the replace is atomic), write the full ``data``,
    re-``chmod`` the tempfile to the destination's PRIOR mode (captured via
    ``os.stat`` BEFORE the write — ``os.replace`` does not carry mode bits
    forward from a freshly ``mkstemp``-ed file) when ``preserve_mode`` is
    True and a destination already exists, then ``os.replace(tmp_name,
    target)``. On ANY exception the tempfile is removed and the original
    exception re-raised, leaving ``target`` untouched — no truncated or
    partially-written destination results from an interrupted write.

    Deliberately does NOT port ``_extract_preserved``/``_merge_env``
    (JSON-merge helpers with no analogue for an opaque, non-JSON destination
    — see ``coordinator_core.install.substrate``'s C6 call site for why) or
    ``_group_is_generated``/``_cmd_path`` (intra-file command-path
    predicates with no file-level analogue). This function is pure write
    MECHANICS only — it carries no destination-provenance knowledge, so a
    caller choosing WHEN to invoke it (e.g. a foreign-tracked-overwrite
    classification) stays entirely the caller's decision, never this
    function's."""
    target = Path(target)
    out_dir = target.parent if str(target.parent) else Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)
    prior_mode = None
    if preserve_mode and target.is_file():
        prior_mode = os.stat(target).st_mode
    fd, tmp_name = tempfile.mkstemp(prefix=".atomic-write.", dir=str(out_dir))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if prior_mode is not None:
            os.chmod(tmp_name, prior_mode)
        os.replace(tmp_name, str(target))
    except Exception:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise

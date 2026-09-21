"""coordinator_core.ops.ceremony.commit_admission -- the doctrine-surface
admission check at the one point every engine commit passes through.

The admission predicate (`hooks.claude_md_ledger.admission_check_for_surface`)
governs growth of the `GOVERNED_AUTHORING_SURFACES` -- the always-on boot
payload. Its enforcement points are tool-call guards: PreToolUse on Write/Edit
and on Bash. An engine op that rewrites one of those files in-process makes no
tool call, and every engine commit lands through `_commit_via_head_spine` or
its plumbing ladder, neither of which runs a git hook. So an op-driven change
to the boot payload reached the tree with admission never consulted, and the
surfaces grew past their recorded watermarks by exactly that route.

`governed_surface_refusal` runs the same predicate against the exact blobs a
commit is about to land. `_commit_via_head_spine` calls it before any of its
own preconditions, so a refusal also stops the ladder: the ladder only runs
when the head spine returns `None`, and a refusal is a failing result, never
`None`.

SCOPE: a surface is enforced only where its ledger exists. A repo that has
never adopted a classification ledger for its `CLAUDE.md` has no admission
policy to enforce, and the predicate's no-ledger disposition (all growth
refused) would freeze that file on every commit. That disposition stays with
the Write/Edit guard, which is where it was authored.

FAILS CLOSED once a governed, ledgered path is touched: an unreadable blob, a
malformed ledger, or any exception evaluating it refuses the commit with the
cause named. Shrinking a surface is always admitted by the predicate itself,
and a deletion is a shrink.

Negative-spec:
    Does NOT spawn: HEAD blobs come from `head_blobs` (in-process tree walk)
    and new blobs from `read_object`.
    Does NOT read the working tree for the surface's content -- only the blobs
    being committed. The LEDGER is read from the working tree, as every other
    enforcement point reads it, so a commit that classifies a section and
    grows it can land together.
    Does NOT carry an override key; the remedy is the ledger (classify the
    section, or bump the watermark with a reason), exactly as on Write/Edit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_object
from coordinator_core.git.git_state import head_blobs
from coordinator_core.hooks.claude_md_ledger import (
    GOVERNED_AUTHORING_SURFACES,
    admission_check_for_surface,
    resolve_ledger_path,
)


def _blob_text(common_dir: Path, sha: str) -> Optional[str]:
    obj = read_object(common_dir, sha)
    if obj is None or obj[0] != "blob":
        return None
    return obj[1].decode("utf-8", errors="replace")


def governed_surface_refusal(root: Path, assembled: Mapping[str, object]) -> Optional[str]:
    """`None` when the commit touches no ledgered governed surface or every one
    it touches is admitted; otherwise the refusal text, one line per surface.

    `assembled` is `_commit_via_head_spine`'s own `{path: (mode, sha) |
    _ABSENT}` -- any value that is not a `(mode, sha)` pair is a deletion.
    """
    root = Path(root)
    governed = [
        path
        for path in assembled
        if path in GOVERNED_AUTHORING_SURFACES and resolve_ledger_path(root, path).is_file()
    ]
    if not governed:
        return None

    common_dir = resolve_git_common_dir(root)
    old_blobs = head_blobs(root, governed)
    refusals = []
    for path in governed:
        entry = assembled[path]
        try:
            old_entry = old_blobs.get(path)
            old_text = "" if old_entry is None else _blob_text(common_dir, old_entry[1])
            new_text = (
                _blob_text(common_dir, entry[1])
                if isinstance(entry, tuple) and len(entry) == 2
                else ""
            )
            if old_text is None or new_text is None:
                refusals.append(
                    f"{path}: a blob this commit would land could not be read, so its "
                    "admission could not be checked -- refusing rather than landing an "
                    "unchecked change to a governed doctrine surface."
                )
                continue
            allowed, message = admission_check_for_surface(path, old_text, new_text, root)
        except Exception as exc:  # noqa: BLE001 -- fail closed, see module docstring.
            refusals.append(
                f"{path}: the doctrine-surface admission check could not complete "
                f"({exc.__class__.__name__}: {exc}) -- refusing rather than landing an "
                "unchecked change to a governed doctrine surface."
            )
            continue
        if not allowed:
            refusals.append(f"{path}: {message}")
    return "\n".join(refusals) if refusals else None

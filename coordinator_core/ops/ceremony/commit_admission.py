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

Two further entry points carry the same predicate to the other places the
boot payload changes without a tool call:
  - `governed_write_refusal` is for an op that rewrites a file in-process: it
    asks before writing, so an op never leaves refused text in the tree.
  - `uncommitted_surface_refusals` compares the working tree with HEAD. Text
    that is written but not yet committed is already in the next session's boot
    payload, and no commit has checked it. A SessionStart hook reports it.

Negative-spec:
    Does NOT spawn: HEAD blobs come from `head_blobs` (in-process tree walk)
    and new blobs from `read_object`.
    `governed_surface_refusal` does NOT read the working tree for the
    surface's content -- only the blobs being committed. The LEDGER is read from the working tree, as every other
    enforcement point reads it, so a commit that classifies a section and
    grows it can land together.
    Does NOT carry an override key; the remedy is the ledger (classify the
    section, or bump the watermark with a reason), exactly as on Write/Edit.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Mapping, Optional

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
        except Exception as exc:  # noqa: BLE001 -- fail closed, see module docstring.
            refusals.append(_incomplete(path, exc))
            continue
        if old_text is None or new_text is None:
            refusals.append(
                f"{path}: a blob this commit would land could not be read, so its "
                "admission could not be checked -- refusing rather than landing an "
                "unchecked change to a governed doctrine surface."
            )
            continue
        refusal = governed_write_refusal(root, path, old_text, new_text)
        if refusal is not None:
            refusals.append(refusal)
    return "\n".join(refusals) if refusals else None


def _incomplete(path: str, exc: Exception) -> str:
    return (
        f"{path}: the doctrine-surface admission check could not complete "
        f"({exc.__class__.__name__}: {exc}) -- refusing rather than landing an "
        "unchecked change to a governed doctrine surface."
    )


def governed_write_refusal(root: Path, path: str, old_text: str, new_text: str) -> Optional[str]:
    """`None` when replacing `old_text` with `new_text` at repo-relative `path` is
    admitted, or when `path` is not a ledgered governed surface; otherwise the
    refusal text. Fails closed."""
    root = Path(root)
    if path not in GOVERNED_AUTHORING_SURFACES or not resolve_ledger_path(root, path).is_file():
        return None
    try:
        allowed, message = admission_check_for_surface(path, old_text, new_text, root)
    except Exception as exc:  # noqa: BLE001 -- fail closed, see module docstring.
        return _incomplete(path, exc)
    return None if allowed else f"{path}: {message}"


def uncommitted_surface_refusals(root: Path) -> List[str]:
    """One refusal line per ledgered governed surface whose working-tree text
    differs from HEAD in a way admission refuses. A surface with no HEAD blob
    counts as growth from empty; a surface missing from the working tree is a
    deletion, which is a shrink, and is always admitted."""
    root = Path(root)
    governed = [p for p in GOVERNED_AUTHORING_SURFACES if resolve_ledger_path(root, p).is_file()]
    if not governed:
        return []
    common_dir = resolve_git_common_dir(root)
    old_blobs = head_blobs(root, governed)
    refusals: List[str] = []
    for path in governed:
        live = root / path
        if not live.is_file():
            continue
        try:
            # `.replace("\r\n", "\n")` -- a git blob is LF-normalized
            # content (git's own storage convention); the WORKING-TREE
            # read is not, and on a Windows checkout with the common
            # `core.autocrlf=true` default, git rewrites every LF back to
            # CRLF on checkout. Comparing the two unnormalized would read
            # that checkout-time rewrite alone as "every line grew" on an
            # otherwise byte-identical, untouched file -- a false refusal
            # this fleet's "Windows is first-class" bar does not allow
            # (CLAUDE.md's Runtime conventions). `commit_authored_content`'s
            # own callers pass content in-process, never round-tripping a
            # checkout, so this is the one read site that needs it.
            new_text = live.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
            old_entry = old_blobs.get(path)
            old_text = "" if old_entry is None else _blob_text(common_dir, old_entry[1])
        except Exception as exc:  # noqa: BLE001 -- fail closed, see module docstring.
            refusals.append(_incomplete(path, exc))
            continue
        if old_text is None:
            refusals.append(f"{path}: its HEAD blob could not be read, so its admission could not be checked.")
            continue
        if new_text == old_text:
            continue
        refusal = governed_write_refusal(root, path, old_text, new_text)
        if refusal is not None:
            refusals.append(refusal)
    return refusals

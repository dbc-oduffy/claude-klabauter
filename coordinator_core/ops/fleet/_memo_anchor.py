"""coordinator_core.ops.fleet._memo_anchor -- shared anchor helper: write,
list and resolve `refs/coordinator/inbox/*`, in-process, zero spawns.

Purpose: one copy of the anchor-ref namespace rule, shared by three
consumers so they cannot drift apart on its shape --
`memo.send` (writer), `memo.heal_inbox` (reader, adopter, retirer), and
`memo.check_deliveries` (reader).

Shape (Review: eng-director F1): `<ANCHOR_REF_PREFIX><filename>/<delivery
commit sha>` -- two ref-path levels under the prefix, the memo's inbox
filename first, then the sha of the commit that delivered it. A ref, once
written, keeps the memo's payload reachable as a blob even after every
branch/commit that once carried it is gone or garbage-collected -- `git gc`
never reaps an object a live ref still points at, loose or packed.

Negative-spec:
    - `write_anchor` never raises on a lost CAS race and never retries --
      the caller decides whether to retry, this module only reports.
    - `anchor_names` never raises on a missing `refs/coordinator/inbox/`
      directory or a missing/absent `packed-refs` file; both mean "no
      anchors", not an error.
    - The filename check REFUSES (returns `None`), it never normalises or
      sanitises, a filename git would reject as a single ref-path
      component.
    - `anchor_names` returns triples, not a `filename -> sha` mapping
      (Review: eng-director F1) -- a reachability discriminator downstream
      needs the delivery commit sha alongside each anchor, which a
      filename-keyed mapping cannot carry when the same filename has been
      delivered (and re-anchored) more than once.

Spec backlink: docs/plans/2026-09-11-memo-deliveries-survive-the-receiver-s-o.md, chunk C3
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional, Tuple, Union

from coordinator_core.git.git_objects import (
    _read_object,
    cas_ref,
    read_packed_ref,
    write_object,
)

ANCHOR_REF_PREFIX = "refs/coordinator/inbox/"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Control characters, space, and the literal characters git's
# `check-ref-format` refuses inside a single ref-path component.
_DISALLOWED_CHARS_RE = re.compile(r"[\x00-\x1f\x7f ~^:?*\[\\]")


def _valid_ref_component(name: str) -> bool:
    """True iff `name` is safe as one `/`-delimited ref path component --
    refused (never normalised) on any of: control characters, space,
    `~^:?*[\\`, a `..` substring, an `@{` substring, a leading `.`, or a
    trailing `.lock`. Memo inbox filenames are slugs, so refusal here is
    always the defect path, never the common case."""
    if not name:
        return False
    if _DISALLOWED_CHARS_RE.search(name):
        return False
    if ".." in name or "@{" in name:
        return False
    if name.startswith("."):
        return False
    if name.endswith(".lock"):
        return False
    return True


def _valid_commit_sha(sha: str) -> bool:
    return bool(_SHA_RE.match(sha))


def _read_loose_ref_value(common_dir: Path, ref: str) -> Optional[str]:
    """The literal content of a loose ref file, or `None` when it does not
    exist or is unreadable -- mirrors `git_objects._read_ref_raw` without
    reaching past that module's own underscore boundary for a one-line
    read."""
    try:
        text = (common_dir / ref).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return text or None


def write_anchor(
    common_dir: Union[str, Path], filename: str, commit_sha: str, data: bytes
) -> Optional[str]:
    """Writes `data` as a blob and points
    `refs/coordinator/inbox/<filename>/<commit_sha>` at it via a locked CAS.

    Refuses (returns `None`, writes nothing) when `filename` or
    `commit_sha` fails validation. On a valid ref, the blob is written
    first (content-addressed, cheap even when the CAS below then loses),
    the ref's current value is read (loose, then `read_packed_ref`), and
    the CAS swaps that exact value for the new blob sha. Returns the blob
    sha on success, `None` on a lost CAS race -- never raises, never
    retries; the caller decides.
    """
    if not _valid_ref_component(filename) or not _valid_commit_sha(commit_sha):
        return None
    common_dir = Path(common_dir)
    blob_sha = write_object(common_dir, b"blob", data)
    ref = ANCHOR_REF_PREFIX + filename + "/" + commit_sha
    current = _read_loose_ref_value(common_dir, ref)
    if current is None:
        current = read_packed_ref(common_dir, ref)
    if not cas_ref(common_dir, ref, current, blob_sha):
        return None
    return blob_sha


def anchor_names(common_dir: Union[str, Path]) -> List[Tuple[str, str, str]]:
    """`(filename, commit_sha, blob_sha)` triples for every anchor found
    under `<common_dir>/refs/coordinator/inbox/` -- the union of loose refs
    (walked two levels: a filename directory, then a commit-sha leaf file)
    and matching lines in `packed-refs`. Loose shadows packed, matching
    git's own precedence. A missing anchors directory, or a missing/absent
    `packed-refs`, contributes no anchors and is never an error.
    """
    common_dir = Path(common_dir)
    found: "dict[tuple[str, str], str]" = {}

    # Packed first, so a loose entry for the same (filename, commit_sha)
    # overwrites -- shadows -- it below.
    try:
        packed_text = (common_dir / "packed-refs").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        packed_text = ""
    for line in packed_text.splitlines():
        if not line or line[0] in "#^":
            continue
        sha, _, name = line.partition(" ")
        name = name.strip()
        if not name.startswith(ANCHOR_REF_PREFIX):
            continue
        rest = name[len(ANCHOR_REF_PREFIX):]
        fname, _, csha = rest.partition("/")
        if fname and csha:
            found[(fname, csha)] = sha.strip()

    anchors_dir = common_dir / "refs" / "coordinator" / "inbox"
    try:
        fname_entries = list(os.scandir(anchors_dir))
    except OSError:
        fname_entries = []
    for fentry in fname_entries:
        if not fentry.is_dir():
            continue
        try:
            sha_entries = list(os.scandir(fentry.path))
        except OSError:
            continue
        for sentry in sha_entries:
            if not sentry.is_file():
                continue
            try:
                blob_sha = Path(sentry.path).read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
            except OSError:
                continue
            if blob_sha:
                found[(fentry.name, sentry.name)] = blob_sha

    return [(fname, csha, bsha) for (fname, csha), bsha in found.items()]


def resolve_anchor(common_dir: Union[str, Path], sha: str) -> Optional[bytes]:
    """The payload bytes stored at `sha`, only when its object kind is
    `blob`; `None` for a missing object, a non-blob object, or an invalid
    sha."""
    if not _valid_commit_sha(sha):
        return None
    common_dir = Path(common_dir)
    result = _read_object(common_dir, sha)
    if result is None:
        return None
    kind, payload = result
    if kind != "blob":
        return None
    return payload

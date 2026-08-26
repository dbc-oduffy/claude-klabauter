#!/usr/bin/env python3
"""No publish-staging directory may be TRACKED in this mirror.

WHY THIS EXISTS
---------------
``publish.py::_create_publish_staging_dir`` mints ``.<dest-basename>.publish-
staging-<hex>/`` beside the destination, fills it with PRE-transform source
bytes, transforms it in place, swaps it over the destination, and sweeps it.
Nothing in it is ever meant to be committed: it is scratch that happens to
live inside the mirror's working tree.

Every other content check in this harness therefore skips those directories,
and is right to -- scanning scratch fails a round on bytes that never ship.
That reasoning has one premise: the scratch is UNTRACKED. On 2026-08-26 a
blanket ``git add`` broke the premise, putting 4045 pre-transform files under
``.coordinator_core.publish-staging-4f5zkrth/`` onto a PUBLIC remote. Because
the skip was unconditional, the identity check reported exit 0 over 4620
files while 3017 unscrubbed ones sat beside them, unlooked-at.

``_repo.repo_files`` now scans a staging directory once it is tracked, so
those bytes are no longer invisible to the content checks. This check is the
separate, stronger statement: a tracked staging directory is a defect even
when its contents happen to scrub clean, because it is by definition
untransformed source shipped as payload. It is the check that names the
CAUSE; the content checks only catch a symptom of it.

``.gitignore``'s ``*publish-staging-*/`` entry stops the next one being
added. Neither an ignore rule nor a skip list can undo one that already
landed -- that is this check's job.

EXIT CONTRACT
  0 -- no tracked path lives under a publish-staging directory (including a
       tree with no git index at all, where nothing can be tracked)
  1 -- one or more do (fix command printed)
"""

from __future__ import annotations

import pathlib
import shlex
import sys

sys.dont_write_bytecode = True  # never litter the published tree with a __pycache__
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _repo import repo_root, tracked_publish_staging_paths  # noqa: E402

#: Offending paths listed individually before collapsing to a directory count.
#: A real incident is thousands of files under one or two directories; the
#: sample is there to make the shape recognizable, not to enumerate it.
SAMPLE_LIMIT = 10


def _staging_dir_roots(paths: list[str]) -> list[str]:
    """The shortest distinct directory prefix of each offending path.

    The fix has to be issued per STAGING DIRECTORY, never per file: the
    incident that motivated this check left 4045 tracked paths, and a
    ``git rm --cached`` enumerating them all exceeds Windows' 32767-character
    command line (WinError 206) -- which is how the leak went on to break
    every subsequent publish round rather than being cleaned up in one.
    """
    roots: set[str] = set()
    for path in paths:
        parts = path.split("/")
        for depth, part in enumerate(parts[:-1]):
            if "publish-staging-" in part:
                roots.add("/".join(parts[: depth + 1]))
                break
    return sorted(roots)


def main() -> int:
    root = repo_root()
    offenders = tracked_publish_staging_paths(root)

    if not offenders:
        print("Staging-leftover check passed (no tracked publish-staging paths).")
        return 0

    dirs = _staging_dir_roots(offenders)
    print(
        f"Staging-leftover check FAILED — {len(offenders)} tracked file(s) under "
        f"{len(dirs)} publish-staging director{'y' if len(dirs) == 1 else 'ies'}."
    )
    print("These are PRE-transform source bytes committed as published payload.")
    print()
    for path in offenders[:SAMPLE_LIMIT]:
        print(f"  {path}")
    if len(offenders) > SAMPLE_LIMIT:
        print(f"  ... and {len(offenders) - SAMPLE_LIMIT} more")
    print()
    print("Untrack them by DIRECTORY (never by file — the path list overruns")
    print("Windows' command-line limit at this scale):")
    for directory in dirs:
        print(f"  git rm -r --cached --quiet -- {shlex.quote(directory)}")
    print()
    print("Then delete the directories from the working tree and commit the removal.")
    print("A public remote that already carries them keeps the blobs in its history:")
    print("treat the leaked content as disclosed, not merely as a file to delete.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

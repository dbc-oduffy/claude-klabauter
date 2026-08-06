#!/usr/bin/env python3
"""Every tracked file whose blob begins with '#!' must be committed at mode 100755.

WHY THIS EXISTS
---------------
On Windows with ``core.fileMode=false`` git does not preserve executable bits in
the index. Scripts committed from Windows land at 100644 even after
``git update-index --chmod=+x``. A clean clone on any POSIX machine then
installs non-functional entrypoints, and the failure is silent because Windows
callers invoke the interpreter explicitly and never exercise the mode bit.

The check reads the INDEX mode and the OBJECT STORE content, not on-disk stat —
mode-blindness on disk is the exact failure being caught, and the index is the
authoritative record of what a clone will deliver.

MID-BOOTSTRAP DEGRADATION
-------------------------
The mirror is populated before its initial commit exists. An empty index is
therefore an expected state, not a failure: the check reports that it had
nothing authoritative to inspect and exits 0. It becomes load-bearing from the
first commit onward. Likewise, a tree that is not a git repository at all (a
percolate dry-run staging directory) is skipped rather than failed.

EXIT CONTRACT
  0 — all shebanged tracked files are at 100755, or the index is empty / absent
  1 — one or more shebanged tracked files are at 100644 (fix command printed)
"""

from __future__ import annotations

import pathlib
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True  # never litter the published tree with a __pycache__
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _repo import NO_WINDOW, repo_root  # noqa: E402


def staged_entries(root: pathlib.Path) -> list[tuple[str, str, str]] | None:
    """[(mode, blob_hash, path)] from the index, or None when git is unusable here."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--stage"],
            capture_output=True, text=True,
            creationflags=NO_WINDOW,
        )
    except (OSError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None

    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        # <mode> SP <object> SP <stage> TAB <path>
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        meta = parts[0].split()
        if len(meta) < 2:
            continue
        mode, obj_hash, path = meta[0], meta[1], parts[1]
        # During a merge conflict the same path appears at stages 1/2/3; keep
        # the first so a conflicted tree does not produce phantom offenders.
        if path in seen:
            continue
        seen.add(path)
        entries.append((mode, obj_hash, path))
    return entries


def shebanged_blobs(root: pathlib.Path, obj_hashes: list[str]) -> set[str]:
    """Subset of ``obj_hashes`` whose blob content starts with '#!'."""
    if not obj_hashes:
        return set()

    result = subprocess.run(
        ["git", "-C", str(root), "cat-file", "--batch"],
        input=("\n".join(obj_hashes) + "\n").encode(),
        capture_output=True,
        creationflags=NO_WINDOW,
    )
    if result.returncode != 0:
        print(f"ERROR: git cat-file --batch failed: {result.stderr.decode().strip()}")
        sys.exit(1)

    hits: set[str] = set()
    raw = result.stdout
    pos = 0
    while pos < len(raw):
        newline = raw.find(b"\n", pos)
        if newline == -1:
            break
        header = raw[pos:newline].decode("ascii", errors="replace")
        pos = newline + 1

        parts = header.split()
        if len(parts) < 3 or parts[1] == "missing":
            continue
        obj_hash = parts[0]
        try:
            size = int(parts[2])
        except ValueError:
            # Silent desync of a binary stream parser is worse than a hard exit.
            print(f"ERROR: unexpected cat-file header: {header!r}", file=sys.stderr)
            sys.exit(1)

        if raw[pos:pos + 2] == b"#!":
            hits.add(obj_hash)
        pos += size
        if raw[pos:pos + 1] == b"\n":
            pos += 1

    return hits


def main() -> int:
    root = repo_root()
    entries = staged_entries(root)

    if entries is None:
        print("Exec-bit check skipped: not a git working tree (nothing authoritative to read).")
        return 0
    if not entries:
        print("Exec-bit check skipped: index is empty (pre-initial-commit bootstrap).")
        return 0

    candidates = [(h, p) for mode, h, p in entries if mode == "100644"]
    if not candidates:
        print(f"Exec-bit check passed ({len(entries)} tracked files).")
        return 0

    hits = shebanged_blobs(root, [h for h, _ in candidates])
    offenders = sorted(p for h, p in candidates if h in hits)

    if not offenders:
        print(f"Exec-bit check passed ({len(entries)} tracked files).")
        return 0

    print("Exec-bit check FAILED — shebanged files committed at 100644:")
    for path in offenders:
        print(f"  {path}")
    print()
    print("Fix with:")
    print(f"  git update-index --chmod=+x {' '.join(shlex.quote(p) for p in offenders)}")
    print()
    print("Then commit the mode change WITHOUT a path-restricted '-- <paths>' suffix:")
    print("Windows core.fileMode=false silently resets exec bits when path-restricting.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

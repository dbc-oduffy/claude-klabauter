"""Merge machinery residue at the OLD `state/` paths into `.coordinator-local/`.

WHY THE DIRECTION IS OLD -> NEW, AND WHY THAT IS THE OPPOSITE OF AN HOUR AGO.
Before the republish, the served engine (claude-klabauter) had no
`session/machinery_paths.py` and built `state/subagent-share/<sid>` by hand, so
the data had crossed the publish boundary and the code had not. Rolling the data
back was then the coherent fix. The engine republished at 4fd50d7d and now
resolves `.coordinator-local/` -- verified by making the served tree produce a
path, not by observing the file exists -- so the new root is where every writer
now writes and the residue is what is left behind at the old paths.

SCOPE IS EVERY REPO, INCLUDING ONES THE SWEEP NEVER TOUCHED. A repo whose data
was never moved still splits the moment the republished engine starts writing its
new root beside the old corpus: the writer moved even where the data did not.
DoE-claude is the worked example -- 7,127 files at its old path, 4 at a new root
that is fourteen minutes NEWER. Any inventory taken before the republish
understates this and must be re-derived.

COLLISION RULE -- NEWEST MTIME WINS, LOSER PRESERVED, NEVER DISCARDED. Verified
specimens from a peer session, both of which a prefer-the-new-root merge would
have silently destroyed:

    coordinatorreview-integrator.ad6440ebd61f1a40c.md  old newer by 427s
    advisory-fire-counts.jsonl                         old newer by 1580s

Recency is not a preference here, it is the only rule safe in both observed
directions: append-only counter spools kept their accumulated history at the new
root while restarting empty at the old, and integrator sidecars did the reverse.
Neither side is uniformly "the good one", so the loser is copied into a snapshot
rather than dropped.

NEGATIVE SPEC: never commits; never deletes a file lacking a destination
counterpart; never overwrites a newer file with an older one; never touches
`.git/`; only repos named on the CLI.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from typing import Dict, List, Tuple

BUCKETS = (
    "subagent-share",
    "review-trail",
    "ceremony",
    "dispatch-briefs",
    "plan-sidecars",
)

SNAPSHOT_LEAF = "residue-merge-snapshot-2026-09-02"


def _iter_files(root: str):
    for dirpath, _d, filenames in os.walk(root):
        for name in filenames:
            yield os.path.join(dirpath, name)


def _plan(repo: str) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]], Dict[str, int]]:
    """(moves, superseded, stats). `moves` restore old-path files into the new
    root. `superseded` are old-path files whose new-root counterpart is newer --
    those stay put and the old copy is snapshotted."""
    moves: List[Tuple[str, str]] = []
    superseded: List[Tuple[str, str]] = []
    stats = {"old_files": 0, "collisions": 0, "old_newer": 0}
    for bucket in BUCKETS:
        src_root = os.path.join(repo, "state", bucket)
        if not os.path.isdir(src_root):
            continue
        dst_root = os.path.join(repo, ".coordinator-local", bucket)
        for src in _iter_files(src_root):
            stats["old_files"] += 1
            rel = os.path.relpath(src, src_root)
            dst = os.path.join(dst_root, rel)
            if os.path.exists(dst):
                stats["collisions"] += 1
                if os.path.getmtime(dst) >= os.path.getmtime(src):
                    superseded.append((src, dst))
                    continue
                stats["old_newer"] += 1
            moves.append((src, dst))
    return moves, superseded, stats


def _snapshot(repo: str, paths: List[str]) -> int:
    if not paths:
        return 0
    base = os.path.join(repo, ".coordinator-local", SNAPSHOT_LEAF)
    n = 0
    for p in paths:
        rel = os.path.relpath(p, os.path.join(repo, "state"))
        target = os.path.join(base, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(p, target)
        n += 1
    return n


def run(repo: str, apply: bool) -> int:
    repo = os.path.abspath(repo)
    if not os.path.isdir(os.path.join(repo, ".git")):
        print(f"## {repo}\n- not a git worktree -- skipped")
        return 1
    moves, superseded, stats = _plan(repo)
    print(f"## {repo}")
    print(f"- residue at old paths : {stats['old_files']}")
    print(f"- collisions           : {stats['collisions']} "
          f"(old newer in {stats['old_newer']})")
    print(f"- would move to new root: {len(moves)}")
    print(f"- new root newer, kept : {len(superseded)}")
    if not apply:
        return 0
    kept = _snapshot(repo, [s for s, _d in superseded])
    if kept:
        print(f"- snapshotted {kept} superseded old-path file(s)")
    done = 0
    for src, dst in moves:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        done += 1
    print(f"- merged {done} file(s) into the new root")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", action="append", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    rc = 0
    for repo in args.repo:
        rc |= run(repo, args.apply)
    if not args.apply:
        print("\nDRY RUN -- nothing moved. Re-run with --apply.")
    return rc


if __name__ == "__main__":
    sys.exit(main())

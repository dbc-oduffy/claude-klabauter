"""
coordinator_core.ops.shim_usage_census -- per-shim invocation census.

Purpose: C8 converted 12 of the 13 `-assemble` entry points into thin
shims that resolve through `coordinator/bin/lib/entry_point_shim.py ::
run_target`. Before any of those shims (or their pre-shim `.py` files) can
be deleted, the plan needs evidence that nothing still calls the OLD name
-- and "no hits in this repo" is not that evidence, because DoE's own
ceremony prose (which invokes these entry points by name) lives in a
sibling tree this repo cannot grep. This module is the durable record that
closes that gap: every invocation `entry_point_shim.run_target` observes
is appended here, so a later census reads "has this shim ever been
called, and when" off DISK EVIDENCE rather than a repo-local search.

Spec backlink: docs/plans/2026-08-16-a-process-per-predicate.md, chunk C9
               (AC8: "A census reports, per shim, whether anything still
               invokes it -- so the deprecation window closes on evidence,
               not a guess.")

Cheap by construction -- this is the hard constraint, and the reason for
every shape decision below:
    - `record_invocation()` fires on the SAME hot path `entry_point_shim.
      run_target` occupies, which is the path this whole plan exists to
      unload (C7: a spawn costs ~13.6ms; the in-process seam this plan
      built resolves in 0.14ms). An expensive telemetry write here would
      eat the win the shim conversion just bought.
    - No subprocess spawn anywhere in this module. Repo-root resolution
      reuses `coordinator_core.git.repo_root.show_toplevel` -- the shared,
      cwd-keyed, WALK-based (not spawn-based) memoized seam C3/C4 already
      ship, so a hot invocation pays at most one directory-walk stat loop
      on a cold cwd and a dict lookup on every call after (same process,
      same cwd, which is the ordinary case for one shim invocation).
    - No lock. `open(path, "a")`'s append-mode write is what POSIX and
      NTFS both guarantee is atomic FOR A SINGLE `write()` SYSCALL sized
      well under the filesystem's atomic-append boundary (a few hundred
      bytes here, far under the 4KiB PIPE_BUF-class guarantee both
      platforms honor for local disk appends) -- concurrent writers each
      get their own complete line, never an interleaved half-line, without
      taking a lock. This is the same pattern `coordinator_core.telemetry.
      cost_census._append_row` and `state/raw-cmdline-transport-ledger.
      jsonl`'s writer already use in this repo; not invented here.
    - One JSON object per line (`{"name": ..., "ts": ...}`), no formatting
      whitespace, `separators=(",", ":")` -- the smallest correct encoding,
      matching `cost_census`'s own convention.

Failure discipline (negative-spec): `record_invocation()` NEVER raises,
under any failure -- unwritable file, missing repo root, disk full,
concurrent-rename race, anything. A census that can break the shim it
observes turns an observability feature into an outage on every ceremony
that calls a converted `-assemble` entry point; every exception this
module could produce is caught and swallowed at the point of use. The read
side (`census()`) is bounded by file size but never raises either -- a
missing or corrupt series degrades to "never observed", not an exception.

Where the series lives: `state/shim-usage-census.jsonl`, sibling to
`state/cost-census.jsonl` and `state/raw-cmdline-transport-ledger.jsonl`
-- the same flat-append-only-JSONL-under-state/ convention already
established twice in this repo, reused rather than invented.

Negative-spec:
    - Never spawns a subprocess (repo-root resolution walks; `git.
      repo_root` only spawns as a last-resort fallback when no `.git`
      entry is found by the walk, which does not apply to a call
      originating inside this repo's own tree).
    - Never takes a lock, in-process or on-disk.
    - Never truncates or rewrites the series file -- strictly append-only.
    - Never raises out of `record_invocation()` for any reason.
    - Does not itself hook `entry_point_shim.run_target` -- that one-line
      call site is owned by the C10 dispatch (concurrent sibling chunk
      actively editing that file); this module only provides the callable
      to be wired in.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from coordinator_core.engine_root import (
    engine_source_root,
    is_published_engine_mirror,
)
from coordinator_core.git.repo_root import show_toplevel


def _series_path(repo_root: Path) -> Path:
    return repo_root / "state" / "shim-usage-census.jsonl"


def record_invocation(
    name: str,
    *,
    repo_root: Optional[Path] = None,
    now: Optional[float] = None,
) -> None:
    """Best-effort, append-only record that shim `name` was invoked.

    Intended call site: `coordinator/bin/lib/entry_point_shim.py ::
    run_target`, once per invocation, before or after dispatching to the
    resolved target -- either ordering is fine since this never affects
    the target's own outcome. Never raises; every failure (unresolvable
    repo root, unwritable/contended file, any OSError) is swallowed here
    so a census write can never turn into a shim outage. See module
    docstring's cheapness and failure-discipline sections.
    """
    try:
        root = repo_root if repo_root is not None else _resolve_repo_root()
        if root is None:
            return
        ts = now if now is not None else time.time()
        line = json.dumps({"name": name, "ts": ts}, separators=(",", ":")) + "\n"
        path = _series_path(root)
        os.makedirs(path.parent, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    except Exception:
        # Negative-spec: a census write must never break the shim it
        # observes. Every failure mode (unwritable file, contended disk,
        # missing repo root, encoding error) degrades to "not recorded"
        # rather than propagating.
        pass


def _resolve_repo_root() -> Optional[Path]:
    """Resolve the repo whose series this invocation belongs to.

    Normally cwd's git toplevel: a shim used inside a sibling repo is that
    repo's usage, and its series belongs there. The exception is the published
    engine mirror. A shim invoked with cwd inside it appended to
    `<mirror>/state/shim-usage-census.jsonl`, which is gitignored there and
    tracked nowhere -- the series was being written to a sink no reader can
    open, and `census()` reading from any real repo could never see those rows.
    A mirror is a build artifact, not a repo whose shim usage anyone asked
    about, so those invocations are redirected to the engine source tree where
    the rest of the series already lives.

    Backlink: state/audits/2026-08-21-transform-resolved-writer-inventory.md
    """
    toplevel = show_toplevel()
    if toplevel is None:
        return None
    if is_published_engine_mirror(toplevel):
        source_root = engine_source_root()
        return Path(source_root) if source_root else None
    return Path(toplevel)


def census(
    targets: Iterable[str],
    *,
    repo_root: Optional[Path] = None,
) -> Dict[str, dict]:
    """Read-only report, per name in `targets`: has anything invoked it,
    how many times, and its first/last observed timestamp. A name absent
    from `targets` is silently ignored even if the series has rows for it
    (keeps the report scoped to the caller's known target set, matching
    `cost_census`'s `HOT_PATH_OPS`-scoped precedent).

    Never raises: a missing or unreadable series file, or a malformed
    line, degrades to "never observed" for the affected rows rather than
    propagating -- same failure discipline as `record_invocation()`.

    Every row also carries `series_present`: False when no series file
    exists on disk at all (nothing has ever been observed on this box, or
    the writer has never fired -- e.g. a fresh worktree, or before
    `entry_point_shim.run_target`'s wiring lands), True when the series
    file exists (telemetry has been collected, even if this particular
    name has zero rows in it). Callers deciding whether it is safe to
    delete a shim MUST check `series_present` before trusting `invoked`:
    `invoked=False, series_present=False` means "we have not been
    watching yet", not "we watched and saw nothing" -- conflating the two
    would licence deleting a shim on the strength of a census that was
    simply never created. See review-census.md P2.
    """
    result: Dict[str, dict] = {
        name: {
            "invoked": False,
            "count": 0,
            "first_ts": None,
            "last_ts": None,
            "series_present": False,
        }
        for name in targets
    }
    root = repo_root if repo_root is not None else _resolve_repo_root()
    if root is None:
        return result
    path = _series_path(root)
    if not path.is_file():
        return result
    for rec in result.values():
        rec["series_present"] = True
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                if name not in result:
                    continue
                ts = entry.get("ts")
                rec = result[name]
                rec["invoked"] = True
                rec["count"] += 1
                if isinstance(ts, (int, float)):
                    if rec["first_ts"] is None or ts < rec["first_ts"]:
                        rec["first_ts"] = ts
                    if rec["last_ts"] is None or ts > rec["last_ts"]:
                        rec["last_ts"] = ts
    except OSError:
        pass
    return result


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint -- `python -m coordinator_core.ops.shim_usage_census
    [name ...]`. With no args, reports on every name in
    `entry_point_shim.ASSEMBLE_TARGETS` (imported lazily so this module has
    no import-time dependency on `coordinator/bin/lib`, which is not
    normally on `sys.path`)."""
    argv = list(argv) if argv is not None else []
    root = _resolve_repo_root()
    if root is None:
        print("shim_usage_census: could not resolve repo root")
        return 1

    targets = argv
    if not targets:
        lib_dir = str(root / "coordinator" / "bin" / "lib")
        import sys

        if lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)
        try:
            from entry_point_shim import ASSEMBLE_TARGETS

            targets = list(ASSEMBLE_TARGETS)
        except ImportError:
            targets = []

    report = census(targets, repo_root=root)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))

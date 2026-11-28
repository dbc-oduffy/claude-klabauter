"""Falsifier: a landing on a store with several hundred packs, and the engine bound on pack count.

Builds a tmp repo with 400 engine-shaped small packs (no `.rev` sibling), a `multi-pack-index` over 7
of them and an aged `multi-pack-index.lock`. Every object read of the landing's own blobs is forced
past its wall once the ref has moved (the observed `PackReadDeadline` in the post-ref worktree sync);
nothing else in production code is patched. Then `commit_paths` runs at default arguments and
`maintenance_tick.run_due(repo, ["daily"])` runs up to MAX_BATCHES times with the claim stamps
backdated between calls. Prints one line:

    landed=<bool> deadline_raised=<bool> packs_added=<n> midx_covers_all=<bool> max_batch_process_ms=<n>

`landed` is True when `commit_paths` returned an outcome; `deadline_raised` is True when a
`PackReadDeadline` escaped it; `packs_added` is the pack count after the daily batches minus
PACKS; `midx_covers_all` is True when the MIDX names every pack. Process time is `time.process_time` plus the descendants' CPU, never
wall clock. Diagnostics go to stderr.

Run: `python coordinator_core/benchmarks/landing_pack_count_bound.py <scratch-dir>` (a directory
under the session scratch; this script creates and removes one subdirectory of it).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.win_portability import no_console_creationflags

PACKS = 400
MIDX_COVERED = 7
PATHS = 4
MAX_BATCHES = 12
_MSG = "bench landing\n\nSession-Id: 11111111-1111-4111-8111-111111111111"
_BACKDATED_STAMPS = ("store-hygiene.claim", "store-hygiene.last-batch")
_TICK_STAMP_PREFIX = "maintenance-tick-"


def _git(repo: Path, *args: str, data: Optional[bytes] = None) -> str:
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(repo.parent))
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, input=data,
        env=env, **no_console_creationflags(),
    ).stdout.decode().strip()


def _body(i: int, tag: str = "") -> bytes:
    return "".join(f"p{i} line {n}{tag if n == 2 else ''}\n" for n in range(1, 41)).encode()


def _build_repo(scratch: Path) -> Tuple[Path, Dict[str, str], Dict[str, str]]:
    """A repo with PATHS committed paths, each peer-dirty in the worktree (line 30 edited), and a
    landed variant blob (line 2 edited) per path. Returns `(repo, landed blobs, parent blobs)`."""
    repo = scratch / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "b@b.example")
    _git(repo, "config", "user.name", "b")
    _git(repo, "config", "core.autocrlf", "false")
    names = [f"f{i}.py" for i in range(PATHS)]
    for i, name in enumerate(names):
        (repo / name).write_bytes(_body(i))
    _git(repo, "add", "--", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    landed_dir = scratch / "landed"
    landed_dir.mkdir()
    for i, name in enumerate(names):
        (landed_dir / name).write_bytes(_body(i, " LANDED"))
    shas = _git(
        repo, "hash-object", "-w", "--no-filters", "--stdin-paths",
        data="".join(f"{landed_dir / name}\n" for name in names).encode(),
    ).split()
    blobs = dict(zip(names, shas))
    parents = {name: _git(repo, "rev-parse", f"HEAD:{name}") for name in names}
    for i, name in enumerate(names):
        (repo / name).write_bytes(_body(i).replace(b"line 30\n", b"PEER 30\n"))
    return repo, blobs, parents


def _mint_packs(pack_dir: Path, first: int, count: int) -> None:
    """`count` engine-shaped packs of one blob each: no `.rev`, `.keep`, `.promisor`, `.mtimes`."""
    from coordinator_core.git import git_objects

    for i in range(first, first + count):
        payload = f"engine pack {i}\n".encode() * 8
        sha = hashlib.sha1(b"blob %d\x00" % len(payload) + payload).hexdigest()
        pack, idx, name = git_objects._build_pack({sha: (b"blob", payload)})
        git_objects._place_pack(pack_dir, pack, idx, name)


def _build_store(repo: Path) -> Path:
    from coordinator_core.git import store_hygiene

    pack_dir = repo / ".git" / "objects" / "pack"
    _mint_packs(pack_dir, 0, MIDX_COVERED)
    _git(repo, "multi-pack-index", "write")
    _mint_packs(pack_dir, MIDX_COVERED, PACKS - MIDX_COVERED)
    lock = pack_dir / "multi-pack-index.lock"
    lock.write_bytes(b"")
    aged = time.time() - store_hygiene.ORPHAN_PACK_AGE_S - 24 * 3600
    os.utime(lock, (aged, aged))
    return pack_dir


class _ForcedWall:
    """Replaces `git_objects._under_read_wall`: a first-time object read of one of `shas` raises
    `PackReadDeadline` once `head_ref` no longer names `head_before` (the ref has moved, so the
    read is the post-ref sync's). Every other read goes through the real wall."""

    def __init__(self, shas: set, head_ref: Path, head_before: str) -> None:
        self.shas = shas
        self.head_ref = head_ref
        self.head_before = head_before
        self.hits = 0

    def __enter__(self) -> "_ForcedWall":
        from coordinator_core.git import git_objects

        self._mod = git_objects
        self._real = git_objects._under_read_wall
        wall = self

        def forced(kind: str, common_dir: Path, sha: str, fn) -> object:
            if kind == "object-read" and sha in wall.shas and wall._ref_moved():
                wall.hits += 1
                raise git_objects.PackReadDeadline(f"forced past the wall: {kind} of {sha}")
            return wall._real(kind, common_dir, sha, fn)

        setattr(git_objects, "_under_read_wall", forced)
        return self

    def __exit__(self, *exc) -> None:
        setattr(self._mod, "_under_read_wall", self._real)

    def _ref_moved(self) -> bool:
        try:
            return self.head_ref.read_text().strip() != self.head_before
        except OSError:
            return False


class _Meter:
    """Process time of one bracket: this process's CPU plus every descendant's."""

    def __init__(self) -> None:
        self._child = None
        try:
            from coordinator_core.benchmarks.commit_leg_profile import _ChildMeter

            self._child = _ChildMeter()
        except Exception as exc:  # noqa: BLE001 -- a missing child meter degrades to own CPU, said on stderr
            print(f"child process time unavailable: {exc}", file=sys.stderr)

    def __enter__(self) -> "_Meter":
        self.ms = 0.0
        self._t0 = time.process_time()
        if self._child is not None:
            self._child.start()
        return self

    def __exit__(self, *exc) -> None:
        own = (time.process_time() - self._t0) * 1000.0
        self.ms = own + (self._child.stop_ms() if self._child is not None else 0.0)


def _backdate_stamps(common: Path) -> None:
    from coordinator_core.git import maintenance_tick

    when = time.time() - 2 * maintenance_tick.WEEKLY_INTERVAL_S
    stamp_dir = common / maintenance_tick._STAMP_DIR
    if not stamp_dir.is_dir():
        return
    for entry in stamp_dir.iterdir():
        if entry.name in _BACKDATED_STAMPS or entry.name.startswith(_TICK_STAMP_PREFIX):
            os.utime(entry, (when, when))


def measure(scratch: Path) -> dict:
    """Build the fixture under `scratch`, run the landing and the daily batches; the result dict."""
    from coordinator_core.git import maintenance_tick, store_hygiene
    from coordinator_core.git.commit import commit_paths
    from coordinator_core.git.git_objects import PackReadDeadline
    from coordinator_core.git.git_dir import resolve_git_common_dir

    repo, blobs, parents = _build_repo(scratch)
    pack_dir = _build_store(repo)
    common = Path(resolve_git_common_dir(repo))
    head_before = _git(repo, "rev-parse", "HEAD")
    branch = _git(repo, "symbolic-ref", "HEAD")
    packs_before = store_hygiene.status(common)

    landed = False
    deadline_raised = False
    outcome = None
    wall = _ForcedWall(set(blobs.values()) | set(parents.values()), repo / ".git" / branch, head_before)
    with wall:
        try:
            outcome = commit_paths(repo, sorted(blobs), _MSG, supplied_blobs=blobs)
            landed = True
        except PackReadDeadline as exc:
            deadline_raised = True
            print(f"commit_paths raised {type(exc).__name__}: {exc}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 -- reported, never mistaken for a deadline
            print(f"commit_paths raised {type(exc).__name__}: {exc}", file=sys.stderr)
    head_after = _git(repo, "rev-parse", "HEAD")

    batch_ms: List[float] = []
    for _ in range(MAX_BATCHES):
        _backdate_stamps(common)
        with _Meter() as meter:
            maintenance_tick.run_due(repo, ["daily"])
        batch_ms.append(meter.ms)
        covered = store_hygiene._midx_pack_count(pack_dir / "multi-pack-index") or 0
        if len(store_hygiene._small_packs(pack_dir)) <= store_hygiene.CONSOLIDATE_MAX_PACKS and covered == store_hygiene.status(common).pack_count:
            break
    after = store_hygiene.status(common)
    return {
        "landed": landed,
        "deadline_raised": deadline_raised,
        "small_packs_after": len(store_hygiene._small_packs(pack_dir)),
        "max_batch_process_ms": round(max(batch_ms), 1),
        "forced_wall_hits": wall.hits,
        "head_moved": head_after != head_before,
        "left_alone": len(getattr(outcome, "left_alone", ())),
        "sync_refused": len(getattr(outcome, "sync_refused", ())),
        "packs_before": packs_before.pack_count,
        "midx_before": packs_before.midx_pack_count,
        "packs_after": after.pack_count,
        "midx_after": after.midx_pack_count,
        "packs_added": after.pack_count - PACKS,
        "midx_covers_all": (after.midx_pack_count or 0) == after.pack_count,
        "batches": len(batch_ms),
    }


def main(argv: List[str]) -> int:
    if len(argv) != 1:
        print("usage: python coordinator_core/benchmarks/landing_pack_count_bound.py SCRATCH_DIR", file=sys.stderr)
        return 2
    declare_benchmark_origin()
    parent = Path(argv[0]).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    scratch = parent / f"landing-pack-count-bound-{os.getpid()}"
    scratch.mkdir()
    try:
        with fixture_settings_home(scratch):
            result = measure(scratch)
    finally:
        rmtree_or_raise(scratch, label="landing-pack-count-bound")
    print("diagnostics: " + " ".join(f"{k}={v}" for k, v in result.items()), file=sys.stderr)
    print(
        f"landed={result['landed']} deadline_raised={result['deadline_raised']} "
        f"packs_added={result['packs_added']} midx_covers_all={result['midx_covers_all']} "
        f"max_batch_process_ms={result['max_batch_process_ms']:g}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

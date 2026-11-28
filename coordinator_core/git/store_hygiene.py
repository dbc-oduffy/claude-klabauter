"""Cheap, subprocess-free read of an object store's hygiene state, plus the constants
that decide when the engine owes the store maintenance.

`status` answers three questions from a few syscalls: how many packs exist, how many of
them the multi-pack-index covers, and roughly how many loose objects pile up.
`owed_by_threshold` folds those, a stale multi-pack-index and aged orphan pack files into
one spawn-free "does this store need packing now" answer; the daily tier reaps with
`sweep_orphan_pack_files` and merges small packs with `consolidate_pack_batch`.

Invariants:
  - `status`, `owed_by_threshold`, `midx_names_missing_pack` and `sweep_orphan_pack_files`
    spawn nothing and import nothing from the ops surface (cold-start budget); the daily
    child imports ops modules lazily.
  - `sweep_orphan_pack_files` and `consolidate_pack_batch` mutate the pack dir: the caller
    holds the object-store lock.
  - `consolidate_pack_batch` rewrites the MIDX before releasing its hold; load gates only
    the daily batch leg, never the MIDX heal and write.
  - `midx_pack_count` is None for no MIDX, a torn header, or an unreadable file; a
    caller treats None as "uncovered", never as an error.
  - `loose_estimate` is `entries in ONE sampled fan-out dir * 256`: coarse (multiples of
    256) by design, a trigger signal and not a count.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import NamedTuple, Optional

from coordinator_core.git import fleet_maintenance_lock, pack_deletion_pause
from coordinator_core.git.git_dir import resolve_git_common_dir

#: Uncovered packs (pack_count - midx_pack_count) past which the daily tier owes a MIDX write.
STALE_PACK_SLACK = 32
#: Estimated loose objects past which the daily tier owes a loose-packing leg.
STALE_LOOSE_SLACK = 512
#: Minimum seconds between claims of the daily tier.
CLAIM_INTERVAL_S = 3600
#: Packs under this size are engine per-batch packs (1.7KB up to hundreds of KB); git's own
#: packs are larger and left to `incremental-repack`.
CONSOLIDATE_SIZE_FLOOR = 1024 * 1024
#: Small-pack count the store may carry; past it the daily tier merges a batch. Each
#: extra pack costs lookup time and a cache slot, and a few dozen is below any budget.
CONSOLIDATE_MAX_PACKS = 32
#: Smallest packs merged by one `consolidate_pack_batch` call (measured ~190 ms for 40).
CONSOLIDATE_BATCH_PACKS = 40
#: Pack bytes merged by one call at most, bounding the merge's memory and time.
CONSOLIDATE_GROUP_BYTES = 16 * 1024 * 1024
#: 1-minute load per logical CPU at or past which the daily child defers its heavy batch leg
#: (bounded by CONSOLIDATE_MAX_DEFER_S / PACK_COUNT_FORCE); MIDX upkeep never waits on it.
BUSY_CPU_LOAD = 0.85
#: Longest, in seconds, a heavy consolidation batch may be deferred for load before it runs
#: anyway: how long is a busy box allowed to starve merging.
CONSOLIDATE_MAX_DEFER_S = 6 * 3600
#: Small-pack count past which a consolidation batch runs regardless of load: how many packs
#: may pile up before load stops being an excuse.
PACK_COUNT_FORCE = 4 * CONSOLIDATE_MAX_PACKS

#: An orphan or temp pack file untouched this long belongs to a dead writer. Git and the
#: engine finish a pack in milliseconds (idx renamed last), and a live temp pack keeps
#: its mtime moving, so an hour of silence is not a writer in flight.
ORPHAN_PACK_AGE_S = 3600

_TMP_PACK_RE = re.compile(
    r"^(?:\.tmp-\d+-pack-.+\.(?:pack|idx|rev)|tmp_(?:pack|idx|rev)_.+"
    r"|pack-.+\.(?:pack|idx|rev)\.tmp\d+)$"
)
#: A valid pack is a 12-byte header plus a 20-byte trailer; smaller is torn, and one torn
#: pack in a `--stdin-packs` batch would fail the merge on every call.
_MIN_PACK_BYTES = 32
_GIT_SIBLING_EXTS = (".rev", ".keep", ".promisor", ".mtimes", ".bitmap")
_PNAM = b"PNAM"
_MIDX_CHUNK_ENTRY_LEN = 12
_SAMPLE_FANOUT = "00"
_LOOSE_NAME_LEN = 38
_MIDX_MAGIC = b"MIDX"
_MIDX_HEADER_LEN = 12
_MIDX_LOCK_NAME = "multi-pack-index.lock"


class StoreHygiene(NamedTuple):
    pack_count: int
    midx_pack_count: Optional[int]
    loose_dirs_sampled: int
    loose_estimate: int
    #: `{age_s, pack_count}` while an aged, unheld `multi-pack-index.lock` blocks MIDX writes.
    stale_midx_lock: Optional[dict] = None


def _midx_pack_count(midx_path: Path) -> Optional[int]:
    """Header shape of `archive_terminal_handoffs._sha_in_multi_pack_index`: bytes 0..4
    magic, byte 4 version, bytes 8..12 big-endian pack count."""
    try:
        with open(midx_path, "rb") as f:
            header = f.read(_MIDX_HEADER_LEN)
    except OSError:
        return None
    if len(header) < _MIDX_HEADER_LEN or header[:4] != _MIDX_MAGIC or header[4] != 1:
        return None
    return int.from_bytes(header[8:12], "big")


def _count_suffix(directory: Path, suffix: str) -> int:
    try:
        with os.scandir(directory) as it:
            return sum(1 for e in it if e.name.endswith(suffix))
    except OSError:
        return 0


def _loose_in_fanout(objects_dir: Path) -> int:
    try:
        with os.scandir(objects_dir / _SAMPLE_FANOUT) as it:
            return sum(1 for e in it if len(e.name) == _LOOSE_NAME_LEN)
    except OSError:
        return 0


def status(common_dir: Path) -> StoreHygiene:
    """Hygiene state of `common_dir`'s object store. Never raises on a missing store."""
    objects = Path(common_dir) / "objects"
    pack_dir = objects / "pack"
    pack_count = _count_suffix(pack_dir, ".idx")
    return StoreHygiene(
        pack_count=pack_count,
        midx_pack_count=_midx_pack_count(pack_dir / "multi-pack-index"),
        loose_dirs_sampled=1,
        loose_estimate=_loose_in_fanout(objects) * 256,
        stale_midx_lock=_stale_midx_lock(pack_dir, pack_count),
    )


def _pack_dir_names(pack_dir: Path) -> set[str]:
    try:
        with os.scandir(pack_dir) as it:
            return {e.name for e in it}
    except OSError:
        return set()


def midx_names_missing_pack(common_dir) -> bool:
    """True when `objects/pack/multi-pack-index` names a pack whose `.idx` is absent.

    Reads the PNAM chunk (header, chunk table of `(4-byte id, 8-byte offset)` entries, then
    NUL-separated names) and compares it to one pack-dir listing; spawns nothing. False for
    no MIDX, a torn or foreign one, or one without a PNAM chunk: git rewrites those itself,
    and only a MIDX that names deleted packs fails every git leg ("could not load pack")."""
    pack_dir = Path(common_dir) / "objects" / "pack"
    try:
        with open(pack_dir / "multi-pack-index", "rb") as f:
            header = f.read(_MIDX_HEADER_LEN)
            if len(header) < _MIDX_HEADER_LEN or header[:4] != _MIDX_MAGIC or header[4] != 1:
                return False
            table_len = _MIDX_CHUNK_ENTRY_LEN * (header[6] + 1)
            table = f.read(table_len)
            if len(table) < table_len:
                return False
            entries = [
                (table[i : i + 4], int.from_bytes(table[i + 4 : i + 12], "big"))
                for i in range(0, table_len, _MIDX_CHUNK_ENTRY_LEN)
            ]
            for k in range(len(entries) - 1):
                if entries[k][0] == _PNAM:
                    start, end = entries[k][1], entries[k + 1][1]
                    if end <= start:
                        return False
                    f.seek(start)
                    blob = f.read(end - start)
                    break
            else:
                return False
    except OSError:
        return False
    present = _pack_dir_names(pack_dir)
    return any(n.decode("utf-8", "replace") not in present for n in blob.split(b"\0") if n)


def midx_upkeep_owed(common_dir) -> bool:
    """True when the MIDX needs a (sub-second) rewrite, whatever the box load: it names a pack
    whose `.idx` is gone, or the packs it does not cover exceed STALE_PACK_SLACK (zero while
    `pack_deletion_pause` holds: nothing folds, so any uncovered pack is one more per-`.idx` probe
    for the reader). Stat calls plus one read of the MIDX file; no spawn, no lock, no liveness
    read, no loose-object sample."""
    common_dir = Path(common_dir)
    pack_dir = common_dir / "objects" / "pack"
    midx_count = _midx_pack_count(pack_dir / "multi-pack-index") or 0
    slack = 0 if pack_deletion_pause.paused() else STALE_PACK_SLACK
    if _count_suffix(pack_dir, ".idx") - midx_count > slack:
        return True
    return midx_names_missing_pack(common_dir)


def _orphan_pack_files(pack_dir: Path, names: set[str], now: float) -> list[tuple[Path, str]]:
    """`(path, partner_name)` for each pack-dir file that is a dead writer's leaving and
    untouched for ORPHAN_PACK_AGE_S: a temp pack name, a `.pack` with no `.idx`, a `.rev`
    with no `.pack` (or whose `.pack` is itself idx-less). `partner_name` is the sibling
    whose absence the unlink re-checks. Packs sort before their revs. Unknown names stay."""
    cutoff = now - ORPHAN_PACK_AGE_S
    idxless = {n[:-5] for n in names if n.startswith("pack-") and n.endswith(".pack") and n[:-5] + ".idx" not in names}
    found: list[tuple[str, str]] = []
    for name in sorted(names):
        if _is_tmp_pack_name(name):
            found.append((name, ""))
        elif name.startswith("pack-"):
            stem, _, ext = name.rpartition(".")
            if ext == "pack" and stem in idxless:
                found.append((name, stem + ".idx"))
            elif ext == "rev" and (stem + ".pack" not in names or stem in idxless):
                found.append((name, stem + (".idx" if stem in idxless else ".pack")))
    out = []
    for name, partner in sorted(found, key=lambda t: t[0].endswith(".rev")):
        path = pack_dir / name
        try:
            st = path.stat()
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_mtime < cutoff:
            out.append((path, partner))
    return out


def sweep_orphan_pack_files(common_dir, now: Optional[float] = None) -> int:
    """Unlink the pack dir's dead-writer leavings; returns files removed.

    Reaps `_orphan_pack_files`: temp packs, an idx-less `.pack`, a `.rev` without its
    `.pack`, each untouched for ORPHAN_PACK_AGE_S. Live `.pack`/`.idx`/`.rev` triplets,
    young files and unknown names stay. A partner that reappears between the listing and
    the unlink keeps its file. The caller holds the object-store lock. An aged
    `multi-pack-index.lock` is reaped even while `pack_deletion_pause` holds (it is no pack);
    pack files are unlinked only when unpaused. Nothing is unlinked while another process
    holds the fleet maintenance lock."""
    try:
        with fleet_maintenance_lock.hold(wait_s=0, holder_label="store_hygiene:orphan-sweep"):
            return _sweep_orphan_pack_files_locked(common_dir, now)
    except (fleet_maintenance_lock.FleetMaintenanceBusy, fleet_maintenance_lock.FleetLockHomeUnresolved):
        return 0


def _lock_unheld(path: Path) -> bool:
    """True when no process holds `path` open. Spawn-free probe: a rename away and back
    succeeds only for an unheld file on Windows; POSIX renames an open file, so there age
    alone is the evidence (a live midx write finishes in milliseconds)."""
    if os.name != "nt":
        return True
    probe = path.with_name(path.name + ".probe")
    try:
        os.rename(path, probe)
    except OSError:
        return False
    try:
        os.rename(probe, path)
    except OSError:
        _unlink_quiet(probe)
    return True


def _aged_midx_lock(pack_dir: Path, now: float, probe_held: bool = False) -> Optional[Path]:
    """The pack dir's `multi-pack-index.lock` when it is a regular file untouched for
    ORPHAN_PACK_AGE_S, else None. With `probe_held` (the sweep only) a lock some process
    holds is also None; read-only callers pass False so they never touch the file."""
    path = pack_dir / _MIDX_LOCK_NAME
    try:
        st = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_mtime >= now - ORPHAN_PACK_AGE_S:
        return None
    return path if not probe_held or _lock_unheld(path) else None


def _stale_midx_lock(pack_dir: Path, pack_count: int) -> Optional[dict]:
    """`{age_s, pack_count}` for an aged `multi-pack-index.lock`, else None. Age only, write-free:
    the held probe runs in the sweep alone."""
    now = time.time()
    lock = _aged_midx_lock(pack_dir, now)
    if lock is None:
        return None
    try:
        age = now - lock.stat().st_mtime
    except OSError:
        return None
    return {"age_s": int(age), "pack_count": pack_count}


def _sweep_orphan_pack_files_locked(common_dir, now: Optional[float]) -> int:
    pack_dir = Path(common_dir) / "objects" / "pack"
    now = time.time() if now is None else now
    removed = 0
    lock = _aged_midx_lock(pack_dir, now, probe_held=True)
    if lock is not None:
        removed += _unlink_quiet(lock)
    if pack_deletion_pause.paused():
        return removed
    names = _pack_dir_names(pack_dir)
    for path, partner in _orphan_pack_files(pack_dir, names, now):
        if partner and (pack_dir / partner).exists():
            continue
        removed += _unlink_quiet(path)
    return removed


def owed_by_threshold(common_dir) -> bool:
    """True when the store needs daily maintenance now, from stat calls alone (no spawn, no
    lock, no liveness read): packs the MIDX does not cover past STALE_PACK_SLACK, a loose
    estimate past STALE_LOOSE_SLACK, a MIDX naming a missing pack, an aged orphan pack file,
    or more small packs than CONSOLIDATE_MAX_PACKS."""
    common_dir = Path(common_dir)
    if midx_upkeep_owed(common_dir):
        return True
    s = status(common_dir)
    if s.loose_estimate > STALE_LOOSE_SLACK:
        return True
    pack_dir = common_dir / "objects" / "pack"
    names = _pack_dir_names(pack_dir)
    now = time.time()
    if _aged_midx_lock(pack_dir, now) is not None or _orphan_pack_files(pack_dir, names, now):
        return True
    return s.pack_count > CONSOLIDATE_MAX_PACKS and len(_small_packs(pack_dir, names)) > CONSOLIDATE_MAX_PACKS


_CLAIM_RELPATH = ("coordinator", "store-hygiene.claim")


def _record_failure(repo_root: Path, exc: BaseException) -> None:
    try:
        from coordinator_core.ops.ceremony.detached_spawn import record_child_failure

        record_child_failure(str(repo_root), __file__, exc=exc)
    except Exception:  # noqa: BLE001 -- the failure log is best-effort
        pass


def _liveness_stale(repo_root: Path) -> bool:
    from coordinator_core.ops.ceremony import housekeeping_liveness as hl

    return (
        hl.liveness_status(str(repo_root), [hl.GIT_MAINTENANCE])[hl.GIT_MAINTENANCE]
        == hl.STATUS_STALE
    )


def _owes_maintenance(repo_root: Path, common_dir: Path) -> bool:
    return owed_by_threshold(common_dir) or _liveness_stale(repo_root)


def _claim(common_dir: Path) -> bool:
    """O_EXCL claim of the daily tier. A claim younger than CLAIM_INTERVAL_S refuses; an
    older one is unlinked and re-created once. Two callers that both see the same stale
    claim can both win (one unlinks the other's fresh claim); the cost is a duplicate
    daily run, which `run_tier` tolerates."""
    path = Path(common_dir, *_CLAIM_RELPATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            return True
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime < CLAIM_INTERVAL_S:
                    return False
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                return False
    return False


def _release_claim(common_dir: Path) -> None:
    try:
        Path(common_dir, *_CLAIM_RELPATH).unlink()
    except OSError:
        pass


def _box_busy() -> bool:
    """True when the 1-minute CPU load is at or past BUSY_CPU_LOAD. An unreadable load is
    not busy. Child-only: the Windows read may sleep a sampling gap."""
    try:
        from coordinator_core.telemetry import host_sampler

        load = host_sampler.read_load().get("cpu_load")
    except Exception:  # noqa: BLE001 -- an unreadable load must not block maintenance
        return False
    return load is not None and load >= BUSY_CPU_LOAD


def _small_packs(pack_dir: Path, names: Optional[set[str]] = None) -> list[tuple[Path, Path]]:
    """`(idx, pack)` pairs under CONSOLIDATE_SIZE_FLOOR that are engine packs, smallest first.
    An engine pack never has a `.rev`, `.keep`, `.promisor`, `.mtimes` or `.bitmap` sibling, so a
    pack with one is git's and is never a source, whatever its size."""
    if names is None:
        names = _pack_dir_names(pack_dir)
    out = []
    for name in sorted(n for n in names if n.endswith(".idx") and n.startswith("pack-")):
        stem = name[:-4]
        if stem + ".pack" not in names or any(stem + ext in names for ext in _GIT_SIBLING_EXTS):
            continue
        try:
            size = (pack_dir / (stem + ".pack")).stat().st_size
        except OSError:
            continue
        if size < CONSOLIDATE_SIZE_FLOOR:
            out.append((size, pack_dir / name, pack_dir / (stem + ".pack")))
    out.sort(key=lambda t: t[0])
    return [(idx, pack) for _, idx, pack in out]


def _unlink_quiet(path: Path) -> bool:
    """Unlink `path`; True when it is gone. Git writes pack files read-only, which Windows
    refuses to unlink, so a refusal retries once after clearing the read-only bit."""
    for _ in range(2):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            try:
                os.chmod(path, stat.S_IWRITE)
            except OSError:
                pass
            time.sleep(0.05)
    return False


def consolidate_pack_batch(common_dir) -> int:
    """Merge the smallest packs into one with `git pack-objects --stdin-packs`; returns
    source packs removed.

    One bounded batch per call (CONSOLIDATE_BATCH_PACKS packs, CONSOLIDATE_GROUP_BYTES
    bytes), a no-op at or under CONSOLIDATE_MAX_PACKS small packs. The merged pack lands
    first; then the MIDX is unlinked (it would name the deleted sources, and git refuses to
    rewrite a stale one) and each source goes idx-first, so a source that cannot be unlinked
    (Windows handle) stays whole for a later call. A MIDX that cannot be unlinked leaves
    every source in place. The MIDX is rewritten under the same hold once the sources are
    gone, so it never names a deleted pack; a failed write leaves no MIDX, never a stale one.
    Lock order is fleet (outer), object-store (inner): the caller must already hold the fleet
    maintenance lock (as `run_tier` does), which this call re-enters with wait_s=0; calling it
    with only the object-store lock held inverts the order. Returns 0 with no git spawned
    while `pack_deletion_pause` holds, or while another process holds the fleet maintenance
    lock (nothing unlinked on a busy or unresolvable one)."""
    if pack_deletion_pause.paused():
        return 0
    try:
        with fleet_maintenance_lock.hold(wait_s=0, holder_label="store_hygiene:consolidate"):
            return _consolidate_locked(common_dir)
    except (fleet_maintenance_lock.FleetMaintenanceBusy, fleet_maintenance_lock.FleetLockHomeUnresolved):
        return 0


def _consolidate_locked(common_dir) -> int:
    from coordinator_core.git.run import run_git

    common_dir = Path(common_dir)
    pack_dir = common_dir / "objects" / "pack"
    small = _small_packs(pack_dir)
    if len(small) <= CONSOLIDATE_MAX_PACKS:
        return 0
    batch: list[tuple[Path, Path]] = []
    total = 0
    for idx, pack in small[:CONSOLIDATE_BATCH_PACKS]:
        try:
            size = pack.stat().st_size
        except OSError:
            continue
        if size < _MIN_PACK_BYTES:
            continue
        if batch and total + size > CONSOLIDATE_GROUP_BYTES:
            break
        batch.append((idx, pack))
        total += size
    if len(batch) < 2:
        return 0
    stdin = "".join(pack.name + "\n" for _, pack in batch).encode("ascii")
    res = run_git(
        ["pack-objects", "--stdin-packs", "-q", str(pack_dir / "pack")], cwd=str(common_dir), input=stdin
    )
    merged = "pack-" + res.stdout.strip()
    if res.returncode != 0 or not (pack_dir / (merged + ".idx")).exists():
        return 0
    midx = pack_dir / "multi-pack-index"
    if midx.exists() and not _unlink_quiet(midx):
        return 0
    removed = 0
    for idx, pack in batch:
        if idx.stem == merged:
            continue
        if _unlink_quiet(idx) and _unlink_quiet(pack):
            removed += 1
            _unlink_quiet(pack.with_suffix(".rev"))
    mw = run_git(["multi-pack-index", "write"], cwd=str(common_dir))
    if mw.returncode != 0:
        print(f"store_hygiene: multi-pack-index write failed rc={mw.returncode}: {mw.stderr.strip()}", file=sys.stderr)
    return removed


def _is_tmp_pack_name(name: str) -> bool:
    return _TMP_PACK_RE.match(name) is not None


_LAST_BATCH_RELPATH = ("coordinator", "store-hygiene.last-batch")


def _stamp_last_batch(common_dir: Path) -> None:
    path = Path(common_dir, *_LAST_BATCH_RELPATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        pass


def _batch_forced(common_dir) -> bool:
    """True when a consolidation batch is owed and load may no longer defer it: small packs
    past PACK_COUNT_FORCE, or past CONSOLIDATE_MAX_PACKS with the last heavy run (mtime of
    the last-batch stamp, absent = never) older than CONSOLIDATE_MAX_DEFER_S. Stat calls only."""
    common_dir = Path(common_dir)
    if status(common_dir).pack_count <= CONSOLIDATE_MAX_PACKS:
        return False
    small = len(_small_packs(common_dir / "objects" / "pack"))
    if small <= CONSOLIDATE_MAX_PACKS:
        return False
    if small > PACK_COUNT_FORCE:
        return True
    try:
        return time.time() - Path(common_dir, *_LAST_BATCH_RELPATH).stat().st_mtime > CONSOLIDATE_MAX_DEFER_S
    except OSError:
        return True


def _run_daily_outcome(repo: Path) -> tuple[int, bool]:
    """Run the daily tier; returns `(rc, deferred)`. `deferred` is True when the tier did not
    run for want of the box (busy with nothing cheap owed, or the tier deferred), so the
    caller retries soon; not-owed is not a deferral. Load defers only the heavy batch leg:
    a busy box still gets the light legs (sweep, MIDX heal and write) when `midx_upkeep_owed`,
    and the batch once `_batch_forced`. While pack deletion is paused the tier runs light."""
    try:
        from coordinator_core.ops.git_maintenance import run_tier

        common_dir = resolve_git_common_dir(str(repo))
        if not _owes_maintenance(repo, common_dir):
            return 0, False
        paused = pack_deletion_pause.paused()
        busy = _box_busy()
        forced = busy and not paused and _batch_forced(common_dir)
        if busy and not forced and not midx_upkeep_owed(common_dir):
            return 0, True
        heavy = not paused and (not busy or forced)
        result = run_tier(repo, "daily", heavy=heavy)
        if result.deferred:
            _release_claim(common_dir)
            return result.rc, True
        if heavy and not result.errors:
            _stamp_last_batch(common_dir)
        return result.rc, False
    except Exception as exc:  # noqa: BLE001
        _record_failure(repo, exc)
        return 1, False


def _run_daily(repo: Path) -> int:
    return _run_daily_outcome(repo)[0]


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "--repo" and argv[2] == "--daily":
        return _run_daily(Path(argv[1]))
    if len(argv) != 2 or argv[0] != "--repo":
        print("usage: python -m coordinator_core.git.store_hygiene --repo PATH [--daily]", file=sys.stderr)
        return 2
    print(json.dumps(status(resolve_git_common_dir(argv[1]))._asdict()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

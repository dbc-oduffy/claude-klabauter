"""Cheap, subprocess-free read of an object store's hygiene state, plus the constants
that decide when the engine owes the store maintenance.

`status` answers three questions from a few syscalls: how many packs exist, how many of
them the multi-pack-index covers, and roughly how many loose objects pile up. Callers
on the commit route compare it to the `STALE_*` slacks; they never run git here.

Invariants:
  - `status` spawns nothing and imports nothing from the ops surface (cold-start budget);
    only `maybe_refresh` imports ops modules, lazily, and spawns only through
    `detached_spawn.spawn_detached`.
  - `midx_pack_count` is None for no MIDX, a torn header, or an unreadable file; a
    caller treats None as "uncovered", never as an error.
  - `loose_estimate` is `entries in ONE sampled fan-out dir * 256`: coarse (multiples of
    256) by design, a trigger signal and not a count.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple, Optional

from coordinator_core.git.git_dir import resolve_git_common_dir

#: Uncovered packs (pack_count - midx_pack_count) past which the daily tier owes a MIDX write.
STALE_PACK_SLACK = 32
#: Estimated loose objects past which the daily tier owes a loose-packing leg.
STALE_LOOSE_SLACK = 512
#: Minimum seconds between claims of the daily tier by commit-route triggers.
CLAIM_INTERVAL_S = 3600

_SAMPLE_FANOUT = "00"
_LOOSE_NAME_LEN = 38
_MIDX_MAGIC = b"MIDX"
_MIDX_HEADER_LEN = 12


class StoreHygiene(NamedTuple):
    pack_count: int
    midx_pack_count: Optional[int]
    loose_dirs_sampled: int
    loose_estimate: int


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
    return StoreHygiene(
        pack_count=_count_suffix(pack_dir, ".idx"),
        midx_pack_count=_midx_pack_count(pack_dir / "multi-pack-index"),
        loose_dirs_sampled=1,
        loose_estimate=_loose_in_fanout(objects) * 256,
    )


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
    s = status(common_dir)
    if s.pack_count - (s.midx_pack_count or 0) > STALE_PACK_SLACK:
        return True
    if s.loose_estimate > STALE_LOOSE_SLACK:
        return True
    return _liveness_stale(repo_root)


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


def _fire_daily(repo_root: Path) -> bool:
    from coordinator_core.ops.ceremony.detached_spawn import spawn_detached

    engine = Path(__file__).resolve().parents[2]
    return spawn_detached(str(engine), __file__, ["--repo", str(repo_root), "--daily"])


def maybe_refresh(repo_root, common_dir) -> bool:
    """Commit-route trigger: when the store owes maintenance (MIDX lag, loose growth, or a
    stale git_maintenance stamp) and no claim is live, fire the daily tier detached.
    True only when this call fired it. Never raises."""
    try:
        repo_root, common_dir = Path(repo_root), Path(common_dir)
        if not _owes_maintenance(repo_root, common_dir):
            return False
        if not _claim(common_dir):
            return False
        fired = False
        try:
            fired = bool(_fire_daily(repo_root))
        finally:
            if not fired:
                _release_claim(common_dir)
        return fired
    except Exception as exc:  # noqa: BLE001 -- must never fail a landed commit
        _record_failure(Path(repo_root), exc)
        return False


def _run_daily(repo: Path) -> int:
    try:
        from coordinator_core.ops.git_maintenance import run_tier

        return run_tier(repo, "daily").rc
    except Exception as exc:  # noqa: BLE001
        _record_failure(repo, exc)
        return 1


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

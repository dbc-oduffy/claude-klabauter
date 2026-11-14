"""Object-read stall profile: where a slow `git_objects.read_object` waits, and what it costs.

Each case runs on a one-worker pool (the warm-pool stand-in) with a stall shim that blocks a
read on an Event, modelling the page-fault wait of the live stall dump (0.203s CPU over
10.258s elapsed). A concurrent trivial op is queued behind it. Per case it reports the
op-elapsed until the read returns or raises, process time (time.process_time), spawn count
(subprocess.Popen) and whether the concurrent op answered before the shim was released.
Process time and spawns are the verdict axes; elapsed only shows whether the wall held.

    python coordinator_core/benchmarks/object_read_stall_profile.py [--json] [--deadline 1.0]
    python coordinator_core/benchmarks/object_read_stall_profile.py --git-objects-file <path>

`--git-objects-file` runs the same cases against another copy of git_objects.py (a
`git show <ref>:...` export), which is how a committed-ref baseline is taken while the
working tree carries uncommitted edits.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.git import git_objects as _default_go  # noqa: E402
from coordinator_core.temp_layout import make_managed_temp_dir  # noqa: E402

BATCH_READS = 500
BIG_LOOSE_BYTES = 8 * 1024 * 1024
DELTA_BASE_BYTES = 60000
_CACHE_NAMES = (
    "_PACK_LISTING_CACHE",
    "_PACK_INDEXES_BY_LISTING",
    "_LAST_HIT_PACK",
    "_LOOKUP_SOURCES",
    "_MIDX_CACHE",
    "_PACK_INDEX_CACHE",
    "_OBJECT_CACHE",
    "_PACK_STALL_UNTIL",
)


def load_git_objects(path: str | None):
    """The module under test: the working-tree `git_objects`, or the file at `path`."""
    if path is None:
        return _default_go
    spec = importlib.util.spec_from_file_location("coordinator_core.git._git_objects_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def clear_caches(go) -> None:
    """Drops every module-level cache `go` holds, to model a cold read."""
    for name in _CACHE_NAMES:
        cache = getattr(go, name, None)
        if cache is not None:
            cache.clear()
    go._PACK_DEADLINE.at = None


def blob_sha(payload: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(payload) + payload).hexdigest()


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _entry_header(type_num: int, size: int) -> bytes:
    first = (type_num << 4) | (size & 0x0F)
    size >>= 4
    out = bytearray()
    while size:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    return bytes(out)


def _copy_all_then_insert(base_len: int, tail: bytes) -> bytes:
    """A git delta stream that copies the whole base from offset 0 and appends `tail`."""
    ops = bytearray(_varint(base_len) + _varint(base_len + len(tail)))
    size_bytes = base_len.to_bytes(3, "little")
    opcode = 0x80
    body = bytearray()
    for i, b in enumerate(size_bytes):
        if b:
            opcode |= 0x10 << i
            body.append(b)
    ops += bytes([opcode]) + bytes(body)
    ops += bytes([len(tail)]) + tail
    return bytes(ops)


def write_loose(common_dir: Path, payload: bytes) -> tuple[str, Path]:
    sha = blob_sha(payload)
    path = common_dir / "objects" / sha[:2] / sha[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zlib.compress(b"blob %d\0" % len(payload) + payload, 1))
    return sha, path


def _place(go, common_dir: Path, entries) -> Path:
    pack, idx, name = go._assemble_pack(entries)
    pack_dir = common_dir / "objects" / "pack"
    go._place_pack(pack_dir, pack, idx, name)
    return pack_dir / f"pack-{name}.pack"


def build_fixtures(go, root: Path) -> SimpleNamespace:
    """One repo holding every case's objects, plus a second repo whose pack listing carries a
    stranded `.tmp-` pair (the `_temp_pair_is_complete` path). All shas are distinct."""
    repo = root / "repo.git"
    pack_payload = b"stalled-pack-blob\n" * 40
    pack_sha = blob_sha(pack_payload)
    pack_path = _place(go, repo, [(bytes.fromhex(pack_sha), (e := go._pack_entry(b"blob", pack_payload)), zlib.crc32(e))])

    base_payload = bytes((i * 7 + 3) & 0xFF for i in range(DELTA_BASE_BYTES))
    base_sha, base_path = write_loose(repo, base_payload)
    tail = b"delta-tail-edit\n"
    delta_payload = base_payload + tail
    delta_sha = blob_sha(delta_payload)
    delta = _copy_all_then_insert(len(base_payload), tail)
    delta_entry = _entry_header(7, len(delta)) + bytes.fromhex(base_sha) + zlib.compress(delta)
    _place(go, repo, [(bytes.fromhex(delta_sha), delta_entry, zlib.crc32(delta_entry))])

    big_payload = _spread(BIG_LOOSE_BYTES)
    big_sha, big_path = write_loose(repo, big_payload)

    batch_payloads = {blob_sha(p): p for p in (b"batch-object-%d\n" % i for i in range(BATCH_READS))}
    batch_pack = _place(
        go,
        repo,
        [(bytes.fromhex(s), (e := go._pack_entry(b"blob", p)), zlib.crc32(e)) for s, p in batch_payloads.items()],
    )

    listing = root / "listing.git"
    listing_payload = b"listing-stall-blob\n"
    listing_sha = blob_sha(listing_payload)
    _place(go, listing, [(bytes.fromhex(listing_sha), (e := go._pack_entry(b"blob", listing_payload)), zlib.crc32(e))])
    pack_dir = listing / "objects" / "pack"
    stranded = hashlib.sha1(b"stranded").hexdigest()
    (pack_dir / f".tmp-1-pack-{stranded}.idx").write_bytes(b"\xfftOc\x00\x00\x00\x02" + b"\0" * 1100)
    (pack_dir / f".tmp-1-pack-{stranded}.pack").write_bytes(b"PACK" + b"\0" * 40)

    return SimpleNamespace(
        common_dir=repo,
        pack_sha=pack_sha,
        pack_payload=pack_payload,
        pack_path=pack_path,
        base_sha=base_sha,
        base_path=base_path,
        base_payload=base_payload,
        delta_sha=delta_sha,
        delta_payload=delta_payload,
        big_sha=big_sha,
        big_path=big_path,
        big_size=len(big_payload),
        batch_shas=list(batch_payloads),
        batch_pack=batch_pack,
        listing_common=listing,
        listing_sha=listing_sha,
    )


def _spread(length: int) -> bytes:
    """Incompressible-ish bytes: a loose blob like a binary asset, whose zlib stream is as
    large as the payload."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(b"big-%d" % counter).digest()
        counter += 1
    return bytes(out[:length])


class Stall:
    """Blocks a read on an Event: `pack_path` blocks `_pack_bytes_for_read`, `read_path` blocks
    `Path.read_bytes`, `listing` blocks `_temp_pair_is_complete`. Auto-releases after `hold`."""

    def __init__(self, go, *, hold: float, pack_path: Path | None = None, read_path: Path | None = None, listing: bool = False):
        self._go = go
        self._pack_path = pack_path
        self._read_path = read_path
        self._listing = listing
        self.hold = hold
        self.release = threading.Event()
        self.entered = threading.Event()
        self.released_at: float | None = None
        self._undo: list = []

    def _block(self) -> None:
        self.entered.set()
        self.release.wait(self.hold)

    def release_now(self) -> None:
        if self.released_at is None:
            self.released_at = time.monotonic()
        self.release.set()

    def __enter__(self) -> "Stall":
        go = self._go
        if self._pack_path is not None:
            orig = go._pack_bytes_for_read
            target = self._pack_path

            def pack_bytes(p, _orig=orig):
                if Path(p) == target:
                    self._block()
                return _orig(p)

            self._undo.append(lambda: setattr(go, "_pack_bytes_for_read", orig))
            go._pack_bytes_for_read = pack_bytes
        if self._read_path is not None:
            orig_rb = Path.read_bytes
            target_rb = self._read_path

            def read_bytes(path_self, _orig=orig_rb):
                if path_self == target_rb:
                    self._block()
                return _orig(path_self)

            self._undo.append(lambda: setattr(Path, "read_bytes", orig_rb))
            Path.read_bytes = read_bytes
        if self._listing:
            orig_tp = go._temp_pair_is_complete

            def temp_pair(idx_path, pack_path, _orig=orig_tp):
                self._block()
                return _orig(idx_path, pack_path)

            self._undo.append(lambda: setattr(go, "_temp_pair_is_complete", orig_tp))
            go._temp_pair_is_complete = temp_pair
        return self

    def __exit__(self, *exc) -> None:
        self.release_now()
        for undo in reversed(self._undo):
            undo()


class InflateSpy:
    """Counts bytes produced by `zlib.decompress` and `zlib.decompressobj().decompress`."""

    def __init__(self) -> None:
        self.bytes_out = 0

    def __enter__(self) -> "InflateSpy":
        spy = self
        self._decompress = zlib.decompress
        self._decompressobj = zlib.decompressobj

        class _Obj:
            def __init__(self, *a, **k):
                self._d = spy._decompressobj(*a, **k)

            def decompress(self, data, *a):
                out = self._d.decompress(data, *a)
                spy.bytes_out += len(out)
                return out

            def __getattr__(self, name):
                return getattr(self._d, name)

        def decompress(data, *a, **k):
            out = spy._decompress(data, *a, **k)
            spy.bytes_out += len(out)
            return out

        zlib.decompress = decompress
        zlib.decompressobj = _Obj
        return self

    def __exit__(self, *exc) -> None:
        zlib.decompress = self._decompress
        zlib.decompressobj = self._decompressobj


class _Counts:
    """Process spawns (Popen) and run_bounded wall threads started while active."""

    def __init__(self) -> None:
        self.spawns = 0
        self.wall_threads = 0

    def __enter__(self) -> "_Counts":
        counts = self
        self._popen_init = subprocess.Popen.__init__
        self._thread_start = threading.Thread.start

        def popen_init(p_self, *a, **k):
            counts.spawns += 1
            return counts._popen_init(p_self, *a, **k)

        def thread_start(t_self):
            if t_self.name.startswith("bounded-io:"):
                counts.wall_threads += 1
            return counts._thread_start(t_self)

        subprocess.Popen.__init__ = popen_init
        threading.Thread.start = thread_start
        return self

    def __exit__(self, *exc) -> None:
        subprocess.Popen.__init__ = self._popen_init
        threading.Thread.start = self._thread_start


def _timed(work):
    try:
        out = work()
        outcome = "returned"
    except BaseException as exc:  # noqa: BLE001 - the outcome is the datum
        out, outcome = None, f"raised:{type(exc).__name__}"
    return outcome, time.monotonic(), out


def measure(go, case: str, work, *, deadline: float, stall: Stall | None = None) -> dict:
    """Run `work` on a one-worker pool with a concurrent trivial op queued behind it."""
    clear_caches(go)
    hold = stall.hold if stall is not None else 30.0
    pool = ThreadPoolExecutor(max_workers=1)
    row: dict = {"case": case}
    try:
        with _Counts() as counts:
            cpu0 = time.process_time()
            t0 = time.monotonic()
            fut = pool.submit(_timed, work)
            probe = pool.submit(time.monotonic)
            wait([fut], timeout=hold)
            finished_in_hold = fut.done()
            cpu = time.process_time() - cpu0
            if stall is not None:
                if finished_in_hold:
                    probe_at = probe.result(timeout=5.0)
                    stall.release_now()
                    row["concurrent_op_answered_during_stall"] = probe_at < stall.released_at
                else:
                    stall.release_now()
                    row["concurrent_op_answered_during_stall"] = False
        outcome, t_end, _ = fut.result(timeout=hold + 30.0)
        row["outcome"] = outcome if finished_in_hold else f"blocked past hold; then {outcome}"
        row["op_elapsed_s"] = round((t_end - t0) if finished_in_hold else hold, 3)
        row["bounded_by_one_wall"] = bool(
            finished_in_hold and outcome == "raised:PackReadDeadline" and (t_end - t0) <= deadline + 0.5
        )
        row["process_ms"] = round(cpu * 1000.0, 1)
        row["spawns"] = counts.spawns
        row["wall_threads"] = counts.wall_threads
    finally:
        if stall is not None:
            stall.release_now()
        pool.shutdown(wait=True)
    return row


def _inflate_cost(go, fn, repeats: int = 8) -> tuple[float, int, object]:
    """Per-call process ms and inflated bytes of `fn`, averaged over cold repeats
    (process_time ticks at ~15.6ms on Windows, so one inflate reads as zero)."""
    out = None
    with InflateSpy() as spy:
        cpu0 = time.process_time()
        for _ in range(repeats):
            clear_caches(go)
            out = fn()
        cpu = time.process_time() - cpu0
    return cpu * 1000.0 / repeats, spy.bytes_out // repeats, out


def profile(go, deadline: float, scratch: Path) -> dict:
    go.PACK_READ_DEADLINE_SECS = deadline
    fx = build_fixtures(go, scratch)
    common = fx.common_dir
    hold = deadline * 3
    rows = []

    with Stall(go, hold=hold, pack_path=fx.pack_path) as s:
        rows.append(measure(go, "stalled_pack", lambda: go.read_object(common, fx.pack_sha), deadline=deadline, stall=s))
    with Stall(go, hold=hold, read_path=fx.base_path) as s:
        rows.append(measure(go, "stalled_ref_delta_base", lambda: go.read_object(common, fx.delta_sha), deadline=deadline, stall=s))
    with Stall(go, hold=hold, read_path=fx.big_path) as s:
        rows.append(measure(go, "large_loose_stalled", lambda: go.read_object(common, fx.big_sha), deadline=deadline, stall=s))
    with Stall(go, hold=hold, listing=True) as s:
        rows.append(
            measure(go, "packed_contains_listing_stalled", lambda: go.packed_contains(fx.listing_common, fx.listing_sha), deadline=deadline, stall=s)
        )

    ms, inflated, got = _inflate_cost(go, lambda: go.read_object(common, fx.big_sha))
    rows.append(
        {
            "case": "large_loose_read_unstalled",
            "outcome": "returned" if got is not None else "missing",
            "payload_bytes": fx.big_size,
            "process_ms": round(ms, 1),
            "inflated_bytes": inflated,
            "spawns": 0,
        }
    )

    header = getattr(go, "read_object_header", None)
    if header is None:
        rows.append({"case": "header_only_probe", "outcome": "not_built", "note": "read_object_header absent; callers needing size/type pay the large_loose_read_unstalled row"})
    else:
        ms, inflated, got = _inflate_cost(go, lambda: header(common, fx.big_sha))
        rows.append(
            {
                "case": "header_only_probe",
                "outcome": "returned" if got == ("blob", fx.big_size) else f"wrong:{got!r}",
                "process_ms": round(ms, 1),
                "inflated_bytes": inflated,
                "spawns": 0,
            }
        )

    clear_caches(go)
    with _Counts() as counts:
        cpu0 = time.process_time()
        t0 = time.monotonic()
        ok = all(go.read_object(common, s) is not None for s in fx.batch_shas)
        cpu = time.process_time() - cpu0
        elapsed = time.monotonic() - t0
    rows.append(
        {
            "case": "unstalled_batch",
            "outcome": "returned" if ok else "missing",
            "reads": len(fx.batch_shas),
            "process_ms": round(cpu * 1000.0, 1),
            "process_us_per_read": round(cpu * 1e6 / len(fx.batch_shas), 1),
            "op_elapsed_s": round(elapsed, 3),
            "spawns": counts.spawns,
            "wall_threads": counts.wall_threads,
        }
    )
    return {
        "deadline_secs": deadline,
        "wall_in_tree": hasattr(go, "_PACK_WORKER"),
        "git_objects_file": getattr(go, "__file__", None),
        "cases": rows,
    }


def _print(report: dict) -> None:
    print(f"deadline_secs={report['deadline_secs']} wall_in_tree={report['wall_in_tree']}")
    print(f"git_objects_file={report['git_objects_file']}")
    for row in report["cases"]:
        print("  " + " ".join(f"{k}={v}" for k, v in row.items()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--deadline", type=float, default=1.0, help="PACK_READ_DEADLINE_SECS for the run (production: 4.0)")
    ap.add_argument("--git-objects-file", default=None, help="run against this git_objects.py instead of the working tree")
    args = ap.parse_args(argv)
    go = load_git_objects(args.git_objects_file)
    tmp = make_managed_temp_dir(prefix="object-read-stall-")
    saved = go.PACK_READ_DEADLINE_SECS
    try:
        report = profile(go, args.deadline, tmp)
    finally:
        go.PACK_READ_DEADLINE_SECS = saved
        rmtree_or_raise(tmp, label="object-read-stall")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

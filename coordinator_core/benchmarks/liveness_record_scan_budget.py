"""Liveness of ~50 held runs with 100MB+ owner transcripts is decided from run records, with no transcript read.

Fixture: 50 held run tokens. 25 sit at stage `minted` past `warp_live.MINTED_RUNNER_GRACE_SECONDS` with a
submission that names a `transcript_path`; 25 carry a `halted.json`. Every owner transcript is a 100MiB+
file made by truncate (zeros, no content written). Three surfaces are timed with `time.process_time`
at default arguments, and every `open` of a transcript path is counted with `sys.addaudithook`:

  (a) `warp_run_deliverable.live_run_write_claims`
  (b) `ask_stage._refuse_live_write_overlap`
  (c) `land_run._halted_only_release` then `land_run._peer_held`

Run: `python -m coordinator_core.benchmarks.liveness_record_scan_budget`.

BASELINE (base 6d06c8d711, Windows 11, Intel Ultra 9 285K; transcript opens per surface call and
process time; 50 held runs, 25 minted + 25 halted, 100MiB transcripts):
    live_run_write_claims            opens=50  process_ms=32922
    _refuse_live_write_overlap       opens=50  process_ms=34688
    _halted_only_release+_peer_held  opens=50  process_ms=36234
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional
from unittest import mock

BAR_MS = 500.0
HELD_RUNS = 50
MINTED_RUNS = 25
HALTED_RUNS = HELD_RUNS - MINTED_RUNS
TRANSCRIPT_BYTES = 100 * 1024 * 1024 + 1
SHARED_PATH = "src/shared/contended.txt"
SURFACES = ("live_run_write_claims", "refuse_live_write_overlap", "halted_release_and_peer_held")
_SIZING = "state/sizings/liveness-budget.yaml"
_SCRIPT = "scratch/warp/liveness-budget.mjs"
_LANDING_RUN = "ask-20261103T120000-ffffff"
_PAST_GRACE_SECONDS = 300


_WATCHED: Optional[set] = None
_OPENS: list = []
_HOOK_INSTALLED = False


def _key(path: object) -> Optional[str]:
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, (str, os.PathLike)):
        return None
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _audit(event: str, args: tuple) -> None:
    if event != "open" or _WATCHED is None or not args:
        return
    key = _key(args[0])
    if key is not None and key in _WATCHED:
        _OPENS.append(key)


@contextlib.contextmanager
def counting_opens(paths) -> Iterator[list]:
    """Yield the list that collects each `open` event whose path is one of `paths`.
    Trap: an audit hook cannot be removed, so it is installed once and armed only inside this block."""
    global _WATCHED, _HOOK_INSTALLED
    if not _HOOK_INSTALLED:
        sys.addaudithook(_audit)
        _HOOK_INSTALLED = True
    _OPENS.clear()
    _WATCHED = {k for k in (_key(p) for p in paths) if k is not None}
    try:
        yield _OPENS
    finally:
        _WATCHED = None


@dataclass
class Fixture:
    root: Path
    transcripts: list
    minted: list
    halted: list


@dataclass
class Row:
    surface: str
    process_ms: float
    transcript_opens: int
    result: object


def _make_transcripts(directory: Path, count: int) -> list:
    """`count` distinct 100MiB+ files: one truncated file, hard-linked under the other names when the
    filesystem allows, else truncated per name."""
    directory.mkdir(parents=True, exist_ok=True)
    first = directory / "transcript-0.jsonl"
    with open(first, "wb") as handle:
        handle.truncate(TRANSCRIPT_BYTES)
    paths = [first]
    for i in range(1, count):
        path = directory / f"transcript-{i}.jsonl"
        try:
            os.link(first, path)
        except OSError:
            with open(path, "wb") as handle:
                handle.truncate(TRANSCRIPT_BYTES)
        paths.append(path)
    return paths


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def build_fixture(root: Path, scratch: Path, held: int = HELD_RUNS) -> Fixture:
    """`held` tokens, half minted and half halted, under `root` (a bare repo: `.git` dir only),
    transcripts under `scratch`."""
    from coordinator_core import warp_live
    from coordinator_core.ops.dispatch_emit import halted_run, run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, record_dir

    (root / ".git").mkdir(parents=True, exist_ok=True)
    transcripts = _make_transcripts(scratch / "transcripts", held)
    run_ids = [f"ask-20261103T120000-{i:06x}" for i in range(held)]
    minted, halted = run_ids[: held // 2], run_ids[held // 2:]
    for i, run_id in enumerate(run_ids):
        sizing = f"state/sizings/liveness-budget-{i}.yaml"
        run_token.mint(root, run_id, _SCRIPT, session_id=f"session-{i}", sizing_rel=sizing)
        record_path = run_token._record_path(root, run_id)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["minted_at"] = time.time() - _PAST_GRACE_SECONDS
        record_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        warp_live.record_submission(
            root, run_id, session_id=f"session-{i}", transcript_path=str(transcripts[i]),
            script_path=_SCRIPT, sizing_rel=sizing,
        )
        _write_json(
            root / RUN_DIR_ROOT / run_id / "manifest.json", {"rows": [{"id": "C0", "writes": [SHARED_PATH]}]}
        )
        _write_json(record_dir(root, run_id) / "manifest.json", {"rows": [{"id": "C0", "writes": [SHARED_PATH]}]})
        if run_id in minted:
            _write_json(record_dir(root, run_id) / "run-stage.json", {"stage": "minted"})
        else:
            halted_run.write_halt_marker(record_dir(root, run_id), halted="commit", sizing_rel=sizing)
    halted_run.write_halt_marker(record_dir(root, _LANDING_RUN), halted="commit", sizing_rel=_SIZING)
    return Fixture(root=root, transcripts=transcripts, minted=minted, halted=halted)


@contextlib.contextmanager
def live_environment(scratch: Path) -> Iterator[None]:
    """Settings home, launch dirs and session liveness fixed so every minted run reads as a live peer's."""
    from coordinator_core import warp_live
    from coordinator_core.session import touch_record

    managed, legacy = scratch / "managed", scratch / "legacy"
    managed.mkdir(parents=True, exist_ok=True)
    legacy.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, {"COORDINATOR_SETTINGS_HOME": str(scratch / "settings")}))
        stack.enter_context(mock.patch.object(warp_live, "managed_tempdir", lambda: managed))
        stack.enter_context(mock.patch.object(warp_live, "legacy_top_level_path", lambda name: legacy / name))
        stack.enter_context(mock.patch.object(touch_record, "session_live", lambda sid, cwd=None: True))
        stack.enter_context(mock.patch.object(warp_live, "_owner_exited", lambda session_id, minted_at: False))
        yield


def _measure(surface: str, fixture: Fixture, call) -> Row:
    with counting_opens(fixture.transcripts) as opens:
        start = time.process_time()
        result = call()
        elapsed = (time.process_time() - start) * 1000.0
    return Row(surface, round(elapsed, 1), len(opens), result)


def measure_surface(surface: str, fixture: Fixture) -> Row:
    """One call of `surface` at default arguments. A surface can consume tokens or write halt markers,
    so each is measured on a fixture of its own."""
    from coordinator_core.ops import warp_run_deliverable
    from coordinator_core.ops.dispatch_emit import ask_stage, land_run

    root = fixture.root

    def claims():
        return warp_run_deliverable.live_run_write_claims(root, [SHARED_PATH], exclude_session="")

    def overlap():
        try:
            ask_stage._refuse_live_write_overlap(root, "ask-20261103T120000-fffffe", [SHARED_PATH])
        except ask_stage.AskStageError as exc:
            return str(exc)
        return None

    def release_and_peer_held():
        released = land_run._halted_only_release(
            root, _LANDING_RUN, None, {SHARED_PATH: fixture.halted[0]}, [SHARED_PATH], None
        )
        held = land_run._peer_held(root, [SHARED_PATH], "landing-session", _LANDING_RUN, _SCRIPT)
        return released, held

    calls = {
        "live_run_write_claims": claims,
        "refuse_live_write_overlap": overlap,
        "halted_release_and_peer_held": release_and_peer_held,
    }
    return _measure(surface, fixture, calls[surface])


def measure_all(base: Path) -> tuple:
    """`(fixtures by surface, rows by surface)` for the three surfaces, each on a fresh fixture under `base`.
    Each surface first runs once on a two-run fixture so first-call imports are not billed to the
    measured call: the engine the surfaces serve is warm."""
    fixtures: dict = {}
    rows: dict = {}
    with live_environment(base):
        for surface in SURFACES:
            measure_surface(surface, build_fixture(base / "warm" / surface / "repo", base / "warm" / surface, held=2))
        for surface in SURFACES:
            fixtures[surface] = build_fixture(base / surface / "repo", base / surface)
            rows[surface] = measure_surface(surface, fixtures[surface])
    return fixtures, rows


def run_benchmark(base: Optional[Path] = None) -> dict:
    """Measure the three surfaces under `base` (a fresh temp dir when None, removed after)."""
    owned = base is None
    base = Path(tempfile.mkdtemp(prefix="liveness-budget-")) if owned else Path(base)
    try:
        return measure_all(base)[1]
    finally:
        if owned:
            import shutil

            shutil.rmtree(base, ignore_errors=True)


def main() -> int:
    rows = run_benchmark()
    failed = False
    for surface in SURFACES:
        row = rows[surface]
        bad = row.transcript_opens > 0 or row.process_ms >= BAR_MS
        failed = failed or bad
        print(f"{surface:34s} opens={row.transcript_opens:4d} process_ms={row.process_ms:9.1f}  {'OVER' if bad else 'ok'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

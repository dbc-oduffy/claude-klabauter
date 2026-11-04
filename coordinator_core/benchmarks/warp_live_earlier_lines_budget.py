"""A repeat per-hook liveness check on a 64MiB transcript with a live run stays under the 200ms bar.

Fixture: one session with one live run token and a launch record; its transcript is a 64MiB JSONL
file whose launch notification sits in the head window and whose 4MiB tail holds no terminal
notification, so `live_run_for_session` reaches `_earlier_lines`. Three calls are timed with
`time.process_time` at default arguments:

  cold    the first call (scans the head window)
  repeat  the same call again on the unchanged file (the per-hook case)
  append  one call after a 64KiB append (scans only the appended bytes)

Run: `python -m coordinator_core.benchmarks.warp_live_earlier_lines_budget`.

BASELINE (before the memo, Windows 11, Intel Ultra 9 285K): every call re-streamed the 60MiB head
window: repeat ~ cold. END (same box): cold 62.5ms, repeat 0.0ms, append 0.0ms process time (Windows
process_time ticks at ~15.6ms); repeat reads the 4MiB tail and a 64-byte probe only.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator, Optional
from unittest import mock

MANIFEST_KEY = "warp_live.live_run_for_session_repeat"
TRANSCRIPT_BYTES = 64 * 1024 * 1024
APPEND_BYTES = 64 * 1024
SESSION = "bench-session"
TASK = "wbenchtask1"
SCRIPT = "scratch/warp/bench.mjs"
RUN_ID = "ask-20261104T000000-bench01"


def _line(i: int, width: int = 180) -> bytes:
    return (json.dumps({"type": "assistant", "n": i, "text": "x" * width}) + "\n").encode()


def _write_transcript(path: Path, size: int) -> None:
    """A JSONL file of about `size` bytes: a launch line naming TASK near the start, filler after."""
    block = b"".join(_line(i) for i in range(4096))
    launch = (json.dumps({"type": "user", "note": f"<task-notification><task-id>{TASK}</task-id>"
                          "<status>running</status></task-notification>"}) + "\n").encode()
    with open(path, "wb") as handle:
        handle.write(launch)
        written = len(launch)
        while written < size:
            handle.write(block)
            written += len(block)


def build_fixture(base: Path, size: int = TRANSCRIPT_BYTES) -> tuple:
    """`(repo root, transcript path)` with one live run of SESSION whose task never ends."""
    from coordinator_core import launch_records, warp_live
    from coordinator_core.ops.dispatch_emit import run_token

    root = base / "repo"
    (root / ".git").mkdir(parents=True, exist_ok=True)
    managed = base / "managed"
    managed.mkdir(parents=True, exist_ok=True)
    transcript = base / "transcript.jsonl"
    _write_transcript(transcript, size)
    run_token.mint(root, RUN_ID, SCRIPT, session_id=SESSION)
    warp_live.record_submission(
        root, RUN_ID, session_id=SESSION, transcript_path=str(transcript), script_path=SCRIPT, sizing_rel=None
    )
    record = launch_records.record_path(str(managed), SESSION, TASK)
    Path(record).write_text(json.dumps({"scriptPath": SCRIPT, "fired_at": time.time()}), encoding="utf-8")
    return root, transcript


@contextlib.contextmanager
def live_environment(base: Path) -> Iterator[None]:
    """Settings home and launch dirs fixed under `base`; the owner reads as alive."""
    from coordinator_core import warp_live

    managed, legacy = base / "managed", base / "legacy"
    managed.mkdir(parents=True, exist_ok=True)
    legacy.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, {"COORDINATOR_SETTINGS_HOME": str(base / "settings")}))
        stack.enter_context(mock.patch.object(warp_live, "managed_tempdir", lambda: managed))
        stack.enter_context(mock.patch.object(warp_live, "legacy_top_level_path", lambda name: legacy / name))
        stack.enter_context(mock.patch.object(warp_live, "_owner_exited", lambda session_id, minted_at: False))
        yield


def _timed(root: Path) -> tuple:
    from coordinator_core import warp_live

    start = time.process_time()
    live = warp_live.live_run_for_session(root, SESSION)
    return round((time.process_time() - start) * 1000.0, 1), live


def run_benchmark(base: Optional[Path] = None, size: int = TRANSCRIPT_BYTES) -> dict:
    """`{cold, repeat, append}` rows of `(process_ms, live run id)` under `base` (a temp dir when None)."""
    owned = base is None
    base = Path(tempfile.mkdtemp(prefix="warp-live-earlier-")) if owned else Path(base)
    try:
        with live_environment(base):
            warm_root, _ = build_fixture(base / "warm", size=2 * 1024 * 1024)
            _timed(warm_root)
            root, transcript = build_fixture(base / "main", size=size)
            rows = {"cold": _timed(root), "repeat": _timed(root)}
            with open(transcript, "ab") as handle:
                handle.write(b"".join(_line(i) for i in range(APPEND_BYTES // 200)))
            rows["append"] = _timed(root)
        return rows
    finally:
        if owned:
            shutil.rmtree(base, ignore_errors=True)


def manifest_bar_ms() -> float:
    """`target_ms * (1 + tolerance)` of the manifest row; the gate reads it so the row is enforced."""
    manifest = json.loads((Path(__file__).with_name("budget-manifest.json")).read_text(encoding="utf-8"))
    row = manifest["overrides"][MANIFEST_KEY]
    return float(row["target_ms"]) * (1.0 + float(row["tolerance"]["value"]))


def main() -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    bar_ms = manifest_bar_ms()
    rows = run_benchmark()
    failed = False
    for name in ("cold", "repeat", "append"):
        ms, live = rows[name]
        bad = live != RUN_ID or (name != "cold" and ms >= bar_ms)
        failed = failed or bad
        print(f"{name:8s} process_ms={ms:9.1f} live={live}  {'OVER' if bad else 'ok'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    sys.exit(main())

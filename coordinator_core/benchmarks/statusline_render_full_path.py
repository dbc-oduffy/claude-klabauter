"""Driver for one `coordinator/bin/statusline.py` render, measured as a process tree.

    python -m coordinator_core.benchmarks.statusline_render_full_path <dir> --prepare
    python -m coordinator_core.benchmarks.statusline_render_full_path <dir> --mode MODE [--setup-only]

`--prepare` builds a fixture session under <dir> (main transcript, one subagent
transcript, one workflow agent transcript, ~6.6 MB, over the byte budget) and
converges a cache for it. It is not measured. `--mode` re-creates the cache state
the mode needs, then runs the statusline with a payload on stdin; `--setup-only`
stops before the render, so the render's cost is `full - setup-only`.

Modes:
  baseline   payload without transcript_path: the line with no cost field.
  throttled  converged cache, scanned_at = now: a render reads only the cache file.
  append     converged cache past the throttle, one row appended: a rescan plus a tail parse.
  backlog    no cache: one render absorbs a byte budget of the fixture and prints `+`.
  real-cold / real-throttled / real-rescan   the same states against a real session
             given by --transcript (prepare with --transcript converges it first).

The statusline runs in-process via runpy, as a render does inside the warm door;
a field mode exits non-zero unless the printed line carries the `~$N.NN` segment,
so a timing is never taken from a render that dropped the field.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import runpy
import sys
import time
from pathlib import Path

from coordinator_core.benchmarks import declare_benchmark_origin

SESSION_ID = "2b6c1f5e-4d7a-4e1b-9c3a-5f8d2e7a9b10"
MODES = ("baseline", "throttled", "append", "backlog", "real-cold", "real-throttled", "real-rescan")
_PAD = "x" * 1700
_STATUSLINE = Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "statusline.py"
_FIELD = re.compile(r"~\$\d+\.\d\d\+?")


def _row(i: int, model: str) -> str:
    usage = {
        "input_tokens": 120 + i,
        "output_tokens": 800 + i,
        "cache_read_input_tokens": 40000 + i,
        "cache_creation_input_tokens": 900,
    }
    event = {
        "type": "assistant",
        "requestId": f"req_{model[:8]}_{i}",
        "message": {"id": f"msg_{model[:8]}_{i}", "model": model, "usage": usage, "pad": _PAD},
    }
    return json.dumps(event) + "\n"


def _write_rows(path: Path, count: int, model: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for i in range(count):
            handle.write(_row(i, model))


def _session_paths(workdir: Path) -> tuple[Path, Path]:
    projects = workdir / "projects"
    return projects / f"{SESSION_ID}.jsonl", projects / SESSION_ID


def _cache_path(workdir: Path, session_id: str) -> Path:
    return workdir / "settings" / "state" / "session-cost" / f"{session_id}.json"


def _cost_module():
    from coordinator_core.usage import session_cost

    return session_cost


def _prepare(workdir: Path, transcript: Path | None) -> None:
    os.environ["COORDINATOR_SETTINGS_HOME"] = str(workdir / "settings")
    if transcript is None:
        main, session_dir = _session_paths(workdir)
        _write_rows(main, 2000, "claude-opus-5-5")
        _write_rows(session_dir / "subagents" / "agent-aaa.jsonl", 800, "claude-sonnet-5-5")
        _write_rows(session_dir / "subagents" / "workflows" / "wf_x" / "agent-bbb.jsonl", 500, "claude-haiku-5-5")
        transcript = main
    sizes = {}
    cost = _cost_module()
    cache_path = _cache_path(workdir, transcript.stem)
    cache_path.unlink(missing_ok=True)
    result = cost.advance(str(transcript), cache_path, now=time.time(), byte_budget=1 << 62)
    if not result["complete"] or not result["files"]:
        raise SystemExit(f"prepare: cache did not converge: {result!r}")
    primed = json.loads(cache_path.read_text(encoding="utf-8"))
    for rel, entry in primed["files"].items():
        sizes[rel] = entry["offset"]
    (workdir / "primed.json").write_text(json.dumps(primed), encoding="utf-8")
    (workdir / "meta.json").write_text(
        json.dumps({"transcript": str(transcript), "sizes": sizes, "usd": result["usd"]}), encoding="utf-8"
    )
    print(f"prepared {len(sizes)} files, usd={result['usd']:.2f}")


def _restore_fixture(meta: dict, transcript: Path) -> None:
    base = transcript.parent
    for rel, size in meta["sizes"].items():
        path = base / rel
        if path.stat().st_size != size:
            os.truncate(path, size)


def _stage(workdir: Path, mode: str) -> tuple[str | None, str]:
    """Re-create the cache state `mode` measures; returns (transcript_path, session_id)."""
    meta = json.loads((workdir / "meta.json").read_text(encoding="utf-8"))
    transcript = Path(meta["transcript"])
    session_id = transcript.stem
    cache_path = _cache_path(workdir, session_id)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "baseline":
        return None, session_id
    fixture_owned = transcript.is_relative_to(workdir)
    if fixture_owned:
        _restore_fixture(meta, transcript)
    if mode in ("backlog", "real-cold"):
        cache_path.unlink(missing_ok=True)
        return str(transcript), session_id
    primed = json.loads((workdir / "primed.json").read_text(encoding="utf-8"))
    primed["scanned_at"] = time.time() if mode in ("throttled", "real-throttled") else 0.0
    cache_path.write_text(json.dumps(primed), encoding="utf-8")
    if mode == "append":
        if not fixture_owned:
            raise SystemExit("append mode writes a synthetic row; refused on a transcript outside the fixture")
        with open(transcript, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(_row(10**6, "claude-opus-5-5"))
    return str(transcript), session_id


def _render(workdir: Path, transcript_path: str | None, session_id: str) -> str:
    payload = {
        "session_id": session_id,
        "model": {"display_name": "Opus 5.5"},
        "context_window": {"used_percentage": 15, "context_window_size": 1000000},
    }
    if transcript_path:
        payload["transcript_path"] = transcript_path
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8")), encoding="utf-8")
    sys.stdout = captured = io.StringIO()
    try:
        namespace = runpy.run_path(str(_STATUSLINE), run_name="statusline_under_measurement")
        rc = namespace["main"]([])
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    if rc != 0:
        raise SystemExit(f"statusline main returned {rc}")
    return captured.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workdir")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--setup-only", action="store_true")
    parser.add_argument("--transcript")
    args = parser.parse_args(argv)
    declare_benchmark_origin()
    workdir = Path(args.workdir)
    os.environ["COORDINATOR_SETTINGS_HOME"] = str(workdir / "settings")
    if args.prepare:
        _prepare(workdir, Path(args.transcript) if args.transcript else None)
        return 0
    if not args.mode:
        parser.error("--mode or --prepare is required")
    transcript_path, session_id = _stage(workdir, args.mode)
    if args.setup_only:
        return 0
    line = _render(workdir, transcript_path, session_id)
    has_field = bool(_FIELD.search(line))
    if args.mode == "baseline" and has_field:
        raise SystemExit(f"baseline carries a cost field: {line!r}")
    if args.mode != "baseline" and not has_field:
        raise SystemExit(f"{args.mode}: no ~$N.NN field in {line!r}")
    if args.mode in ("backlog", "real-cold") and not re.search(r"~\$\d+\.\d\d\+", line):
        raise SystemExit(f"{args.mode}: expected a `+` backlog marker in {line!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

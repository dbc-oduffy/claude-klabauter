"""The preuse_bash render leg stays cheap with 100+ launch records, and a render overrun keeps the verdict.

Fixture: one session with `RECORDS` run tokens, each with a submission record, and one launch record
per token (for another script, so no token joins a launch and each is judged through the
transcript-reading path); the transcript is `TRANSCRIPT_MIB` MiB of filler. Every token reads as aged
out, so each `live_run_for_session` call scans all of them and consumes none.

`hooks.preuse_bash_dispatch._handler` is driven at default arguments with the guard chain stubbed to
answer nothing. The two render-leg consumers are stood in by callers of
`engine_route_state.session_route_phase`, the shared reach of `guard_em_prefire_survey` and
`nudge_em_turn_budget` (their own gates are not under test). Two measurements:

  render_ms          thread CPU ms of the "render" leg (`leg_attribution`) on the first hook
  repeat_render_ms   the same leg on a second hook (the per-hook case)
  overrun_verdict    "deny" / "advisory" / "no_advisory": what the handler returns when the hook
                     deadline expires right after the guard chain answered an advisory

The 50 ms bar is judged by the caller of this script, not by its exit code.

Run: `python -m coordinator_core.benchmarks.preuse_render_leg_budget [--records N] [--transcript-mib N]`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Callable, Generator, Optional
from unittest import mock

RECORDS = 120
TRANSCRIPT_MIB = 8
SESSION = "bench-session"
SESSION_WORKFLOW_RUNS = ["bench-workflow-run"]
CHAIN_ADVISORY_TEXT = "bench-chain-advisory"


def _run_id(i: int) -> str:
    return f"ask-20261104T000000-{i:06x}"


def _script(i: int) -> str:
    return f"scratch/warp/bench{i}.mjs"


def build_fixture(base: Path, records: int = RECORDS, transcript_mib: int = TRANSCRIPT_MIB) -> Path:
    """The repo root holding `records` aged-out tokens of SESSION and one launch record per token."""
    from coordinator_core import launch_records, warp_live
    from coordinator_core.benchmarks.warp_live_earlier_lines_budget import _write_transcript
    from coordinator_core.ops.dispatch_emit import run_token

    root = base / "repo"
    (root / ".git").mkdir(parents=True, exist_ok=True)
    managed = base / "managed"
    managed.mkdir(parents=True, exist_ok=True)
    transcript = base / "transcript.jsonl"
    _write_transcript(transcript, transcript_mib * 1024 * 1024)
    for i in range(records):
        run_token.mint(root, _run_id(i), _script(i), session_id=SESSION)
        warp_live.record_submission(
            root,
            _run_id(i),
            session_id=SESSION,
            transcript_path=str(transcript),
            script_path=_script(i),
            sizing_rel=None,
        )
        record = launch_records.record_path(str(managed), SESSION, f"wbench{i:05d}")
        Path(record).write_text(
            json.dumps({"scriptPath": f"scratch/warp/other{i}.mjs", "fired_at": time.time()}), encoding="utf-8"
        )
    return root


@contextlib.contextmanager
def _stubbed_chain(root: Path, on_evaluate: Optional[Callable[[], dict]] = None) -> Generator[None]:
    """The guard chain answers `on_evaluate()` (None by default); the two render consumers each call
    `session_route_phase`; the poll-in-flight join reports one run so the lookup is reached."""
    from coordinator_core.bash_guards import dispatch as chain
    from coordinator_core.hooks import flag_em_poll_in_flight, guard_em_prefire_survey, nudge_em_turn_budget
    from coordinator_core.ops import warm_guard_evaluate
    from coordinator_core.session import engine_route_state as ers

    def evaluate(*_args: object, **_kwargs: object) -> Optional[dict]:
        return on_evaluate() if on_evaluate is not None else None

    def prefire(_params: dict) -> None:
        ers.session_route_phase(root, SESSION, None)
        return None

    def turn_budget(_params: dict) -> str:
        ers.session_route_phase(root, SESSION, None)
        return ""

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(chain, "evaluate_payload_json", evaluate))
        stack.enter_context(mock.patch.object(warm_guard_evaluate, "_policy_file_for", lambda params: None))
        stack.enter_context(mock.patch.object(warm_guard_evaluate, "_engine_resolution_class", lambda: None))
        stack.enter_context(mock.patch.object(guard_em_prefire_survey, "evaluate", prefire))
        stack.enter_context(mock.patch.object(nudge_em_turn_budget, "advisory_text", turn_budget))
        stack.enter_context(
            mock.patch.object(flag_em_poll_in_flight, "session_workflow_run_ids", lambda sid: list(SESSION_WORKFLOW_RUNS))
        )
        yield


def _params(root: Path) -> dict:
    return {
        "payload": {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": SESSION, "cwd": str(root)}
    }


def _hook(root: Path) -> tuple:
    """`(render leg cpu ms, handler result)` for one `_handler` call at default arguments."""
    from coordinator_core.bash_guards import leg_attribution
    from coordinator_core.hooks import preuse_bash_dispatch

    real = leg_attribution.summary
    seen: dict = {}

    def spy(top: int = 3, floor_ms: float = 50.0) -> Optional[list]:
        rows = real(top=1000, floor_ms=0.0) or []
        seen.update({row["name"]: row for row in rows})
        return real(top=top, floor_ms=floor_ms)

    with mock.patch.object(leg_attribution, "summary", spy):
        out = preuse_bash_dispatch._handler(_params(root))
    return float(seen.get("render", {}).get("cpu_ms", 0.0)), out


def classify(out: object) -> str:
    """"deny", "advisory" (any other envelope content) or "no_advisory" (an empty answer)."""
    specific = out.get("hookSpecificOutput") if isinstance(out, dict) else None
    if not isinstance(specific, dict):
        return "no_advisory"
    return "deny" if specific.get("permissionDecision") == "deny" else "advisory"


def _expire_hook_deadline() -> dict:
    """Stand-in chain answer that also makes the hook deadline read as spent (this thread only)."""
    from coordinator_core.hooks import _preuse_phases

    _preuse_phases._HOOK_DEADLINE.set(time.monotonic() - 1.0)
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": CHAIN_ADVISORY_TEXT}}


def run_benchmark(
    base: Optional[Path] = None, records: int = RECORDS, transcript_mib: int = TRANSCRIPT_MIB
) -> dict:
    """`{render_ms, repeat_render_ms, live, overrun_verdict, overrun_out}` under `base` (a temp dir when None)."""
    from coordinator_core import warp_live
    from coordinator_core.benchmarks.warp_live_earlier_lines_budget import live_environment
    from coordinator_core.temp_layout import make_managed_temp_dir

    owned = base is None
    base = make_managed_temp_dir(prefix="preuse-render-leg-") if owned else Path(base)
    try:
        with live_environment(base), mock.patch.object(warp_live, "UNLAUNCHED_GRACE_SECONDS", -1), mock.patch.object(
            warp_live, "MINTED_RUNNER_GRACE_SECONDS", -1
        ):
            with _stubbed_chain(base):
                warm_root = build_fixture(base / "warm", records=2, transcript_mib=1)
                with _stubbed_chain(warm_root):
                    _hook(warm_root)
            root = build_fixture(base / "main", records=records, transcript_mib=transcript_mib)
            live = warp_live.live_run_for_session(root, SESSION)
            with _stubbed_chain(root):
                render_ms, _ = _hook(root)
                repeat_ms, _ = _hook(root)
            with _stubbed_chain(root, on_evaluate=_expire_hook_deadline):
                _, overrun = _hook(root)
        return {
            "render_ms": round(render_ms, 1),
            "repeat_render_ms": round(repeat_ms, 1),
            "live": live,
            "overrun_verdict": classify(overrun),
            "overrun_out": overrun,
        }
    finally:
        if owned:
            shutil.rmtree(base, ignore_errors=True)


def main(argv: Optional[list] = None) -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--records", type=int, default=RECORDS)
    parser.add_argument("--transcript-mib", type=int, default=TRANSCRIPT_MIB)
    args = parser.parse_args(argv)
    declare_benchmark_origin()
    row = run_benchmark(records=args.records, transcript_mib=args.transcript_mib)
    print(
        f"records={args.records} render_ms={row['render_ms']} repeat_render_ms={row['repeat_render_ms']} "
        f"live={row['live']} overrun_verdict={row['overrun_verdict']}"
    )
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    sys.exit(main())

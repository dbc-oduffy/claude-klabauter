"""Profile of the `clean_overlay_verify.reverify_on_mismatch` landing step on a default-path landing.

Drives `dispatch.land_run` at default arguments (the scenario of
`ops/dispatch_emit/tests/test_land_reverify_budget_left.py`, run in a pytest child so the test
fixtures and their isolation apply) over a mismatch-keyed overlay verdict, then through the designed
re-send, and prints the contract C2, C3 and C4 read, one `field=value` line each (`--json`: one object):

  outcome                 landed | parked | refused: where the landing ends after the designed re-send
  first_send              the same vocabulary for the first send alone
  budget_left_secs        op budget left at step entry
  step_process_time_ms    this process's CPU time inside the step
  step_spawns             spawns inside the step (the detached overlay launch)
  reaper_process_time_ms  one `orphan_reaper.find_orphans` scan at default arguments
  earlier_legs_secs       summed wall of the legs that ran before the step
  queued_for_resume       the first send's park flag
  bound_by                what bound the park (`step` when the park carries none)

Process time and spawns only; wall clock is never a figure. Exits 1 when the scenario did not run.

    python coordinator_core/benchmarks/land_reverify_step_profile.py [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.benchmarks import fixture_settings_home  # noqa: E402
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

TEST_FILE = "coordinator_core/ops/dispatch_emit/tests/test_land_reverify_budget_left.py"
OUT_ENV = "LAND_REVERIFY_PROFILE_OUT"
FIELDS = (
    "outcome", "first_send", "budget_left_secs", "step_process_time_ms", "step_spawns",
    "reaper_process_time_ms", "earlier_legs_secs", "queued_for_resume", "bound_by",
)
TIMEOUT_SECS = 300


def run_profile(repo: Path = _REPO) -> dict:
    """The scenario's measured row; raises RuntimeError when the pytest child wrote none."""
    with tempfile.TemporaryDirectory(prefix="reverify-profile-") as tmp, fixture_settings_home(tmp):
        out = Path(tmp) / "row.json"
        env = {**os.environ, OUT_ENV: str(out)}
        ran = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-x", TEST_FILE,
             "-k", "the_step_is_reached"],
            cwd=str(repo), env=env, capture_output=True, text=True, timeout=TIMEOUT_SECS,
            **no_console_creationflags(),
        )
        if not out.is_file():
            raise RuntimeError(f"scenario wrote no row (rc={ran.returncode}):\n{ran.stdout[-2000:]}{ran.stderr[-1000:]}")
        return json.loads(out.read_text(encoding="utf-8"))


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="land_reverify_step_profile")
    parser.add_argument("--json", action="store_true", help="print the whole row as one JSON object")
    args = parser.parse_args(argv)
    try:
        row = run_profile()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"land_reverify_step_profile: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(row, indent=1, sort_keys=True))
    else:
        for field in FIELDS:
            print(f"{field}={row.get(field)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

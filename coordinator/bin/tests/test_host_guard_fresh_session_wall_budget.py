"""Fresh-session pin: a first-touch Grok guard() call delivers the DIY nag through the real worker.join budget."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

sys.path.insert(0, str(Path(__file__).resolve().parent))
import host_guard_cost_probe as probe  # noqa: E402

GUARD_SPEC = importlib.util.spec_from_file_location("unimatrix_host_guard_fresh", probe.BIN / "unimatrix-host-guard.py")
guard_mod = importlib.util.module_from_spec(GUARD_SPEC)
GUARD_SPEC.loader.exec_module(guard_mod)


def fresh_grok_row(work):
    """One guard() call in a fresh interpreter: fresh uuid session id, empty settings home and warm base, the lane
    record as the only `.coordinator-local` content, the engine root a separate tree from the edited repo."""
    out = probe.run_child(probe.grok_fresh_argv(work, "pin"), env=probe.grok_fresh_env(work))
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_budget_is_the_dr402_half_second():
    assert guard_mod.DEFAULT_BUDGET_SECS == 0.5


def test_fresh_session_grok_delivers_nag_inside_the_join(tmp_path):
    row = fresh_grok_row(tmp_path)
    assert "error" not in row, row
    # Load-bearing: the real join held, so the answer is not the degraded timeout substitute.
    assert row["state"] != "degraded", row
    assert row["degrade_rows"] == 0, row
    assert probe.carries_nag(row["answer"]), row
    assert row["answer"]["hookSpecificOutput"]["additionalContext"], row


def test_fresh_session_leaves_no_session_state_seeded_by_the_test(tmp_path):
    """The fresh shape is the point: the repo's `.coordinator-local` holds only what the engine's lane record put there."""
    argv = probe.grok_fresh_argv(tmp_path, "unseeded")
    repo = Path(json.loads(argv[2])["cwd"])
    sid = json.loads(argv[2])["session_id"]
    names = sorted(p.name for p in (repo / ".coordinator-local").rglob("*") if p.is_file())
    assert names == ["xs-lane.json"], names
    assert (repo / ".coordinator-local" / "coordinator-sessions" / sid / "xs-lane.json").is_file()

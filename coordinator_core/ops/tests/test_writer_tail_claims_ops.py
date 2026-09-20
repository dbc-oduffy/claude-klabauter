"""
coordinator_core.ops.tests.test_writer_tail_claims_ops — AC5 for C5
(migration batch A: coordinator_core/ops to-fix writers onto the seam).

One behavioural test per (entry-path class x seam entry point) pair this
batch migrated, driven through its real entry, asserting the claim lands.
Watched RED with the seam call swapped back for the raw primitive it
replaced (``goal_append.append_goal``'s ``log_file.open("a") + fh.write``),
per the row body's instruction.

Spec backlink: docs/plans/2026-09-11-state-writers-claim-through-one-seam.md
§ C5.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import cli_entry
from coordinator_core.ops.goal_append import append_goal
from coordinator_core.session import core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SESSION_ENV_VAR = "COORDINATOR_SESSION_ID"


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, **no_console_creationflags())
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, **no_console_creationflags()
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, **no_console_creationflags())
    (repo / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, **no_console_creationflags())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, **no_console_creationflags())
    return repo


def _touch_record_text(session_dir) -> str:
    from coordinator_core.session import scope

    sink = Path(session_dir) / scope._TOUCH_RECORD_FILENAME
    lines, _degraded = scope._read_touch_record_as_legacy_lines(sink)
    return "\n".join(lines)


def test_append_goal_claims_its_log_line_through_the_seam(tmp_path, monkeypatch):
    """entry-path class: direct-call op body (append_goal) x seam entry point:
    append_claimed_line. Watched RED with the seam call swapped back for the
    pre-migration ``log_file.open("a") + fh.write`` primitive -- that shape
    never calls ``declare_write``, so the touch record below stays empty."""
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    repo = _make_repo(tmp_path)
    sid = "sid-c5-goal-append"
    core.init(sid, cwd=str(repo))
    monkeypatch.setenv(_SESSION_ENV_VAR, sid)

    central_state_root = repo / "state"
    with cli_entry.recording_declared_writes(cwd=str(repo)):
        result = append_goal(
            period="day",
            period_value="2026-09-19",
            text="claim lands via the seam",
            repo="test-repo",
            coordinator_root_path=".",
            hostname="test-host",
            central_state_root=central_state_root,
        )

    log_file = Path(result["log_file"])
    assert log_file.exists()
    assert "claim lands via the seam" in log_file.read_text(encoding="utf-8")

    content = _touch_record_text(core.session_dir(sid, cwd=str(repo)))
    assert log_file.name in content, (
        "append_goal's write must be declared through the seam "
        "(session/claimed_write.py::append_claimed_line), not a raw open()+write "
        "that leaves the touch record empty"
    )

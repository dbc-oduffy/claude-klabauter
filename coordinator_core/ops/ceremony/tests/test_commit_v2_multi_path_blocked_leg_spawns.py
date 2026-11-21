"""A 19-path ceremony.commit_v2 replies under the 500ms process budget or its phase row names the blocked leg."""
from __future__ import annotations

from coordinator_core.session_hub_path import hub_for_common

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.ceremony import commit_v2_phases as cp
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "11111111-2222-4333-8444-555555555555"


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_default_commit_of_19_paths_is_in_budget_or_names_its_leg(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_OP_LATENCY_DISABLE", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "--", "README.md"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    names = [f"f{i}.txt" for i in range(19)]
    for n in names:
        (root / n).write_text(n + "\n", encoding="utf-8")

    result = commit_v2._handler(
        {"paths": names, "message": "add nineteen\n", "session_id": _SID},
        repo_root=root / ".git",
    )
    assert result["committed"] is True

    sink = hub_for_common(root / ".git") / "logs" / "op-latency.jsonl"
    rows = [json.loads(x) for x in sink.read_text(encoding="utf-8").splitlines() if x]
    (row,) = [r for r in rows if r.get("kind") == "phase_time"]
    assert row["paths"] == 19
    assert row["total_ms"] < cp.BUDGET_MS or row["blocked_leg"]["leg"] in row["phases_ms"]

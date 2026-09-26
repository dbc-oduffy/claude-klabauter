from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.pickup_brief as pa
from coordinator_core.win_portability import no_console_creationflags

# fixture stands in for. The spawn ratchet's `_BASELINE` is shrink-only
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


from coordinator_core.session.record_homes import record_path
from coordinator_core.pickup_assemble.tests._git_harness import (
    git as _git,
    init_repo as _init_repo,
)


def _write_handoff(repo: Path, name: str, fm_extra: str) -> Path:
    path = Path(record_path(str(repo), "handoffs", name))
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: claimed\n"
        'predecessor: "none"\n'
        f"{fm_extra}"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def test_continued_with_resolving_successor_blocks_coast_and_names_it(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_handoff(
        repo,
        "successor.md",
        "deployment_state: in_flight\n",
    )
    _write_handoff(
        repo,
        "predecessor.md",
        "deployment_state: continued\n"
        "continued_into: state/handoffs/successor.md\n",
    )

    result = pa.brief("state/handoffs/predecessor.md", repo_root=repo)
    d = result.decision_object

    assert d["gates"]["coast"]["verdict"] == "blocked"
    jp_ids = [jp["id"] for jp in d["judgment_points"]]
    assert "j-supersession" in jp_ids
    assert d["directives"] == []
    assert d["gates"]["supersession"]["successor_resolves"] is True
    assert d["gates"]["supersession"]["successor_path"] == "state/handoffs/successor.md"
    assert "state/handoffs/successor.md" in d["narration"]


def test_continued_with_dangling_continued_into_still_blocks_and_states_it(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_handoff(
        repo,
        "predecessor.md",
        "deployment_state: continued\n"
        "continued_into: state/handoffs/does-not-exist.md\n",
    )

    result = pa.brief("state/handoffs/predecessor.md", repo_root=repo)
    d = result.decision_object

    assert d["gates"]["coast"]["verdict"] == "blocked"
    jp_ids = [jp["id"] for jp in d["judgment_points"]]
    assert "j-supersession" in jp_ids
    assert d["directives"] == []
    assert d["gates"]["supersession"]["successor_resolves"] is False
    assert d["gates"]["supersession"]["successor_path"] is None
    assert "dangling" in d["narration"].lower()


def test_continued_with_missing_continued_into_still_blocks_and_states_it(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_handoff(
        repo,
        "predecessor.md",
        "deployment_state: continued\n",
    )

    result = pa.brief("state/handoffs/predecessor.md", repo_root=repo)
    d = result.decision_object

    assert d["gates"]["coast"]["verdict"] == "blocked"
    jp_ids = [jp["id"] for jp in d["judgment_points"]]
    assert "j-supersession" in jp_ids
    assert d["directives"] == []
    assert d["gates"]["supersession"]["successor_resolves"] is False
    assert d["gates"]["supersession"]["successor_path"] is None
    assert "missing" in d["narration"].lower() or "blank" in d["narration"].lower()


def test_no_claim_acquired_when_gate_fires_with_claim_at_brief(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_handoff(
        repo,
        "successor.md",
        "deployment_state: in_flight\n",
    )
    _write_handoff(
        repo,
        "predecessor.md",
        "deployment_state: continued\n"
        "continued_into: state/handoffs/successor.md\n",
    )

    result = pa.brief(
        "state/handoffs/predecessor.md", repo_root=repo, claim_at_brief=True
    )
    d = result.decision_object

    assert d["gates"]["coast"]["verdict"] == "blocked"
    claim_dir = repo / ".git" / "coordinator-sessions"
    if claim_dir.exists():
        stamped = list(claim_dir.rglob("*predecessor*"))
        assert stamped == []
    status = _git(repo, "status", "--porcelain")
    assert status.stdout.strip() == ""


def test_ordinary_in_flight_baton_is_unaffected_by_the_gate(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_handoff(
        repo,
        "plain.md",
        "deployment_state: in_flight\n",
    )

    result = pa.brief("state/handoffs/plain.md", repo_root=repo)
    d = result.decision_object

    jp_ids = [jp["id"] for jp in d["judgment_points"]]
    assert "j-supersession" not in jp_ids
    assert "supersession" not in d["gates"]
    assert d["directives"] != []


@pytest.mark.parametrize("terminal_state", ["closed", "abandoned"])
def test_a_successorless_terminal_baton_blocks_coast_and_takes_no_claim(tmp_path: Path, terminal_state: str):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_handoff(repo, "finished.md", f"deployment_state: {terminal_state}\n")

    result = pa.brief("state/handoffs/finished.md", repo_root=repo, claim_at_brief=True)
    d = result.decision_object

    assert d["gates"]["coast"]["verdict"] == "blocked"
    assert "j-terminal" in [jp["id"] for jp in d["judgment_points"]]
    assert d["directives"] == []
    claim_dir = repo / ".git" / "coordinator-sessions"
    if claim_dir.exists():
        assert list(claim_dir.rglob("*finished*")) == []

"""
coordinator_core.pickup_assemble.tests.test_brief_supersession_gate

Purpose: `brief()` never read `continued_into` before this fix — a handoff
stamped `deployment_state: continued` (retained on disk by
`archival._is_terminal_or_archived_child`'s 2026-07-17 reverse-membership
fix, correctly, while its successor is still `in_flight`) briefed as an
ordinary live pickup target: `coast: clear`, `judgment_points: []`, and a
RECLAIM of the predecessor's holder-absent claim — telling the EM to
dispatch a superseded baton's directives against a moved premise. Residency
in `state/handoffs/` is not pickupability; this is pickup's own
inference to make, never archival's job (`archival.py` is untouched by
this fix on purpose).

Negative spec: an ordinary `in_flight` baton (no `continued_into`) is
completely unaffected by this gate — same coast/directives shape as before,
proving the gate is inert off-path.

Spec backlink: cross-repo memo 2026-09-02-pickup-serves-a-superseded-
baton-as-live-claude-klabauter.

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_brief_supersession_gate.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.pickup_assemble as pa
from coordinator_core.win_portability import no_console_creationflags

# Declared, not excused: this file spawns a real process (git) because
# `brief()` reads real git state (tree quiescence, claim resolution) that no
# fixture stands in for. The spawn ratchet's `_BASELINE` is shrink-only
# pre-existing residue and is explicitly not the route for a new file —
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _isolated_git_env(anchor: Path) -> dict[str, str]:
    empty_config = anchor / "empty.gitconfig"
    if not empty_config.exists():
        empty_config.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = str(empty_config)
    env["GIT_CONFIG_SYSTEM"] = str(empty_config)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=_isolated_git_env(repo.parent),
        **no_console_creationflags(),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _write_handoff(repo: Path, name: str, fm_extra: str) -> Path:
    path = repo / "state" / "handoffs" / name
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
        # No brief-stage lock artifact should exist for this predecessor —
        # the gate returns before `acquire_brief_claim`/`route_baton_
        # adoption` ever run.
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

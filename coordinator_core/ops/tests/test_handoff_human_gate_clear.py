"""
coordinator_core.ops.tests.test_handoff_human_gate_clear

Purpose: pin `handoff.human_gate_clear` through ipc dispatch -- a valid PM-turn or commit
citation clears the human leg, stamps `gate_cleared_by` and commits only that file; every
refusal leaves the file bytes and HEAD untouched.

Real-git fixture, module-local: `locked_rmw` and `commit_paths` need a real repository.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core import ipc
import coordinator_core.ops.handoff_human_gate_clear  # noqa: F401 -- fires @register_op
from coordinator_core.ops import baton_pm_turns
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_NO_CONSOLE = no_console_creationflags()
_SID = "11111111-2222-3333-4444-555555555555"
_VERBATIM = "the PM cleared the gate"


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=str(self.root), capture_output=True, check=True, **_NO_CONSOLE
        )
        return done.stdout.decode().strip()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def seed(self, name: str, legs: list) -> Path:
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = {
            "title": "Human gate",
            "created": "2026-01-01",
            "branch": "work/test/2026-01-01",
            "predecessor": "none",
            "status": "open",
            "deployment_state": "awaiting_gate",
            "gate_notes": "PM must clear",
            "pickup_ready": False,
            "kind": "spinoff-roadmap",
            "roadmap_id": "rdm-human",
            "stub_id": "human-01",
            "wave": 1,
            "blocks": [],
            "blocked_by": [],
            "gate_evidence": {"covers_prose": True, "legs": legs},
        }
        path.write_text(
            "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n# Handoff\n\nBody.\n",
            encoding="utf-8",
        )
        self.git("add", str(path))
        self.git("commit", "-m", f"add {name}")
        return path

    def fm(self, name: str) -> dict:
        text = (self.root / "state" / "handoffs" / name).read_text(encoding="utf-8")
        return yaml.safe_load(text.split("---", 2)[1]) or {}


@pytest.fixture
def repo(tmp_path) -> _Repo:
    root = tmp_path / "repo"
    root.mkdir()
    r = _Repo(root)
    r.git("init", "-b", "main")
    r.git("config", "user.email", "human-gate-clear@claude-claude-klabauter.test")
    r.git("config", "user.name", "Human Gate Clear Test")
    r.git("config", "commit.gpgsign", "false")
    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    r.git("add", "-A")
    r.git("commit", "-m", "chore: skeleton")
    return r


@pytest.fixture
def pm_log(tmp_path, monkeypatch) -> Path:
    log = tmp_path / "pm_turns.jsonl"
    log.write_text(
        "\n".join(
            json.dumps({"turn": n, "prompt": f"pm says {n}", "ts": "2026-10-30T00:00:00Z"})
            for n in range(3)
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(baton_pm_turns, "log_path", lambda sid, cwd=None: log)
    return log


_HUMAN = {"leg_id": "pm-ok", "kind": "human", "reason": "PM must approve"}
_DEADLINE = {"leg_id": "wait", "kind": "deadline", "ref": "2999-01-01"}


def _call(repo: _Repo, params: dict) -> dict:
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "handoff.human_gate_clear",
        "params": params,
        "_origin_worktree": str(repo.root),
    }
    reply = asyncio.run(ipc.dispatch_message(msg))
    return reply.get("result") or {"ok": False, "error": json.dumps(reply.get("error"))}


def _path(repo: _Repo, name: str = "h.md") -> str:
    return str(repo.root / "state" / "handoffs" / name)


def _assert_untouched(repo: _Repo, path: Path, before: bytes, head: str, result: dict) -> None:
    assert result["ok"] is False and result.get("error")
    assert path.read_bytes() == before
    assert repo.head() == head


def test_valid_pm_turn_clears_stamps_and_commits(repo: _Repo, pm_log: Path) -> None:
    repo.seed("h.md", [_HUMAN])
    head = repo.head()
    result = _call(repo, {"handoff_path": _path(repo), "pm_turn": {"session_id": _SID, "turn": 1}})
    assert result["ok"] is True, result
    citation = f"pm-turn:{_SID}#1"
    assert result["gate_cleared_by"] == [citation]
    assert result["deployment_state"] == "ready_to_fire"
    fm = repo.fm("h.md")
    assert fm["gate_cleared_by"] == [citation]
    assert fm["deployment_state"] == "ready_to_fire"
    assert "gate_evidence" not in fm
    assert result["commit_sha"] == repo.head() != head
    assert repo.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD") == (
        "state/handoffs/h.md"
    )


def test_valid_commit_sha_stamps_full_sha(repo: _Repo) -> None:
    repo.seed("h.md", [_HUMAN])
    repo.git("commit", "--allow-empty", "-m", f"decision: {_VERBATIM}")
    sha = repo.head()
    result = _call(
        repo, {"handoff_path": _path(repo), "commit_sha": sha, "verbatim": _VERBATIM}
    )
    assert result["ok"] is True, result
    assert repo.fm("h.md")["gate_cleared_by"] == [sha]
    assert result["commit_sha"] == repo.head() != sha


def test_rerun_is_idempotent(repo: _Repo, pm_log: Path) -> None:
    repo.seed("h.md", [_HUMAN])
    params = {"handoff_path": _path(repo), "pm_turn": {"session_id": _SID, "turn": 0}}
    assert _call(repo, params)["ok"] is True
    head = repo.head()
    again = _call(repo, params)
    assert again["ok"] is True and again["applied"] is False
    assert repo.head() == head
    assert repo.fm("h.md")["gate_cleared_by"] == [f"pm-turn:{_SID}#0"]


@pytest.mark.parametrize(
    "turn",
    [{"session_id": _SID, "turn": 99}, {"session_id": _SID, "turn": -1}, {"turn": 1}],
)
def test_bogus_or_negative_turn_refused(repo: _Repo, pm_log: Path, turn: dict, monkeypatch) -> None:
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    path = repo.seed("h.md", [_HUMAN])
    before, head = path.read_bytes(), repo.head()
    result = _call(repo, {"handoff_path": str(path), "pm_turn": turn})
    _assert_untouched(repo, path, before, head, result)


def test_unrecorded_session_refused(repo: _Repo, tmp_path, monkeypatch) -> None:
    missing = tmp_path / "absent" / "pm_turns.jsonl"
    monkeypatch.setattr(baton_pm_turns, "log_path", lambda sid, cwd=None: missing)
    path = repo.seed("h.md", [_HUMAN])
    before, head = path.read_bytes(), repo.head()
    result = _call(repo, {"handoff_path": str(path), "pm_turn": {"session_id": _SID, "turn": 0}})
    _assert_untouched(repo, path, before, head, result)


def test_bogus_sha_refused(repo: _Repo) -> None:
    path = repo.seed("h.md", [_HUMAN])
    before, head = path.read_bytes(), repo.head()
    result = _call(
        repo, {"handoff_path": str(path), "commit_sha": "0" * 40, "verbatim": _VERBATIM}
    )
    _assert_untouched(repo, path, before, head, result)


def test_sha_without_verbatim_in_message_refused(repo: _Repo) -> None:
    path = repo.seed("h.md", [_HUMAN])
    repo.git("commit", "--allow-empty", "-m", "unrelated work")
    before, head = path.read_bytes(), repo.head()
    result = _call(
        repo, {"handoff_path": str(path), "commit_sha": head, "verbatim": _VERBATIM}
    )
    _assert_untouched(repo, path, before, head, result)


def test_commit_sha_without_verbatim_refused(repo: _Repo) -> None:
    path = repo.seed("h.md", [_HUMAN])
    before, head = path.read_bytes(), repo.head()
    result = _call(repo, {"handoff_path": str(path), "commit_sha": head})
    _assert_untouched(repo, path, before, head, result)


def test_both_citations_refused(repo: _Repo, pm_log: Path) -> None:
    path = repo.seed("h.md", [_HUMAN])
    before, head = path.read_bytes(), repo.head()
    result = _call(
        repo,
        {
            "handoff_path": str(path),
            "pm_turn": {"session_id": _SID, "turn": 0},
            "commit_sha": head,
            "verbatim": "x",
        },
    )
    _assert_untouched(repo, path, before, head, result)


def test_not_awaiting_gate_refused(repo: _Repo, pm_log: Path) -> None:
    path = repo.seed("h.md", [_HUMAN])
    text = path.read_text(encoding="utf-8").replace(
        "deployment_state: awaiting_gate", "deployment_state: in_flight"
    )
    path.write_text(text, encoding="utf-8")
    repo.git("add", str(path))
    repo.git("commit", "-m", "park state")
    before, head = path.read_bytes(), repo.head()
    result = _call(repo, {"handoff_path": str(path), "pm_turn": {"session_id": _SID, "turn": 0}})
    _assert_untouched(repo, path, before, head, result)


def test_no_human_leg_refused(repo: _Repo, pm_log: Path) -> None:
    path = repo.seed("h.md", [_DEADLINE])
    before, head = path.read_bytes(), repo.head()
    result = _call(repo, {"handoff_path": str(path), "pm_turn": {"session_id": _SID, "turn": 0}})
    _assert_untouched(repo, path, before, head, result)


def test_mixed_human_and_deadline_stays_awaiting_gate(repo: _Repo, pm_log: Path) -> None:
    repo.seed("h.md", [_HUMAN, _DEADLINE])
    result = _call(repo, {"handoff_path": _path(repo), "pm_turn": {"session_id": _SID, "turn": 2}})
    assert result["ok"] is True, result
    assert result["deployment_state"] == "awaiting_gate"
    fm = repo.fm("h.md")
    assert fm["deployment_state"] == "awaiting_gate"
    assert fm["gate_cleared_by"] == [f"pm-turn:{_SID}#2"]
    by_id = {leg["leg_id"]: leg for leg in fm["gate_evidence_results"]["legs"]}
    assert by_id["pm-ok"]["status"] == "satisfied"
    assert by_id["wait"]["status"] != "satisfied"
    assert result["commit_sha"] == repo.head()


def test_unknown_leg_id_refused(repo: _Repo, pm_log: Path) -> None:
    path = repo.seed("h.md", [_HUMAN])
    before, head = path.read_bytes(), repo.head()
    result = _call(
        repo,
        {
            "handoff_path": str(path),
            "pm_turn": {"session_id": _SID, "turn": 0},
            "leg_ids": ["nope"],
        },
    )
    _assert_untouched(repo, path, before, head, result)

"""review.prep at default worktree arguments freezes the run's own hunk of a shared file.

The file also carries a halted peer's uncommitted hunk (``foreign_halted_withheld`` names it). Prep keeps
the path and freezes HEAD plus only the run's hunks; it neither drops the file (zero slices) nor freezes
the peer's hunk.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops import review_prep as rp
from coordinator_core.ops import warp_run_deliverable
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.session import claim_index, liveness
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261118T180107-7d0882"
PEER = "warp-2026-11-18-1bba15-1-adopt-orphan-stall-dir-import-fence-20261118T174659-417cc6"
SESSION = "sess-own"
SHARED = "pkg/tests/test_shared.py"
OWN_LINE = "ENTRY_01 = 'own'"
PEER_LINE = "ENTRY_20 = 'peer'"
BASE_LINES = [f"ENTRY_{i:02d} = 'seed'" for i in range(1, 21)]


class _Found(dict):
    recorded_kind = None


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _text(lines: list) -> str:
    return "\n".join(lines) + "\n"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    for rel, text in {
        SHARED: _text(BASE_LINES),
        "docs/plans/p.md": "---\nplan_id: pln-test-1\n---\n\n# p\n",
    }.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    run_dir = record_dir(tmp_path, RUN)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(json.dumps({"review_declared_paths": [SHARED]}), encoding="utf-8")

    peer_lines = [*BASE_LINES[:-1], PEER_LINE]
    blob = git_objects.write_object(resolve_git_common_dir(tmp_path), b"blob", _text(peer_lines).encode("utf-8"))
    peer_dir = record_dir(tmp_path, PEER)
    peer_dir.mkdir(parents=True, exist_ok=True)
    (peer_dir / "touched-blobs.json").write_text(
        json.dumps({SHARED: {"base": _git(tmp_path, "rev-parse", "HEAD").strip(), "run": blob}}), encoding="utf-8"
    )

    monkeypatch.setattr(claim_index, "lookup", lambda paths, **kw: _Found({p: [] for p in paths}))
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(liveness, "session_abandoned", lambda sid, cwd=None: False)
    monkeypatch.setattr(warp_run_deliverable, "live_run_write_claims", lambda *a, **kw: {})
    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda root, rid, plan, paths: {SHARED: PEER})
    return tmp_path


def _write_worktree(repo: Path, lines: list) -> None:
    (repo / SHARED).write_text(_text(lines), encoding="utf-8")


def _prep(repo: Path) -> dict:
    return rp._handler(
        {
            "run_id": RUN,
            "base": _git(repo, "rev-parse", "HEAD").strip(),
            "worktree": True,
            "plan_path": "docs/plans/p.md",
            "session_id": SESSION,
        },
        repo,
    )


def _frozen(reply: dict) -> str:
    return "".join(Path(s["diff_path"]).read_text(encoding="utf-8") for s in reply["slices"])


def test_the_run_own_hunk_is_frozen_without_the_halted_peer_hunk(repo: Path) -> None:
    _write_worktree(repo, [OWN_LINE, *BASE_LINES[1:-1], PEER_LINE])

    for _fire in ("first", "refire"):
        reply = _prep(repo)
        assert reply["error"] is None, (_fire, reply)
        assert [f for s in reply["slices"] for f in s["files"]] == [SHARED], (_fire, reply)
        frozen = _frozen(reply)
        assert f"+{OWN_LINE}" in frozen, _fire
        assert f"-{BASE_LINES[0]}" in frozen, _fire
        assert PEER_LINE not in frozen, _fire
        whole = Path(reply["whole_diff_path"]).read_text(encoding="utf-8")
        assert PEER_LINE not in whole, _fire
        assert SHARED in reply["peer_held"], _fire


def test_a_file_holding_only_the_halted_peer_hunk_is_not_reviewed(repo: Path) -> None:
    _write_worktree(repo, [*BASE_LINES[:-1], PEER_LINE])

    reply = _prep(repo)

    assert reply["slices"] == [], reply
    assert not reply.get("whole_diff_path") or PEER_LINE not in Path(reply["whole_diff_path"]).read_text(encoding="utf-8")

"""dispatch.terminal_commit leaves paths named in a run's ``withhold.jsonl`` unstaged, records them in
``run_artifacts.withheld``, reports rows whose only writes are withheld, and names ``withhold_paths``
in its peer-claim and blob-drift halt texts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PEER = "wh-peer"
SELF = "wh-self"
RUN = "ask-20261016T110000-bbbbbb"
OLD_RUN = "ask-20261016T100000-aaaaaa"
SIZING = "state/sizings/s.yaml"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return root


def _withhold(repo: Path, run: str, entries: list) -> None:
    d = repo / "state" / "warp" / run
    d.mkdir(parents=True, exist_ok=True)
    with (d / "withhold.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
        for path, reason in entries:
            fh.write(json.dumps({"path": path, "reason": reason, "at": "t"}) + "\n")


def _request(repo: Path) -> CommitRequest:
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    (repo / "c.py").write_text("c\n", encoding="utf-8")
    return CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t1", paths=("a.py",)),
            ChunkCommit(id="C2", title="t2", paths=("b.py",)),
            ChunkCommit(id="C3", title="t3", paths=("b.py", "c.py")),
        )
    )


def _run(repo: Path, request: CommitRequest, **params) -> dict:
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {
            "script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
            "run_id": RUN, **params,
        },
        repo_root=repo / ".git",
    )


def _committed(repo: Path) -> list:
    """Every path any commit after the seed carried (the run lands product, then its record dir)."""
    out = _git(["log", "--name-only", "--format="], repo)
    return sorted(set(ln for ln in out.splitlines() if ln.strip()) - {"README.md"})


def _status(repo: Path) -> list:
    return [ln for ln in _git(["status", "--porcelain"], repo).splitlines()]


def test_withheld_path_stays_dirty_and_the_rest_land(repo):
    request = _request(repo)
    _withhold(repo, RUN, [("b.py", "peer run hunks")])
    out = _run(repo, request)
    assert out["committed"] is True, out
    landed = _committed(repo)
    assert "a.py" in landed and "b.py" not in landed
    assert "?? b.py" in _status(repo)
    assert out["run_artifacts"]["withheld"]["b.py"] == "withhold_paths: peer run hunks"


def test_a_withheld_only_row_is_reported_withheld_not_landed(repo):
    request = _request(repo)
    _withhold(repo, RUN, [("b.py", "peer run hunks")])
    out = _run(repo, request, incomplete_chunks=[], landed_chunks=[])
    assert out["withheld_chunks"] == {"C2": [{"path": "b.py", "reason": "peer run hunks"}]}
    assert "C2" not in out["chunks_committed"]
    assert "C2" not in out.get("no_product_hunk", [])
    assert "C1" in out["chunks_committed"]
    assert out["stranded"]["C2"] == ["b.py"]
    assert out["incomplete_reasons"]["C2"] == "withheld: peer run hunks"


def test_a_partly_withheld_row_lands_and_names_its_withheld_path(repo):
    request = _request(repo)
    _withhold(repo, RUN, [("b.py", "peer run hunks")])
    out = _run(repo, request)
    assert "C3" in out["chunks_committed"]
    assert "C3" not in out["withheld_chunks"]
    assert "c.py" in _committed(repo)
    assert "b.py" in out["run_artifacts"]["withheld"]


def test_a_withheld_peer_claimed_path_no_longer_refuses(repo):
    request = _request(repo)
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "b.py", cwd=str(repo))
    refused = _run(repo, request)
    assert refused["refused"] == "peer-claim"
    _withhold(repo, RUN, [("b.py", "peer owns it")])
    out = _run(repo, request)
    assert out["committed"] is True, out
    assert "b.py" not in _committed(repo)


def test_all_withheld_lands_only_the_record_dir(repo):
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C2", title="t2", paths=("b.py",)),))
    _withhold(repo, RUN, [("b.py", "peer run hunks")])
    out = _run(repo, request)
    assert out["committed"] is True, out
    assert _committed(repo) == [f"state/warp/{RUN}/withhold.jsonl"]
    assert "?? b.py" in _status(repo)
    assert list(out["withheld_chunks"]) == ["C2"]


def test_all_withheld_with_nothing_else_to_land_is_nothing_to_commit(repo):
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    (repo / ".gitignore").write_text("withhold.jsonl\n", encoding="utf-8")
    _git(["add", ".gitignore"], repo)
    _git(["commit", "-q", "-m", "ignore"], repo)
    request = CommitRequest(chunks=(ChunkCommit(id="C2", title="t2", paths=("b.py",)),))
    _withhold(repo, RUN, [("b.py", "peer run hunks")])
    head = _git(["rev-parse", "HEAD"], repo)
    out = _run(repo, request)
    assert out["committed"] is False and out["nothing_to_commit"] is True, out
    assert out["run_artifacts"]["withheld"] == {"b.py": "withhold_paths: peer run hunks"}
    assert list(out["withheld_chunks"]) == ["C2"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_an_unmatched_entry_is_reported(repo):
    request = _request(repo)
    _withhold(repo, RUN, [("nowhere.py", "stale")])
    out = _run(repo, request)
    assert out["committed"] is True, out
    assert out["withhold_unmatched"] == ["nowhere.py"]
    assert "withheld_chunks" not in out


def test_a_predecessor_dirs_withhold_applies_to_the_refire(repo):
    request = _request(repo)
    for run in (OLD_RUN, RUN):
        d = repo / "state" / "warp" / run
        d.mkdir(parents=True)
        (d / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    (repo / "state" / "sizings").mkdir(parents=True)
    (repo / SIZING).write_text("id: s\n", encoding="utf-8")
    _withhold(repo, OLD_RUN, [("b.py", "from the halted run")])
    out = _run(repo, request)
    assert out["committed"] is True, out
    assert "b.py" not in _committed(repo)
    assert out["run_artifacts"]["withheld"]["b.py"] == "withhold_paths: from the halted run"


def test_without_a_bound_sizing_only_the_runs_own_dir_is_read(repo):
    request = _request(repo)
    _withhold(repo, OLD_RUN, [("b.py", "predecessor")])
    out = _run(repo, request)
    assert out["committed"] is True, out
    assert "b.py" in _committed(repo)
    assert "withheld_chunks" not in out


def test_the_runs_own_entry_wins_over_a_predecessors(repo):
    request = _request(repo)
    for run in (OLD_RUN, RUN):
        d = repo / "state" / "warp" / run
        d.mkdir(parents=True)
        (d / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    (repo / "state" / "sizings").mkdir(parents=True)
    (repo / SIZING).write_text("id: s\n", encoding="utf-8")
    _withhold(repo, OLD_RUN, [("b.py", "old reason")])
    _withhold(repo, RUN, [("b.py", "new reason")])
    out = _run(repo, request)
    assert out["run_artifacts"]["withheld"]["b.py"] == "withhold_paths: new reason"


def test_the_peer_claim_text_names_withhold_paths_and_keeps_the_wait_clause(repo):
    request = _request(repo)
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "a.py", cwd=str(repo))
    out = _run(repo, request)
    assert out["refused"] == "peer-claim"
    assert "withhold_paths" in out["error"] and f"run_id={RUN}" in out["error"]
    assert "Wait for the peer" in out["error"]
    assert "session-claim-cli release-artifact artifact" in out["error"]


def test_the_blob_drift_remedy_names_withhold_paths():
    generic = "drift. " + terminal_commit._GENERIC_DRIFT_REMEDY
    assert "withhold_paths" in terminal_commit._blob_drift_remedy(generic, "r1")
    assert "withhold_paths" in terminal_commit._blob_drift_remedy("drift.", "r1")

"""A failed resumed run commits its own halt record through the path a first halt uses."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT, record_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-2026-11-20-passive-freshness-status-969ba3-20261120T005822-b26336"
FIRST = "warp-2026-11-20-other-111111-20261120T000000-aaaaaa"
ERROR = "AgentFailed: codex returned no schema-valid result: 'arm' is a required property"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True,
                          **no_console_creationflags()).stdout


@pytest.fixture(autouse=True)
def _no_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(halted_run, "_schedule_resume_build", lambda *_a, **_k: None)
    monkeypatch.setattr(halted_run, "find_split_trees", lambda *_a, **_k: [])


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _record(root: Path, run_id: str) -> Path:
    rd = record_dir(root, run_id)
    rd.mkdir(parents=True)
    (rd / "run.json").write_text("{}", encoding="utf-8")
    (rd / "manifest.json").write_text(json.dumps({"head_sha": "a" * 40}), encoding="utf-8")
    return rd


def _trailers(root: Path) -> dict:
    out = {}
    for line in _git(root, "log", "-1", "--format=%B").splitlines():
        key, sep, value = line.partition(": ")
        if sep and key in ("Halted-By", "Halt-Reason", "Checkpoint-Run-Halted"):
            out[key] = value
    return out


def test_failed_resumed_run_commits_its_halt_like_a_first_halt(repo: Path) -> None:
    from coordinator_core.ops.dispatch_emit import failed_fire_halt

    _record(repo, FIRST)
    first = terminal_commit._handler(
        {"run_id": FIRST, HALTED_RECORD_PARAM: {"halted": "ledger-check", "reason": "row R1 open"}},
        repo_root=repo / ".git")
    assert first["committed"] is True, first
    first_trailers = _trailers(repo)

    rd = _record(repo, RUN)
    halted_run.write_halt_marker(rd, halted="ledger-check", sizing_rel=None, reason="original halt",
                                 run_base_sha="a" * 40)
    _git(repo, "add", RECORD_DIR_ROOT)
    _git(repo, "commit", "-q", "-m", "first halt")
    assert halted_run.mark_resumed(rd)
    _git(repo, "add", RECORD_DIR_ROOT)
    _git(repo, "commit", "-q", "-m", "resumed")
    (repo / "peer-dirty.txt").write_text("peer\n", encoding="utf-8")
    before = _git(repo, "rev-parse", "HEAD")

    commits: dict = {}
    assert failed_fire_halt.halt_failed_fire(repo, [RUN], ERROR, commits) == [RUN]

    marker = json.loads((rd / "halted.json").read_text(encoding="utf-8"))
    assert marker["status"] == "halted" and marker["halted"] == "run_failed"
    assert commits[RUN]["committed"] is True, commits
    assert _git(repo, "rev-parse", "HEAD") != before
    assert json.loads(_git(repo, "show", f"HEAD:{RECORD_DIR_ROOT}/{RUN}/halted.json"))["status"] == "halted"
    paths = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert paths and all(f"/{RUN}/" in p for p in paths), paths
    assert "peer-dirty.txt" not in _git(repo, "show", "--name-only", "--format=", "HEAD")
    got = _trailers(repo)
    assert set(got) >= set(first_trailers) - {"Checkpoint-Run-Halted"}
    assert got["Halted-By"] == "run_failed" and "required property" in got["Halt-Reason"]
    assert "Checkpoint-Run-Halted" in got


def _halt_through_headless(repo: Path, result: object, error: str, script: str = "fire.workflow.js") -> dict:
    from coordinator_core.ops.workflow_fire import headless

    record = {"repo_root": str(repo), "script_path": script}
    headless._halt_failed_fire(record, result, error)
    return record


def test_incomplete_wake_digest_fire_commits_a_halt_naming_it(repo: Path) -> None:
    from coordinator_core.ops.workflow_fire import headless

    _record(repo, RUN)
    digest = {"schema": "wake-digest", "run_id": RUN, "outcome": "incomplete", "completed": False}
    error = "RuntimeError: " + headless._result_failure(digest)
    assert "wake digest is incomplete" in error

    record = _halt_through_headless(repo, digest, error)

    assert record.get("halt_error") is None, record
    assert record["halted_runs"] == [RUN]
    assert record["halt_commits"][RUN]["committed"] is True, record
    assert "wake digest is incomplete" in _trailers(repo)["Halt-Reason"]
    assert json.loads(_git(repo, "show", f"HEAD:{RECORD_DIR_ROOT}/{RUN}/halted.json"))["status"] == "halted"


def test_codex_parse_failure_inside_halt_commit_still_commits_a_halt_naming_it(repo: Path) -> None:
    _record(repo, RUN)
    error = ("AgentFailed: codex returned no schema-valid result: Expecting ',' delimiter: line 1 column 6093 "
             "(char 6092) (one repair re-ask also failed: codex returned no schema-valid result: Expecting ',' delimiter)")

    record = _halt_through_headless(repo, None, error, script=f"{RUN}.workflow.js")

    assert record["halted_runs"] == [RUN]
    assert record["halt_commits"][RUN]["committed"] is True, record
    assert "Expecting ',' delimiter" in _trailers(repo)["Halt-Reason"]

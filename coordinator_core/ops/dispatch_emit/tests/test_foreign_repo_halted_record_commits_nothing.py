"""In a repo without coordinator state a halted record commit and the abandoned-run reaper commit nothing."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import abandoned_runs, process_artifact_policy, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.foreign_repo, pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261015T000000-aaaaaa"
SIZING = "state/sizings/orphan.yaml"
SELF = "5e1f0000-0000-4000-8000-0000000000b3"
RECORD_FILES = ("ask.md", "run.json")


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _exclude_lines(root: Path) -> list[str]:
    target = root / ".git" / "info" / "exclude"
    return target.read_text(encoding="utf-8").splitlines() if target.is_file() else []


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "run.mjs").write_text("// script\n", encoding="utf-8")
    _git(["add", "run.mjs"], root)
    _git(["commit", "-q", "-m", "script"], root)
    assert not process_artifact_policy.hosts_coordinator_state(root)
    return root


def _write_record(root: Path, *, age: float = 0.0) -> None:
    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "ask.md").write_text("ask\n", encoding="utf-8")
    (rec / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    if age:
        stamp = time.time() - age
        for f in rec.iterdir():
            os.utime(f, (stamp, stamp))


def _record_paths() -> set[str]:
    return {f"{RECORD_DIR_ROOT}/{RUN}/{name}" for name in RECORD_FILES}


def _halt(root: Path) -> dict:
    return terminal_commit._handler(
        {
            "script_path": "run.mjs", "run_id": RUN, "session_id": SELF,
            HALTED_RECORD_PARAM: {"halted": "refusal", "reason": "r"},
        },
        repo_root=root / ".git",
    )


def test_halted_record_commit_lands_nothing_and_names_the_withheld_paths(repo):
    _write_record(repo)
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = _halt(repo)

    assert out.get("committed") is False and out.get("nothing_to_commit") is True, out
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
    withheld = out["withheld"]
    assert _record_paths() <= set(withheld)
    assert set(withheld.values()) == {process_artifact_policy.WITHHELD_REASON}
    assert _git(["status", "--porcelain", "-uall"], repo) == ""
    assert "/" + f"{RECORD_DIR_ROOT}/{RUN}/" in _exclude_lines(repo)
    assert not (repo / "archive").exists()


def test_halted_record_commit_second_run_adds_no_duplicate_exclude_lines(repo):
    _write_record(repo)
    _halt(repo)
    first = _exclude_lines(repo)

    _write_record(repo)
    out = _halt(repo)

    assert out.get("committed") is False, out
    assert _exclude_lines(repo) == first
    assert len(first) == len(set(first))


def test_onboarded_control_still_commits_the_halted_record(repo):
    (repo / "archive").mkdir()
    _write_record(repo)

    out = _halt(repo)

    assert out.get("committed") is True, out
    landed = set(_git(["show", "--name-only", "--format=", out["sha"]], repo).split())
    assert f"{RECORD_DIR_ROOT}/{RUN}/ask.md" in landed


def test_abandoned_run_reaper_commits_nothing_and_reports_withheld(repo):
    (repo / "state" / "sizings").mkdir(parents=True)
    (repo / SIZING).write_text("a: 1\n", encoding="utf-8")
    _write_record(repo, age=run_token.TTL_SECONDS + 3600)
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = abandoned_runs._handler({}, repo_root=repo / ".git")

    assert out["ok"] is True, out
    assert out["reaped"] == [RUN]
    assert out["committed_sha"] is None
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
    skipped = {s["path"]: s["reason"] for s in out["skipped"] if s["reason"] == process_artifact_policy.WITHHELD_REASON}
    assert _record_paths() <= set(skipped)
    assert _git(["status", "--porcelain", "-uall"], repo) == ""
    lines = _exclude_lines(repo)
    assert all(f"/{p}" in lines for p in _record_paths())
    assert not (repo / "archive").exists()

    again = abandoned_runs._handler({}, repo_root=repo / ".git")
    assert again["reaped"] == [] and again["committed_sha"] is None
    assert _exclude_lines(repo) == lines

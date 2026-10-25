"""sibling_claims against a real tmp_path git repo: halted-run extra paths and the single index parse."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import sibling_claims
from coordinator_core.ops.dispatch_emit.tests.test_sibling_claims import OWN, SELF, _declare, _halt, _stamped
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

HALT_AT = "2026-10-24T01:00:00Z"
START_AT = "2026-10-24T02:00:00Z"
EXTRA = "src/extra.py"
EXTRA2 = "src/extra2.py"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), capture_output=True, check=True, **no_console_creationflags())


def _blob(text: str) -> str:
    from coordinator_core.ops.dispatch_emit.kept_work import blob_sha

    return blob_sha(text.encode("utf-8"))


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    root = tmp_path / "grepo"
    (root / "src").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    for rel in (EXTRA, EXTRA2):
        (root / rel).write_bytes(b"base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    return root


def _stage_record(root: Path, run_id: str, stage: str, started: str, activity: str) -> None:
    (root / "state" / "warp" / run_id / "run-stage.json").write_text(
        json.dumps({"stage": stage, "started_at": started, "last_activity_at": activity}), encoding="utf-8"
    )


def _halted_extra_holder(root: Path, tag: str, path: str, b_h: str | None) -> str:
    run_id = _stamped(tag)
    extra = {"review_declared_paths": [path]}
    if b_h is not None:
        extra["write_baseline"] = {path: b_h}
    rdir = _declare(root, run_id, **extra)
    _halt(rdir)
    _stage_record(root, run_id, "halted", "2026-10-24T00:00:00Z", HALT_AT)
    return run_id


def _landing_run(root: Path, baseline: dict) -> None:
    rdir = _declare(root, OWN, write_baseline=baseline)
    assert rdir.is_dir()
    _stage_record(root, OWN, "staged", START_AT, START_AT)


def test_halted_extra_dirty_only_with_the_landing_runs_bytes_claims_nothing(git_repo):
    _halted_extra_holder(git_repo, "fa0001", EXTRA, _blob("base\n"))
    _landing_run(git_repo, {EXTRA: _blob("base\n")})
    (git_repo / EXTRA).write_bytes(b"landing bytes\n")
    assert sibling_claims.sibling_run_claims(git_repo, OWN, [EXTRA]) == {}


def test_halted_extra_with_pre_stage_foreign_dirt_is_claimed(git_repo):
    run_id = _halted_extra_holder(git_repo, "fa0002", EXTRA, _blob("base\n"))
    _landing_run(git_repo, {EXTRA: _blob("foreign\n")})
    (git_repo / EXTRA).write_bytes(b"foreign\nlanding\n")
    assert sibling_claims.sibling_run_claims(git_repo, OWN, [EXTRA]) == {EXTRA: run_id}


def test_halted_extra_still_equal_to_its_baseline_is_not_dirty_so_not_claimed(git_repo):
    _halted_extra_holder(git_repo, "fa0003", EXTRA, _blob("base\n"))
    _landing_run(git_repo, {EXTRA: _blob("base\n")})
    assert sibling_claims.sibling_run_claims(git_repo, OWN, [EXTRA]) == {}


def test_halted_extra_with_no_recorded_baseline_claims_nothing_even_when_dirty(git_repo):
    """The 432570 shape: a declared path, no `write_baseline` and no kept work, dirty with someone else's bytes."""
    _halted_extra_holder(git_repo, "432570", EXTRA, None)
    _landing_run(git_repo, {EXTRA: _blob("base\n")})
    (git_repo / EXTRA).write_bytes(b"landing bytes\n")
    assert sibling_claims.sibling_run_claims(git_repo, OWN, [EXTRA]) == {}


def test_index_file_is_parsed_at_most_once_per_call_with_two_halted_runs(git_repo, monkeypatch):
    from coordinator_core.git import git_state

    first = _halted_extra_holder(git_repo, "fa0004", EXTRA, _blob("base\n"))
    second = _halted_extra_holder(git_repo, "fa0005", EXTRA2, _blob("base\n"))
    _landing_run(git_repo, {EXTRA: _blob("foreign\n"), EXTRA2: _blob("foreign\n")})
    (git_repo / EXTRA).write_bytes(b"foreign\nlanding\n")
    (git_repo / EXTRA2).write_bytes(b"foreign\nlanding\n")
    parses: list = []
    real = git_state._parse_index_bytes
    monkeypatch.setattr(git_state, "_parse_index_bytes", lambda *a, **k: parses.append(1) or real(*a, **k))
    claims = sibling_claims.sibling_run_claims(git_repo, OWN, [EXTRA, EXTRA2])
    assert claims == {EXTRA: first, EXTRA2: second}
    assert len(parses) == 1

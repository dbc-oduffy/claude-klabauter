"""A checkpoint of a run whose baseline predates a non-run peer's edit to the same file lands only
the run's own hunk and names the peer hunk as withheld."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import checkpoint_hunks
from coordinator_core.ops.dispatch_emit import peer_hunk_gate, run_write_prints
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "runA"
HEAD_LINES = [f"line {i:02d}\n" for i in range(1, 21)]


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
    _git(["config", "core.autocrlf", "false"], root)
    (root / "f.txt").write_text("".join(HEAD_LINES), encoding="utf-8", newline="")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    record = root / RECORD_DIR_ROOT / RUN
    record.mkdir(parents=True)
    head_blob = _git(["rev-parse", "HEAD:f.txt"], root).strip()
    (record / "manifest.json").write_text(json.dumps({"write_baseline": {"f.txt": head_blob}}), encoding="utf-8")
    return root


def test_non_run_peer_hunk_after_the_run_print_is_withheld_and_named(repo):
    own = list(HEAD_LINES)
    own[1] = "line 02 run edit\n"
    (repo / "f.txt").write_text("".join(own), encoding="utf-8", newline="")
    run_write_prints.record(repo, RUN, ["f.txt"])
    peer = list(own)
    peer[17] = "line 18 peer edit\n"
    (repo / "f.txt").write_text("".join(peer), encoding="utf-8", newline="")
    reasons: dict[str, str] = {}

    blobs, withheld = checkpoint_hunks.checkpoint_own_blobs(repo, RUN, ["f.txt"], reasons=reasons)

    assert withheld == [], (blobs, withheld, reasons)
    landed = _git(["cat-file", "blob", blobs["f.txt"]], repo)
    assert landed == "".join(own)
    assert "line 18" in reasons.get("f.txt", ""), reasons


def test_first_checkpoint_after_a_write_time_print_withholds_the_peer_hunk(repo):
    own = list(HEAD_LINES)
    own[1] = "line 02 run edit\n"
    (repo / "f.txt").write_text("".join(own), encoding="utf-8", newline="")
    run_write_prints.record(repo, RUN, ["f.txt"])
    peer = list(own)
    peer[17] = "line 18 peer edit\n"
    (repo / "f.txt").write_text("".join(peer), encoding="utf-8", newline="")
    reasons: dict[str, str] = {}

    blobs, withheld = checkpoint_hunks.checkpoint_own_blobs(repo, RUN, ["f.txt"], reasons=reasons)

    assert withheld == []
    assert _git(["cat-file", "blob", blobs["f.txt"]], repo) == "".join(own)
    assert "line 18" in reasons.get("f.txt", ""), reasons


def test_first_checkpoint_without_any_print_withholds_the_file_and_names_it(repo):
    peer = list(HEAD_LINES)
    peer[1] = "line 02 run edit\n"
    peer[17] = "line 18 peer edit\n"
    (repo / "f.txt").write_text("".join(peer), encoding="utf-8", newline="")
    reasons: dict[str, str] = {}

    blobs, withheld = checkpoint_hunks.checkpoint_own_blobs(repo, RUN, ["f.txt"], reasons=reasons)

    assert (blobs, withheld) == ({}, ["f.txt"])
    assert "no write-time print" in reasons["f.txt"]
    assert run_write_prints.read(repo, RUN) is None


def test_a_print_of_another_path_does_not_cover_this_one(repo):
    (repo / "f.txt").write_text("".join(HEAD_LINES[:-1]), encoding="utf-8", newline="")
    run_write_prints.record(repo, RUN, ["other.txt"])

    assert checkpoint_hunks.checkpoint_own_blobs(repo, RUN, ["f.txt"]) == ({}, ["f.txt"])

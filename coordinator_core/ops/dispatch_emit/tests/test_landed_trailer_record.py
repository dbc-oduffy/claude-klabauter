"""landed_trailer_record: the in-process trailer record equals the spawned ``git log`` answer."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landed_trailer_record as rec
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PLAN_TRAILER
from coordinator_core.ops.dispatch_emit.checkpoint_stamp import STOP_RULE_TRAILER
from coordinator_core.win_portability import no_console_creationflags

PLAN = "docs/plans/p.md"
_FORMAT = (
    "--format=%s\x1f%(trailers:key=Checkpoint-Plan,valueonly,separator=%x1d)"
    f"\x1f%(trailers:key={HALT_PLAN_TRAILER},valueonly,separator=%x1d)"
    "\x1f%(trailers:key=Halted-By,valueonly,separator=%x1d)"
    "\x1f%(trailers:key=Lands-Chunks,valueonly,separator=%x1d)\x1f%H"
    f"\x1f%(trailers:key={STOP_RULE_TRAILER},valueonly,separator=%x1d)\x1e"
)


class Repo:
    def __init__(self, root: Path):
        self.root = root
        self.clock = 1_700_000_000
        root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.commit("seed")

    def git(self, *args, date=None) -> str:
        env = dict(os.environ)
        if date is not None:
            env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = f"{date} +0000"
        return subprocess.run(
            ["git", *args], cwd=str(self.root), capture_output=True, text=True, check=True,
            env=env, **no_console_creationflags(),
        ).stdout

    def commit(self, message: str, date=None) -> str:
        self.clock += 10
        self.git("commit", "-q", "--allow-empty", "-m", message, date=date or self.clock)
        return self.git("rev-parse", "HEAD").strip()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    def spawned(self) -> list:
        out = self.git(
            "log", "-n", "500", "--grep=^Checkpoint-Plan: ", f"--grep=^{HALT_PLAN_TRAILER}: ",
            "--grep=^warp: record halted ", _FORMAT,
        )
        return rec._parse_stdout(out)

    def seed_record(self) -> None:
        out = self.git(
            "log", "-n", "500", "--grep=^Checkpoint-Plan: ", f"--grep=^{HALT_PLAN_TRAILER}: ",
            "--grep=^warp: record halted ", _FORMAT,
        )
        rec.refresh_from_git_log(self.root, out, self.head())


CHECKPOINT = f"C1: land it\n\nCheckpoint-Plan: {PLAN}\nLands-Chunks: C1, C2\n"
HALT = (
    f"warp: record halted run-1\n\n{HALT_PLAN_TRAILER}: {PLAN}\nHalted-By: ledger-check\n"
    f"Lands-Chunks: C3\n{STOP_RULE_TRAILER}: C4\n"
)
FOLDED = f"C5: folded\n\nCheckpoint-Plan: {PLAN}\nLands-Chunks: C5,\n C6\n"
PROSE = f"C7: prose\n\nCheckpoint-Plan: {PLAN}\nsome prose after the line\n"
WARP = "warp: record halted run-2\n\nbody only, no trailers\n"


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path / "r")


def test_no_record_returns_none(repo):
    assert rec.trailer_entries(repo.root) is None


def test_zero_new_commits_returns_record(repo):
    repo.commit(CHECKPOINT)
    repo.seed_record()
    assert rec.trailer_entries(repo.root) == repo.spawned()


@pytest.mark.parametrize("n", [1, 3])
def test_delta_parity_with_spawned_log(repo, n):
    repo.commit(CHECKPOINT)
    repo.seed_record()
    for message in [HALT, FOLDED, PROSE, WARP, "plain: no trailers"][:n]:
        repo.commit(message)
    got = rec.trailer_entries(repo.root)
    assert got == repo.spawned()


def test_every_trailer_shape_matches_git(repo):
    repo.seed_record()
    for message in (CHECKPOINT, HALT, FOLDED, PROSE, WARP, "plain"):
        repo.commit(message)
    got = rec.trailer_entries(repo.root)
    assert got == repo.spawned()
    assert len(got) == 5


def test_second_call_reads_persisted_head(repo, monkeypatch):
    repo.seed_record()
    repo.commit(CHECKPOINT)
    first = rec.trailer_entries(repo.root)
    monkeypatch.setattr(rec, "_delta", lambda *a: pytest.fail("walked again"))
    assert rec.trailer_entries(repo.root) == first == repo.spawned()


def test_merge_with_older_side_branch_commit_is_found(repo):
    repo.git("checkout", "-q", "-b", "side")
    repo.commit(CHECKPOINT, date=repo.clock - 5)
    repo.git("checkout", "-q", "main")
    repo.commit("main one")
    repo.commit("main two")
    repo.seed_record()
    repo.clock += 100
    repo.git("merge", "--no-ff", "-q", "-m", "Merge side", "side", date=repo.clock)
    got = rec.trailer_entries(repo.root)
    assert got == repo.spawned()
    assert any(e[0].startswith("C1:") for e in got)


def test_merge_of_old_ancestor_is_parity_or_none(repo):
    repo.commit(CHECKPOINT)
    old = repo.head()
    repo.git("checkout", "-q", "-b", "topic")
    repo.commit("topic one")
    repo.commit(HALT)
    repo.seed_record()
    repo.git("checkout", "-q", "main")
    repo.commit("main moved")
    repo.git("checkout", "-q", "topic")
    repo.clock += 100
    repo.git("merge", "--no-ff", "-q", "-m", "Merge old main", old, date=repo.clock)
    repo.commit(FOLDED)
    got = rec.trailer_entries(repo.root)
    assert got is None or got == repo.spawned()


def test_rewrite_returns_none(repo):
    repo.commit("a")
    repo.commit(CHECKPOINT)
    repo.seed_record()
    repo.git("reset", "-q", "--hard", "HEAD~2")
    repo.commit(CHECKPOINT.replace("C1", "C9"))
    assert rec.trailer_entries(repo.root) is None


def test_reset_to_ancestor_returns_none(repo):
    repo.commit(CHECKPOINT)
    repo.seed_record()
    repo.git("reset", "-q", "--hard", "HEAD~1")
    assert rec.trailer_entries(repo.root) is None


def test_delta_over_budget_returns_none_and_within_budget_is_served(repo, monkeypatch):
    for _ in range(14):
        repo.commit("filler")
    repo.seed_record()
    monkeypatch.setattr(rec, "MAX_WALK_READS", 10)
    for _ in range(3):
        repo.commit(CHECKPOINT)
    assert rec.trailer_entries(repo.root) == repo.spawned()
    for _ in range(12):
        repo.commit(CHECKPOINT)
    assert rec.trailer_entries(repo.root) is None


@pytest.mark.parametrize("payload", ["{not json", '{"head": 3, "entries": []}', '{"head": "x", "entries": [["a"]]}', ""])
def test_corrupt_record_returns_none(repo, payload):
    repo.commit(CHECKPOINT)
    repo.seed_record()
    rec.record_path(repo.root).write_text(payload, encoding="utf-8")
    repo.commit("more")
    assert rec.trailer_entries(repo.root) is None


def test_entries_capped_at_log_window(repo):
    stdout = "".join(f"s{i}\x1f\x1f\x1f\x1f\x1f{i:040x}\x1f\x1e\n" for i in range(rec.MAX_ENTRIES + 7))
    out = rec.refresh_from_git_log(repo.root, stdout, repo.head())
    assert len(out) == rec.MAX_ENTRIES
    assert out[0][5] == f"{0:040x}"
    assert len(rec._load(rec.record_path(repo.root))["entries"]) == rec.MAX_ENTRIES


def test_stale_writer_never_regresses_record(repo):
    repo.seed_record()
    old_head = repo.head()
    repo.commit(CHECKPOINT)
    new_head = repo.head()
    common = rec.resolve_git_common_dir(repo.root)
    path = rec.record_path(repo.root)
    rec._persist(common, path, new_head, [("n",) * 7], False)
    rec._persist(common, path, old_head, [("o",) * 7], True)
    assert rec._load(path)["head"] == new_head
    rec._persist(common, path, old_head, [("o",) * 7], False)
    assert rec._load(path)["head"] == new_head


def test_reseed_replaces_unrelated_head(repo):
    repo.commit("a")
    repo.seed_record()
    repo.git("reset", "-q", "--hard", "HEAD~1")
    repo.commit(CHECKPOINT)
    repo.seed_record()
    assert rec._load(rec.record_path(repo.root))["head"] == repo.head()
    assert rec.trailer_entries(repo.root) == repo.spawned()


def test_record_lives_under_git_common_dir(repo):
    repo.seed_record()
    assert rec.record_path(repo.root) == repo.root / ".git" / "coordinator" / "landed-trailers.json"
    assert rec.record_path(repo.root).is_file()

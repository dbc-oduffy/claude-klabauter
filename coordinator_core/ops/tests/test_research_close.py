"""research.close: scouts commits nothing; corpus and deep make exactly one commit."""

from __future__ import annotations

import asyncio
import datetime
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.research_close import _handler

pytestmark = pytest.mark.spawns_process

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, capture_output=True, text=True, check=True, creationflags=_NOWIN,
    )
    return r.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "seed.txt").write_text("seed\n")
    _git(r, "add", "seed.txt")
    _git(r, "commit", "-q", "-m", "seed")
    return r


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    s = tmp_path / "scratch"
    s.mkdir()
    (s / "digest-a.md").write_text("alpha\n")
    (s / "digest-b.md").write_text("beta\n")
    (s / "final.md").write_text("final body\n")
    return s


def _close(repo: Path, scratch: Path, tier: str, **extra) -> dict:
    params = {"scratch_dir": str(scratch), "tier": tier, "run_id": "r1", "topic_slug": "topic"}
    params.update(extra)
    return asyncio.run(_handler(params, repo / ".git"))


def _count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD"))


def test_scouts_commits_nothing_and_writes_digest_only(repo, scratch):
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, scratch, "scouts")
    assert out["committed"] is False
    assert out["digest_path"] == (scratch / "digest.md").as_posix()
    assert (scratch / "digest.md").read_text() == "alpha\n\nbeta\n"
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("tier", ["corpus", "deep"])
def test_one_commit_whose_delta_is_the_copied_paths(repo, scratch, tier):
    before = _count(repo)
    out = _close(repo, scratch, tier, outputs=["final.md", "digest-a.md"])
    assert out["exit_code"] == 0 and out["committed"] is True
    assert _count(repo) == before + 1
    assert _git(repo, "rev-parse", "HEAD") == out["sha"]
    changed = set(_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines())
    assert changed == set(out["paths"])
    assert len(changed) == 2 and all(p.startswith("docs/research/") for p in changed)
    assert _git(repo, "log", "-1", "--format=%s").startswith("research(topic): close r1")


def test_second_close_is_noop_returning_same_sha(repo, scratch):
    first = _close(repo, scratch, "corpus", outputs=["final.md"])
    n = _count(repo)
    second = _close(repo, scratch, "corpus", outputs=["final.md"])
    assert second["committed"] is True and second["sha"] == first["sha"]
    assert _count(repo) == n


def test_uncommitted_destination_is_committed_not_reported_landed(repo, scratch):
    dest = repo / "docs" / "research" / f"{datetime.date.today().isoformat()}-topic"
    dest.mkdir(parents=True)
    shutil.copyfile(scratch / "final.md", dest / "final.md")
    before = _count(repo)
    out = _close(repo, scratch, "corpus", outputs=["final.md", "digest-a.md"])
    assert out["exit_code"] == 0 and out["committed"] is True
    assert _count(repo) == before + 1
    assert out["sha"] == _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "log", "-1", "--format=%s") == "research(topic): close r1"


def test_missing_output_is_refusal_without_partial_commit(repo, scratch):
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, scratch, "corpus", outputs=["final.md", "nope.md"])
    assert out["exit_code"] == 1 and out["committed"] is False
    assert _git(repo, "rev-parse", "HEAD") == head
    assert not (repo / "docs").exists()


def test_different_existing_destination_is_not_overwritten(repo, scratch):
    out = _close(repo, scratch, "corpus", outputs=["final.md"])
    (scratch / "final.md").write_text("changed\n")
    again = _close(repo, scratch, "corpus", outputs=["final.md"])
    assert again["exit_code"] == 1 and "refusing to overwrite" in again["error"]
    assert (repo / out["paths"][0]).read_text() == "final body\n"


def test_output_escaping_scratch_is_refused(repo, scratch):
    out = _close(repo, scratch, "corpus", outputs=["../seed.txt"])
    assert out["exit_code"] == 1


def test_unsafe_slug_refused(repo, scratch):
    out = _close(repo, scratch, "corpus", topic_slug="../x")
    assert out["exit_code"] == 1


def test_process_time_under_brightline(repo, scratch):
    t0 = time.process_time()
    out = _close(repo, scratch, "corpus", outputs=["final.md"])
    assert out["committed"] is True
    assert time.process_time() - t0 < 0.5


def test_a_secret_shaped_value_refuses_the_close_before_any_copy(repo, scratch):
    (scratch / "final.md").write_text("config\nDB_PASSWORD = 'hunter2Xq8v3LmN0pR7'\n", encoding="utf-8")
    before = _count(repo)
    out = _close(repo, scratch, "corpus", outputs=["final.md"])
    assert out["exit_code"] == 1 and out["secret_hits"] == ["final.md:2: secret-named assignment"]
    assert "hunter2" not in str(out)
    assert _count(repo) == before and not (repo / "docs" / "research").exists()


def test_a_recorded_key_name_is_not_a_secret(repo, scratch):
    (scratch / "final.md").write_text("The service reads API_KEY and DB_PASSWORD from env.\n", encoding="utf-8")
    assert _close(repo, scratch, "corpus", outputs=["final.md"])["exit_code"] == 0


def test_an_untracked_research_file_written_during_the_run_is_reported(repo, scratch):
    stray = repo / "docs" / "research" / "2026-10-09-elsewhere.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("duplicate\n", encoding="utf-8")
    out = _close(repo, scratch, "corpus", outputs=["final.md"])
    assert out["exit_code"] == 0
    assert out["strays"] == ["docs/research/2026-10-09-elsewhere.md"]


def test_every_segment_dir_is_archived_at_its_relative_path(repo, scratch):
    (scratch / "web").mkdir()
    (scratch / "web" / "synthesis.md").write_text("web synthesis\n")
    (scratch / "mail").mkdir()
    (scratch / "mail" / "_em.jsonl").write_text("{}\n")
    out = _close(repo, scratch, "corpus", segment_dirs=["web"])
    assert out["exit_code"] == 0, out
    names = {p.split("/", 3)[-1] for p in out["paths"]}
    assert {"final.md", "web/synthesis.md"} <= names
    assert not any(n.startswith("mail/") for n in names)


def test_a_missing_or_unsafe_segment_dir_is_refused(repo, scratch):
    assert _close(repo, scratch, "corpus", segment_dirs=["web"])["exit_code"] != 0
    assert _close(repo, scratch, "corpus", segment_dirs=["../x"])["exit_code"] != 0

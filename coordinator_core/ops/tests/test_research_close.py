"""research.close: every class makes exactly one commit carrying the trail record."""

from __future__ import annotations

import asyncio
import datetime
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.research_close import _handler

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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


def _trail_files(repo: Path) -> set[str]:
    return set(_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines())


def test_scouts_commits_the_digest_and_trail_record(repo, scratch):
    before = _count(repo)
    out = _close(repo, scratch, "scouts")
    assert out["exit_code"] == 0 and out["committed"] is True, out
    assert (scratch / "digest.md").read_text() == "alpha\n\nbeta\n"
    assert _count(repo) == before + 1
    digest = f"docs/research/{datetime.date.today().isoformat()}-topic/digest.md"
    assert _trail_files(repo) == {digest, "docs/research/trail/r1.yaml"}
    assert _git(repo, "show", f"HEAD:{digest}") == "alpha\n\nbeta"
    assert _git(repo, "status", "--porcelain") == ""


def test_scouts_without_digests_refuses_with_no_commit(repo, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, empty, "scouts")
    assert out["exit_code"] == 1 and out["committed"] is False
    assert _git(repo, "rev-parse", "HEAD") == head and not (repo / "docs").exists()


def _write_sizing(repo: Path, rel: str = "state/sizings/x.yaml") -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("size: S\n", encoding="utf-8")
    return p


def test_a_cited_sizing_lands_in_the_trail_commit_and_is_tracked(repo, scratch):
    _write_sizing(repo)
    out = _close(repo, scratch, "corpus", outputs=["final.md"], sizing="state/sizings/x.yaml")
    assert out["exit_code"] == 0, out
    assert "state/sizings/x.yaml" in _trail_files(repo)
    assert _git(repo, "ls-files", "state/sizings/x.yaml") == "state/sizings/x.yaml"
    assert _git(repo, "status", "--porcelain") == ""


def test_a_missing_sizing_is_refused_with_no_file_written(repo, scratch):
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, scratch, "corpus", sizing="state/sizings/nope.yaml")
    assert out["exit_code"] == 1 and out["committed"] is False and "nope.yaml" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head and not (repo / "docs").exists()
    assert not (repo / "state").exists()


def test_a_sizing_escaping_the_repo_is_refused(repo, scratch):
    out = _close(repo, scratch, "corpus", sizing="../outside.yaml")
    assert out["exit_code"] == 1 and out["committed"] is False
    assert not (repo / "docs").exists()


def test_reclose_with_a_sizing_returns_the_landed_sha(repo, scratch):
    _write_sizing(repo)
    first = _close(repo, scratch, "corpus", outputs=["final.md"], sizing="state/sizings/x.yaml")
    n = _count(repo)
    second = _close(repo, scratch, "corpus", outputs=["final.md"], sizing="state/sizings/x.yaml")
    assert second["committed"] is True and second["sha"] == first["sha"] and _count(repo) == n


@pytest.mark.parametrize("tier", ["scouts", "corpus", "deep"])
def test_trail_record_is_in_the_commit_with_repo_relative_paths(repo, scratch, tier):
    _write_sizing(repo)
    out = _close(repo, scratch, tier, sizing="state/sizings/x.yaml")
    assert out["exit_code"] == 0, out
    assert "docs/research/trail/r1.yaml" in _trail_files(repo)
    text = _git(repo, "show", "HEAD:docs/research/trail/r1.yaml")
    assert f"value_class: \"{tier}\"" in text and f"tier: \"{tier}\"" in text
    assert 'destination: "repo"' in text and 'sizing: "state/sizings/x.yaml"' in text
    assert "\\" not in text and ":/" not in text and "paths:\n  - \"docs/research/" in text


def test_value_class_param_is_named_in_the_record_and_validated(repo, scratch):
    assert _close(repo, scratch, "corpus", value_class="nope")["exit_code"] == 1
    assert not (repo / "docs").exists()
    out = _close(repo, scratch, "corpus", value_class="scouts", outputs=["final.md"])
    assert out["exit_code"] == 0
    assert 'value_class: "scouts"' in _git(repo, "show", "HEAD:docs/research/trail/r1.yaml")


def test_an_absolute_sizing_is_refused_with_no_commit(repo, scratch):
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, scratch, "corpus", sizing="/etc/x.yaml")
    assert out["exit_code"] == 1 and "trail record refused" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head and not (repo / "docs").exists()


def test_a_differing_existing_trail_record_is_refused(repo, scratch):
    trail = repo / "docs" / "research" / "trail" / "r1.yaml"
    trail.parent.mkdir(parents=True)
    trail.write_text("run_id: \"other\"\n", encoding="utf-8")
    out = _close(repo, scratch, "corpus", outputs=["final.md"])
    assert out["exit_code"] == 1 and "trail record exists with different content" in out["error"]
    assert trail.read_text(encoding="utf-8") == "run_id: \"other\"\n"
    assert not any((repo / "docs" / "research").glob("20*"))


def test_a_later_day_reclose_is_refused_as_differing(repo, scratch, monkeypatch):
    first = _close(repo, scratch, "corpus", outputs=["final.md"])
    assert first["exit_code"] == 0

    tomorrow = datetime.date.today() + datetime.timedelta(days=1)

    class _Later(datetime.date):
        @classmethod
        def today(cls):
            return tomorrow

    monkeypatch.setattr("coordinator_core.ops.research_close.datetime.date", _Later)
    again = _close(repo, scratch, "corpus", outputs=["final.md"])
    assert again["exit_code"] == 1 and "trail record exists with different content" in again["error"]


@pytest.mark.parametrize("tier", ["corpus", "deep"])
def test_one_commit_whose_delta_is_the_copied_paths(repo, scratch, tier):
    before = _count(repo)
    out = _close(repo, scratch, tier, outputs=["final.md", "digest-a.md"])
    assert out["exit_code"] == 0 and out["committed"] is True
    assert _count(repo) == before + 1
    assert _git(repo, "rev-parse", "HEAD") == out["sha"]
    changed = set(_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines())
    assert changed == set(out["paths"]) | {"docs/research/trail/r1.yaml"}
    assert len(changed) == 3 and all(p.startswith("docs/research/") for p in changed)
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


def test_local_only_archives_outside_the_repo_and_commits_only_the_trail_record(repo, scratch, tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    before = _count(repo)
    out = _close(repo, scratch, "corpus", local_root=local.as_posix())
    assert out["exit_code"] == 0 and out["committed"] is True and out["local_only"] is True, out
    dest = Path(out["destination"])
    assert dest.parent == local.resolve() and (dest / "final.md").read_text() == "final body\n"
    assert out["claims_out"] == (dest / "topic").as_posix()
    assert _count(repo) == before + 1 and _git(repo, "status", "--porcelain") == ""
    assert _trail_files(repo) == {"docs/research/trail/r1.yaml"}
    text = _git(repo, "show", "HEAD:docs/research/trail/r1.yaml")
    assert 'destination: "local-only"' in text and "paths" not in text
    assert "output_count: 3" in text
    assert str(local) not in text and local.as_posix() not in text and "final body" not in text


def test_local_only_with_no_artifact_refuses_with_no_commit(repo, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    local = tmp_path / "local"
    local.mkdir()
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, empty, "corpus", local_root=local.as_posix())
    assert out["exit_code"] == 1 and out["committed"] is False
    assert _git(repo, "rev-parse", "HEAD") == head and not (repo / "docs").exists()


def test_local_only_reclose_returns_the_landed_sha(repo, scratch, tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    first = _close(repo, scratch, "corpus", local_root=local.as_posix())
    n = _count(repo)
    second = _close(repo, scratch, "corpus", local_root=local.as_posix())
    assert second["exit_code"] == 0 and second["sha"] == first["sha"] and _count(repo) == n


def test_local_only_refuses_a_root_inside_a_checkout(repo, scratch):
    out = _close(repo, scratch, "corpus", local_root=(repo / "corpus").as_posix())
    assert out["exit_code"] != 0 and "inside the git checkout" in out["error"]


@pytest.mark.parametrize("body", [None, "[]", "{}", "not json", '"x"'])
def test_require_claims_refuses_missing_empty_or_non_array(repo, scratch, body):
    if body is not None:
        (scratch / "claims.json").write_text(body)
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, scratch, "corpus", require_claims=["claims.json"])
    assert out["exit_code"] == 1 and out["committed"] is False and out["deliverable"] is False
    assert "claims.json holds no claims" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head and not (repo / "docs").exists()


def test_require_claims_with_a_claim_closes_deliverable(repo, scratch):
    (scratch / "claims.json").write_text('[{"id": 1}]')
    out = _close(repo, scratch, "corpus", require_claims=["claims.json"])
    assert out["exit_code"] == 0 and out["committed"] is True and out["deliverable"] is True


def _three_segment_run(scratch: Path) -> None:
    for seg in ("web", "repo"):
        (scratch / seg).mkdir()
        (scratch / seg / "claims.json").write_text('[{"id": 1}]')
        (scratch / seg / "synthesis.md").write_text(f"{seg} synthesis\n")


_SEGS = ["nlm-preflight", "web", "notebooklm", "repo"]
_CLAIMS = ["web/claims.json", "notebooklm/merged-claims.json", "repo/claims.json"]


def test_halted_segment_is_excused_and_named_in_the_reply(repo, scratch):
    _three_segment_run(scratch)
    for seg in ("nlm-preflight", "notebooklm"):
        assert not (scratch / seg).exists()
    out = _close(
        repo, scratch, "corpus", segment_dirs=_SEGS, require_claims=_CLAIMS,
        halted_segments=["nlm-preflight", "notebooklm"],
    )
    assert out["exit_code"] == 0 and out["committed"] is True and out["deliverable"] is True, out
    assert out["halted_segments"] == ["nlm-preflight", "notebooklm"]
    names = {p.split("/", 3)[-1] for p in out["paths"]}
    assert {"web/synthesis.md", "repo/synthesis.md"} <= names
    assert not any(n.startswith("notebooklm/") for n in names)


def test_same_run_without_halted_segments_is_refused(repo, scratch):
    _three_segment_run(scratch)
    out = _close(repo, scratch, "corpus", segment_dirs=_SEGS, require_claims=_CLAIMS)
    assert out["exit_code"] == 1 and out["deliverable"] is False
    assert "notebooklm/merged-claims.json" in out["error"] and "halted_segments" not in out


def test_a_non_halted_segment_with_empty_claims_is_still_refused(repo, scratch):
    _three_segment_run(scratch)
    (scratch / "web" / "claims.json").write_text("[]")
    out = _close(
        repo, scratch, "corpus", segment_dirs=_SEGS, require_claims=_CLAIMS,
        halted_segments=["nlm-preflight", "notebooklm"],
    )
    assert out["exit_code"] == 1 and out["deliverable"] is False
    assert "web/claims.json" in out["error"]
    assert out["halted_segments"] == ["nlm-preflight", "notebooklm"]


def test_a_present_halted_segment_dir_is_archived_as_evidence(repo, scratch):
    _three_segment_run(scratch)
    (scratch / "notebooklm").mkdir()
    (scratch / "notebooklm" / "halt.md").write_text("halted\n")
    out = _close(
        repo, scratch, "corpus", segment_dirs=["web", "notebooklm", "repo"],
        require_claims=_CLAIMS, halted_segments=["notebooklm"],
    )
    assert out["exit_code"] == 0, out
    assert any(p.endswith("notebooklm/halt.md") for p in out["paths"])


def test_a_root_level_claims_entry_is_never_excused(repo, scratch):
    out = _close(
        repo, scratch, "corpus", require_claims=["claims.json"], halted_segments=["claims.json"],
    )
    assert out["exit_code"] == 1 and out["deliverable"] is False


def test_a_halted_name_outside_segment_dirs_is_only_reported(repo, scratch):
    out = _close(repo, scratch, "corpus", halted_segments=["notebooklm"])
    assert out["exit_code"] == 0 and out["halted_segments"] == ["notebooklm"]


def test_halted_segments_is_echoed_on_a_local_only_close(repo, scratch, tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    out = _close(repo, scratch, "corpus", local_root=local.as_posix(), halted_segments=["x"])
    assert out["exit_code"] == 0 and out["halted_segments"] == ["x"]


@pytest.mark.parametrize("bad", ["notebooklm", ["../x"], [1], ["a/b"]])
def test_an_unsafe_halted_segments_is_refused(repo, scratch, bad):
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, scratch, "corpus", halted_segments=bad)
    assert out["exit_code"] == 1 and out["committed"] is False
    assert "halted_segments must be a list" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head


def test_require_claims_escape_is_refused(repo, scratch):
    out = _close(repo, scratch, "corpus", require_claims=["../x.json"])
    assert out["exit_code"] == 1 and "escapes" in out["error"]


def test_without_require_claims_behaviour_is_unchanged(repo, scratch):
    out = _close(repo, scratch, "corpus")
    assert out["exit_code"] == 0 and out["committed"] is True


def _absolute_output(repo: Path, scratch: Path) -> str:
    """Write an output naming a scratch path, a worktree path and an out-of-root path."""
    outside = "/elsewhere/keep/me.md"
    (scratch / "final.md").write_text(
        f"see {scratch}/x.md and {repo}/docs/y.md and {outside}\n", encoding="utf-8"
    )
    return outside


def test_absolute_scratch_and_worktree_paths_are_committed_repo_relative(repo, scratch):
    outside = _absolute_output(repo, scratch)
    out = _close(repo, scratch, "corpus")
    assert out["exit_code"] == 0, out
    dest = f"docs/research/{datetime.date.today().isoformat()}-topic"
    body = _git(repo, "show", f"HEAD:{dest}/final.md")
    assert body == f"see {dest}/x.md and docs/y.md and {outside}"
    assert str(repo) not in body and str(scratch) not in body


def test_reclose_over_a_rewritten_output_returns_the_landed_sha(repo, scratch):
    _absolute_output(repo, scratch)
    first = _close(repo, scratch, "corpus")
    before = _count(repo)
    second = _close(repo, scratch, "corpus")
    assert second["exit_code"] == 0, second
    assert second["sha"] == first["sha"] and _count(repo) == before


def test_non_utf8_output_is_archived_byte_identical(repo, scratch):
    raw = b"\xff\xfe" + str(repo).encode() + b"/docs/y.md\x80"
    (scratch / "final.md").write_bytes(raw)
    out = _close(repo, scratch, "corpus")
    assert out["exit_code"] == 0, out
    dest = repo / f"docs/research/{datetime.date.today().isoformat()}-topic/final.md"
    assert dest.read_bytes() == raw


def test_local_only_keeps_absolute_paths_byte_identical(repo, scratch, tmp_path):
    _absolute_output(repo, scratch)
    local = tmp_path / "local"
    local.mkdir()
    out = _close(repo, scratch, "corpus", local_root=local.as_posix())
    assert out["exit_code"] == 0, out
    assert (Path(out["destination"]) / "final.md").read_bytes() == (scratch / "final.md").read_bytes()

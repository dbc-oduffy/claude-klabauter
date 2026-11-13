"""research.close: every class makes exactly one commit carrying the trail record."""

from __future__ import annotations

import asyncio
import datetime
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.research_close import _handler
from coordinator_core.session import claim_index, touch_record

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
    (r / ".git" / "info" / "exclude").write_text(".coordinator-local/\n")
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


def _run(slug: str = "topic", topic: str | None = None) -> str:
    return f"docs/research/{topic or slug}/{datetime.date.today().isoformat()}-{slug}"


_TRAIL = f"{_run()}/trail.yaml"


def test_scouts_commits_the_digest_and_trail_record(repo, scratch):
    before = _count(repo)
    out = _close(repo, scratch, "scouts")
    assert out["exit_code"] == 0 and out["committed"] is True, out
    assert (scratch / "digest.md").read_text() == "alpha\n\nbeta\n"
    assert _count(repo) == before + 1
    digest = f"{_run()}/synthesis.md"
    assert _trail_files(repo) == {digest, _TRAIL}
    assert out["run_dir"] == _run() and out["workdir"] == f"{_run()}/workdir"
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


@pytest.mark.parametrize("tier", ["scouts", "corpus", "unblock"])
def test_trail_record_is_in_the_commit_with_repo_relative_paths(repo, scratch, tier):
    _write_sizing(repo)
    out = _close(repo, scratch, tier, sizing="state/sizings/x.yaml")
    assert out["exit_code"] == 0, out
    assert _TRAIL in _trail_files(repo)
    text = _git(repo, "show", f"HEAD:{_TRAIL}")
    assert f"value_class: \"{tier}\"" in text and f"tier: \"{tier}\"" in text
    assert 'destination: "repo"' in text and 'sizing: "state/sizings/x.yaml"' in text
    assert "\\" not in text and ":/" not in text and "paths:\n  - \"docs/research/" in text


def test_legacy_tier_deep_closes_and_records_unblock(repo, scratch):
    _write_sizing(repo)
    out = _close(repo, scratch, "deep", sizing="state/sizings/x.yaml")
    assert out["exit_code"] == 0, out
    text = _git(repo, "show", f"HEAD:{_TRAIL}")
    assert 'tier: "unblock"' in text and 'value_class: "unblock"' in text
    assert 'tier: "deep"' not in text


def test_value_class_param_is_named_in_the_record_and_validated(repo, scratch):
    assert _close(repo, scratch, "corpus", value_class="nope")["exit_code"] == 1
    assert not (repo / "docs").exists()
    out = _close(repo, scratch, "corpus", value_class="scouts", outputs=["final.md"])
    assert out["exit_code"] == 0
    assert 'value_class: "scouts"' in _git(repo, "show", f"HEAD:{_TRAIL}")


def test_an_absolute_sizing_is_refused_with_no_commit(repo, scratch):
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, scratch, "corpus", sizing="/etc/x.yaml")
    assert out["exit_code"] == 1 and "trail record refused" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head and not (repo / "docs").exists()


def test_a_differing_existing_trail_record_is_refused(repo, scratch):
    trail = repo / "docs" / "research" / "trail" / "r1.yaml"
    trail.parent.mkdir(parents=True)
    trail.write_text("run_id: \"other\"\n", encoding="utf-8")
    _git(repo, "add", "docs")
    _git(repo, "commit", "-q", "-m", "legacy trail")
    out = _close(repo, scratch, "corpus", outputs=["final.md"])
    assert out["exit_code"] == 1 and "trail record exists with different content" in out["error"]
    assert trail.read_text(encoding="utf-8") == "run_id: \"other\"\n"
    assert not (repo / "docs" / "research" / "topic").exists()


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


def test_a_different_run_id_the_same_day_files_under_a_versioned_run_dir(repo, scratch):
    first = _close(repo, scratch, "corpus", outputs=["final.md"])
    first_trail = (repo / _TRAIL).read_bytes()
    (scratch / "final.md").write_text("second body\n")
    second = _close(repo, scratch, "corpus", outputs=["final.md"], run_id="r2")
    assert second["exit_code"] == 0 and second["committed"] is True, second
    assert second["run_dir"] == f"{_run()}-v2" and second["trail"] == f"{_run()}-v2/trail.yaml"
    assert 'run_id: "r2"' in (repo / second["trail"]).read_text(encoding="utf-8")
    assert (repo / _TRAIL).read_bytes() == first_trail
    assert (repo / _run() / "synthesis.md").exists() is False
    assert (repo / _run() / "outputs" / "final.md").read_text() == "final body\n"
    third = _close(repo, scratch, "corpus", outputs=["final.md"], run_id="r3")
    assert third["run_dir"] == f"{_run()}-v3"
    n = _count(repo)
    again = _close(repo, scratch, "corpus", outputs=["final.md"], run_id="r2")
    assert again["sha"] == second["sha"] and again["run_dir"] == second["run_dir"] and _count(repo) == n
    assert first["run_dir"] == _run()


def test_a_same_run_id_drift_the_same_day_is_not_versioned(repo, scratch):
    assert _close(repo, scratch, "corpus", outputs=["final.md"])["exit_code"] == 0
    (scratch / "final.md").write_text("changed\n")
    again = _close(repo, scratch, "corpus", outputs=["final.md"])
    assert again["exit_code"] == 1 and "different content" in again["error"]
    assert not (repo / f"{_run()}-v2").exists()


@pytest.mark.parametrize("tier", ["corpus", "unblock"])
def test_one_commit_whose_delta_is_the_copied_paths(repo, scratch, tier):
    before = _count(repo)
    out = _close(repo, scratch, tier, outputs=["final.md", "digest-a.md"])
    assert out["exit_code"] == 0 and out["committed"] is True
    assert _count(repo) == before + 1
    assert _git(repo, "rev-parse", "HEAD") == out["sha"]
    changed = set(_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines())
    assert changed == set(out["paths"]) | {_TRAIL}
    assert len(changed) == 3 and all(p.startswith("docs/research/") for p in changed)
    assert _git(repo, "log", "-1", "--format=%s").startswith("research(topic): close r1")


def test_second_close_is_noop_returning_same_sha(repo, scratch):
    first = _close(repo, scratch, "corpus", outputs=["final.md"])
    n = _count(repo)
    second = _close(repo, scratch, "corpus", outputs=["final.md"])
    assert second["committed"] is True and second["sha"] == first["sha"]
    assert _count(repo) == n


def test_uncommitted_destination_is_committed_not_reported_landed(repo, scratch):
    dest = repo / _run() / "outputs"
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


@pytest.fixture(autouse=True)
def claims(tmp_path: Path, monkeypatch) -> Path:
    """Point the ownership projection at a tmp claim ledger, never the box's sessions."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    real = claim_index.commit_set
    monkeypatch.setattr(
        claim_index, "commit_set",
        lambda sid, sessions_dir=None, cwd=None: real(sid, sessions_dir=str(sessions), cwd=cwd),
    )
    return sessions


def _hold(sessions: Path, sid: str, path: str) -> None:
    touch_record.append_event(
        str(sessions / sid / "touch-record.jsonl"), session_id=sid, agent_id=None,
        verb=touch_record.VERB_TOUCH, path=path, timestamp=1786183200.0, kind="w",
    )


def test_an_own_untracked_research_file_written_during_the_run_refuses_the_close_with_no_write(
    repo, scratch, claims
):
    stray = repo / "docs" / "research" / "2026-10-09-elsewhere.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("duplicate\n", encoding="utf-8")
    _hold(claims, "me", "docs/research/2026-10-09-elsewhere.md")
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, scratch, "corpus", outputs=["final.md"], session_id="me")
    assert out["exit_code"] == 1 and out["committed"] is False and out["deliverable"] is False, out
    assert out["strays"] == ["docs/research/2026-10-09-elsewhere.md"]
    assert _git(repo, "rev-parse", "HEAD") == head
    assert sorted(p.name for p in (repo / "docs" / "research").iterdir()) == ["2026-10-09-elsewhere.md"]


def test_a_peer_untracked_research_file_is_noted_once_not_refused_and_left_intact(
    repo, scratch, claims
):
    peer = repo / "docs" / "research" / "peer-live.md"
    peer.parent.mkdir(parents=True)
    peer.write_bytes(b"peer output\n")
    _hold(claims, "peer", "docs/research/peer-live.md")
    unclaimed = repo / "docs" / "research" / "nobody.md"
    unclaimed.write_bytes(b"orphan\n")
    out = _close(repo, scratch, "corpus", outputs=["final.md"], session_id="me")
    assert out["exit_code"] == 0 and out["committed"] is True and out["deliverable"] is True, out
    assert out["peer_files"] == ["docs/research/nobody.md", "docs/research/peer-live.md"]
    assert peer.read_bytes() == b"peer output\n" and unclaimed.read_bytes() == b"orphan\n"
    assert out["paths"] == [f"{_run()}/outputs/final.md"]
    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert sorted(committed) == sorted([*out["paths"], _TRAIL])
    assert _git(repo, "status", "--porcelain", "--untracked-files=all").count("peer-live.md") == 1


def test_a_peer_stray_does_not_retrigger_on_a_second_close(repo, scratch, claims):
    peer = repo / "docs" / "research" / "peer-live.md"
    peer.parent.mkdir(parents=True)
    peer.write_text("peer\n", encoding="utf-8")
    _hold(claims, "peer", "docs/research/peer-live.md")
    first = _close(repo, scratch, "corpus", outputs=["final.md"], session_id="me")
    again = _close(repo, scratch, "corpus", outputs=["final.md"], session_id="me")
    assert first["exit_code"] == again["exit_code"] == 0 and again["sha"] == first["sha"]


def test_a_contested_stray_is_not_own_and_is_noted(repo, scratch, claims):
    shared = repo / "docs" / "research" / "shared.md"
    shared.parent.mkdir(parents=True)
    shared.write_text("both\n", encoding="utf-8")
    _hold(claims, "me", "docs/research/shared.md")
    _hold(claims, "peer", "docs/research/shared.md")
    out = _close(repo, scratch, "corpus", outputs=["final.md"], session_id="me")
    assert out["exit_code"] == 0 and out["peer_files"] == ["docs/research/shared.md"], out


def test_a_file_inside_the_run_dir_is_not_a_stray(repo, scratch):
    kept = repo / _run() / "workdir" / "notes.md"
    kept.parent.mkdir(parents=True)
    kept.write_text("scratch\n", encoding="utf-8")
    out = _close(repo, scratch, "corpus", outputs=["final.md"])
    assert out["exit_code"] == 0 and "strays" not in out, out


def test_strays_then_the_printed_remedy_then_reclose_records_every_output(repo, scratch, claims):
    research = repo / "docs" / "research"
    research.mkdir(parents=True)
    names = ["head-nlm.md", "head-nlm-advisory.md"]
    for n in names:
        (research / n).write_text(f"{n} body\n", encoding="utf-8")
        _hold(claims, "me", f"docs/research/{n}")
    first = _close(repo, scratch, "corpus", outputs=["final.md"], session_id="me")
    assert first["exit_code"] == 1 and sorted(first["strays"]) == sorted(f"docs/research/{n}" for n in names)
    assert "move each into the run's scratch dir and re-close" in first["error"]

    for n in names:
        shutil.move(str(research / n), str(scratch / n))
    again = _close(repo, scratch, "corpus", outputs=["final.md", *names], session_id="me")
    assert again["exit_code"] == 0 and again["committed"] is True, again
    assert "strays" not in again
    dest = f"{_run()}/outputs"
    expected = [f"{dest}/final.md", *(f"{dest}/{n}" for n in names)]
    assert sorted(again["paths"]) == sorted(expected)
    trail = (repo / _TRAIL).read_text(encoding="utf-8")
    assert "output_count: 3" in trail and all(p in trail for p in expected)
    assert _git(repo, "status", "--porcelain") == ""
    assert _close(repo, scratch, "corpus", outputs=["final.md", *names])["sha"] == again["sha"]


def test_a_reclose_that_drops_an_output_is_still_refused(repo, scratch):
    (scratch / "extra.md").write_text("extra\n", encoding="utf-8")
    assert _close(repo, scratch, "corpus", outputs=["final.md", "extra.md"])["exit_code"] == 0
    again = _close(repo, scratch, "corpus", outputs=["final.md"])
    assert again["exit_code"] == 1 and "trail record exists with different content" in again["error"]


def test_a_reclose_that_changes_a_non_path_key_is_still_refused(repo, scratch):
    (scratch / "extra.md").write_text("extra\n", encoding="utf-8")
    assert _close(repo, scratch, "corpus", outputs=["final.md"])["exit_code"] == 0
    again = _close(repo, scratch, "scouts", outputs=["final.md", "extra.md"])
    assert again["exit_code"] == 1, again


def test_every_segment_dir_is_archived_at_its_relative_path(repo, scratch):
    (scratch / "web").mkdir()
    (scratch / "web" / "synthesis.md").write_text("web synthesis\n")
    (scratch / "mail").mkdir()
    (scratch / "mail" / "_em.jsonl").write_text("{}\n")
    out = _close(repo, scratch, "corpus", segment_dirs=["web"])
    assert out["exit_code"] == 0, out
    names = {p.removeprefix(_run() + "/") for p in out["paths"]}
    assert {"outputs/final.md", "outputs/web/synthesis.md"} <= names
    assert not any(n.startswith("outputs/mail/") for n in names)


def test_default_close_writes_only_classifying_run_children(repo, scratch):
    from coordinator_core.ops import _research_layout as layout

    (scratch / "synthesis.md").write_text("syn\n")
    (scratch / "verdict.md").write_text("ver\n")
    out = _close(repo, scratch, "corpus", outputs=["final.md", "synthesis.md", "verdict.md"])
    assert out["exit_code"] == 0, out
    written = _trail_files(repo)
    assert written == {f"{_run()}/{c}" for c in ("outputs/final.md", "synthesis.md", "verdict.md", "trail.yaml")}
    assert all(layout.classify(p.removeprefix("docs/research/")) == layout.RUN_CHILD for p in written)
    assert not (repo / "docs" / "research" / "trail").exists()


def test_topic_param_names_the_topic_dir_and_a_bad_topic_is_refused(repo, scratch):
    assert _close(repo, scratch, "corpus", outputs=["final.md"], topic="Bad Topic")["exit_code"] == 1
    assert _close(repo, scratch, "corpus", outputs=["final.md"], topic="trail")["exit_code"] == 1
    assert not (repo / "docs").exists()
    out = _close(repo, scratch, "corpus", outputs=["final.md"], topic="game-ai")
    assert out["exit_code"] == 0 and out["run_dir"] == _run("topic", "game-ai"), out


def test_a_known_suffix_slug_folds_to_its_base_topic_and_an_odd_slug_files_under_misc(repo, scratch):
    out = _close(repo, scratch, "corpus", outputs=["final.md"], topic_slug="head-nlm-advisory")
    assert out["run_dir"] == _run("head-nlm-advisory", "head-nlm")
    out = _close(repo, scratch, "corpus", outputs=["final.md"], run_id="r2", topic_slug="2026-10-01-x")
    assert out["exit_code"] == 0 and out["run_dir"] == _run("2026-10-01-x", "misc"), out


def test_a_slug_with_uppercase_or_underscore_is_normalized_into_the_run_name(repo, scratch):
    out = _close(repo, scratch, "corpus", outputs=["final.md"], topic_slug="My_Topic")
    assert out["exit_code"] == 0 and out["run_dir"] == _run("my-topic"), out


def test_synthesis_and_digest_outputs_colliding_on_one_child_are_refused(repo, scratch):
    (scratch / "synthesis.md").write_text("syn\n")
    (scratch / "digest.md").write_text("dig\n")
    out = _close(repo, scratch, "corpus", outputs=["synthesis.md", "digest.md"])
    assert out["exit_code"] == 1 and "same run-dir child" in out["error"]
    assert not (repo / "docs").exists()


def test_a_legacy_flat_trail_for_the_run_refuses_a_new_layout_close(repo, scratch):
    legacy = repo / "docs" / "research" / "trail" / "r1.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('run_id: "r1"\n', encoding="utf-8")
    _git(repo, "add", "docs")
    _git(repo, "commit", "-q", "-m", "legacy trail")
    out = _close(repo, scratch, "corpus", outputs=["final.md"])
    assert out["exit_code"] == 1 and "docs/research/trail/r1.yaml" in out["error"]


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
    assert dest.parent.parent == local.resolve() and dest.parent.name == "topic"
    assert (dest / "outputs" / "final.md").read_text() == "final body\n"
    assert out["claims_out"] == (dest / "topic").as_posix()
    assert _count(repo) == before + 1 and _git(repo, "status", "--porcelain") == ""
    assert _trail_files(repo) == {_TRAIL}
    text = _git(repo, "show", f"HEAD:{_TRAIL}")
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
    names = {p.removeprefix(_run() + "/") for p in out["paths"]}
    assert {"outputs/web/synthesis.md", "outputs/repo/synthesis.md"} <= names
    assert not any(n.startswith("outputs/notebooklm/") for n in names)


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
    dest = _run()
    body = _git(repo, "show", f"HEAD:{dest}/outputs/final.md")
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
    dest = repo / _run() / "outputs" / "final.md"
    assert dest.read_bytes() == raw


def test_local_only_keeps_absolute_paths_byte_identical(repo, scratch, tmp_path):
    _absolute_output(repo, scratch)
    local = tmp_path / "local"
    local.mkdir()
    out = _close(repo, scratch, "corpus", local_root=local.as_posix())
    assert out["exit_code"] == 0, out
    assert (Path(out["destination"]) / "outputs" / "final.md").read_bytes() == (scratch / "final.md").read_bytes()


def _digest_path() -> str:
    return f"{_run()}/synthesis.md"


_GOOD = (
    "## digest-a.md\n- **grounded** a holds\n## digest-b.md\n- **grounded** b holds\n"
    "## Contradictions\n- **real** sources disagree\n"
)


def _report(scratch: Path, body: str) -> None:
    (scratch / "grounding.md").write_text(body, encoding="utf-8")


def _refused(repo, scratch, **extra) -> dict:
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, scratch, "scouts", **extra)
    assert out["exit_code"] == 1 and out["committed"] is False and out["deliverable"] is False, out
    assert "grounding.md" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head
    assert not (scratch / "digest.md").exists() and not (repo / "docs").exists()
    return out


@pytest.mark.parametrize("require", [False, True])
@pytest.mark.parametrize(
    "line,verdict",
    [
        ("- **ungrounded** a claim no source backs", "ungrounded"),
        ("- **contradicted** a claim its source denies", "contradicted"),
        ("- **invented** a contradiction nobody holds", "invented"),
    ],
)
def test_ungrounded_contradicted_or_invented_line_refuses_with_counts(
    repo, scratch, line, verdict, require
):
    _report(scratch, _GOOD + line + "\n")
    out = _refused(repo, scratch, require_grounding=require, halted_segments=["x"])
    assert verdict in out["error"] and out["grounding"][verdict] == 1
    assert out["grounding"]["grounded"] == 2 and out["halted_segments"] == ["x"]


def test_ungrounded_report_missing_a_digest_section_is_refused(repo, scratch):
    _report(scratch, "## digest-a.md\n- **grounded** a holds\n")
    out = _refused(repo, scratch)
    assert "digest-b.md" in out["error"]


def test_ungrounded_report_with_no_claim_line_is_refused(repo, scratch):
    _report(scratch, "checked sources\n\nall held\n")
    out = _refused(repo, scratch)
    assert "grades no claim" in out["error"] and out["grounding"]["grounded"] == 0


def test_all_grounded_report_with_a_real_ruling_commits_digest_and_trail(repo, scratch):
    _report(scratch, _GOOD)
    out = _close(repo, scratch, "scouts")
    assert out["exit_code"] == 0 and out["deliverable"] is True, out
    assert _trail_files(repo) == {_digest_path(), _TRAIL}


def test_ungrounded_report_fixed_then_reclosed_commits(repo, scratch):
    _report(scratch, _GOOD + "- **ungrounded** bad\n")
    _refused(repo, scratch)
    _report(scratch, _GOOD)
    assert _close(repo, scratch, "scouts")["exit_code"] == 0


def test_open_questions_lines_pass_close_and_are_reported_open(repo, scratch):
    _report(scratch, _GOOD + "## Open questions\n- **ungrounded** q1\n- **ungrounded** q2\n")
    out = _close(repo, scratch, "scouts")
    assert out["exit_code"] == 0 and out["deliverable"] is True, out
    assert out["open"] == 2


def test_ungrounded_line_outside_open_questions_still_refuses_with_open_count(repo, scratch):
    _report(scratch, _GOOD + "- **ungrounded** bad\n## Open questions\n- **ungrounded** q1\n")
    out = _refused(repo, scratch)
    assert out["grounding"]["ungrounded"] == 1 and out["grounding"]["open"] == 1


def test_close_without_open_questions_omits_open(repo, scratch):
    _report(scratch, _GOOD)
    assert "open" not in _close(repo, scratch, "scouts")


def _age(path: Path, delta: float) -> None:
    now = time.time()
    os.utime(path, (now + delta, now + delta))


def test_refusal_over_an_edited_digest_names_reground(repo, scratch):
    _report(scratch, _GOOD + "- **contradicted** bad\n")
    _age(scratch / "grounding.md", -100)
    _age(scratch / "digest-a.md", 0)
    out = _refused(repo, scratch)
    assert "research.reground" in out["error"]
    assert out["next_action"] == {
        "op": "research.reground",
        "params": {"run_id": "r1", "scratch_dir": scratch.resolve().as_posix()},
    }


def test_refusal_with_grounding_newer_than_digests_has_no_next_action(repo, scratch):
    _report(scratch, _GOOD + "- **contradicted** bad\n")
    _age(scratch / "digest-a.md", -200)
    _age(scratch / "digest-b.md", -200)
    _age(scratch / "grounding.md", -100)
    out = _refused(repo, scratch)
    assert "next_action" not in out and "reground" not in out["error"]


def test_clean_grounding_over_an_edited_digest_still_closes(repo, scratch):
    _report(scratch, _GOOD)
    _age(scratch / "grounding.md", -100)
    _age(scratch / "digest-a.md", 0)
    out = _close(repo, scratch, "scouts")
    assert out["exit_code"] == 0 and "next_action" not in out


def test_grounding_report_is_the_last_section_of_the_committed_digest(repo, scratch):
    _report(scratch, _GOOD)
    out = _close(repo, scratch, "scouts", require_grounding=True)
    assert out["exit_code"] == 0 and out["deliverable"] is True, out
    assert _git(repo, "show", f"HEAD:{_digest_path()}") == (
        "alpha\n\nbeta\n\n## Grounding\n\n" + _GOOD.rstrip("\n")
    )
    assert _trail_files(repo) == {_digest_path(), _TRAIL}


@pytest.mark.parametrize("body", [None, "", "  \n\t\n"])
def test_grounding_required_but_absent_or_blank_refuses_without_a_write(repo, scratch, body):
    if body is not None:
        (scratch / "grounding.md").write_text(body, encoding="utf-8")
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, scratch, "scouts", require_grounding=True, halted_segments=["x"])
    assert out["exit_code"] == 1 and out["committed"] is False and out["deliverable"] is False
    assert "grounding.md" in out["error"] and out["halted_segments"] == ["x"]
    assert _git(repo, "rev-parse", "HEAD") == head
    assert not (scratch / "digest.md").exists() and not (repo / "docs").exists()


def test_grounding_not_required_leaves_a_run_without_it_closing_as_baseline(repo, scratch):
    out = _close(repo, scratch, "scouts", require_grounding=False)
    assert out["exit_code"] == 0, out
    assert _git(repo, "show", f"HEAD:{_digest_path()}") == "alpha\n\nbeta"


def test_grounding_blank_report_without_the_param_adds_no_section(repo, scratch):
    (scratch / "grounding.md").write_text("\n \n", encoding="utf-8")
    assert _close(repo, scratch, "scouts")["exit_code"] == 0
    assert _git(repo, "show", f"HEAD:{_digest_path()}") == "alpha\n\nbeta"


def test_grounding_run_never_commits_probe_or_grounding_files(repo, scratch):
    _report(scratch, _GOOD)
    (scratch / "probe-1.md").write_text("probe body\n", encoding="utf-8")
    out = _close(repo, scratch, "scouts")
    assert out["exit_code"] == 0, out
    assert _trail_files(repo) == {_digest_path(), _TRAIL}


@pytest.mark.parametrize("bad", ["true", 1, None, []])
def test_grounding_param_that_is_not_a_bool_is_refused_before_any_write(repo, scratch, bad):
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, scratch, "scouts", require_grounding=bad)
    assert out["exit_code"] == 1 and "require_grounding must be a bool" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head and not (scratch / "digest.md").exists()


@pytest.mark.parametrize("tier", ["corpus", "unblock"])
def test_grounding_param_on_a_non_scouts_tier_is_refused(repo, scratch, tier):
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, scratch, tier, require_grounding=True)
    assert out["exit_code"] == 1 and "scouts only" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head and not (repo / "docs").exists()


def test_grounding_reclose_of_a_grounded_run_returns_the_landed_sha(repo, scratch):
    _report(scratch, _GOOD)
    first = _close(repo, scratch, "scouts", require_grounding=True)
    before = _count(repo)
    again = _close(repo, scratch, "scouts", require_grounding=True)
    assert again["exit_code"] == 0 and again["sha"] == first["sha"], again
    assert _count(repo) == before


_RESEARCH_SIZING = "size: S\nresearch:\n  class: corpus\nnote: keep\n"


def _research_sizing(repo: Path, text: str = _RESEARCH_SIZING) -> Path:
    p = _write_sizing(repo)
    p.write_bytes(text.encode("utf-8"))
    return p


def test_a_close_drops_the_sizing_research_block_in_the_close_commit(repo, scratch):
    p = _research_sizing(repo)
    out = _close(repo, scratch, "corpus", outputs=["final.md"], sizing="state/sizings/x.yaml")
    assert out["exit_code"] == 0 and out["research_block_dropped"] is True, out
    assert "research" not in p.read_text() and "note: keep" in p.read_text()
    assert "state/sizings/x.yaml" in _trail_files(repo)
    assert _git(repo, "status", "--porcelain") == ""


def test_a_sizing_with_no_block_reports_not_dropped(repo, scratch):
    _write_sizing(repo)
    out = _close(repo, scratch, "corpus", outputs=["final.md"], sizing="state/sizings/x.yaml")
    assert out["exit_code"] == 0 and out["research_block_dropped"] is False, out


def test_a_crlf_sizing_stays_crlf_after_the_drop(repo, scratch):
    p = _research_sizing(repo, _RESEARCH_SIZING.replace("\n", "\r\n"))
    out = _close(repo, scratch, "corpus", outputs=["final.md"], sizing="state/sizings/x.yaml")
    assert out["research_block_dropped"] is True, out
    raw = p.read_bytes()
    assert b"research" not in raw and b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_a_refused_close_leaves_the_sizing_byte_identical(repo, scratch):
    p = _research_sizing(repo)
    before = p.read_bytes()
    (scratch / "final.md").write_text("api_key = abcd1234efgh5678ijkl9012\n")
    out = _close(repo, scratch, "corpus", outputs=["final.md"], sizing="state/sizings/x.yaml")
    assert out["exit_code"] == 1, out
    assert p.read_bytes() == before


def test_a_refused_commit_restores_the_sizing(repo, scratch, monkeypatch):
    from coordinator_core.git.commit import CommitRefused
    from coordinator_core.ops import research_close

    p = _research_sizing(repo)
    before = p.read_bytes()

    def boom(*a, **k):
        raise CommitRefused("no")

    monkeypatch.setattr(research_close, "commit_paths", boom)
    out = _close(repo, scratch, "corpus", outputs=["final.md"], sizing="state/sizings/x.yaml")
    assert out["exit_code"] == 1 and "commit refused" in out["error"], out
    assert p.read_bytes() == before


def test_reclose_of_a_landed_run_whose_sizing_kept_its_block_drops_and_commits(repo, scratch):
    p = _research_sizing(repo)
    first = _close(repo, scratch, "corpus", outputs=["final.md"], sizing="state/sizings/x.yaml")
    assert first["exit_code"] == 0
    p.write_bytes(_RESEARCH_SIZING.encode("utf-8"))
    _git(repo, "add", "state/sizings/x.yaml")
    _git(repo, "commit", "-q", "-m", "restore block")
    before = _count(repo)
    second = _close(repo, scratch, "corpus", outputs=["final.md"], sizing="state/sizings/x.yaml")
    assert second["exit_code"] == 0 and second["research_block_dropped"] is True, second
    assert second["sha"] != first["sha"] and _count(repo) == before + 1
    assert "research" not in p.read_text(), "the re-close must drop the stale block"
    assert _git(repo, "status", "--porcelain") == ""


def _basis_and_carry(scratch: Path, carried: dict, rulings=(), basis_of=("digest-a.md", "digest-b.md")) -> None:
    """Write grounding.basis.json from the digests on disk and a carry file holding `carried`."""
    import json

    from coordinator_core.ops import _research_grounding as rg

    basis = {n: rg.line_hashes((scratch / n).read_text(encoding="utf-8")) for n in basis_of}
    (scratch / rg.BASIS_FILE).write_text(json.dumps(basis), encoding="utf-8")
    judged = {n: rg.basis_sha(h) for n, h in basis.items()}
    keyed = [r if isinstance(r, dict) else rg.key_ruling(r, judged, grader=rg.GRADER_VERSION) for r in rulings]
    doc = {"generation": 1, "carried": carried, "rulings": keyed, "flagged": {}, "grader": rg.GRADER_VERSION}
    (scratch / rg.CARRY_FILE).write_text(json.dumps(doc), encoding="utf-8")


_CARRIED_A = ["- **grounded** a holds"]
_CARRIED_B = ["- **grounded** b holds"]


def test_carried_clean_digests_close_with_the_merged_report_archived(repo, scratch):
    _basis_and_carry(scratch, {"digest-a.md": _CARRIED_A, "digest-b.md": _CARRIED_B})
    out = _close(repo, scratch, "scouts")
    assert out["exit_code"] == 0, out
    committed = _git(repo, "show", f"HEAD:{_digest_path()}")
    assert "## Grounding" in committed and "- **grounded** a holds" in committed
    assert "digest-b.md" in committed.split("## Grounding", 1)[1]


@pytest.mark.parametrize(
    "carried_a,rulings,verdict",
    [
        (["- **contradicted** a claim its source denies"], [], "contradicted"),
        (_CARRIED_A, ["- **invented** digest-a.md vs digest-b.md nobody holds this"], "invented"),
    ],
)
def test_carry_a_flagged_claim_keeps_blocking_the_close(repo, scratch, carried_a, rulings, verdict):
    _basis_and_carry(scratch, {"digest-a.md": carried_a, "digest-b.md": _CARRIED_B}, rulings)
    out = _refused(repo, scratch)
    assert verdict in out["error"] and out["grounding"][verdict] == 1
    _report(scratch, _GOOD)
    again = _refused(repo, scratch)
    assert verdict in again["error"], "a fresh report cannot clear a carried digest's flag"


def test_carry_a_real_ruling_does_not_refuse_the_close(repo, scratch):
    _basis_and_carry(
        scratch,
        {"digest-a.md": _CARRIED_A, "digest-b.md": _CARRIED_B},
        ["- **real** digest-a.md and digest-b.md genuinely disagree"],
    )
    out = _close(repo, scratch, "scouts")
    assert out["exit_code"] == 0 and out["deliverable"] is True, out


def test_carry_discards_a_fresh_nit_under_a_carried_digest(repo, scratch):
    _basis_and_carry(scratch, {"digest-a.md": _CARRIED_A, "digest-b.md": _CARRIED_B})
    _report(scratch, "## digest-a.md\n- **contradicted** fresh nit\n## Contradictions\n- **invented** digest-a.md nit\n")
    out = _close(repo, scratch, "scouts")
    assert out["exit_code"] == 0, out


def test_carry_a_digest_changed_after_the_basis_is_refused_as_ungraded(repo, scratch):
    _basis_and_carry(scratch, {"digest-a.md": _CARRIED_A, "digest-b.md": _CARRIED_B})
    (scratch / "digest-a.md").write_text("alpha edited\n", encoding="utf-8")
    out = _refused(repo, scratch)
    assert "digest-a.md" in out["error"] and "no grounding section" in out["error"]
    assert out["next_action"]["op"] == "research.reground"


def test_carry_a_regraded_digest_takes_its_fresh_section(repo, scratch):
    _basis_and_carry(scratch, {"digest-b.md": _CARRIED_B}, basis_of=("digest-b.md",))
    _report(scratch, "## digest-a.md\n- **grounded** a holds\n## digest-b.md\n- **contradicted** ignored\n")
    out = _close(repo, scratch, "scouts")
    assert out["exit_code"] == 0, out


def test_carry_a_touched_but_unchanged_digest_does_not_name_reground(repo, scratch):
    _basis_and_carry(scratch, {"digest-a.md": _CARRIED_A, "digest-b.md": ["- **contradicted** bad"]})
    _report(scratch, _GOOD)
    _age(scratch / "grounding.md", -100)
    _age(scratch / "digest-a.md", 0)
    out = _refused(repo, scratch)
    assert "next_action" not in out and "reground" not in out["error"]


def test_carry_a_changed_digest_with_a_basis_names_reground_even_when_older(repo, scratch):
    _basis_and_carry(scratch, {"digest-a.md": _CARRIED_A, "digest-b.md": ["- **contradicted** bad"]})
    (scratch / "digest-a.md").write_text("alpha edited\n", encoding="utf-8")
    _report(scratch, _GOOD)
    _age(scratch / "digest-a.md", -500)
    out = _refused(repo, scratch)
    assert out["next_action"]["op"] == "research.reground"


@pytest.mark.parametrize("name", ["grounding.carry.json", "grounding.basis.json"])
@pytest.mark.parametrize("body", ["{not json", "[]"])
def test_carry_an_unreadable_carry_or_basis_file_refuses_naming_it(repo, scratch, name, body):
    _basis_and_carry(scratch, {"digest-a.md": _CARRIED_A, "digest-b.md": _CARRIED_B})
    (scratch / name).write_text(body, encoding="utf-8")
    out = _refused(repo, scratch)
    assert name in out["error"]


_A_ONLY = {"digest-b.md": _CARRIED_B}
_INVENTED_A = "- **invented** digest-a.md asserts a split the sources do not support"


def test_ruling_rewritten_before_next_generation_does_not_count(repo, scratch):
    _basis_and_carry(scratch, _A_ONLY, [_INVENTED_A])
    (scratch / "digest-a.md").write_text("alpha rewritten\n", encoding="utf-8")
    _report(scratch, _GOOD)
    out = _close(repo, scratch, "scouts")
    assert out["exit_code"] == 0 and out["deliverable"] is True, out


def test_a_ruling_on_a_deleted_digest_does_not_count(repo, scratch):
    _basis_and_carry(
        scratch, {"digest-a.md": _CARRIED_A},
        ["- **invented** digest-b.md asserts a split the sources do not support"],
    )
    (scratch / "digest-b.md").unlink()
    _report(scratch, _GOOD)
    out = _close(repo, scratch, "scouts")
    assert out["exit_code"] == 0, out


def test_a_keyed_invented_ruling_on_unchanged_digests_still_refuses(repo, scratch):
    _basis_and_carry(scratch, _A_ONLY, [_INVENTED_A])
    _report(scratch, _GOOD)
    out = _refused(repo, scratch)
    assert out["grounding"]["invented"] == 1


def test_dismissed_only_report_closes(repo, scratch):
    _report(
        scratch,
        "## digest-a.md\n- **grounded** a holds\n## digest-b.md\n- **grounded** b holds\n"
        "## Contradictions\n- **dismissed** digest-a.md vs digest-b.md: no contradiction exists\n",
    )
    out = _close(repo, scratch, "scouts")
    assert out["exit_code"] == 0 and out["deliverable"] is True, out


def test_a_refusal_counts_dismissed_rulings(repo, scratch):
    _report(
        scratch,
        "## digest-a.md\n- **ungrounded** nothing backs this\n## digest-b.md\n- **grounded** b holds\n"
        "## Contradictions\n- **dismissed** digest-a.md vs digest-b.md: no contradiction exists\n",
    )
    out = _refused(repo, scratch)
    assert out["grounding"]["dismissed"] == 1


def _set_carry_rulings(scratch: Path, rulings: list, **doc_edits) -> None:
    import json

    path = scratch / "grounding.carry.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["rulings"] = rulings
    doc.update(doc_edits)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _assert_reground_remedy(out: dict) -> None:
    assert out["exit_code"] == 1 and out["deliverable"] is False, out
    assert "research.reground" in out["error"]
    assert out["next_action"]["op"] == "research.reground"


def test_a_legacy_string_real_ruling_does_not_refuse(repo, scratch):
    _basis_and_carry(scratch, {"digest-a.md": _CARRIED_A, "digest-b.md": _CARRIED_B})
    _set_carry_rulings(scratch, ["- **real** digest-a.md and digest-b.md genuinely disagree"])
    out = _close(repo, scratch, "scouts")
    assert out["exit_code"] == 0, out


def test_a_legacy_string_invented_ruling_refuses_with_the_reground_remedy(repo, scratch):
    _basis_and_carry(scratch, {"digest-a.md": _CARRIED_A, "digest-b.md": _CARRIED_B})
    _set_carry_rulings(scratch, [_INVENTED_A])
    _assert_reground_remedy(_close(repo, scratch, "scouts"))


def _stale(scratch: Path, line: str) -> dict:
    from coordinator_core.ops import _research_grounding as rg

    sha = rg.digest_sha((scratch / "digest-a.md").read_text(encoding="utf-8"))
    return {"line": line, "digests": {"digest-a.md": sha}, "grader": "old"}


def test_a_stale_version_invented_ruling_refuses_with_the_reground_remedy(repo, scratch):
    _basis_and_carry(scratch, {"digest-a.md": _CARRIED_A, "digest-b.md": _CARRIED_B})
    _set_carry_rulings(scratch, [_stale(scratch, _INVENTED_A)])
    _assert_reground_remedy(_refused(repo, scratch))


def test_a_stale_version_real_or_dismissed_ruling_does_not_refuse(repo, scratch):
    _basis_and_carry(scratch, {"digest-a.md": _CARRIED_A, "digest-b.md": _CARRIED_B})
    _set_carry_rulings(scratch, [
        _stale(scratch, "- **real** digest-a.md sources disagree"),
        _stale(scratch, "- **dismissed** digest-a.md no contradiction"),
    ])
    assert _close(repo, scratch, "scouts")["exit_code"] == 0


def test_a_first_generation_invented_ruling_refuses_with_the_reground_remedy(repo, scratch):
    _report(scratch, _GOOD + "- **invented** a contradiction nobody holds\n")
    _assert_reground_remedy(_refused(repo, scratch))


def test_a_current_grader_invented_ruling_refuses_without_the_remedy(repo, scratch):
    _basis_and_carry(scratch, _A_ONLY)
    _report(scratch, _GOOD + "- **invented** digest-a.md a contradiction nobody holds\n")
    out = _refused(repo, scratch)
    assert "next_action" not in out and "reground" not in out["error"]


def test_a_first_generation_real_and_dismissed_only_report_closes(repo, scratch):
    _report(scratch, _GOOD + "- **dismissed** digest-a.md no contradiction exists\n")
    out = _close(repo, scratch, "scouts")
    assert out["exit_code"] == 0 and out["deliverable"] is True, out


def test_carry_a_basis_without_a_carry_file_still_closes_on_the_fresh_report(repo, scratch):
    _basis_and_carry(scratch, {})
    (scratch / "grounding.carry.json").unlink()
    _report(scratch, _GOOD)
    out = _close(repo, scratch, "scouts")
    assert out["exit_code"] == 0, out

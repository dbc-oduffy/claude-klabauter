
from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.hooks import nudge_unrouted_sizing as m
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git_init(repo):
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    (repo / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, **no_console_passthrough_kwargs())


def _session_dir(repo, session_id):
    d = repo / ".git" / "coordinator-sessions" / session_id
    os.makedirs(d, exist_ok=True)
    return d


def _write_legacy_touched(repo, session_id, *rel_paths):
    d = _session_dir(repo, session_id)
    (d / "touched.txt").write_text("\n".join(rel_paths) + "\n", encoding="utf-8")


def _append_jsonl_touch(repo, session_id, rel_path):
    d = _session_dir(repo, session_id)
    touch_record.append_event(
        d / m._TOUCH_RECORD_FILENAME,
        session_id=session_id,
        agent_id=None,
        verb=touch_record.VERB_TOUCH,
        path=rel_path,
    )


@pytest.fixture
def repo(tmp_path):
    _git_init(tmp_path)
    return tmp_path


def test_legacy_only_touched_txt_no_longer_reads(repo):
    session_id = "sess-legacy"
    _write_legacy_touched(repo, session_id, "state/sizings/a.yaml")

    assert m._session_touched_lines(session_id, str(repo)) == []


def test_jsonl_only_family_reads_via_seam(repo):
    session_id = "sess-jsonl"
    _append_jsonl_touch(repo, session_id, "state/sizings/b.yaml")

    assert m._session_touched_lines(session_id, str(repo)) == ["state/sizings/b.yaml"]


def test_legacy_touched_txt_ignored_when_jsonl_family_present(repo):
    session_id = "sess-union"
    _write_legacy_touched(repo, session_id, "state/sizings/legacy.yaml")
    _append_jsonl_touch(repo, session_id, "state/sizings/jsonl.yaml")

    assert m._session_touched_lines(session_id, str(repo)) == [
        "state/sizings/jsonl.yaml",
    ]


def test_no_record_at_all_returns_empty(repo):
    session_id = "sess-absent"

    assert m._session_touched_lines(session_id, str(repo)) == []


def test_degraded_read_returns_empty(repo, monkeypatch):
    session_id = "sess-degraded"
    _write_legacy_touched(repo, session_id, "state/sizings/a.yaml")

    monkeypatch.setattr(
        m, "_read_touch_record_as_legacy_lines", lambda sink_path: (["state/sizings/a.yaml"], True)
    )

    assert m._session_touched_lines(session_id, str(repo)) == []


def test_unresolvable_repo_root_never_raises(tmp_path):
    no_git_dir = tmp_path / "no-git-here"
    no_git_dir.mkdir()

    assert m._session_touched_lines("sess-x", str(no_git_dir)) == []


def test_downstream_sizing_filter_still_works_through_the_seam(repo):
    session_id = "sess-filter"
    _append_jsonl_touch(repo, session_id, "state/sizings/c.yaml")
    _append_jsonl_touch(repo, session_id, "docs/plans/unrelated.md")

    assert m._session_touched_sizing_files(session_id, str(repo)) == ["state/sizings/c.yaml"]

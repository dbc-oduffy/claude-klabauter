"""C2 — unit coverage for `nudge_unrouted_sizing._session_touched_lines`
reading through the C0 union seam (`session.scope._read_touch_record_as_
legacy_lines`) rather than parsing a session's `touched.txt` directly.

Spec backlink: docs/plans/2026-08-25-the-legacy-touch-record-is-retired-by-
repointing-its-writers.md, chunk C2. C0 (already landed, commit 727e1d5ad)
built the seam this reader now goes through; this file exercises only the
reader's own move onto it, not the seam's own union/fold behaviour (that is
`coordinator_core/session/tests/test_scope.py`'s remit).

Negative-spec: does not exercise the full Stop-hook `op()` entry point or
either seam's criteria/state-machine logic — that is
`test_nudge_unrouted_sizing.py`'s existing remit. This file is scoped to
`_session_touched_lines` alone: legacy-only, jsonl-only, the union of both,
an absent record, and a degraded read.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.hooks import nudge_unrouted_sizing as m
from coordinator_core.session import touch_record

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git_init(repo):
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


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


def test_legacy_only_touched_txt_still_reads(repo):
    """A bare `touched.txt` with no jsonl family — the pre-C2 shape — still
    reads through the seam unchanged."""
    session_id = "sess-legacy"
    _write_legacy_touched(repo, session_id, "state/sizings/a.yaml")

    assert m._session_touched_lines(session_id, str(repo)) == ["state/sizings/a.yaml"]


def test_jsonl_only_family_reads_via_seam(repo):
    """No `touched.txt` at all — only a `touch-record.jsonl` family — reads
    correctly now the reader moves onto the union seam."""
    session_id = "sess-jsonl"
    _append_jsonl_touch(repo, session_id, "state/sizings/b.yaml")

    assert m._session_touched_lines(session_id, str(repo)) == ["state/sizings/b.yaml"]


def test_union_prepends_legacy_ahead_of_jsonl(repo):
    """Both dialects present: the seam prepends the legacy line ahead of the
    jsonl-derived one, so this reader sees both paths."""
    session_id = "sess-union"
    _write_legacy_touched(repo, session_id, "state/sizings/legacy.yaml")
    _append_jsonl_touch(repo, session_id, "state/sizings/jsonl.yaml")

    assert m._session_touched_lines(session_id, str(repo)) == [
        "state/sizings/legacy.yaml",
        "state/sizings/jsonl.yaml",
    ]


def test_no_record_at_all_returns_empty(repo):
    """No `touched.txt` and no `touch-record.jsonl` family for this session
    — silence, matching the pre-C2 OSError-catch posture."""
    session_id = "sess-absent"

    assert m._session_touched_lines(session_id, str(repo)) == []


def test_degraded_read_returns_empty(repo, monkeypatch):
    """A degraded read from the seam is treated as silence — the same safe
    direction the prior bare OSError catch already had: a missed nudge
    costs one un-nudged turn, never a false fire from a corrupted read."""
    session_id = "sess-degraded"
    _write_legacy_touched(repo, session_id, "state/sizings/a.yaml")

    monkeypatch.setattr(
        m, "_read_touch_record_as_legacy_lines", lambda sink_path: (["state/sizings/a.yaml"], True)
    )

    assert m._session_touched_lines(session_id, str(repo)) == []


def test_unresolvable_repo_root_never_raises(tmp_path):
    """No `.git` anywhere above `cwd` — the shared `git_common_dir` failure
    path — returns [] rather than raising, matching the module's existing
    fail-toward-availability posture on every other ambiguous read."""
    no_git_dir = tmp_path / "no-git-here"
    no_git_dir.mkdir()

    assert m._session_touched_lines("sess-x", str(no_git_dir)) == []


def test_downstream_sizing_filter_still_works_through_the_seam(repo):
    """Regression: `_session_touched_sizing_files`, a thin regex filter over
    `_session_touched_lines`, still sees a jsonl-only sizing touch once the
    underlying reader moves onto the seam."""
    session_id = "sess-filter"
    _append_jsonl_touch(repo, session_id, "state/sizings/c.yaml")
    _append_jsonl_touch(repo, session_id, "docs/plans/unrelated.md")

    assert m._session_touched_sizing_files(session_id, str(repo)) == ["state/sizings/c.yaml"]

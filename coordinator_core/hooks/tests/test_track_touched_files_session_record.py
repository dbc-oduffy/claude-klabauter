"""
coordinator_core.hooks.tests.test_track_touched_files_session_record — guards the
liveness-registry half of the hook's session-dir precondition: a session whose
ONLY writes go through the Write/Edit tool path still gets a ``meta.json``.

WHY THIS EXISTS. ``session/liveness.py`` keys session liveness on ``meta.json``
(``live_session_ids`` / claim-holder liveness), and duplicating pid state into
the claim dir was explicitly rejected — so a session dir with a populated
``touched.txt`` and no ``meta.json`` is not a degraded registry entry, it is no
entry at all. ``bash_guards/dispatch_checks.py::_rm_peer_claim_of`` states the
consequence in its own docstring: "If the sid has NO meta.json, it is outside
canonical's scan scope", and the destructive-rm peer-claim check degrades to a
30-minute ``touched.txt`` mtime backstop. Appending a ``T`` event IS claim
acquisition, so the writer of that event owes the registry entry.

Reported by doe-claude-em with one confirmed live instance
(``cross-repo/inbox/2026-08-25-doe-claude-em-touched-files-path-never-creates-meta-json.md``):
session ``b0706df6`` held claims for over an hour, editing files through the
tool path only, invisible to every peer's liveness check.

Relationship to the plan that removed session bootstrap from this hook
(docs/plans/2026-08-22-track-touched-files-pays-only-for-the-append.md § C1,
AC3/AC8): C1's premise was that liveness stamping "belongs at the claiming
ceremony", and AC8 accepted the record-less population as a WATCHDOG concern
(``stable_pid_watch``'s ``no_meta_json`` miss) rather than a fix. The watchdog
detects; it does not put the holder in the registry. This test pins the fix,
and the watchdog stays as the detection surface for a create that fails.

Negative-spec:
    Do NOT extend these cases into a ``last_activity`` refresh assertion. The
    guard is on ABSENCE only; a per-tool-call refresh is the cadence
    ``session/scope.py::touch``'s docstring pins as a separate question and
    DoE's ``session-heartbeat.py`` was retired over.
    Do NOT assert a timing budget here — the measurement lives in
    ``_ensure_session_record_sync``'s docstring (0.39ms / 0 spawns per init,
    once per session), and a timing assertion on a box running 50-70 LLMs
    measures peer load, not this code.

Spec backlink: docs/plans/2026-08-22-track-touched-files-pays-only-for-the-append.md AC8
"""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from coordinator_core.hooks import track_touched_files as ttf
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_passthrough_kwargs

# Spawns a real external process (git init fixture); runs at cadence gates,
# not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, **no_console_passthrough_kwargs())
    return tmp_path


def _fire(repo, common_dir, session_id, target):
    params = {
        "session_id": session_id,
        "tool_name": "Edit",
        "file_path": str(target),
    }
    asyncio.run(ttf._handler(params, repo_root=common_dir))


class TestSessionRecordAccompaniesTheClaim:
    """A session that never runs a coordinator CLI and never reaches a
    claiming ceremony — the Write/Edit-only shape — must still be resolvable
    by ``live_session_ids``."""

    def test_meta_json_is_created_alongside_the_first_claim_acquiring_tool_call(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / "new.py"
        target.write_text("y")

        common_dir = git_common_dir(repo)
        session_id = "recordfeed00000001"
        session_dir = common_dir / "coordinator-sessions" / session_id

        assert not session_dir.exists(), (
            "test fixture leaked a pre-existing session dir; the point of "
            "this test is a session whose first tool call is an Edit"
        )

        _fire(repo, common_dir, session_id, target)

        touch_record_sink = session_dir / "touch-record.jsonl"
        meta_file = session_dir / "meta.json"
        assert touch_record_sink.exists() and touch_record_sink.read_bytes(), (
            "the T event itself did not land — precondition for this guard"
        )
        decoded = [
            touch_record.decode_line(line)
            for line in touch_record.iter_complete_lines(touch_record_sink.read_bytes())
        ]
        assert decoded, "the T event itself did not decode — precondition for this guard"
        assert meta_file.is_file(), (
            "session holds a claim (T event on disk) with no meta.json — it is "
            "outside live_session_ids' scan scope entirely, so every peer's "
            "claim-contention check degrades to the 30-minute mtime backstop"
        )
        record = json.loads(meta_file.read_text(encoding="utf-8"))
        assert record.get("session_id") == session_id

    def test_existing_meta_json_is_not_rewritten_by_a_later_edit(self, tmp_path):
        """The guard is on ABSENCE, never staleness: once the record exists,
        subsequent Edit/Write fires cost one ``isfile`` stat and nothing else.
        Pinned by a sentinel field ``init``'s refresh branch would not
        preserve — ``last_activity`` moving would mean the hook had become the
        per-tool-call heartbeat this deliberately is not."""
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / "new.py"
        target.write_text("y")

        common_dir = git_common_dir(repo)
        session_id = "recordfeed00000002"
        session_dir = common_dir / "coordinator-sessions" / session_id

        _fire(repo, common_dir, session_id, target)
        meta_file = session_dir / "meta.json"
        before = meta_file.read_text(encoding="utf-8")

        target.write_text("z")
        _fire(repo, common_dir, session_id, target)

        assert meta_file.read_text(encoding="utf-8") == before, (
            "meta.json was rewritten on a second Edit — the absence guard has "
            "become a refresh cadence, which is a different (and separately "
            "ruled-on) question"
        )

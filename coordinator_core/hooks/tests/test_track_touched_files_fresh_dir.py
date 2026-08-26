"""
coordinator_core.hooks.tests.test_track_touched_files_fresh_dir — guards the
mkdir precondition(s) a session dir that does not exist yet depends on, for
BOTH halves of `_handler`'s work: the T-event record itself and the
`meta.json` liveness registry entry `_ensure_session_record_sync` owns.

C7 (docs/plans/2026-08-25-the-legacy-touch-record-is-retired-by-repointing-
its-writers.md) RETIRES this module's original premise. Before C7, this
hook's own `os.makedirs(session_dir, exist_ok=True)` was the ONLY thing
standing between a session whose first tool call is an Edit and a silently
lost T event: the OLD append path (`_append_locked` -> `locked_rmw`, falling
back to `_append`'s bare `open(path, "a")` when `locked_rmw` was unavailable)
did not create parent directories on that fallback, so a missing session dir
raised `FileNotFoundError`, swallowed by this module's silent-failure
contract.

C7 deletes that append mechanism entirely -- `_append_touch_record` now
routes through `touch_record.append_event` (-> `atomic_append.append_line`),
and `append_event`'s own contract is to create its sink's parent directory
on EVERY call (`touch_record.py`'s own docstring: "Creates sink's parent
directory first, per append_line's own contract that callers do so"). The
append half of this hook's work no longer depends on the hook's own
`os.makedirs` call AT ALL -- proven directly below
(``TestAppendTouchRecordSelfCreatesItsParentDir``), isolated from
`_ensure_session_record_sync` entirely.

The record half (`_ensure_session_record_sync`'s own `meta.json`
create-once-per-session) is SEPARATELY self-sufficient too:
`session.core.init` (called when `meta.json` is absent) does its own
`sdir.mkdir(parents=True, exist_ok=True)` before writing anything --
verified below (``TestSessionRecordSelfCreatesViaCoreInit``). The hook's own
`os.makedirs(session_dir, exist_ok=True)` call is KEPT in production (it is
cheap, harmless, and untouched by this chunk -- see
`track_touched_files.py`'s own docstring for why removing it was judged out
of this chunk's scope), but this module's ORIGINAL claim that it is the
"ONLY thing" preventing a lost T event or a missing `meta.json` no longer
holds, and this module stops asserting that claim as a guard.

This module has three cases now:

- ``test_t_event_lands_when_session_dir_did_not_exist`` -- end-to-end smoke
  coverage through the real `_handler`, driving a session dir that does not
  exist beforehand, confirming the whole pipeline still lands the T event.
  Not isolated to any one mkdir (multiple self-creating calls now cover the
  same ground) -- kept as cheap coverage of the common case, not a guard for
  any one of them.
- ``TestAppendTouchRecordSelfCreatesItsParentDir`` -- isolates
  `_append_touch_record` from `_ensure_session_record_sync` entirely (calls
  it directly against a sink whose parent directory chain does not exist)
  and proves `touch_record.append_event`'s own self-creation is what makes
  the append half work, independent of the hook's `os.makedirs`.
- ``TestSessionRecordSelfCreatesViaCoreInit`` -- isolates
  `_ensure_session_record_sync` and proves `session.core.init`'s own mkdir
  creates `meta.json`'s parent directory even when the hook's own
  `os.makedirs` call is neutered to a no-op.

Negative-spec: does NOT assert anything about `started_at` or
`head_at_start` -- those ride along inside `core.init` and were never this
module's concern. Does NOT assert anything about the OLD `locked_rmw`
fallback path -- that mechanism no longer exists in this module at all
(`ttf.locked_rmw` is not an attribute after C7); a test that still
monkeypatches it would fail with `AttributeError`, not exercise a real
branch.

Spec backlink: docs/plans/2026-08-22-track-touched-files-pays-only-for-the-append.md § C1
Spec backlink: docs/plans/2026-08-25-the-legacy-touch-record-is-retired-by-repointing-its-writers.md § C7
Review backlink: state/subagent-share/26c961e1-b1da-43f7-a851-3dce6fd60700/2026-08-23-codereview-sliceP3-test-surface-track-touched-files-fresh-dir.md
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from coordinator_core.hooks import track_touched_files as ttf
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.session import touch_record

# Spawns a real external process (git init fixture); runs at cadence gates,
# not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
    return tmp_path


def _decoded_paths(sink_path) -> list[str]:
    if not sink_path.exists():
        return []
    events = [
        touch_record.decode_line(line)
        for line in touch_record.iter_complete_lines(sink_path.read_bytes())
    ]
    return [event.path for event in events]


class TestHandlerWritesIntoFreshSessionDir:
    """Drives `_handler` with a session_id whose session dir does not exist
    beforehand — end-to-end smoke coverage that the whole pipeline (not any
    one mkdir in isolation — see the isolated classes below for that) still
    lands the T event."""

    def test_t_event_lands_when_session_dir_did_not_exist(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / "new.py"
        target.write_text("y")

        common_dir = git_common_dir(repo)  # production shape: <repo>/.git
        session_id = "freshdirfeed0001"
        session_dir = common_dir / "coordinator-sessions" / session_id

        assert not session_dir.exists(), (
            "test fixture leaked a pre-existing session dir; the point of "
            "this test is a session whose first tool call is an Edit"
        )

        params = {
            "session_id": session_id,
            "tool_name": "Edit",
            "file_path": str(target),
        }
        asyncio.run(ttf._handler(params, repo_root=common_dir))

        touch_record_sink = session_dir / "touch-record.jsonl"
        assert touch_record_sink.exists(), (
            "handler did not create touch-record.jsonl in a session dir "
            "that did not exist beforehand"
        )
        entries = _decoded_paths(touch_record_sink)
        assert entries, (
            "touch-record.jsonl exists but carries no T event — the append "
            "itself silently failed against the fresh session dir"
        )
        assert "src/new.py" in entries


class TestAppendTouchRecordSelfCreatesItsParentDir:
    """Isolates `_append_touch_record` from `_ensure_session_record_sync` and
    the rest of `_handler` entirely — calls it directly against a sink whose
    entire parent directory chain does not exist. Proves
    `touch_record.append_event`'s own self-creation (not the hook's
    `os.makedirs`, which this test never invokes) is what makes a fresh
    session's first append land."""

    def test_append_creates_missing_parent_chain_and_lands_the_event(self, tmp_path):
        sink = tmp_path / "does" / "not" / "exist" / "yet" / "touch-record.jsonl"
        assert not sink.parent.exists(), (
            "test fixture leaked a pre-existing parent dir; the point of "
            "this test is a sink whose parent chain is entirely absent"
        )

        ttf._append_touch_record(str(sink), "sess-fresh-01", None, "src/new.py")

        assert sink.exists(), (
            "_append_touch_record did not create its sink despite a missing "
            "parent directory chain — touch_record.append_event's own "
            "self-creation contract did not hold"
        )
        entries = _decoded_paths(sink)
        assert entries == ["src/new.py"]


class TestSessionRecordSelfCreatesViaCoreInit:
    """Isolates `_ensure_session_record_sync` with the hook's own
    `os.makedirs` neutered to a no-op, proving `session.core.init`'s own
    `sdir.mkdir(parents=True, exist_ok=True)` (not the hook's makedirs,
    which this test disables) is what creates `meta.json`'s parent
    directory for a session dir that did not exist beforehand."""

    def test_meta_json_lands_via_core_init_mkdir_even_with_hooks_makedirs_disabled(
        self, tmp_path, monkeypatch
    ):
        repo = _make_repo(tmp_path)
        common_dir = git_common_dir(repo)
        session_id = "freshdirinit0001"
        session_dir = common_dir / "coordinator-sessions" / session_id
        sessions_base = common_dir / "coordinator-sessions"

        assert not session_dir.exists(), (
            "test fixture leaked a pre-existing session dir; the point of "
            "this test is a session whose dir does not exist beforehand"
        )

        # Neuter the hook's own makedirs call to a no-op — session.core's
        # own `Path.mkdir` (a different call) is left untouched, so this
        # isolates core.init's self-creation from the hook's own mkdir.
        monkeypatch.setattr(ttf.os, "makedirs", lambda *args, **kwargs: None)

        ttf._ensure_session_record_sync(
            str(session_dir), session_id, str(sessions_base), str(repo)
        )

        meta_file = session_dir / "meta.json"
        assert meta_file.is_file(), (
            "meta.json was not created for a fresh session dir with the "
            "hook's own os.makedirs neutered — session.core.init's own "
            "mkdir did not create the parent directory"
        )

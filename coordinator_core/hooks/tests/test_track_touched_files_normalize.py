"""
coordinator_core.hooks.tests.test_track_touched_files_normalize — coverage
for session.scope.normalize_touch_path's absolute-path handling (imported into
track_touched_files as `normalize_touch_path`; the module no longer carries
its own parallel `_normalize_path` implementation), AND (C7, AC3) for
`session.touch_record.encode_line`'s own OutOfWorktreePath containment check
now that this hook's writes route through it.

Spec backlink: DoE security-audit 2026-07-31 (coordinatorsecurity-audit-worker
-1671c577.md) — Writer 1 finding: on POSIX, os.path.relpath's only exception
(ValueError) is the Windows cross-drive case, and the pre-fix fallback
returned file_path UNCHANGED (still absolute) rather than skipping — an
absolute, drive-lettered entry would land in touched.txt on a real
multi-drive Windows checkout.

This module runs entirely on macOS/Linux CI, where a genuine cross-drive
ValueError cannot occur (there are no drive letters) — the Windows leg is
therefore exercised here by SIMULATING the ValueError os.path.relpath raises
in that case, not by a live cross-drive repro. Untested on a real Windows
box; flagging this per the P0 multi-OS mandate rather than claiming live
verification.

C7 (docs/plans/2026-08-25-the-legacy-touch-record-is-retired-by-repointing-
its-writers.md): the handler's own sink is now `touch-record.jsonl`, the
self-describing dialect `session.touch_record` owns; every case in this file
that reads back written entries decodes them via `touch_record.decode_line`
rather than the retired `scope.parse_touch_event` bare-line format.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.hooks import track_touched_files as ttf
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.session import scope as touch_scope
from coordinator_core.session import core as session_core
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_passthrough_kwargs


# Spawns a real external process; runs at cadence gates, not per-commit.
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


def _decoded_paths(sink_path: Path) -> list[str]:
    """Read a `touch-record.jsonl` sink and return every decoded entry's
    `path` field, in file order. Fails loudly (via `decode_line`) on a
    malformed line — these tests exercise only well-formed writes."""
    if not sink_path.exists():
        return []
    events = [
        touch_record.decode_line(line)
        for line in touch_record.iter_complete_lines(sink_path.read_bytes())
    ]
    return [event.path for event in events]


class TestNormalizeTouchPathIsolatedUnit:
    """Unit-isolated coverage for ``session.scope.normalize_touch_path`` itself,
    called directly with a hand-built worktree root — the SAME root shape the
    `_handler` end-to-end path (see ``TestHandlerEndToEndCommonDirScopedRoot``
    below) derives via ``main_worktree_root(common_dir)`` before handing it to
    this function as ``cwd``. This class deliberately bypasses the handler's
    own common_dir → worktree-root derivation step; it does NOT cover that
    derivation (a wrong derivation would still make these cases pass). That
    derivation is exercised end-to-end only by
    ``TestHandlerEndToEndCommonDirScopedRoot``.
    """

    def test_relative_path_passthrough(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert ttf.normalize_touch_path("src/foo.py", str(repo)) == "src/foo.py"

    def test_absolute_tracked_path_via_git_ls_files(self, tmp_path):
        repo = _make_repo(tmp_path)
        target = repo / "README.md"
        assert ttf.normalize_touch_path(str(target), str(repo)) == "README.md"

    def test_absolute_untracked_path_via_relpath(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / "new.py"
        target.write_text("y")
        assert ttf.normalize_touch_path(str(target), str(repo)) == "src/new.py"

    def test_cross_drive_relpath_failure_skips_not_absolute(self, tmp_path, monkeypatch):
        """Simulates the Windows cross-drive ValueError os.path.relpath raises
        when file_path is on a different drive than git_root — the fallback
        MUST return None (skip), never file_path unchanged (absolute)."""
        repo = _make_repo(tmp_path)

        def _boom(*a, **k):
            raise ValueError("simulated cross-drive relpath failure")

        monkeypatch.setattr(touch_scope.os.path, "relpath", _boom)
        outside = "/totally/outside/xyz.py"  # git ls-files will miss this
        result = ttf.normalize_touch_path(outside, str(repo))
        assert result is None
        assert result != outside

    def test_drive_qualified_path_recognized_as_absolute(self):
        # A Windows drive-qualified path is recognized as absolute by the
        # module's own leading-slash-or-drive-letter regex, the same class
        # scope._is_absolute matches — pure regex check, no filesystem
        # involved, and no concrete machine path cited.
        drive_letter = "X"
        drive_qualified = drive_letter + ":" + "\\some\\path\\file.py"
        assert re.match(r"^[A-Za-z]:", drive_qualified) is not None


class TestHandlerEndToEndCommonDirScopedRoot:
    """Drives the actual `_handler` op entrypoint with the PRODUCTION call
    shape: `repo_root` scoped to the git common dir (`<repo>/.git`), not the
    worktree root. `op_scopes.py` registers
    `"hooks.track_touched_files": "common_dir"`, so `ipc.py`'s dispatch always
    hands this handler `git_common_dir(request_repo)` — never the worktree
    root the other cases in this file pass. This is the exact gap the
    §Problem section's 92%-corrupt-corpus finding traces to: every existing
    case in this file (before this class was added) exercised a call shape
    production never makes.

    Was authored as a RED test against pre-fix HEAD (`_normalize_path`,
    fed the common dir directly, produced a `../`-prefixed entry); C2 in
    this same change makes it green by routing through
    `main_worktree_root(common_dir)` before normalization.

    Spec backlink: pln-touched-txt-path-poisoning-nor-ecab01
    § C1 / AC3.
    """

    def test_written_entry_is_clean_repo_relative(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / "new.py"
        target.write_text("y")

        common_dir = git_common_dir(repo)  # production shape: <repo>/.git

        params = {
            "session_id": "deadbeefcafe0001",
            "tool_name": "Edit",
            "file_path": str(target),
        }
        asyncio.run(ttf._handler(params, repo_root=common_dir))

        touch_record_sink = (
            common_dir / "coordinator-sessions" / params["session_id"] / "touch-record.jsonl"
        )
        assert touch_record_sink.exists(), "handler did not create the session touch-record.jsonl"

        entries = _decoded_paths(touch_record_sink)
        assert entries, "handler wrote no entries to touch-record.jsonl"

        for entry in entries:
            assert not entry.startswith("../"), (
                f"touch-record.jsonl entry {entry!r} carries a '../' prefix — the "
                "handler was handed the common_dir as git_root and computed "
                "relpath(file_path, <repo>/.git) instead of the worktree root"
            )
            assert not (entry.startswith("/") or re.match(r"^[A-Za-z]:", entry)), (
                f"touch-record.jsonl entry {entry!r} is absolute, not repo-relative"
            )
        assert "src/new.py" in entries


class TestHandlerRuntimeErrorFallbackNonGitFixture:
    """Drives the `except RuntimeError` fallback branch in `_handler` (fires
    when `git_common_dir(repo_root)` raises — non-git fixture / git
    unavailable). Confirms the fallback resolves `_sessions_base` to
    `<repo_root>/coordinator-sessions` (no doubled `.git` segment) and that
    `_handler` does not raise, even though `_common_dir` stays `None` and
    both `_sessions_base` and `_worktree_root` collapse onto the same
    `git_root` value on this path (Finding 1, coordinatorcode-reviewer-228e0ba7.md
    — documented-inert for production; this test only confirms it stays inert
    and crash-free for a non-git fixture, not that the collapse is fixed).
    """

    def test_session_dir_created_without_doubled_git_segment(self, tmp_path):
        non_git_root = tmp_path / "not_a_repo"
        non_git_root.mkdir()
        (non_git_root / "src").mkdir()
        target = non_git_root / "src" / "new.py"
        target.write_text("y")

        params = {
            "session_id": "deadbeefcafe0002",
            "tool_name": "Edit",
            "file_path": str(target),
        }
        asyncio.run(ttf._handler(params, repo_root=non_git_root))

        session_dir = non_git_root / "coordinator-sessions" / params["session_id"]
        assert session_dir.is_dir(), (
            "fallback branch did not create the expected session dir at "
            "<repo_root>/coordinator-sessions"
        )
        assert not (non_git_root / ".git" / "coordinator-sessions").exists(), (
            "fallback branch re-introduced the doubled '.git' join"
        )


class TestHandlerNormalizeTouchPathRootWiring:
    """Pins the `_handler` call site's `normalize_touch_path(..., root=...)`
    keyword wiring (break-class fix, 2026-08-08): `asyncio.to_thread` forwards
    positionally, so the pre-fix call `normalize_touch_path(file_path,
    _worktree_root)` bound `_worktree_root` to `cwd` only, never `root` —
    `normalize_touch_path`'s ``resolved_root = root if root else
    core.git_root(cwd)`` fallback then re-spawned ``git rev-parse
    --show-toplevel`` on every untracked path, the exact spawn the ``root``
    parameter exists to remove. `TestNormalizeTouchPathSpawnCount` in
    `coordinator_core/session/tests/test_scope.py` calls
    `normalize_touch_path` directly with `root=` and so never exercised this
    caller; this class drives the real `_handler` entrypoint instead.

    Monkeypatches `scope.core.git_root` (the sole subprocess seam for the
    worktree-root re-derivation `normalize_touch_path` falls back to when
    `root` is absent) to a plain counter — a call count of 0 proves the hook
    supplied `root` and no re-derivation spawn occurred.
    """

    def test_untracked_path_handled_without_git_root_respawn(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / "new.py"
        target.write_text("y")

        common_dir = git_common_dir(repo)  # production shape: <repo>/.git

        calls = {"git_root": 0}
        real_git_root = session_core.git_root

        def _counted_git_root(cwd=None):
            calls["git_root"] += 1
            return real_git_root(cwd)

        monkeypatch.setattr(touch_scope.core, "git_root", _counted_git_root)

        params = {
            "session_id": "deadbeefcafe0003",
            "tool_name": "Edit",
            "file_path": str(target),
        }
        asyncio.run(ttf._handler(params, repo_root=common_dir))

        assert calls["git_root"] == 0, (
            "normalize_touch_path re-derived the worktree root via "
            "core.git_root(cwd) — the hook failed to supply its own "
            "`root=` keyword to normalize_touch_path"
        )

        touch_record_sink = (
            common_dir / "coordinator-sessions" / params["session_id"] / "touch-record.jsonl"
        )
        entries = _decoded_paths(touch_record_sink)
        assert "src/new.py" in entries


class TestHandlerZeroSpawnFastArmAtCaller:
    """Proves the zero-spawn fast arm (``scope._touch_path_fast_arm_eligible``
    over ``scope._GUARD_CLAUSES``, landed C1 `b1e0881d3`) fires at the
    CALLER — ``hooks.track_touched_files::_handler`` — not merely at
    ``normalize_touch_path`` called directly with a hand-supplied ``root=``.

    Spec backlink: docs/plans/2026-08-08-prove-the-arms-agree-then-stop-
    asking-gi.md § AC1. This is the F0-catching test: staff-eng finding F0
    on that plan noted every pre-existing spawn-count test called
    ``normalize_touch_path`` directly with ``root=`` supplied, so a
    function-level AC1 test proved nothing about production reachability —
    the hot-path caller could (and once did, until `c53ad6774`) pass
    ``root`` positionally, leaving it ``None`` at the fast-arm's truthy-
    ``root`` gate and the guard permanently unreachable in production, with
    every existing AC still green. A test that only calls
    ``normalize_touch_path`` directly, with ``root=`` hand-supplied, does
    NOT satisfy AC1 — this class drives the real ``_handler`` entrypoint on
    the production call shape instead
    (``repo_root=git_common_dir(repo)``, per
    ``TestHandlerEndToEndCommonDirScopedRoot`` above).

    Counted, and why: this monkeypatches the SAME two subprocess seams
    ``TestNormalizeTouchPathSpawnCount._spy`` in
    ``coordinator_core/session/tests/test_scope.py`` uses —
    ``coordinator_core.session.scope._git_run`` (the sole seam for
    ``ls-files``) and ``coordinator_core.session.core.git_root`` (the sole
    seam for worktree-root re-derivation) — so a count of 0 proves no git
    process was spawned at all, not merely that its result went unused.

    Deliberately EXCLUDED from the zero-spawn assertion: `_handler` itself
    may legitimately spawn git for reasons OTHER than
    ``normalize_touch_path`` — session bootstrap (`_bootstrap_session` /
    `core.init`) reads `git rev-parse HEAD` / `--abbrev-ref HEAD` via a
    SEPARATE, unpatched `subprocess.check_output` call, and `git_common_dir`
    resolution happens before `_handler` is even entered here (the test
    computes it itself, outside the counted call). Weakening the assertion
    to "fewer spawns than before" would hide exactly the regression this
    test exists to catch, so instead: (1) the `_git_run` spy asserts NO call
    carried `ls-files` in its args (the sole seam `normalize_touch_path`'s
    slow-arm and fast-arm-declined path would use), and (2) `core.git_root`
    is asserted never called (the sole seam the `root`-absent fallback
    would re-derive the worktree root through). Both together prove the
    fast arm — not merely "fewer spawns" — is what fired.
    """

    def test_tracked_path_zero_ls_files_and_zero_git_root_at_handler(
        self, tmp_path, monkeypatch
    ):
        repo = _make_repo(tmp_path)
        target = repo / "README.md"  # tracked by _make_repo, guard-eligible

        git_run_calls = []
        real_git_run = touch_scope._git_run

        def _counted_git_run(args, cwd=None):
            git_run_calls.append(list(args))
            return real_git_run(args, cwd)

        git_root_calls = {"count": 0}
        real_git_root = session_core.git_root

        def _counted_git_root(cwd=None):
            git_root_calls["count"] += 1
            return real_git_root(cwd)

        monkeypatch.setattr(touch_scope, "_git_run", _counted_git_run)
        monkeypatch.setattr(touch_scope.core, "git_root", _counted_git_root)

        common_dir = git_common_dir(repo)  # production shape: <repo>/.git

        params = {
            "session_id": "deadbeefcafe0004",
            "tool_name": "Edit",
            "file_path": str(target),
        }
        asyncio.run(ttf._handler(params, repo_root=common_dir))

        ls_files_calls = [
            args for args in git_run_calls if "ls-files" in args
        ]
        assert ls_files_calls == [], (
            "normalize_touch_path spawned `git ls-files` for a guard-"
            f"eligible tracked path — the fast arm did not fire: {ls_files_calls!r}"
        )
        assert git_root_calls["count"] == 0, (
            "normalize_touch_path re-derived the worktree root via "
            "core.git_root(cwd) — the fast arm did not fire, or the "
            "handler failed to supply `root=`"
        )

        touch_record_sink = (
            common_dir / "coordinator-sessions" / params["session_id"] / "touch-record.jsonl"
        )
        entries = _decoded_paths(touch_record_sink)
        assert "README.md" in entries
        for entry in entries:
            assert not entry.startswith("../")
            assert not (entry.startswith("/") or re.match(r"^[A-Za-z]:", entry))


class TestAC3OutOfWorktreePathReachableThroughHandler:
    """AC3: with `normalize_touch_path` no longer the only thing holding the
    out-of-worktree invariant, `touch_record.encode_line`'s own
    `OutOfWorktreePath` containment check (touch_record.py AC23) becomes
    reachable through this hook's write path — a second, independent layer,
    not merely a documented intention.

    Proven, not merely noted: `ttf.normalize_touch_path` is monkeypatched to
    return an absolute path that slips past the handler's own `if not
    file_path_norm: return` guard (which only checks for falsy/None, not
    absoluteness — the handler trusts `normalize_touch_path` to have already
    excluded that case). This simulates a defect in `normalize_touch_path`
    to exercise the SECOND layer directly, proving it is live at this call
    site rather than merely present in `touch_record.py`'s own unit tests.
    """

    def test_absolute_normalized_path_is_rejected_by_encode_line_not_written(
        self, tmp_path, monkeypatch
    ):
        repo = _make_repo(tmp_path)
        target = repo / "README.md"
        common_dir = git_common_dir(repo)  # production shape: <repo>/.git

        def _return_absolute(*args, **kwargs):
            return "/etc/passwd"

        monkeypatch.setattr(ttf, "normalize_touch_path", _return_absolute)

        params = {
            "session_id": "outofworktree00001",
            "tool_name": "Edit",
            "file_path": str(target),
        }

        # Must not raise: OutOfWorktreePath is caught and swallowed by
        # `_append_touch_record`'s silent-failure contract (this hook must
        # never block or error-propagate to the tool call).
        asyncio.run(ttf._handler(params, repo_root=common_dir))

        touch_record_sink = (
            common_dir / "coordinator-sessions" / params["session_id"] / "touch-record.jsonl"
        )
        entries = _decoded_paths(touch_record_sink)
        assert entries == [], (
            "an out-of-worktree (absolute) path reached encode_line and was "
            "rejected (AC23) -- it must never land in touch-record.jsonl"
        )

    def test_encode_line_itself_raises_for_an_absolute_path(self):
        """Direct, module-level confirmation of the exception this call site
        relies on catching — not a substitute for the handler-level proof
        above, which is what shows the exception is actually REACHABLE
        through this hook rather than merely defined."""
        with pytest.raises(touch_record.OutOfWorktreePath):
            touch_record.encode_line(
                session_id="s1",
                agent_id=None,
                verb=touch_record.VERB_TOUCH,
                path="/etc/passwd",
            )

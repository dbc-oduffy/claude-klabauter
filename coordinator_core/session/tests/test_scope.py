"""
coordinator_core.session.tests.test_scope — parity tests for
coordinator_core.session.scope (session touch-tracking + scoped-staging-set
computation).

Port of: scope.sh (DoE e34f2484, 2026-07-22).

Two strategies (per the T4a test pattern):
  (a) The scope.sh boundary matrix transcribed 1:1 — the touch()
      normalization/dedup/still-absolute-skip cases, the
      compute_scope() set-math cases (incl. the nested
      _cs_other_claim_owner first-writer-wins scan), and the archive()
      idempotency/date cases.
  (b) GOLDEN-DIFF against reality — compute_scope() is exercised against a
      real git repo with real `git diff`/`git ls-files` output and real
      on-disk mtimes, and against a FROZEN copy of this repo's own session
      registry corpus (see below for why frozen, not live).

The corpus test in strategy (b) used to be a live
``sorted(Path(core.git_root() or ".", ".git", "coordinator-sessions").glob(
"*/meta.json"))[:25]`` walk. That corpus lives inside ``.git/`` — untracked,
machine-local, never cloned — so on a fresh clone, in CI, or on any machine
that has never run a coordinator session, the glob returns ``[]``,
``parametrize`` silently collects ZERO tests, and this golden-diff reports
nothing wrong while exercising nothing at all. Frozen 2026-07-22 into
``fixtures/frozen_coordinator_sessions_corpus_2026-07-22.json`` (the same
snapshot test_core.py freezes) and rebuilt as a self-contained tmp_path git
repo — every frozen session directory present as a sibling, so the Step 3
other-session-claims scan still exercises real cross-session shapes rather
than a single session in isolation. Never re-point this test at the LIVE
``.git/`` corpus again — that is exactly the bug being fixed.

Recipe: scratch/subagent-sandbox/bash-to-python-engine-migration/
recipe-t4a-coordinator-session-hub.md § scope.py
Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4a-g1
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.session import core, scope, touch_record
from coordinator_core.testing import symlink_capability
from coordinator_core.win_portability import no_console_creationflags, no_console_passthrough_kwargs

# Every test in this file builds its repo via `_make_repo(tmp_path)`, spawning
# real git (init/config/add/commit) because the production code under test --
# `scope`'s `ls-files`/status seams and `core.git_root()` -- reads real git
# state (working tree entries, index status) that no mock stands in for.
# `tmp_path` is function-scoped and several tests write session/claim state
# under the SAME session ids across the file, so the repo fixture stays
# per-test rather than hoisted to module scope. The spawn ratchet's
# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the
# route for this file -- coordinator_core/tests/test_no_new_spawning_tests.py
# Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agent_claim(agent_dir, *paths, owner_sid=None):
    """Record an agent dir's claims in the one dialect its readers read.

    Was a bare-path ``touched.txt`` write until the compat union came out
    (2026-08-26); such a file is now inert, so a fixture that writes one
    silently claims nothing. ``owner_sid`` defaults to the dir's own
    ``em-session-id.txt`` back-pointer, which is the identity every reader
    attributes an agent's claims to.
    """
    agent_dir = Path(agent_dir)
    if owner_sid is None:
        backptr = agent_dir / "em-session-id.txt"
        owner_sid = (
            backptr.read_text(encoding="utf-8").splitlines()[0].strip()
            if backptr.is_file()
            else agent_dir.name
        )
    sink = agent_dir / scope._TOUCH_RECORD_FILENAME
    for entry in paths:
        touch_record.append_event(
            sink,
            session_id=owner_sid,
            agent_id=agent_dir.name,
            verb=touch_record.VERB_TOUCH,
            path=entry,
        )
    return sink


def _make_repo(tmp_path):
    # Review: staff-eng F12 — check=True on every fixture-setup git call: a
    # silent fixture-setup failure (e.g. a misconfigured test-runner git)
    # must not masquerade as a passing test exercising an empty/unstaged
    # repo; fail loud at setup instead.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    return tmp_path


def _sdir(repo, sid):
    return Path(repo) / ".git" / "coordinator-sessions" / sid


def _age_session_dir_records(sdir, epoch) -> None:
    """C5 (docs/plans/2026-08-25-the-legacy-touch-record-is-retired-by-
    repointing-its-writers.md) widened ``liveness.newest_record_mtime`` to
    scan EVERY regular file in a session dir (bar
    ``_RECORD_MTIME_EXCLUDED_NAMES``) and take the freshest mtime, replacing
    the old single-literal (``touched.txt``) mtime probe. A test that
    stales only the touch-record sink while ``meta.json``/``started_at``/
    ``head_at_start`` stay fresh (their mtime set by the very
    ``core.update_meta_field`` call the test just made) therefore no
    longer reads abandoned -- the freshest file in the dir wins regardless.
    This ages every eligible file in ``sdir`` to ``epoch`` so the
    ``dir_record`` signal is genuinely stale, matching production's actual
    freshest-file-wins policy rather than the retired single-file one."""
    for entry in Path(sdir).iterdir():
        if entry.name == "em-session-id.txt" or not entry.is_file():
            continue
        os.utime(str(entry), (epoch, epoch))


def _decode_events(record_path) -> list:
    """C4 test helper: decode a `touch-record.jsonl` sink's LIVE file only
    (no rotated-family expansion — every test fixture in this file is
    small enough to never rotate), in on-disk order. Thin call-through to
    `touch_record.decode_line`/`iter_complete_lines` -- no independent
    parsing invented here."""
    path = Path(record_path)
    if not path.is_file():
        return []
    raw = path.read_bytes()
    return [touch_record.decode_line(line) for line in touch_record.iter_complete_lines(raw)]


def _write_touch_record(record_path, *, session_id, entries) -> None:
    """C4 test helper: write a peer/agent `touch-record.jsonl` fixture
    directly (bypassing `scope.touch()`) for tests that need an explicit
    verb/timestamp/path sequence -- e.g. a pre-existing R event, or two
    peers claiming the same path at different times. `entries` is an
    iterable of ``(verb, path)`` or ``(verb, path, timestamp)`` tuples, in
    write order. Thin call-through to `touch_record.append_event` -- no
    independent line-encoding invented here."""
    for entry in entries:
        if len(entry) == 2:
            verb, path = entry
            timestamp = None
        else:
            verb, path, timestamp = entry
        touch_record.append_event(
            record_path,
            session_id=session_id,
            agent_id=None,
            verb=verb,
            path=path,
            timestamp=timestamp,
        )


_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_FROZEN_CORPUS_FIXTURE = (
    _FIXTURES_DIR / "frozen_coordinator_sessions_corpus_2026-07-22.json"
)
# Kept in lockstep with test_core.py's constant of the same name — both read
# the same frozen fixture file. A re-freeze that narrows either corpus fails
# loud here rather than silently shrinking this golden-diff.
_EXPECTED_CORPUS_SIZE = 53

_FROZEN_CORPUS_ENTRIES = json.loads(
    _FROZEN_CORPUS_FIXTURE.read_text(encoding="utf-8")
)["entries"]
# Collection-time (not test-time) non-empty + size guard — see module
# docstring: a bare `@pytest.mark.parametrize` over an empty/narrowed list
# just collects fewer tests and reports nothing wrong.
assert len(_FROZEN_CORPUS_ENTRIES) == _EXPECTED_CORPUS_SIZE, (
    f"frozen corpus fixture {_FROZEN_CORPUS_FIXTURE} has "
    f"{len(_FROZEN_CORPUS_ENTRIES)} entries, expected "
    f"{_EXPECTED_CORPUS_SIZE} -- update _EXPECTED_CORPUS_SIZE only as a "
    "deliberate acknowledgment of a corpus re-freeze, never to silence this."
)


# ---------------------------------------------------------------------------
# touch() — required args
# ---------------------------------------------------------------------------


def test_touch_requires_sid():
    with pytest.raises(ValueError):
        scope.touch("", "some/path.txt")


def test_touch_requires_path():
    with pytest.raises(ValueError):
        scope.touch("sid", "")


def test_touch_noop_outside_git_repo(tmp_path):
    # session_dir empty -> fail-open, no raise, no file written.
    assert scope.touch("sid", "some/path.txt", cwd=str(tmp_path)) is None


# ---------------------------------------------------------------------------
# touch() — relative path append + dedup + last_activity
# ---------------------------------------------------------------------------


class TestTouchAppend:
    """C4: `touch()` now writes `touch-record.jsonl` (C3's self-describing
    record) via `touch_record.append_event`, not the bash-dialect
    `touched.txt` -- see `touch()`'s own docstring for the writer flip and
    AC17's dedup-read removal."""

    def test_appends_relative_path_and_creates_session_dir(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("s1", cwd=str(repo))
        scope.touch("s1", "src/foo.py", cwd=str(repo))
        record = _sdir(repo, "s1") / "touch-record.jsonl"
        events = _decode_events(record)
        assert len(events) == 1
        assert (events[0].verb, events[0].path) == (touch_record.VERB_TOUCH, "src/foo.py")

    def test_creates_session_dir_on_first_touch_when_init_skipped(self, tmp_path):
        repo = _make_repo(tmp_path)
        # No core.init first — touch() must fail-safe init the session dir.
        scope.touch("s-lazy", "a/b.py", cwd=str(repo))
        record = _sdir(repo, "s-lazy") / "touch-record.jsonl"
        assert record.is_file()
        paths = [e.path for e in _decode_events(record)]
        assert "a/b.py" in paths

    def test_backfills_meta_json_when_dir_precreated_without_meta(self, tmp_path):
        """Regression (defect A, 2026-07-24): touch() backfills meta.json when a
        prior bookkeeping writer created the session dir first (dir present,
        meta.json absent). The old `if not isdir` guard skipped core.init here,
        leaving Layer-1 liveness unwritten.
        """
        repo = _make_repo(tmp_path)
        sdir = _sdir(repo, "s-precreated")
        sdir.mkdir(parents=True, exist_ok=True)  # another writer won the create race
        (sdir / "push-failures-cursor.txt").write_text("0")
        assert not (sdir / "meta.json").is_file(), "precondition: no meta.json"

        scope.touch("s-precreated", "src/foo.py", cwd=str(repo))

        assert (sdir / "meta.json").is_file(), "meta.json must be backfilled on touch"
        assert core.read_meta_field(str(sdir), "session_id") == "s-precreated"

    def test_repeat_touch_no_dedup_read_but_last_verb_wins_at_read_time(self, tmp_path):
        """C4/AC17: the pre-append dedup READ is gone -- a re-touch of the
        same path always appends a fresh raw event (no skip), and it is the
        READER's last-verb-wins projection (`touch_record.project_live_claims`),
        not the writer, that collapses repeats to one live claim."""
        repo = _make_repo(tmp_path)
        core.init("s2", cwd=str(repo))
        scope.touch("s2", "src/foo.py", cwd=str(repo))
        scope.touch("s2", "src/foo.py", cwd=str(repo))
        record = _sdir(repo, "s2") / "touch-record.jsonl"
        events = _decode_events(record)
        assert len(events) == 2  # both raw events land -- no dedup on write
        assert all(e.path == "src/foo.py" for e in events)
        projection = touch_record.project_live_claims(record, cwd=str(repo))
        assert list(projection.claims.keys()) == ["src/foo.py"]  # one live claim

    def test_dedup_is_full_line_not_substring(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("s3", cwd=str(repo))
        scope.touch("s3", "src/foo.py", cwd=str(repo))
        scope.touch("s3", "src/foo", cwd=str(repo))  # substring — distinct path
        record = _sdir(repo, "s3") / "touch-record.jsonl"
        paths = [e.path for e in _decode_events(record)]
        assert paths == ["src/foo.py", "src/foo"]

    def test_updates_last_activity(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("s4", cwd=str(repo))
        sdir = _sdir(repo, "s4")
        core.update_meta_field(str(sdir), "last_activity", "2000-01-01T00:00:00Z")
        scope.touch("s4", "x.py", cwd=str(repo))
        assert core.read_meta_field(str(sdir), "last_activity") != "2000-01-01T00:00:00Z"


class TestTouchEventAware:
    """C4/AC17: `touch()`'s pre-append dedup scan is REMOVED -- every call
    appends a fresh raw T event, and the CLAIMED/RELEASED decision moves
    entirely to the reader (`touch_record.project_live_claims`'s
    last-verb-wins fold). These tests now pin the reader-side projection
    rather than a writer-side skip."""

    def test_fresh_path_gets_a_t_event(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("s-fresh", cwd=str(repo))
        scope.touch("s-fresh", "src/new.py", cwd=str(repo))
        record = _sdir(repo, "s-fresh") / "touch-record.jsonl"
        events = _decode_events(record)
        assert len(events) == 1
        assert (events[0].verb, events[0].path) == (touch_record.VERB_TOUCH, "src/new.py")

    def test_last_event_t_projects_claimed_after_repeat_touch(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("s-t", cwd=str(repo))
        scope.touch("s-t", "src/foo.py", cwd=str(repo))
        scope.touch("s-t", "src/foo.py", cwd=str(repo))
        record = _sdir(repo, "s-t") / "touch-record.jsonl"
        projection = touch_record.project_live_claims(record, cwd=str(repo))
        assert "src/foo.py" in projection.claims

    def test_last_event_r_gets_a_new_t(self, tmp_path):
        """AC8's falsifying case: an edit after a release must not be
        silently unclaimed."""
        repo = _make_repo(tmp_path)
        core.init("s-r", cwd=str(repo))
        record = _sdir(repo, "s-r") / "touch-record.jsonl"
        touch_record.append_event(
            record, session_id="s-r", agent_id=None,
            verb=touch_record.VERB_RELEASE, path="src/foo.py",
        )
        scope.touch("s-r", "src/foo.py", cwd=str(repo))
        events = _decode_events(record)
        assert len(events) == 2
        assert (events[1].verb, events[1].path) == (touch_record.VERB_TOUCH, "src/foo.py")
        projection = touch_record.project_live_claims(record, cwd=str(repo))
        assert "src/foo.py" in projection.claims


# ---------------------------------------------------------------------------
# touch() — absolute-path normalization + still-absolute skip
# ---------------------------------------------------------------------------


class TestTouchNormalization:
    def test_absolute_path_inside_repo_normalized_to_relative(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("s5", cwd=str(repo))
        (repo / "src").mkdir()
        target = repo / "src" / "new.py"
        target.write_text("y")  # untracked -> relpath branch
        scope.touch("s5", str(target), cwd=str(repo))
        record = _sdir(repo, "s5") / "touch-record.jsonl"
        paths = [e.path for e in _decode_events(record)]
        assert paths == ["src/new.py"]
        # crucially, NOT the absolute form
        assert not any(scope._is_absolute(p) for p in paths)

    def test_absolute_tracked_path_normalized_via_git_ls_files(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("s6", cwd=str(repo))
        # README.md is tracked -> git ls-files --full-name branch
        target = repo / "README.md"
        scope.touch("s6", str(target), cwd=str(repo))
        record = _sdir(repo, "s6") / "touch-record.jsonl"
        assert [e.path for e in _decode_events(record)] == ["README.md"]

    def test_still_absolute_path_skipped_when_normalization_fails(self, tmp_path, monkeypatch):
        """Guard: if the path is STILL absolute after the
        normalization attempt (git ls-files miss + relpath failure), SKIP it.
        On POSIX ``os.path.relpath`` almost always yields a ``../`` relative
        form, so we simulate the normalization miss the bash guard is written
        for (Python unavailable / cross-drive on Windows) by forcing relpath
        to raise — the path then stays absolute and must be skipped."""
        repo = _make_repo(tmp_path)
        core.init("s7", cwd=str(repo))

        def _boom(*a, **k):
            raise ValueError("simulated relpath failure")

        monkeypatch.setattr(scope.os.path, "relpath", _boom)
        outside = "/totally/outside/xyz.py"  # git ls-files will miss this
        assert scope.touch("s7", outside, cwd=str(repo)) is None
        touched = _sdir(repo, "s7") / "touched.txt"
        # touched.txt may be created by init, but the absolute path must NOT
        # have been appended.
        lines = touched.read_text().splitlines() if touched.is_file() else []
        assert outside not in lines
        assert not any(scope._is_absolute(l) for l in lines)

    def test_normalize_touch_path_relative_passthrough(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert scope.normalize_touch_path("src/foo.py", cwd=str(repo)) == "src/foo.py"

    def test_normalize_touch_path_absolute_tracked_via_git_ls_files(self, tmp_path):
        repo = _make_repo(tmp_path)
        target = repo / "README.md"
        assert scope.normalize_touch_path(str(target), cwd=str(repo)) == "README.md"

    def test_normalize_touch_path_absolute_untracked_via_relpath(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / "new.py"
        target.write_text("y")
        assert (
            scope.normalize_touch_path(str(target), cwd=str(repo)) == "src/new.py"
        )

    def test_normalize_touch_path_still_absolute_returns_none(
        self, tmp_path, monkeypatch
    ):
        repo = _make_repo(tmp_path)

        def _boom(*a, **k):
            raise ValueError("simulated relpath failure")

        monkeypatch.setattr(scope.os.path, "relpath", _boom)
        outside = "/totally/outside/xyz.py"
        assert scope.normalize_touch_path(outside, cwd=str(repo)) is None

    def test_normalize_touch_path_non_ascii_tracked_name_via_git_ls_files(
        self, tmp_path
    ):
        """Regression for bug-backlog
        2026-08-08-core-quotepath-corrupts-touched-txt-for-9b099a0360ca:
        with the caller's ``core.quotePath`` left at its default (true), a
        tracked non-ASCII filename must normalize to its real name, NOT
        git's C-quoted-and-octal-escaped form."""
        repo = _make_repo(tmp_path)
        target = repo / "café.md"
        target.write_text("y", encoding="utf-8")
        subprocess.run(["git", "add", "café.md"], cwd=str(repo), check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-m", "add non-ascii file"], cwd=str(repo), check=True,
            **no_console_passthrough_kwargs(),
        )
        result = scope.normalize_touch_path(str(target), cwd=str(repo))
        assert result == "café.md"

    def test_normalize_touch_path_extended_length_prefix_normalizes(self, tmp_path):
        """Regression for bug-backlog
        2026-08-08-extended-length-paths-bypass-absolute-re-3e7b2e5a95ff:
        a Windows extended-length-prefixed absolute path must be recognized
        as absolute (prefix stripped first) and normalized to a repo-
        relative result, not returned unchanged/still-absolute."""
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / "new.py"
        target.write_text("y")
        prefixed = "\\\\?\\" + str(target)
        result = scope.normalize_touch_path(prefixed, cwd=str(repo))
        assert result == "src/new.py"
        assert not scope._is_absolute(result)

    def test_is_absolute_predicate(self):
        assert scope._is_absolute("/etc/passwd") is True
        assert scope._is_absolute("C:/Users/x") is True  # abs-path-ok: synthetic drive-letter literal exercising the predicate, not a real machine path
        assert scope._is_absolute("src/foo.py") is False
        assert scope._is_absolute("") is False


class TestNormalizeTouchPathRelativeDialectFold:
    """C1 (docs/plans/2026-08-11-claim-release-and-the-gate-that-cannot-
    clear.md): normalize_touch_path's relative arm previously fell straight
    through to `return fpath` verbatim -- a backslashed relative pathspec
    reached touched.txt with backslashes intact, while
    claim_index._normalize_key folded unconditionally, so the two dialects
    disagreed. These pins assert the relative arm now folds via the shared
    coordinator_core.session.path_dialect.canonicalize_relative_path."""

    def test_backslashed_and_forward_slashed_relative_paths_are_byte_identical(
        self, tmp_path
    ):
        repo = _make_repo(tmp_path)
        backslashed = scope.normalize_touch_path("state\\x.md", cwd=str(repo))
        forward = scope.normalize_touch_path("state/x.md", cwd=str(repo))
        assert backslashed == forward == "state/x.md"

    def test_relative_arm_matches_claim_index_normalize_key(self, tmp_path):
        from coordinator_core.session import claim_index

        repo = _make_repo(tmp_path)
        scope_key = scope.normalize_touch_path("a\\b\\c.md", cwd=str(repo))
        claim_key = claim_index._normalize_key("a\\b\\c.md")
        assert scope_key == claim_key == "a/b/c.md"

    def test_relative_arm_dotdot_containing_still_returns_a_value(self, tmp_path):
        """Regression: normalize_touch_path guards absoluteness only -- it
        does NOT enforce containment (that is classify_touch_entry's job,
        unchanged by this chunk). A '..'-containing relative entry still
        canonicalizes and returns non-None here."""
        repo = _make_repo(tmp_path)
        assert (
            scope.normalize_touch_path("docs\\..\\peer\\x.md", cwd=str(repo))
            == "peer/x.md"
        )

    def test_relative_arm_dot_relative_entry_normalizes(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert scope.normalize_touch_path("./x.md", cwd=str(repo)) == "x.md"


class TestNormalizeTouchPathSpawnCount:
    """Spawn-COUNT coverage for the ``root``-supplied ``core.git_root(cwd)``
    skip added 2026-08-08 (docs/plans/2026-08-08-touched-path-normalize-
    spawn-diet.md, chunk C1) -- narrowed post-review from an initial
    ``relpath``-first reorder that diverged from git's recorded path via
    in-worktree symlinks/junctions (see ``normalize_touch_path``'s
    docstring negative-spec). ``git ls-files`` now runs FIRST,
    unconditionally, exactly as before this change; ``root`` only skips the
    ``core.git_root(cwd)`` re-derivation on the miss path. Gated on
    subprocess call counts, never wall-clock -- this box runs 50-70
    concurrent LLMs and wall-clock is unusable here.

    Monkeypatches ``scope._git_run`` (the sole subprocess seam for
    ``ls-files``) and ``core.git_root`` (the sole subprocess seam for the old
    worktree-root re-derivation) to plain counters, so a call count of 0
    proves no ``git`` process was spawned at all — not merely that its
    result went unused.
    """

    def _spy(self, monkeypatch):
        calls = {"git_run": 0, "git_root": 0}

        real_git_run = scope._git_run

        def _counted_git_run(args, cwd=None):
            calls["git_run"] += 1
            return real_git_run(args, cwd)

        real_git_root = core.git_root

        def _counted_git_root(cwd=None):
            calls["git_root"] += 1
            return real_git_root(cwd)

        monkeypatch.setattr(scope, "_git_run", _counted_git_run)
        monkeypatch.setattr(scope.core, "git_root", _counted_git_root)
        return calls

    def test_tracked_path_with_root_spawns_ls_files_only(self, tmp_path, monkeypatch):
        """AC1 (C2 update, plan chunk C2a): a tracked file inside the
        worktree, with ``root`` supplied, is now proved eligible for the
        zero-spawn fast arm by :func:`scope._touch_path_fast_arm_eligible`
        (C1's five-clause guard) — ZERO spawns, not one. This test used to
        assert the pre-guard shape (``{"git_run": 1, "git_root": 0}``,
        docstring claiming "root never saves the ls-files spawn") before C1
        landed the guard at ``b1e0881d3``; that claim is no longer true for
        an eligible tracked path, and updating this assertion is exactly what
        C2a's brief calls out as expected (one of the three spawn-count tests
        C1 broke)."""
        repo = _make_repo(tmp_path)
        calls = self._spy(monkeypatch)
        target = repo / "README.md"
        result = scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        assert result == "README.md"
        assert calls == {"git_run": 0, "git_root": 0}

    def test_untracked_path_with_root_spawns_ls_files_only(self, tmp_path, monkeypatch):
        """AC2 (C2 update): an untracked file inside the worktree, with
        ``root`` supplied, is ALSO proved eligible for the fast arm — the
        five-clause guard does not distinguish tracked from untracked, only
        that the pure-Python ``relpath`` candidate is provably what
        ``ls-files`` would have produced (agreeing for an untracked file too,
        since ``ls-files`` would miss and the relpath fallback would run
        anyway). Previously asserted 1 spawn (ls-files miss only); now 0."""
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / "new.py"
        target.write_text("y")
        calls = self._spy(monkeypatch)
        result = scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        assert result == "src/new.py"
        assert calls == {"git_run": 0, "git_root": 0}

    def test_untracked_path_without_root_spawns_as_before(self, tmp_path, monkeypatch):
        """Legacy shape (no ``root``) is unchanged: ls-files (miss) +
        core.git_root, exactly as before ``root`` existed as a parameter."""
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / "new.py"
        target.write_text("y")
        calls = self._spy(monkeypatch)
        result = scope.normalize_touch_path(str(target), cwd=str(repo))
        assert result == "src/new.py"
        assert calls == {"git_run": 1, "git_root": 1}

    def test_tracked_path_without_root_spawns_as_before(self, tmp_path, monkeypatch):
        """Legacy shape (no ``root``) for a tracked file: ls-files alone
        resolves it, exactly as before ``root`` existed as a parameter."""
        repo = _make_repo(tmp_path)
        calls = self._spy(monkeypatch)
        target = repo / "README.md"
        result = scope.normalize_touch_path(str(target), cwd=str(repo))
        assert result == "README.md"
        assert calls == {"git_run": 1, "git_root": 0}

    def test_outside_worktree_with_root_falls_back_and_spawns_ls_files_only(
        self, tmp_path, monkeypatch
    ):
        """A path genuinely outside the worktree, with ``root`` supplied:
        ls-files misses (classified benign via the supplied root), the
        relpath fallback runs against ``root`` without re-deriving it, and
        the result is dropped as outside-repo -- one spawn total, zero
        ``core.git_root`` calls."""
        repo = _make_repo(tmp_path)
        outside_dir = tmp_path.parent / "sibling-repo-scratch"
        outside_dir.mkdir(exist_ok=True)
        outside = outside_dir / "xyz.py"
        outside.write_text("z")
        calls = self._spy(monkeypatch)
        result = scope.normalize_touch_path(str(outside), cwd=str(repo), root=str(repo))
        assert result is None
        assert calls["git_run"] == 1
        assert calls["git_root"] == 0

    def test_diagnostic_latch_arms_on_ls_files_failure_with_root_supplied(
        self, tmp_path, monkeypatch
    ):
        """Regression for review finding P2, UPDATED for C1's fast-arm guard
        (C2a, plan chunk C2): a tracked, guard-eligible path with ``root``
        supplied now takes the ZERO-SPAWN fast arm and never reaches
        ``ls-files`` at all, so a broken ``ls-files`` mock is never invoked
        and the latch correctly does NOT fire for this input — this is one
        of the three spawn-count-adjacent tests C1's guard changed the shape
        of (named in C2a's brief). The regression this test protects (a
        non-benign ``ls-files`` failure must still arm the latch) is now
        exercised via a guard-INeligible, still-IN-worktree input (a
        directory-shaped path, which fails ``_clause_not_a_directory`` and
        always falls through to the unchanged ``ls-files``-first body) so the
        ``ls-files`` mock is actually reached AND the failure is not
        classified benign (an out-of-worktree path would be classified
        benign via ``_path_is_outside_worktree`` regardless of the mocked
        ``ls-files`` failure, which would make this regression untestable)."""
        repo = _make_repo(tmp_path)
        target = repo / "src"
        target.mkdir()

        def _broken_ls_files(args, cwd=None):
            return scope.GitResult(returncode=128, stdout="", stderr="fatal: index corrupt", timed_out=False)

        monkeypatch.setattr(scope, "_git_run", _broken_ls_files)
        monkeypatch.setattr(scope, "_normalize_diag_fired", False)
        scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        assert scope.normalize_diagnostic_fired() is True


def _spawn_spy(monkeypatch):
    """Module-level twin of :meth:`TestNormalizeTouchPathSpawnCount._spy`,
    reused (not re-invented) by the C2a guard-clause and differential test
    classes below so every spawn-count assertion in this file shares one
    counting dialect. See that method's own docstring for the zero-spawn
    proof shape this exists to give a test."""
    calls = {"git_run": 0, "git_root": 0}

    real_git_run = scope._git_run

    def _counted_git_run(args, cwd=None):
        calls["git_run"] += 1
        return real_git_run(args, cwd)

    real_git_root = core.git_root

    def _counted_git_root(cwd=None):
        calls["git_root"] += 1
        return real_git_root(cwd)

    monkeypatch.setattr(scope, "_git_run", _counted_git_run)
    monkeypatch.setattr(scope.core, "git_root", _counted_git_root)
    return calls


def _clauses_without(name):
    """:data:`scope._GUARD_CLAUSES` with the named clause removed — the
    slice a C2a clause-pinning test monkeypatches in to prove that clause,
    and only that clause, is what declines a given input (plan AC3)."""
    return tuple(pair for pair in scope._GUARD_CLAUSES if pair[0] != name)


class TestFastArmGuardClausesPinned:
    """AC3 — pin each of C1's five zero-spawn-fast-arm guard clauses with a
    test that FAILS when that clause alone is removed from
    :data:`scope._GUARD_CLAUSES` (plan chunk C2, docs/plans/2026-08-08-
    prove-the-arms-agree-then-stop-asking-gi.md). Each test below:

      1. builds an input that (with the FULL, unmodified guard) the target
         clause alone declines,
      2. asserts :func:`scope._touch_path_fast_arm_eligible` returns
         ``False`` (or, for clause 5's positive sub-case, ``True``) against
         the full guard,
      3. monkeypatches :data:`scope._GUARD_CLAUSES` to the target clause's
         removal (:func:`_clauses_without`), re-runs the SAME predicate
         against the SAME input, and asserts the verdict FLIPS.

    This proves, per-clause, that the clause is load-bearing — an artifact a
    later re-run of this suite can re-verify, not a prose "I watched it go
    red" attestation.
    """

    def test_clause4_candidate_non_empty_pinned(self, tmp_path, monkeypatch):
        """Clause 4 (``candidate_non_empty``) — an out-of-worktree path
        yields an empty ``candidate`` from :func:`scope._relpath_candidate`;
        every OTHER clause passes vacuously or trivially against an empty
        string, so only this clause's removal can flip eligibility. Also
        asserts the call-site level: ``normalize_touch_path`` falls back and
        classifies the failure (does not silently swallow it) — one spawn,
        entry dropped (``None``)."""
        repo = _make_repo(tmp_path)
        outside_dir = tmp_path.parent / "clause4-outside-scratch"
        outside_dir.mkdir(exist_ok=True)
        outside = outside_dir / "xyz.py"
        outside.write_text("z")

        candidate, _exc = scope._relpath_candidate(str(outside), str(repo))
        assert candidate == ""
        assert scope._touch_path_fast_arm_eligible(str(outside), str(repo), candidate) is False

        monkeypatch.setattr(scope, "_GUARD_CLAUSES", _clauses_without("candidate_non_empty"))
        assert scope._touch_path_fast_arm_eligible(str(outside), str(repo), candidate) is True
        monkeypatch.undo()

        calls = _spawn_spy(monkeypatch)
        result = scope.normalize_touch_path(str(outside), cwd=str(repo), root=str(repo))
        assert result is None
        assert calls == {"git_run": 1, "git_root": 0}

    def test_clause2_ascii_safe_pinned(self, tmp_path, monkeypatch):
        """Clause 2 (``ascii_safe``, module numbering ``_GUARD_CLAUSES[1]``;
        plan brief numbers this "clause 3") — a tracked non-ASCII filename
        produces a non-empty candidate containing a character outside
        :data:`scope._SAFE`; every other clause passes for this input. With
        the ``core.quotePath`` defect fixed at ``5a1c79035``, the fast arm's
        answer for this input is now IDENTICAL to the ``ls-files`` arm's, so
        the call-site assertion below can ONLY prove the FALLBACK WAS TAKEN
        (spawn count), the same weakened shape as the directory-shaped
        clause-2(``not_a_directory``) case below — not that the return value
        would otherwise have been wrong."""
        repo = _make_repo(tmp_path)
        target = repo / "café.md"
        target.write_text("y", encoding="utf-8")
        subprocess.run(["git", "add", "café.md"], cwd=str(repo), check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-m", "add non-ascii file"], cwd=str(repo), check=True,
            **no_console_passthrough_kwargs(),
        )

        candidate, _exc = scope._relpath_candidate(str(target), str(repo))
        assert candidate == "café.md"
        assert scope._touch_path_fast_arm_eligible(str(target), str(repo), candidate) is False

        monkeypatch.setattr(scope, "_GUARD_CLAUSES", _clauses_without("ascii_safe"))
        assert scope._touch_path_fast_arm_eligible(str(target), str(repo), candidate) is True
        monkeypatch.undo()

        calls = _spawn_spy(monkeypatch)
        result = scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        assert result == "café.md"
        assert calls["git_run"] >= 1  # fallback was taken -- see docstring above

    @pytest.mark.parametrize("via", ["symlink", "junction"])
    def test_clause1_relpath_agrees_pinned(self, tmp_path, monkeypatch, via):
        """Clause 1 (``relpath_agrees``) — a tracked file reached through a
        symlinked or junctioned directory INSIDE the worktree: ``git
        ls-files`` returns git's recorded (unresolved) path, but
        ``os.path.realpath`` resolves through the link, so
        ``realpath(fpath) != abspath(fpath)``. Every other clause passes for
        this input. The symlink sub-case is skipped, naming the reason,
        where this host lacks the privilege to create one -- the junction
        sub-case does not depend on that privilege and always runs."""
        repo = _make_repo(tmp_path)
        real_dir = repo / "actual"
        real_dir.mkdir()
        (real_dir / "foo.py").write_text("y")
        subprocess.run(["git", "add", "actual/foo.py"], cwd=str(repo), check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-m", "add actual/foo.py"], cwd=str(repo), check=True,
            **no_console_passthrough_kwargs(),
        )
        link_dir = repo / "linked"

        if via == "symlink":
            if not symlink_capability.CAN_CREATE_SYMLINK:
                pytest.skip(
                    "host lacks privilege to create a directory symlink "
                    "(no Developer Mode / SeCreateSymbolicLink) -- junction "
                    "sub-case covers this clause instead"
                )
            os.symlink(str(real_dir), str(link_dir), target_is_directory=True)
        else:
            if sys.platform != "win32":
                pytest.skip("junction creation is Windows-only")
            import _winapi

            _winapi.CreateJunction(str(real_dir), str(link_dir))

        target = link_dir / "foo.py"
        candidate, _exc = scope._relpath_candidate(str(target), str(repo))
        assert candidate != ""
        assert scope._touch_path_fast_arm_eligible(str(target), str(repo), candidate) is False

        monkeypatch.setattr(scope, "_GUARD_CLAUSES", _clauses_without("relpath_agrees"))
        assert scope._touch_path_fast_arm_eligible(str(target), str(repo), candidate) is True

    def test_clause_not_a_directory_pinned(self, tmp_path, monkeypatch):
        """Clause (``not_a_directory``, plan brief's "clause 2") — a
        directory-shaped ``fpath``: ``realpath(dir) == abspath(dir)`` so
        clause 1 passes, and the candidate is a non-empty ASCII repo-relative
        string, so only this clause declines. NOTE the pre-existing defect
        filed at ``2026-08-08-a-directory-shaped-input-to-normalize-to-
        3779939b507e`` -- ``git ls-files -- <dir>`` lists everything under
        it and the fallback keeps only ``lines[0]``, an unrelated sibling
        file. The call-site assertion below is therefore DELIBERATELY
        WEAKENED to "the fallback was taken" (spawn count), NOT "the return
        value is correct" -- do not strengthen this without first fixing
        that filed defect."""
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("y")
        subprocess.run(["git", "add", "src/a.py"], cwd=str(repo), check=True, **no_console_passthrough_kwargs())
        subprocess.run(["git", "commit", "-m", "add src/a.py"], cwd=str(repo), check=True, **no_console_passthrough_kwargs())
        target = repo / "src"

        candidate, _exc = scope._relpath_candidate(str(target), str(repo))
        assert candidate == "src"
        assert scope._touch_path_fast_arm_eligible(str(target), str(repo), candidate) is False

        monkeypatch.setattr(scope, "_GUARD_CLAUSES", _clauses_without("not_a_directory"))
        assert scope._touch_path_fast_arm_eligible(str(target), str(repo), candidate) is True
        monkeypatch.undo()

        calls = _spawn_spy(monkeypatch)
        scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        assert calls["git_run"] >= 1  # fallback was taken -- see defect note above

    def test_clause5_root_is_worktree_root_pinned_declining(self, tmp_path, monkeypatch):
        """Clause 5 (``root_is_worktree_root``) — ``root`` is a real,
        existing SUBDIRECTORY of the worktree (no ``.git`` entry at it), not
        the worktree root itself. Every other clause passes for this input.
        Call-site assertion is weakened to "fallback was taken" (spawn
        count) -- a subdirectory ``root`` is a caller precondition
        violation, not a class this function can correct for."""
        repo = _make_repo(tmp_path)
        sub = repo / "sub"
        sub.mkdir()
        (sub / "file.txt").write_text("y")
        target = sub / "file.txt"

        candidate, _exc = scope._relpath_candidate(str(target), str(sub))
        assert candidate == "file.txt"
        assert scope._touch_path_fast_arm_eligible(str(target), str(sub), candidate) is False

        monkeypatch.setattr(scope, "_GUARD_CLAUSES", _clauses_without("root_is_worktree_root"))
        assert scope._touch_path_fast_arm_eligible(str(target), str(sub), candidate) is True
        monkeypatch.undo()

        calls = _spawn_spy(monkeypatch)
        scope.normalize_touch_path(str(target), cwd=str(repo), root=str(sub))
        assert calls["git_run"] >= 1  # fallback was taken -- root was not the worktree root

    def test_clause5_root_is_worktree_root_pinned_positive(self, tmp_path, monkeypatch):
        """Clause 5 positive case: ``root`` genuinely has a ``.git`` entry
        (is the real worktree root) -- the fast arm IS taken, zero spawns,
        so this clause is exercised in its ACCEPTING direction too, not only
        its declining one."""
        repo = _make_repo(tmp_path)
        target = repo / "README.md"

        candidate, _exc = scope._relpath_candidate(str(target), str(repo))
        assert scope._touch_path_fast_arm_eligible(str(target), str(repo), candidate) is True

        calls = _spawn_spy(monkeypatch)
        result = scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        assert result == "README.md"
        assert calls == {"git_run": 0, "git_root": 0}


class TestFastArmFailOpen:
    """AC5 — the guard's OWN operations failing must not raise out of
    :func:`scope.normalize_touch_path`; it must fall back to today's
    (pre-guard) body. Each clause callable catches ``OSError`` ONLY (see
    each clause's own docstring in scope.py) -- this class monkeypatches the
    exact ``os.path`` call each clause makes to raise ``OSError`` for the
    guarded path only, leaving every other path (fixture setup, the
    fallback body itself) unaffected."""

    def test_isdir_oserror_falls_back_not_raises(self, tmp_path, monkeypatch):
        """``_clause_not_a_directory``'s ``os.path.isdir`` raising."""
        repo = _make_repo(tmp_path)
        target = repo / "README.md"
        real_isdir = scope.os.path.isdir

        def _boom_isdir(path):
            if os.path.abspath(path) == os.path.abspath(str(target)):
                raise OSError("simulated stat failure")
            return real_isdir(path)

        monkeypatch.setattr(scope.os.path, "isdir", _boom_isdir)
        result = scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        assert result == "README.md"

    def test_realpath_oserror_falls_back_not_raises(self, tmp_path, monkeypatch):
        """``_clause_relpath_agrees``'s ``os.path.realpath`` raising."""
        repo = _make_repo(tmp_path)
        target = repo / "README.md"
        real_realpath = scope.os.path.realpath
        call_count = {"n": 0}

        def _boom_realpath(path):
            call_count["n"] += 1
            # First N calls come from _relpath_candidate (must succeed so a
            # non-empty candidate reaches the guard); only the clause's OWN
            # realpath calls -- made AFTER _relpath_candidate has already
            # run at the call site -- need to raise.
            if call_count["n"] > 2:
                raise OSError("simulated realpath failure")
            return real_realpath(path)

        monkeypatch.setattr(scope.os.path, "realpath", _boom_realpath)
        result = scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        assert result == "README.md"

    def test_exists_oserror_falls_back_not_raises(self, tmp_path, monkeypatch):
        """``_clause_root_is_worktree_root``'s ``os.path.exists`` raising."""
        repo = _make_repo(tmp_path)
        target = repo / "README.md"
        real_exists = scope.os.path.exists

        def _boom_exists(path):
            if os.path.abspath(path) == os.path.abspath(str(repo / ".git")):
                raise OSError("simulated exists failure")
            return real_exists(path)

        monkeypatch.setattr(scope.os.path, "exists", _boom_exists)
        result = scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        assert result == "README.md"


_PRE_C1_SCOPE_SHA = "5a1c79035"


def _load_pre_c1_scope_module():
    """Import the pre-C1 (guardless) ``scope.py`` body at
    :data:`_PRE_C1_SCOPE_SHA` into a fresh module namespace, for AC2's
    differential test -- preferred over a hand-captured expected-value table
    per this chunk's brief, since a captured table re-encodes today's
    answers through this test's own reading rather than actually running the
    pre-change code. ``5a1c79035`` already carries BOTH bug fixes
    (``core.quotepath=false`` and the extended-length-prefix strip) that
    predate C1's guard -- see scope.py's own module docstring -- so this is
    genuinely "guard absent", not "bugs present"."""
    repo_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        ["git", "show", f"{_PRE_C1_SCOPE_SHA}:coordinator_core/session/scope.py"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    source = result.stdout
    module_name = "_pre_c1_scope_for_differential_test"
    module = types.ModuleType(module_name)
    module.__dict__["__name__"] = module_name
    # dataclasses' string-annotation resolution looks the defining module up
    # via sys.modules[cls.__module__] -- register before exec so the
    # dataclass decorators inside this source find it there.
    sys.modules[module_name] = module
    try:
        exec(compile(source, f"<git-show:{_PRE_C1_SCOPE_SHA}:scope.py>", "exec"), module.__dict__)
    finally:
        sys.modules.pop(module_name, None)
    return module


@pytest.fixture(scope="module")
def pre_c1_scope():
    return _load_pre_c1_scope_module()


class TestNormalizeTouchPathDifferential:
    """AC2 -- the highest-value test in this chunk: for a table of input
    classes, the guarded (current) ``normalize_touch_path`` must return
    EXACTLY what the pre-guard implementation at ``5a1c79035`` returned.
    Runs the REAL pre-change body (:func:`_load_pre_c1_scope_module`), not a
    hand-captured expected-value table, for every class except the ones
    named explicitly below as unrunnable.

    No class in this table uses a captured value -- every comparison below
    runs both implementations live. The nominally "unrunnable" classes named
    in this chunk's brief (different-drive) are instead exercised for real:
    this dev box's worktree lives on ``X:``, and the machine's scratchpad
    directory lives on ``C:``, so a genuine cross-drive absolute path is
    available without simulation.
    """

    def test_tracked_file(self, tmp_path, pre_c1_scope):
        repo = _make_repo(tmp_path)
        target = repo / "README.md"
        old = pre_c1_scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        new = scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        assert new == old == "README.md"

    def test_untracked_file(self, tmp_path, pre_c1_scope):
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / "new.py"
        target.write_text("y")
        old = pre_c1_scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        new = scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        assert new == old == "src/new.py"

    def test_outside_worktree(self, tmp_path, pre_c1_scope):
        repo = _make_repo(tmp_path)
        outside_dir = tmp_path.parent / "differential-outside-scratch"
        outside_dir.mkdir(exist_ok=True)
        target = outside_dir / "xyz.py"
        target.write_text("z")
        old = pre_c1_scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        new = scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        assert new == old is None

    def test_different_drive(self, tmp_path, pre_c1_scope):
        repo = _make_repo(tmp_path)
        other_drive_dir = Path(os.environ.get("TEMP") or os.environ.get("TMP") or r"C:\Windows\Temp")
        if os.path.splitdrive(str(repo))[0].lower() == os.path.splitdrive(str(other_drive_dir))[0].lower():
            pytest.skip("no second drive letter available on this host to exercise cross-drive")
        # PID-qualified: `other_drive_dir` is the SHARED machine temp, not a
        # per-run sandbox, and this box carries 50-70 concurrent sessions — a
        # fixed name lets a peer's run of this same test unlink the file out
        # from under this one mid-assert.
        target = other_drive_dir / f"differential-cross-drive-probe-{os.getpid()}.py"
        target.write_text("z")
        try:
            old = pre_c1_scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
            new = scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
            assert new == old is None
        finally:
            target.unlink(missing_ok=True)

    def test_directory_shaped(self, tmp_path, pre_c1_scope):
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("y")
        subprocess.run(["git", "add", "src/a.py"], cwd=str(repo), check=True, **no_console_passthrough_kwargs())
        subprocess.run(["git", "commit", "-m", "add src/a.py"], cwd=str(repo), check=True, **no_console_passthrough_kwargs())
        target = repo / "src"
        old = pre_c1_scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        new = scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        assert new == old

    def test_non_ascii_tracked(self, tmp_path, pre_c1_scope):
        repo = _make_repo(tmp_path)
        target = repo / "café.md"
        target.write_text("y", encoding="utf-8")
        subprocess.run(["git", "add", "café.md"], cwd=str(repo), check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-m", "add non-ascii file"], cwd=str(repo), check=True,
            **no_console_passthrough_kwargs(),
        )
        old = pre_c1_scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        new = scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        assert new == old == "café.md"

    def test_extended_length_prefixed(self, tmp_path, pre_c1_scope):
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / "new.py"
        target.write_text("y")
        prefixed = "\\\\?\\" + str(target)
        old = pre_c1_scope.normalize_touch_path(prefixed, cwd=str(repo), root=str(repo))
        new = scope.normalize_touch_path(prefixed, cwd=str(repo), root=str(repo))
        assert new == old == "src/new.py"

    def test_still_absolute_after_attempt_returns_none(self, tmp_path, monkeypatch, pre_c1_scope):
        repo = _make_repo(tmp_path)
        outside = "/totally/outside/differential-xyz.py"

        def _boom(*a, **k):
            raise ValueError("simulated relpath failure")

        monkeypatch.setattr(pre_c1_scope.os.path, "relpath", _boom)
        old = pre_c1_scope.normalize_touch_path(outside, cwd=str(repo), root=str(repo))
        monkeypatch.undo()

        monkeypatch.setattr(scope.os.path, "relpath", _boom)
        new = scope.normalize_touch_path(outside, cwd=str(repo), root=str(repo))
        assert new == old is None

    @pytest.mark.parametrize("via", ["symlink", "junction"])
    def test_symlink_or_junction_traversed(self, tmp_path, monkeypatch, pre_c1_scope, via):
        """The one class where the guard's OWN documented negative-spec
        (see ``normalize_touch_path``'s docstring: "Do not re-attempt an
        UNGUARDED reorder without this guard") says the two arms CAN
        legitimately disagree for an unguarded fast arm -- proving they do
        NOT disagree here, for the actual guarded implementation, is exactly
        what this differential test exists to pin."""
        repo = _make_repo(tmp_path)
        real_dir = repo / "actual"
        real_dir.mkdir()
        (real_dir / "foo.py").write_text("y")
        subprocess.run(["git", "add", "actual/foo.py"], cwd=str(repo), check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-m", "add actual/foo.py"], cwd=str(repo), check=True,
            **no_console_passthrough_kwargs(),
        )
        link_dir = repo / "linked"

        if via == "symlink":
            if not symlink_capability.CAN_CREATE_SYMLINK:
                pytest.skip(
                    "host lacks privilege to create a directory symlink "
                    "(no Developer Mode / SeCreateSymbolicLink) -- junction "
                    "sub-case covers this class instead"
                )
            os.symlink(str(real_dir), str(link_dir), target_is_directory=True)
        else:
            if sys.platform != "win32":
                pytest.skip("junction creation is Windows-only")
            import _winapi

            _winapi.CreateJunction(str(real_dir), str(link_dir))

        target = link_dir / "foo.py"
        old = pre_c1_scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        new = scope.normalize_touch_path(str(target), cwd=str(repo), root=str(repo))
        assert new == old == "actual/foo.py"


class TestRelpathFailureBenignPredicate:
    """Unit coverage for :func:`scope._relpath_failure_is_benign` — the
    Windows cross-drive discrimination that keeps a routine
    path-outside-this-repo relpath failure from arming
    :func:`scope._emit_normalize_diagnostic`'s latch.

    Uses monkeypatched ``realpath``/``splitdrive`` throughout so every case
    is pinned to a chosen platform shape rather than whatever drive the test
    host actually places ``tmp_path`` on."""

    def test_oserror_is_never_benign(self, monkeypatch):
        monkeypatch.setattr(scope.os.path, "realpath", lambda p: p)
        monkeypatch.setattr(scope.os.path, "splitdrive", lambda p: ("X:", p))
        exc = OSError("boom")
        assert scope._relpath_failure_is_benign(exc, "X:/a/b.py", "X:/repo") is False

    def test_valueerror_same_drive_is_not_benign(self, monkeypatch):
        monkeypatch.setattr(scope.os.path, "realpath", lambda p: p)
        monkeypatch.setattr(scope.os.path, "splitdrive", lambda p: ("X:", p))
        exc = ValueError("simulated")
        assert (
            scope._relpath_failure_is_benign(exc, "X:/outside/x.py", "X:/repo")
            is False
        )

    def test_valueerror_cross_drive_is_benign(self, monkeypatch):
        drives = {"C:/outside/x.py": "C:", "X:/repo": "X:"}
        monkeypatch.setattr(scope.os.path, "realpath", lambda p: p)
        monkeypatch.setattr(scope.os.path, "splitdrive", lambda p: (drives[p], p))
        exc = ValueError("simulated")
        assert (
            scope._relpath_failure_is_benign(exc, "C:/outside/x.py", "X:/repo")
            is True
        )

    def test_valueerror_cross_drive_case_insensitive(self, monkeypatch):
        drives = {"c:/outside/x.py": "c:", "X:/repo": "X:"}
        monkeypatch.setattr(scope.os.path, "realpath", lambda p: p)
        monkeypatch.setattr(scope.os.path, "splitdrive", lambda p: (drives[p], p))
        exc = ValueError("simulated")
        assert (
            scope._relpath_failure_is_benign(exc, "c:/outside/x.py", "X:/repo")
            is True
        )

    def test_valueerror_same_drive_case_insensitive_is_not_benign(self, monkeypatch):
        drives = {"x:/outside/x.py": "x:", "X:/repo": "X:"}
        monkeypatch.setattr(scope.os.path, "realpath", lambda p: p)
        monkeypatch.setattr(scope.os.path, "splitdrive", lambda p: (drives[p], p))
        exc = ValueError("simulated")
        assert (
            scope._relpath_failure_is_benign(exc, "x:/outside/x.py", "X:/repo")
            is False
        )

    def test_posix_has_no_drive_letters_so_never_benign(self, monkeypatch):
        # posixpath.splitdrive always returns ("", path) regardless of the
        # input — simulate that shape deterministically so this pins POSIX
        # behavior even when the test itself runs on Windows.
        monkeypatch.setattr(scope.os.path, "realpath", lambda p: p)
        monkeypatch.setattr(scope.os.path, "splitdrive", lambda p: ("", p))
        exc = ValueError("simulated")
        assert (
            scope._relpath_failure_is_benign(exc, "/outside/x.py", "/repo") is False
        )


class TestNormalizeDiagnostic:
    """C3/AC5: normalize_touch_path's one-shot, deduped-per-process stderr
    diagnostic on its fail-open path (git ls-files / relpath failure) —
    without changing touch()'s or normalize_touch_path's fail-open return
    contract."""

    def _reset_latch(self, monkeypatch):
        monkeypatch.setattr(scope, "_normalize_diag_fired", False)

    def test_diagnostic_fires_once_on_induced_ls_files_failure(
        self, tmp_path, monkeypatch, capsys
    ):
        self._reset_latch(monkeypatch)
        repo = _make_repo(tmp_path)
        # `None` from `_git_run` is git-could-not-be-EXECUTED — operational by
        # construction, never reclassifiable as the benign out-of-repo case.
        monkeypatch.setattr(scope, "_git_run", lambda args, cwd=None: None)

        target = repo / "README.md"
        result1 = scope.normalize_touch_path(str(target), cwd=str(repo))
        err1 = capsys.readouterr().err
        assert "normalize_touch_path" in err1
        assert "git ls-files" in err1

        # Second induced failure in the same process: silent.
        result2 = scope.normalize_touch_path(str(target), cwd=str(repo))
        err2 = capsys.readouterr().err
        assert err2 == ""

        # Return value unchanged by the diagnostic: still resolves via the
        # relpath fallback either way.
        assert result1 == result2 == "README.md"

    def test_diagnostic_fires_once_on_induced_relpath_failure(
        self, tmp_path, monkeypatch, capsys
    ):
        self._reset_latch(monkeypatch)
        repo = _make_repo(tmp_path)
        # Succeed with no match (empty stdout), NOT a git-command failure —
        # isolates the relpath failure path from the ls-files failure path
        # (which fires its own, separately-tested diagnostic reason).
        monkeypatch.setattr(
            scope, "_git_run", lambda args, cwd=None: scope.GitResult(0, "", "", False)
        )

        def _boom(*a, **k):
            raise ValueError("simulated relpath failure")

        monkeypatch.setattr(scope.os.path, "relpath", _boom)
        # Same drive as the repo (or no drive at all on POSIX) — a GENUINE
        # relpath failure, not the Windows cross-drive case, so this must
        # still arm the latch. abs-path-ok: synthetic path, never touched.
        outside = os.path.splitdrive(str(repo))[0] + "/totally/outside/xyz.py"

        result1 = scope.normalize_touch_path(outside, cwd=str(repo))
        err1 = capsys.readouterr().err
        assert "normalize_touch_path" in err1
        assert "relpath" in err1
        assert result1 is None  # unchanged fail-open skip signal

        result2 = scope.normalize_touch_path(outside, cwd=str(repo))
        err2 = capsys.readouterr().err
        assert err2 == ""
        assert result2 is None

    def test_diagnostic_silent_on_windows_cross_drive_outside_repo(
        self, tmp_path, monkeypatch, capsys
    ):
        """The relpath arm's own cross-drive discrimination
        (:func:`scope._relpath_failure_is_benign`), simulated deterministically
        via ``splitdrive`` so it pins the same behavior regardless of which
        real drive the test host happens to place ``tmp_path`` on."""
        self._reset_latch(monkeypatch)
        repo = _make_repo(tmp_path)
        monkeypatch.setattr(
            scope, "_git_run", lambda args, cwd=None: scope.GitResult(0, "", "", False)
        )

        def _boom(*a, **k):
            raise ValueError("simulated cross-drive relpath failure")

        monkeypatch.setattr(scope.os.path, "relpath", _boom)

        root_real = os.path.realpath(str(repo))

        def _fake_splitdrive(p):
            return ("R:", p) if p == root_real else ("F:", p)

        monkeypatch.setattr(scope.os.path, "splitdrive", _fake_splitdrive)

        outside = str(tmp_path / "elsewhere" / "note.md")  # abs-path-ok: synthetic, never touched
        result = scope.normalize_touch_path(outside, cwd=str(repo))

        assert scope.normalize_diagnostic_fired() is False, (
            "a Windows cross-drive relpath failure for a path outside this "
            "repo is the routine case, not a degradation"
        )
        assert capsys.readouterr().err == ""
        assert result is None  # still absolute after the skip -> fail-open None


class TestNormalizeDiagnosticBenignVsOperational:
    """The latch discriminates a path that is simply NOT IN THIS REPO (routine,
    handled by the relpath fallback, must stay silent) from an operational
    ``git ls-files`` failure (systemic, must surface).

    Defect being pinned: `git ls-files -- <abs path outside the repo>` exits
    128, `_git_output` collapsed that into `None`, and the latch fired for
    every session that touched a sibling repo, a settings home, or a scratch
    dir — most sessions in this fleet — while claiming corruption that had not
    happened. Once `safe_commit_offer._render_report` began LEADING with the
    latch (eb1e8b5d76c8), that mis-calibration became a false DEGRADED INPUT
    banner at the head of most reports.
    """

    @pytest.fixture(autouse=True)
    def _reset_latch(self, monkeypatch):
        """The latch is a module global and one-shot per process — reset it
        around EVERY test here so neither a real fail-open earlier in the
        process nor a sibling test leaks a verdict into this one."""
        monkeypatch.setattr(scope, "_normalize_diag_fired", False)

    def test_outside_repo_absolute_path_is_silent_and_still_normalizes(
        self, tmp_path, capsys
    ):
        repo = _make_repo(tmp_path)
        sibling = tmp_path / "sibling-repo"
        sibling.mkdir()
        target = sibling / "note.md"
        target.write_text("x")

        result = scope.normalize_touch_path(str(target), cwd=str(repo))

        assert scope.normalize_diagnostic_fired() is False, (
            "a path outside this repo is the routine case, not a degradation"
        )
        assert capsys.readouterr().err == ""
        assert result is not None and not scope._is_absolute(result), (
            "the relpath fallback still resolves it — the silence must come "
            "from classification, not from skipping the work"
        )

    def test_operational_failure_for_an_in_repo_path_fires(
        self, tmp_path, monkeypatch, capsys
    ):
        repo = _make_repo(tmp_path)
        # Shape of a real lock/ref race: non-zero exit, a stderr that is NOT
        # the out-of-repo fatal, on a path that IS inside the worktree.
        monkeypatch.setattr(
            scope,
            "_git_run",
            lambda args, cwd=None: scope.GitResult(
                128, "", "fatal: Unable to create '.git/index.lock': File exists.\n"
            , False),
        )

        result = scope.normalize_touch_path(str(repo / "README.md"), cwd=str(repo))

        assert scope.normalize_diagnostic_fired() is True
        assert "normalize_touch_path" in capsys.readouterr().err
        assert result == "README.md"  # fail-open contract unchanged

    def test_unclassifiable_failure_fires(self, tmp_path, monkeypatch, capsys):
        """Fail TOWARD surfacing: a failure shape this code cannot positively
        attribute to "not in this repo" — here a signal death, empty stderr,
        no recognisable fatal at all — arms the latch. Benignity is a positive
        verdict; the absence of evidence either way is not it."""
        repo = _make_repo(tmp_path)
        monkeypatch.setattr(
            scope, "_git_run", lambda args, cwd=None: scope.GitResult(-9, "", "", False)
        )

        scope.normalize_touch_path(str(repo / "README.md"), cwd=str(repo))

        assert scope.normalize_diagnostic_fired() is True
        assert "normalize_touch_path" in capsys.readouterr().err

    def test_git_unexecutable_is_never_benign(self, tmp_path, monkeypatch, capsys):
        repo = _make_repo(tmp_path)
        monkeypatch.setattr(scope, "_git_run", lambda args, cwd=None: None)
        outside = tmp_path / "elsewhere.md"
        outside.write_text("x")

        scope.normalize_touch_path(str(outside), cwd=str(repo))

        assert scope.normalize_diagnostic_fired() is True, (
            "a missing/unexecutable git is systemic by definition — the "
            "containment check must not launder it into the benign class"
        )
        assert "normalize_touch_path" in capsys.readouterr().err

    def test_unresolvable_root_is_not_benign(self, tmp_path, monkeypatch, capsys):
        repo = _make_repo(tmp_path)
        monkeypatch.setattr(
            scope,
            "_git_run",
            lambda args, cwd=None: scope.GitResult(128, "", "fatal: something odd\n", False),
        )
        monkeypatch.setattr(scope.core, "git_root", lambda cwd=None: None)

        scope.normalize_touch_path(str(repo / "README.md"), cwd=str(repo))

        assert scope.normalize_diagnostic_fired() is True
        assert "normalize_touch_path" in capsys.readouterr().err

    def test_message_does_not_assert_corruption(self, tmp_path, monkeypatch, capsys):
        repo = _make_repo(tmp_path)
        monkeypatch.setattr(scope, "_git_run", lambda args, cwd=None: None)

        scope.normalize_touch_path(str(repo / "README.md"), cwd=str(repo))
        err = capsys.readouterr().err

        assert "may be corrupted" not in err, (
            "the relpath fallback may well have produced the right path — the "
            "message must not claim more than the code knows"
        )
        assert "mis-normalized or missing" in err

    def test_git_output_semantics_unchanged_for_other_callers(self, tmp_path):
        """`_git_output` keeps collapsing every failure shape into None — the
        discrimination lives at the one call site that needs it, not in the
        shared seam every other caller reads."""
        repo = _make_repo(tmp_path)

        assert scope._git_output(["ls-files", "--", "README.md"], str(repo)) == (
            "README.md\n"
        )
        assert scope._git_output(["ls-files", "--", "/etc/hosts"], str(repo)) is None
        assert scope._git_output(["not-a-git-subcommand"], str(repo)) is None


# ---------------------------------------------------------------------------
# compute_scope() — required arg + out-of-repo
# ---------------------------------------------------------------------------


def test_compute_scope_requires_sid():
    with pytest.raises(ValueError):
        scope.compute_scope("")


def test_compute_scope_out_of_repo_returns_empty(tmp_path):
    result = scope.compute_scope("sid", cwd=str(tmp_path))
    assert result == scope.ScopeResult([], [], [])


# ---------------------------------------------------------------------------
# compute_scope() — set math (golden-diff against a real tmp git repo)
# ---------------------------------------------------------------------------


class TestComputeScope:
    def test_touched_files_are_my_scope(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("s1", cwd=str(repo))
        scope.touch("s1", "a.py", cwd=str(repo))
        scope.touch("s1", "b.py", cwd=str(repo))
        result = scope.compute_scope("s1", cwd=str(repo))
        assert set(result.my_scope) == {"a.py", "b.py"}
        assert result.skipped == []

    def test_extra_candidates_union_into_my_scope(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("s1x", cwd=str(repo))
        scope.touch("s1x", "a.py", cwd=str(repo))
        result = scope.compute_scope(
            "s1x", cwd=str(repo), extra_candidates=["fanout.py"]
        )
        assert set(result.my_scope) == {"a.py", "fanout.py"}

    def test_extra_candidates_still_subject_to_other_session_ownership(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("s1y", cwd=str(repo))
        core.init("owner", cwd=str(repo))
        (repo / "shared.py").write_text("z")  # dirty — post-C2 claims require this
        scope.touch("owner", "shared.py", cwd=str(repo))
        result = scope.compute_scope(
            "s1y", cwd=str(repo), extra_candidates=["shared.py"]
        )
        assert "shared.py" not in result.my_scope
        assert ("shared.py", "owner") in result.skipped

    def test_dirty_untracked_file_after_start_is_mtime_only_not_my_scope(self, tmp_path):
        """Updated 2026-07-31 for the post-C1a/C8 disposition: an
        uncontested mtime-only candidate (dirty, mtime >= started_at, but
        claimed by no session's touched.txt) no longer enters my_scope —
        see test_unclaimed_dirty_after_start_becomes_orphan_not_my_scope."""
        repo = _make_repo(tmp_path)
        # started_at in the past so a freshly-created dirty file qualifies.
        core.init("s2", cwd=str(repo))
        sdir = _sdir(repo, "s2")
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")
        (repo / "untracked.py").write_text("z")  # dirty, mtime now >> start
        result = scope.compute_scope("s2", cwd=str(repo))
        assert "untracked.py" not in result.my_scope
        assert "untracked.py" in result.orphans

    def test_dirty_file_modified_before_start_excluded_by_mtime(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "old.py").write_text("z")
        old_mtime = time.time() - 10_000
        os.utime(repo / "old.py", (old_mtime, old_mtime))
        core.init("s3", cwd=str(repo))
        sdir = _sdir(repo, "s3")
        # started_at AFTER the file's mtime -> file must be excluded.
        (sdir / "started_at").write_text(core.now_iso())
        result = scope.compute_scope("s3", cwd=str(repo))
        assert "old.py" not in result.my_scope
        # but it IS dirty and unclaimed -> orphan
        assert "old.py" in result.orphans

    def test_other_session_claim_is_subtracted(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "shared.py").write_text("z")  # dirty — post-C2 claims require this
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("other", "shared.py", cwd=str(repo))
        result = scope.compute_scope("mine", cwd=str(repo))
        assert "shared.py" not in result.my_scope
        assert ("shared.py", "other") in result.skipped

    def test_unclaimed_dirty_after_start_becomes_orphan_not_my_scope(self, tmp_path):
        """INVERTED 2026-07-31 (plan docs/plans/2026-07-31-unclaimed-dirty-
        file-adoption.md, DR-246): this test used to assert the OPPOSITE —
        that a dirty file modified after started_at, claimed by no one, was
        added to the candidate set by the mtime fallback (step 2) and,
        being unowned, landed in my_scope (step 4). That was the unclaimed-
        adoption defect: an mtime-only candidate silently entered the
        committer's allow-list on no stronger evidence than "somebody
        dirtied this file after I started" — indistinguishable from a
        Bash/script/engine-written file nobody's touched.txt claims. Post
        C1a/C8, an uncontested mtime-only candidate is dropped from
        my_scope in Step 4(c) and falls through to Step 5's orphan
        detection instead."""
        repo = _make_repo(tmp_path)
        core.init("s4", cwd=str(repo))
        sdir = _sdir(repo, "s4")
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")
        (repo / "orphan.py").write_text("o")  # dirty, never touched, mtime now
        result = scope.compute_scope("s4", cwd=str(repo))
        assert "orphan.py" not in result.my_scope
        assert "orphan.py" in result.orphans

    def test_dirty_file_owned_by_other_is_not_orphan(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("s5", cwd=str(repo))
        core.init("owner", cwd=str(repo))
        sdir = _sdir(repo, "s5")
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")
        (repo / "claimed.py").write_text("c")  # dirty
        scope.touch("owner", "claimed.py", cwd=str(repo))
        result = scope.compute_scope("s5", cwd=str(repo))
        # The mtime fallback adds the dirty file to MY candidate set, but the
        # cross-session subtraction removes it (owned by "owner") -> skipped,
        # and it is NOT an orphan (owned).
        assert "claimed.py" not in result.my_scope
        assert "claimed.py" not in result.orphans  # owned, so silent
        assert ("claimed.py", "owner") in result.skipped

    def test_first_writer_wins_on_owner_scan(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        # two other sessions both claim the same path; lexically-first owner
        # (aaa < bbb) wins the first-writer-wins scan.
        core.init("aaa", cwd=str(repo))
        core.init("bbb", cwd=str(repo))
        (repo / "dup.py").write_text("z")  # dirty — post-C2 claims require this
        scope.touch("mine", "dup.py", cwd=str(repo))
        scope.touch("aaa", "dup.py", cwd=str(repo))
        scope.touch("bbb", "dup.py", cwd=str(repo))
        result = scope.compute_scope("mine", cwd=str(repo))
        assert ("dup.py", "aaa") in result.skipped

    def test_archive_and_agents_dirs_skipped_in_owner_scan(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        base = Path(core.sessions_dir(cwd=str(repo)))
        # A .archive dir containing a touched.txt claiming my path must be
        # ignored (bash */ glob excludes dot-entries).
        (base / ".archive" / "old-sid").mkdir(parents=True)
        (base / ".archive" / "old-sid" / "touched.txt").write_text("mine.py\n")
        scope.touch("mine", "mine.py", cwd=str(repo))
        result = scope.compute_scope("mine", cwd=str(repo))
        assert "mine.py" in result.my_scope  # NOT subtracted by .archive

    # -----------------------------------------------------------------
    # Fail-closed regression tests (Tier 2 fix): a read failure inside
    # compute_scope must never WIDEN my_scope, only narrow it.
    # -----------------------------------------------------------------

    def test_unreadable_started_at_does_not_widen_scope(self, tmp_path, monkeypatch):
        """If started_at is unreadable, iso_to_epoch("") == 0 must NOT be
        allowed to make every dirty file's mtime pass the
        `>= started_at_epoch` gate (the pre-fix widening bug). The
        mtime-fallback augmentation must be skipped entirely, so an
        unrelated dirty file that would otherwise qualify stays OUT of
        my_scope."""
        repo = _make_repo(tmp_path)
        core.init("s8", cwd=str(repo))
        scope.touch("s8", "a.py", cwd=str(repo))
        (repo / "unrelated_dirty.py").write_text("z")  # dirty, mtime "now"

        started_at_path = _sdir(repo, "s8") / "started_at"
        orig_read_text = Path.read_text

        def _boom(self, *a, **k):
            if self == started_at_path:
                raise OSError("simulated read failure")
            return orig_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", _boom)
        result = scope.compute_scope("s8", cwd=str(repo))

        assert "a.py" in result.my_scope
        assert "unrelated_dirty.py" not in result.my_scope

    def test_unreadable_started_at_scope_never_wider_than_readable(self, tmp_path, monkeypatch):
        """Pin the invariant directly: computing scope with an unreadable
        started_at must never yield a WIDER my_scope than computing it with
        a readable (old) started_at in the same situation.

        AC5 pin (rebuilt 2026-07-31): the sanity fixture is now a
        touched.txt-CLAIMED file, not a bare mtime-only candidate — post
        C1a/C8, dirty.py being merely dirty-after-started_at no longer
        lands it in my_scope at all (see
        test_unclaimed_dirty_after_start_becomes_orphan_not_my_scope), which
        would have made the "sanity: mtime fallback works" assertion below
        false regardless of whether the read-failure narrowing this test
        actually exists to pin is present or not. Claiming dirty.py via
        touch() keeps it a real my_scope member on the readable side. ONLY
        "readable" touches dirty.py (not "unreadable") — a shared claim by
        both would make each see the other as a foreign owner via Step 3's
        cross-session scan, which is a different assertion than the one
        this test pins. dirty.py never enters "unreadable"'s own candidate
        set at all (untouched by it, and the mtime augmentation that would
        otherwise add it is exactly what this test's read-failure disables)
        — the subset comparison below still exercises the narrowing
        invariant this test is named for."""
        repo = _make_repo(tmp_path)
        core.init("readable", cwd=str(repo))
        core.init("unreadable", cwd=str(repo))
        (_sdir(repo, "readable") / "started_at").write_text("2000-01-01T00:00:00Z")
        (_sdir(repo, "unreadable") / "started_at").write_text("2000-01-01T00:00:00Z")
        (repo / "dirty.py").write_text("z")  # dirty, mtime "now" >> 2000
        scope.touch("readable", "dirty.py", cwd=str(repo))

        readable_result = scope.compute_scope("readable", cwd=str(repo))
        assert "dirty.py" in readable_result.my_scope  # sanity: mtime fallback works

        started_at_path = _sdir(repo, "unreadable") / "started_at"
        orig_read_text = Path.read_text

        def _boom(self, *a, **k):
            if self == started_at_path:
                raise OSError("simulated read failure")
            return orig_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", _boom)
        unreadable_result = scope.compute_scope("unreadable", cwd=str(repo))

        assert set(unreadable_result.my_scope) <= set(readable_result.my_scope)
        assert "dirty.py" not in unreadable_result.my_scope

    def test_unreadable_other_session_claims_withholds_uncontested_candidate(
        self, tmp_path, monkeypatch
    ):
        """If another session's touched.txt cannot be read, its claims are
        indeterminate. The pre-fix behaviour treated the read failure as
        "that session claims nothing" (lines = []), which WIDENS my_scope
        by letting an actually-foreign candidate pass through uncontested.
        Fail-closed: an uncontested candidate must be withheld from
        my_scope (moved to skipped) while any sibling claim set is
        unreadable — the caller-visible outcome is refusal, not silent
        inclusion."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        scope.touch("mine", "maybe-foreign.py", cwd=str(repo))

        other_record = _sdir(repo, "other") / "touch-record.jsonl"
        scope.touch("other", "maybe-foreign.py", cwd=str(repo))  # "other" actually owns it
        orig_read_bytes = Path.read_bytes

        def _boom(self, *a, **k):
            if self == other_record:
                raise OSError("simulated read failure")
            return orig_read_bytes(self, *a, **k)

        monkeypatch.setattr(Path, "read_bytes", _boom)
        result = scope.compute_scope("mine", cwd=str(repo))

        assert "maybe-foreign.py" not in result.my_scope
        skipped_paths = {p for p, _owner in result.skipped}
        assert "maybe-foreign.py" in skipped_paths

    def test_unreadable_other_session_claims_never_widens_scope(self, tmp_path, monkeypatch):
        """Pin the invariant directly: computing scope while a sibling's
        touch record is unreadable must never yield a WIDER my_scope than
        computing it with that same sibling's touch record readable (and
        empty, the most permissive possible content)."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        scope.touch("mine", "candidate.py", cwd=str(repo))

        readable_result = scope.compute_scope("mine", cwd=str(repo))
        assert "candidate.py" in readable_result.my_scope  # sanity

        other_record = _sdir(repo, "other") / "touch-record.jsonl"
        other_record.write_text("")  # exists, so the read path is taken
        orig_read_bytes = Path.read_bytes

        def _boom(self, *a, **k):
            if self == other_record:
                raise OSError("simulated read failure")
            return orig_read_bytes(self, *a, **k)

        monkeypatch.setattr(Path, "read_bytes", _boom)
        unreadable_result = scope.compute_scope("mine", cwd=str(repo))

        assert set(unreadable_result.my_scope) <= set(readable_result.my_scope)
        assert "candidate.py" not in unreadable_result.my_scope

    # -----------------------------------------------------------------
    # Unclaimed-dirty-file adoption (docs/plans/2026-07-31-unclaimed-
    # dirty-file-adoption.md, DR-246): a dirty file claimed by no
    # session's touched.txt (own OR foreign) is never silently adopted
    # into my_scope on mtime alone.
    # -----------------------------------------------------------------

    def test_dirty_file_in_no_sessions_touched_txt_is_orphan_not_my_scope(
        self, tmp_path
    ):
        """AC1/AC3: a dirty file present in NO session's touched.txt — the
        shape a Bash script, a raw engine write, or any writer that bypasses
        the touch() hot path leaves behind — must be ABSENT from another
        session's my_scope and PRESENT in its orphans. This is the exact
        adoption defect: session B must not silently claim a file it never
        recorded touching, on mtime evidence alone."""
        repo = _make_repo(tmp_path)
        core.init("b", cwd=str(repo))
        sdir = _sdir(repo, "b")
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")
        # Written by something that is not any session's touch() call —
        # nobody's touched.txt records it.
        (repo / "bash_written.py").write_text("bash-written")

        result = scope.compute_scope("b", cwd=str(repo))

        assert "bash_written.py" not in result.my_scope
        assert "bash_written.py" in result.orphans

    def test_crashed_sessions_own_touched_file_stays_in_its_own_my_scope(
        self, tmp_path
    ):
        """AC2: a file recorded in crashed session A's touched.txt is still
        in A's OWN my_scope after the crash — and is not adopted by any
        other, live session.

        Empirical basis (probe 1, 12 SIGKILL runs): ``locked_rmw`` is
        mkstemp + os.replace under an flock the kernel releases
        automatically on process death (SIGKILL never runs Python
        finally/atexit cleanup) — so touched.txt is either the fully-
        replaced new file or the untouched prior file, NEVER a half-written
        file, across all 12 kill runs. A's touched.txt survives its death
        fully intact; A's own claim to its own recorded files is unaffected
        by the crash."""
        repo = _make_repo(tmp_path)
        core.init("a-crashed", cwd=str(repo))
        core.init("b-live", cwd=str(repo))
        (repo / "crash_owned.py").write_text("owned-by-a")
        scope.touch("a-crashed", "crash_owned.py", cwd=str(repo))

        # A "crashed" -- nothing further happens to its session dir; its
        # touched.txt is exactly as locked_rmw left it (see docstring).
        a_result = scope.compute_scope("a-crashed", cwd=str(repo))
        assert "crash_owned.py" in a_result.my_scope

        b_result = scope.compute_scope("b-live", cwd=str(repo))
        assert "crash_owned.py" not in b_result.my_scope
        assert ("crash_owned.py", "a-crashed") in b_result.skipped

    def test_other_sessions_touched_txt_claim_still_skips_with_owner(
        self, tmp_path
    ):
        """AC4: a path claimed by another session's touched.txt is still
        skipped with that owner attributed — the pre-existing cross-session
        exclusion (Step 3/4) is untouched by the C1a/C8 mtime-only change.
        This test FAILS if that exclusion is weakened (e.g. if a claimed
        path were ever allowed through to my_scope, or reported as an
        orphan instead of skipped with its real owner)."""
        repo = _make_repo(tmp_path)
        core.init("claimant", cwd=str(repo))
        core.init("victim", cwd=str(repo))
        (repo / "owned_elsewhere.py").write_text("z")
        scope.touch("claimant", "owned_elsewhere.py", cwd=str(repo))

        result = scope.compute_scope("victim", cwd=str(repo))

        assert "owned_elsewhere.py" not in result.my_scope
        assert "owned_elsewhere.py" not in result.orphans
        assert ("owned_elsewhere.py", "claimant") in result.skipped

    def test_peer_agent_dot_agents_claim_skips_with_owning_em_session(
        self, tmp_path
    ):
        """AC13/C8: a dirty file claimed ONLY via a peer session's
        ``.agents/<aid>/touched.txt`` (NOT that session's own top-level
        touched.txt) resolves to ``skipped`` with the OWNING EM SESSION
        attributed as owner — not to orphans, and not to my_scope.

        Path dialect: post-C2, ``.agents`` entries are CLEAN
        repo-root-relative paths -- same dialect as a session's own
        ``touched.txt`` -- because ``track_touched_files`` now normalizes
        against the worktree root, not ``<repo>/.git``, and
        ``coordinator_core.ops.session.safe_commit_offer._normalize_agent_touched_entry``
        no longer re-joins a ``coordinator/`` plugin-dir prefix onto it
        (that join existed only to cancel out the writer's old ``../``
        poisoning). The fixture below writes the raw agent entry as
        ``coordinator/agent_owned.py`` -- the SAME repo-relative path the
        on-disk dirty file lives at.
        """
        repo = _make_repo(tmp_path)
        core.init("em-owner", cwd=str(repo))
        core.init("bystander", cwd=str(repo))

        base = Path(core.sessions_dir(cwd=str(repo)))
        agent_dir = base / ".agents" / "agent-xyz"
        agent_dir.mkdir(parents=True)
        (agent_dir / "em-session-id.txt").write_text("em-owner\n", encoding="utf-8")
        # Clean repo-relative entry, post-C2 dialect.
        _agent_claim(agent_dir, "coordinator/agent_owned.py")

        # The dirty file itself lives at that same repo-relative path.
        (repo / "coordinator").mkdir()
        (repo / "coordinator" / "agent_owned.py").write_text("z")

        result = scope.compute_scope("bystander", cwd=str(repo))

        assert "coordinator/agent_owned.py" not in result.my_scope
        assert "coordinator/agent_owned.py" not in result.orphans
        assert ("coordinator/agent_owned.py", "em-owner") in result.skipped

    def test_undecodable_agent_claim_record_withholds_uncontested_candidate(
        self, tmp_path
    ):
        """Finding 3 (C8 fail-closed regression), re-pinned on the record
        dialect: an agent claim record under .agents/<aid>/ that cannot be read
        in full must withhold the otherwise-uncontested candidate from my_scope,
        mirroring the per-session unreadable-claim tests above.

        The degrade is triggered by a line that will not decode rather than by a
        chmod: it is the same typed AC6 signal an unreadable family member
        raises, and unlike a permission denial it behaves identically on
        Windows. What must never happen is the quiet collapse of "I could not
        read this claimant" into "this claimant holds nothing" -- a negative is
        the only answer that authorizes a write.
        """
        repo = _make_repo(tmp_path)
        core.init("em-owner", cwd=str(repo))
        core.init("bystander", cwd=str(repo))

        base = Path(core.sessions_dir(cwd=str(repo)))
        agent_dir = base / ".agents" / "agent-unreadable"
        agent_dir.mkdir(parents=True)
        (agent_dir / "em-session-id.txt").write_text("em-owner\n", encoding="utf-8")
        sink = _agent_claim(agent_dir, "maybe_foreign.py")
        with open(sink, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("{not json at all}\n")

        scope.touch("bystander", "maybe_foreign.py", cwd=str(repo))

        result = scope.compute_scope("bystander", cwd=str(repo))

        assert "maybe_foreign.py" not in result.my_scope
        skipped_paths = {p for p, _owner in result.skipped}
        assert "maybe_foreign.py" in skipped_paths


class TestC0AgentDirJsonlOnlyUnion:
    """C0: `_read_touch_record_as_legacy_lines` is now a REAL union, and
    every agent-dir branch that used to read `touched.txt` directly (the
    `all_agent_dir_entries` pre-scan, the `touched_probe` recency/size race
    gate, the `attr_agent_touched` attribution branch, and Step 3b's own
    `agent_touched` peer-claim read) now routes through it. This fixture --
    an agent dir carrying ONLY `touch-record.jsonl`, no `touched.txt` at all
    -- proves those four branches read the new dialect too, not merely the
    legacy one every other fixture in this file exercises."""

    def _write_backptr_jsonl_only(self, base: Path, agent_id: str, em_sid: str, path: str) -> Path:
        agent_dir = base / ".agents" / agent_id
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "em-session-id.txt").write_text(em_sid + "\n", encoding="utf-8")
        assert not (agent_dir / "touched.txt").exists()
        _write_touch_record(
            agent_dir / scope._TOUCH_RECORD_FILENAME,
            session_id=em_sid,
            entries=[(touch_record.VERB_TOUCH, path)],
        )
        return agent_dir

    def test_step_3b_peer_claim_read_sees_jsonl_only_agent_dir(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("em-owner", cwd=str(repo))
        core.init("bystander", cwd=str(repo))
        base = Path(core.sessions_dir(cwd=str(repo)))

        (repo / "shared.py").write_text("z")
        self._write_backptr_jsonl_only(base, "agent-jsonl", "em-owner", "shared.py")
        scope.touch("bystander", "shared.py", cwd=str(repo))

        result = scope.compute_scope("bystander", cwd=str(repo))

        assert "shared.py" not in result.my_scope
        assert ("shared.py", "em-owner") in result.skipped

    def test_attribution_branch_sees_jsonl_only_agent_dir(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("em-owner", cwd=str(repo))
        core.init("bystander", cwd=str(repo))
        base = Path(core.sessions_dir(cwd=str(repo)))

        self._write_backptr_jsonl_only(base, "agent-jsonl-2", "em-owner", "attributed.py")
        scope.touch("bystander", "attributed.py", cwd=str(repo))

        result = scope.compute_scope("bystander", cwd=str(repo))

        assert "attributed.py" in result.attribution
        assert result.attribution["attributed.py"].owner == "em-owner"
        assert result.attribution["attributed.py"].claim_source == "agent"

    def test_all_agent_dir_entries_pre_scan_reads_jsonl_only_agent_dir_without_error(
        self, tmp_path
    ):
        """The pre-scan (`all_agent_dir_entries`) unions every agent_dir's
        touch record via the same seam before the main Step 3b loop below
        it runs -- exercise a jsonl-only agent dir through it directly and
        confirm it neither errors nor loses a file-shaped claim from the
        pre-scan's own dedup set (a directory-shaped legacy entry is a
        separate, `touched.txt`-only concern -- `touch_record.encode_line`
        canonicalizes via `posixpath.normpath`, which collapses a trailing
        '/'  the same way the legacy dialect's directory marker never
        survives a jsonl round-trip; out of this test's scope). The
        end-to-end withhold this branch feeds is already pinned by
        `test_step_3b_peer_claim_read_sees_jsonl_only_agent_dir` above."""
        repo = _make_repo(tmp_path)
        core.init("em-owner", cwd=str(repo))
        core.init("bystander", cwd=str(repo))
        base = Path(core.sessions_dir(cwd=str(repo)))
        (repo / "solo.py").write_text("z")

        agent_dir = self._write_backptr_jsonl_only(
            base, "agent-jsonl-3", "em-owner", "solo.py"
        )
        scope.touch("bystander", "solo.py", cwd=str(repo))

        # Direct call, mirroring what the pre-scan itself does per agent_dir.
        pre_lines, pre_degraded = scope._read_agent_touch_record_as_legacy_lines(
            agent_dir / scope._TOUCH_RECORD_FILENAME
        )
        assert pre_lines == ["solo.py"]
        assert pre_degraded is False

        # And the whole call still completes without error end to end.
        result = scope.compute_scope("bystander", cwd=str(repo))
        assert "solo.py" not in result.my_scope

    def test_unreadable_em_session_id_txt_fails_closed_not_soft_skip(
        self, tmp_path, monkeypatch
    ):
        """Finding 1/3: an unreadable em-session-id.txt back-pointer must
        NOT silently degrade to "this agent claims nothing" (the widening
        direction) -- it must be treated with the same fail-closed
        discipline as an unreadable touched.txt, withholding the otherwise-
        uncontested candidate from my_scope."""
        repo = _make_repo(tmp_path)
        core.init("bystander", cwd=str(repo))

        base = Path(core.sessions_dir(cwd=str(repo)))
        agent_dir = base / ".agents" / "agent-backptr-unreadable"
        agent_dir.mkdir(parents=True)
        backptr = agent_dir / "em-session-id.txt"
        backptr.write_text("em-owner\n", encoding="utf-8")
        _agent_claim(agent_dir, "maybe_foreign.py")

        scope.touch("bystander", "maybe_foreign.py", cwd=str(repo))

        orig_read_text = Path.read_text

        def _boom(self, *a, **k):
            if self == backptr:
                raise OSError("simulated read failure")
            return orig_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", _boom)
        result = scope.compute_scope("bystander", cwd=str(repo))

        assert "maybe_foreign.py" not in result.my_scope
        skipped_paths = {p for p, _owner in result.skipped}
        assert "maybe_foreign.py" in skipped_paths

    def test_peer_agent_dot_agents_directory_entry_expands_to_owning_em_session(
        self, tmp_path
    ):
        """Was Finding 4's pin that a directory entry (trailing "/") in a peer
        agent's claim record expands via _dirty_files_under. THE RECORD DIALECT
        CANNOT EXPRESS ONE: ``touch_record.encode_line`` canonicalizes the
        trailing separator away, so "coordinator/agent_dir_owned/" is recorded
        as the FILE path "coordinator/agent_dir_owned" and expands to nothing.

        That is not a regression this test should fail on. A directory claim
        could only ever come from the bare-path legacy dialect, whose corpus is
        drained and whose writers are gone; ``track_touched_files`` records one
        edited FILE per fire and has never emitted a directory. What is pinned
        now is the shape that remains: the dirty file under that directory is
        NOT silently adopted into a bystander's scope. It lands in orphans --
        unclaimed, which is what it now genuinely is -- rather than being
        attributed to the agent's owner.

        If a directory-shaped claim is ever needed again, it needs a real
        representation in the record dialect first; do not restore it by
        reviving a second reader for a dialect nothing writes.

        Review: code-reviewer P1 — the rewrite pinned `orphans`/`skipped`
        membership but never pinned `result.indeterminate`, which is the one
        field that decides whether this orphan is safe to fold into an
        adoption allow-list (`orphans - skipped`, see `ScopeResult.orphans`'s
        own docstring in scope.py, the shape staff-eng review Finding F1
        found live). `indeterminate` MUST stay `False` here: it means "the
        scope computation could not see something", and this scenario saw
        everything — the agent's record is fully readable, it claims the
        literal path `coordinator/agent_dir_owned` (the trailing separator
        canonicalized away at write time), and `inner.py` beneath it is
        genuinely, accurately unclaimed by anyone. Firing `indeterminate`
        on an accurate read would report a degrade that never happened, and
        `indeterminate` is a blunt signal — it withholds the WHOLE
        computation, not one path — so it would cost every other path in
        the session for nothing. The safety margin for a future
        `compute_scope` caller folding `orphans - skipped` into an adoption
        allow-list is exactly this flag: check `indeterminate` first."""
        repo = _make_repo(tmp_path)
        core.init("em-owner", cwd=str(repo))
        core.init("bystander", cwd=str(repo))

        base = Path(core.sessions_dir(cwd=str(repo)))
        agent_dir = base / ".agents" / "agent-dir-owner"
        agent_dir.mkdir(parents=True)
        (agent_dir / "em-session-id.txt").write_text("em-owner\n", encoding="utf-8")
        # Clean repo-relative directory entry (trailing slash), post-C2 dialect.
        _agent_claim(agent_dir, "coordinator/agent_dir_owned/")

        (repo / "coordinator" / "agent_dir_owned").mkdir(parents=True)
        (repo / "coordinator" / "agent_dir_owned" / "inner.py").write_text("z")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, **no_console_passthrough_kwargs())

        result = scope.compute_scope("bystander", cwd=str(repo))

        assert "coordinator/agent_dir_owned/inner.py" not in result.my_scope
        assert "coordinator/agent_dir_owned/inner.py" in result.orphans
        skipped_paths = {path for path, _owner in result.skipped}
        assert "coordinator/agent_dir_owned/inner.py" not in skipped_paths
        # This orphan is accurate, not unexamined -- see the docstring above.
        assert result.indeterminate is False

    def test_missing_backptr_with_recent_agent_activity_fails_closed_not_swept(
        self, tmp_path
    ):
        """36ed64f58 mis-attribution incident regression: a dispatched
        sub-agent writes its OWN ``.agents/<aid>/touched.txt`` entry
        synchronously on every edit (``track_touched_files``), but the
        ``em-session-id.txt`` back-pointer is only written once the ENTIRE
        subagent turn returns to the dispatching session
        (``track_dispatched_agents``, a PostToolUse hook on the Agent/Task
        tool call — for a FOREGROUND dispatch, the common case, that means
        the whole agent lifetime). A peer session ("sweeper") computing its
        own scope DURING that window must never treat the in-flight,
        genuinely-claimed path as uncontested-mine just because the
        back-pointer has not landed yet — that silent fall-through is
        exactly the mechanism that landed a stranger's file into an
        unrelated commit.

        Also pins OVERLAP-SCOPING: an unrelated candidate the sweeper itself
        legitimately touched, with no overlap into the race agent's claims,
        must be UNAFFECTED — the withhold is bounded to the contested path,
        not global.

        No session dir for the eventual owning EM session even exists here
        (liveness is never consulted — the owner is never resolved at all),
        pinning that the fix does not depend on being able to evaluate the
        owning session's liveness."""
        repo = _make_repo(tmp_path)
        core.init("sweeper", cwd=str(repo))
        scope.touch("sweeper", "sweeper_own_unrelated.py", cwd=str(repo))

        base = Path(core.sessions_dir(cwd=str(repo)))
        agent_dir = base / ".agents" / "agent-inflight"
        agent_dir.mkdir(parents=True)
        # Real, synchronously-written activity — no em-session-id.txt yet.
        # Freshly written -> mtime is "now", well inside the recency window.
        _agent_claim(agent_dir, "test_handoff_author_fork.py")

        (repo / "test_handoff_author_fork.py").write_text("z")
        (repo / "sweeper_own_unrelated.py").write_text("z")

        result = scope.compute_scope("sweeper", cwd=str(repo))

        assert "test_handoff_author_fork.py" not in result.my_scope
        skipped_paths = {p for p, _owner in result.skipped}
        assert "test_handoff_author_fork.py" in skipped_paths
        # Overlap-scoping: the sweeper's own unrelated candidate is untouched.
        assert "sweeper_own_unrelated.py" in result.my_scope

    def test_race_window_non_candidate_path_lands_in_orphans_not_skipped(
        self, tmp_path
    ):
        """Staff-eng R1/R2 (2026-08-03, pass 2) -- reverts the earlier Step 5
        `agent_race_paths` surgery (this now supersedes it via
        `ScopeResult.indeterminate`, set below). A race-window path that
        this call never adopted as a CANDIDATE at all (``started_at`` pushed
        into the future, so Step 2's mtime fallback never adds it to
        ``touched_set``) must NOT appear in `result.skipped` -- putting it
        there widened `skipped`'s documented meaning ("candidates of mine
        that were withheld") to include a path that was never a candidate,
        and DoE's coordinator-safe-commit renders `skipped` as "skipping
        <path> — owned by session <owner>", which would show an operator a
        skipping line for a file they never touched. Instead it surfaces in
        `result.orphans` (Step 5's plain my_scope/other_owner check, with no
        `agent_race_paths` special-case), and `result.indeterminate` is
        `True` -- the call-level signal a caller must read before treating
        `orphans` as an adoption allow-list (see
        `coordinator_core.ops.session.safe_commit_offer.compute_offer`)."""
        repo = _make_repo(tmp_path)
        core.init("sweeper", cwd=str(repo))
        sdir = Path(core.session_dir("sweeper", cwd=str(repo)))
        future = datetime.now(timezone.utc).timestamp() + 3600
        (sdir / "started_at").write_text(
            datetime.fromtimestamp(future, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            encoding="utf-8",
        )

        base = Path(core.sessions_dir(cwd=str(repo)))
        agent_dir = base / ".agents" / "agent-inflight"
        agent_dir.mkdir(parents=True)
        _agent_claim(agent_dir, "race_window_file.py")
        (repo / "race_window_file.py").write_text("z")

        result = scope.compute_scope("sweeper", cwd=str(repo))

        assert "race_window_file.py" not in result.my_scope
        skipped_paths = {p for p, _owner in result.skipped}
        assert "race_window_file.py" not in skipped_paths
        assert "race_window_file.py" in result.orphans
        assert result.indeterminate is True

    def test_stale_agent_dir_old_mtime_no_backptr_does_not_withhold(
        self, tmp_path
    ):
        """RECENCY guard regression (found via a real sweep of this repo's
        own ``.git/coordinator-sessions/.agents/`` corpus, 2026-08: 261 of
        2011 agent dirs are permanently in this exact shape — non-empty
        ``touched.txt``, no ``em-session-id.txt``, all long-dead residue,
        not live races). A ``touched.txt`` last modified well outside
        ``liveness._THIRTY_MIN`` must NOT withhold anything -- treating
        every such dir as a live race would make EVERY future scope
        computation return an empty allow-list, permanently, converting the
        occasional mis-attribution into a silent total commit outage."""
        repo = _make_repo(tmp_path)
        core.init("sweeper3", cwd=str(repo))

        base = Path(core.sessions_dir(cwd=str(repo)))
        agent_dir = base / ".agents" / "agent-long-dead"
        agent_dir.mkdir(parents=True)
        stale_touched = agent_dir / "touched.txt"
        stale_touched.write_text("dead_agent_file.py\n", encoding="utf-8")
        # Push the mtime well past the 30-minute recency window.
        old = time.time() - (2 * 3600)
        os.utime(stale_touched, (old, old))

        (repo / "dead_agent_file.py").write_text("z")

        result = scope.compute_scope("sweeper3", cwd=str(repo))

        assert result.skipped == []
        # Uncontested + dirty + no session claims it at all -> orphan, same
        # disposition as any other unclaimed dirty file (Step 5) -- NOT
        # withheld, and NOT silently owned either.
        assert "dead_agent_file.py" in result.orphans
        assert "dead_agent_file.py" not in result.my_scope

    def test_own_background_agent_backptr_already_present_unaffected(
        self, tmp_path
    ):
        """Corrected understanding (2026-08): a BACKGROUND dispatch's
        Agent/Task tool call returns immediately, so its PostToolUse
        back-pointer write fires almost at once -- the earlier draft of this
        fix had this inverted (claimed background was the exposed case; it
        is foreground). Pin the now-common case explicitly: a solo
        session's own background agent, back-pointer already resolved to
        itself, must be completely unaffected by the missing-backptr
        machinery -- no entry in ``skipped``, file lands in the session's
        own ``my_scope`` via the real ``extra_candidates`` union path."""
        repo = _make_repo(tmp_path)
        core.init("solo-bg", cwd=str(repo))

        base = Path(core.sessions_dir(cwd=str(repo)))
        agent_dir = base / ".agents" / "agent-background-done"
        agent_dir.mkdir(parents=True)
        (agent_dir / "em-session-id.txt").write_text("solo-bg\n", encoding="utf-8")
        _agent_claim(agent_dir, "background_agent_authored.py")

        (repo / "background_agent_authored.py").write_text("z")

        result = scope.compute_scope(
            "solo-bg",
            cwd=str(repo),
            extra_candidates=["background_agent_authored.py"],
        )

        assert "background_agent_authored.py" in result.my_scope
        assert result.skipped == []

    def test_empty_new_agent_dir_missing_backptr_is_silently_skipped(
        self, tmp_path
    ):
        """Companion to the regression above: a genuinely empty/new agent
        dir (dispatched but with no recorded activity yet, and no
        back-pointer yet) has nothing to protect — it must NOT trip the
        fail-closed withholding, or every ordinary dispatch-in-progress
        would poison an unrelated session's scope computation for no
        reason."""
        repo = _make_repo(tmp_path)
        core.init("sweeper2", cwd=str(repo))
        scope.touch("sweeper2", "mine.py", cwd=str(repo))

        base = Path(core.sessions_dir(cwd=str(repo)))
        agent_dir = base / ".agents" / "agent-brand-new"
        agent_dir.mkdir(parents=True)
        # No touched.txt at all yet — nothing has happened in this agent dir.

        result = scope.compute_scope("sweeper2", cwd=str(repo))

        assert "mine.py" in result.my_scope
        assert result.skipped == []

    def test_own_subagent_backptr_resolved_still_lands_in_my_own_scope(
        self, tmp_path
    ):
        """Negative pin: a solo session's OWN dispatched sub-agent, once its
        back-pointer HAS resolved to that same session, must not be treated
        as a foreign claim -- the fan-out path (``extra_candidates``, the
        real ``safe_commit_offer`` union mechanism) still lands the
        sub-agent-authored file in the dispatching session's own
        ``my_scope``. Confirms the missing-backptr fail-closed branch above
        is scoped to the ABSENT-backptr window only and does not regress the
        already-resolved self-fan-out path."""
        repo = _make_repo(tmp_path)
        core.init("solo", cwd=str(repo))

        base = Path(core.sessions_dir(cwd=str(repo)))
        agent_dir = base / ".agents" / "agent-mine"
        agent_dir.mkdir(parents=True)
        (agent_dir / "em-session-id.txt").write_text("solo\n", encoding="utf-8")
        _agent_claim(agent_dir, "subagent_authored.py")

        (repo / "subagent_authored.py").write_text("z")

        result = scope.compute_scope(
            "solo", cwd=str(repo), extra_candidates=["subagent_authored.py"]
        )

        assert "subagent_authored.py" in result.my_scope
        assert result.skipped == []


# ---------------------------------------------------------------------------
# compute_scope() — liveness-gated exclusion (release path, dead-holder
# claims no longer contest forever) + clean-path pruning of stale claims.
# ---------------------------------------------------------------------------


class TestComputeScopeLiveness:
    def test_dead_peer_claim_on_dirty_path_lands_in_my_scope(self, tmp_path, monkeypatch):
        """The memo's exact reported case: a dead peer's touched.txt claim
        on a path THIS session also touched must not contest -- the path
        lands in my_scope, not skipped."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("dead-peer", cwd=str(repo))
        (repo / "shared.py").write_text("z")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("dead-peer", "shared.py", cwd=str(repo))

        monkeypatch.setattr(
            scope.liveness, "live_session_ids", lambda cwd=None: frozenset({"mine"})
        )
        result = scope.compute_scope("mine", cwd=str(repo))

        assert "shared.py" in result.my_scope
        skipped_paths = {p for p, _owner in result.skipped}
        assert "shared.py" not in skipped_paths

    def test_live_peer_claim_still_skips_no_regression(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("live-peer", cwd=str(repo))
        (repo / "shared.py").write_text("z")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("live-peer", "shared.py", cwd=str(repo))

        monkeypatch.setattr(
            scope.liveness,
            "live_session_ids",
            lambda cwd=None: frozenset({"mine", "live-peer"}),
        )
        result = scope.compute_scope("mine", cwd=str(repo))

        assert "shared.py" not in result.my_scope
        assert ("shared.py", "live-peer") in result.skipped

    def test_dead_peer_subagent_claim_not_contested_live_still_contested(
        self, tmp_path, monkeypatch
    ):
        repo = _make_repo(tmp_path)
        core.init("bystander", cwd=str(repo))

        base = Path(core.sessions_dir(cwd=str(repo)))
        agent_dir = base / ".agents" / "agent-xyz"
        agent_dir.mkdir(parents=True)
        (agent_dir / "em-session-id.txt").write_text("em-owner\n", encoding="utf-8")
        # Clean repo-relative entry, post-C2 dialect (matches the on-disk
        # dirty file's own path -- see test_peer_agent_dot_agents_claim_...).
        _agent_claim(agent_dir, "coordinator/agent_owned.py")
        (repo / "coordinator").mkdir()
        (repo / "coordinator" / "agent_owned.py").write_text("z")

        # em-owner is DEAD -> not contested.
        monkeypatch.setattr(
            scope.liveness,
            "live_session_ids",
            lambda cwd=None: frozenset({"bystander"}),
        )
        dead_result = scope.compute_scope("bystander", cwd=str(repo))
        # Review: staff-eng F3 — pin the SPECIFIC disposition rather than
        # the my_scope-or-orphans disjunction that used to stand in here.
        # "bystander" never touched this path (it is not in bystander's own
        # touched.txt), so once the dead em-owner's claim stops contesting
        # it, it is an uncontested mtime-only candidate (Step 2/4(c)) — it
        # is dirty, and it is not this session's demonstrable claim, so it
        # is dropped from my_scope and surfaces as an orphan (Step 5), NOT
        # silently adopted into my_scope. See F6's docstring note on this
        # same disposition shift for the untouched-dead-peer-claim case.
        assert "coordinator/agent_owned.py" in dead_result.orphans
        assert "coordinator/agent_owned.py" not in dead_result.my_scope
        skipped_paths = {p for p, _owner in dead_result.skipped}
        assert "coordinator/agent_owned.py" not in skipped_paths

        # em-owner is LIVE -> still contested.
        monkeypatch.setattr(
            scope.liveness,
            "live_session_ids",
            lambda cwd=None: frozenset({"bystander", "em-owner"}),
        )
        live_result = scope.compute_scope("bystander", cwd=str(repo))
        assert "coordinator/agent_owned.py" not in live_result.my_scope
        assert (
            "coordinator/agent_owned.py",
            "em-owner",
        ) in live_result.skipped

    def test_indeterminate_empty_live_set_falls_back_to_unconditional(
        self, tmp_path, monkeypatch
    ):
        """live_session_ids() returning an empty frozenset while a peer
        session dir genuinely exists on disk is indeterminate (same shape
        as "everyone is dead") -- gating disables for this call and the
        pre-existing unconditional exclusion applies (still skipped)."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))
        (repo / "shared.py").write_text("z")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("peer", "shared.py", cwd=str(repo))

        monkeypatch.setattr(
            scope.liveness, "live_session_ids", lambda cwd=None: frozenset()
        )
        result = scope.compute_scope("mine", cwd=str(repo))

        assert "shared.py" not in result.my_scope
        assert ("shared.py", "peer") in result.skipped

    def test_live_session_ids_raising_disables_gating(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))
        (repo / "shared.py").write_text("z")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("peer", "shared.py", cwd=str(repo))

        def _boom(cwd=None):
            raise RuntimeError("simulated liveness enumeration failure")

        monkeypatch.setattr(scope.liveness, "live_session_ids", _boom)
        result = scope.compute_scope("mine", cwd=str(repo))

        assert "shared.py" not in result.my_scope
        assert ("shared.py", "peer") in result.skipped

    def test_clean_path_claim_is_pruned_dirty_path_claim_survives(
        self, tmp_path, monkeypatch
    ):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))

        # clean.py: committed, no uncommitted content -> claim is stale.
        (repo / "clean.py").write_text("z")
        subprocess.run(["git", "add", "clean.py"], cwd=repo, check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-q", "-m", "add clean.py"], cwd=repo, check=True,
            **no_console_passthrough_kwargs(),
        )
        scope.touch("mine", "clean.py", cwd=str(repo))
        scope.touch("peer", "clean.py", cwd=str(repo))

        # dirty.py: peer claims it and it IS dirty -> claim survives.
        (repo / "dirty.py").write_text("z")
        scope.touch("peer", "dirty.py", cwd=str(repo))

        monkeypatch.setattr(
            scope.liveness,
            "live_session_ids",
            lambda cwd=None: frozenset({"mine", "peer"}),
        )
        result = scope.compute_scope("mine", cwd=str(repo))

        assert "clean.py" in result.my_scope
        skipped_paths = {p for p, _owner in result.skipped}
        assert "clean.py" not in skipped_paths
        assert "dirty.py" not in result.my_scope
        assert ("dirty.py", "peer") in result.skipped

    def test_unreadable_peer_touched_txt_still_fails_closed_for_a_live_peer(
        self, tmp_path, monkeypatch
    ):
        """C4/AC16(a) narrows this test's original premise ("unreadable is
        never assumed dead, regardless of liveness"): a peer AC16(a)'s
        pre-read gate already proves DEAD is now skipped before any read
        is even attempted, so there is no "unreadable" outcome left to
        simulate for that peer any more -- see `compute_scope`'s Step 3
        comment on the reorder. What survives is the LIVE-peer half: for a
        peer NOT confirmed dead, an unreadable record still fails closed,
        unconditionally."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("live-peer", cwd=str(repo))
        (repo / "maybe-foreign.py").write_text("z")
        scope.touch("mine", "maybe-foreign.py", cwd=str(repo))

        other_record = _sdir(repo, "live-peer") / "touch-record.jsonl"
        scope.touch("live-peer", "maybe-foreign.py", cwd=str(repo))
        orig_read_bytes = Path.read_bytes

        def _read_boom(self, *a, **k):
            if self == other_record:
                raise OSError("simulated read failure")
            return orig_read_bytes(self, *a, **k)

        monkeypatch.setattr(Path, "read_bytes", _read_boom)
        monkeypatch.setattr(
            scope.liveness,
            "live_session_ids",
            lambda cwd=None: frozenset({"mine", "live-peer"}),
        )
        result = scope.compute_scope("mine", cwd=str(repo))

        assert "maybe-foreign.py" not in result.my_scope
        skipped_paths = {p for p, _owner in result.skipped}
        assert "maybe-foreign.py" in skipped_paths

    def test_git_dirty_scan_failure_disables_prune_and_liveness_live_peer_still_skips(
        self, tmp_path, monkeypatch
    ):
        """Review: staff-eng F0 regression test. Both `_git_output` calls
        that populate `dirty_files_set` return None on failure and are
        swallowed by `or ""` -- an empty dirty set previously read as
        "every peer claim is stale", pruning EVERY peer claim including a
        LIVE peer's, so it fell through Step 4(d) into my_scope. That is a
        WIDENING regression against this function's own fail-closed
        invariant. `dirty_scan_ok` must gate both the clean-path prune AND
        the liveness gate -- a live peer's claim on a shared path must
        still skip even when the git dirty scan itself fails."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("live-peer", cwd=str(repo))
        (repo / "shared.py").write_text("z")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("live-peer", "shared.py", cwd=str(repo))

        monkeypatch.setattr(
            scope.liveness,
            "live_session_ids",
            lambda cwd=None: frozenset({"mine", "live-peer"}),
        )
        monkeypatch.setattr(scope, "_git_output", lambda args, cwd=None: None)

        result = scope.compute_scope("mine", cwd=str(repo))

        assert "shared.py" not in result.my_scope
        assert ("shared.py", "live-peer") in result.skipped

    def test_self_liveness_canary_disables_gating_when_own_sid_missing(
        self, tmp_path, monkeypatch
    ):
        """Review: staff-eng F1 regression test. `live_ids` non-empty but
        missing THIS session's own sid, while this session's own dir
        exists on disk, is treated as an unreliable enumeration (not "I am
        dead") -- gating disables and the pre-existing unconditional
        exclusion applies, so a peer's claim still skips."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))
        (repo / "shared.py").write_text("z")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("peer", "shared.py", cwd=str(repo))

        # live_ids is non-empty (not the total-failure shape the empty-set
        # guard catches) but omits "mine" itself -- the partial-under-report
        # shape only the canary catches.
        monkeypatch.setattr(
            scope.liveness, "live_session_ids", lambda cwd=None: frozenset({"peer"})
        )
        result = scope.compute_scope("mine", cwd=str(repo))

        assert "shared.py" not in result.my_scope
        assert ("shared.py", "peer") in result.skipped

    def test_dead_peer_untouched_dirty_file_becomes_orphan_not_silently_owned(
        self, tmp_path, monkeypatch
    ):
        """Review: staff-eng F6 regression test, pinning the orphan-
        disposition change referenced by compute_scope's own docstring: a
        dirty path claimed ONLY by a now-dead peer, and never touched by
        THIS session, used to read as "owned" (excluded from orphans)
        unconditionally. Post-liveness-gate, the dead peer's claim is
        skipped wholesale, so the path is neither claimed nor mine, and
        must surface as an orphan instead of silently vanishing."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("dead-peer", cwd=str(repo))
        (repo / "untouched_by_mine.py").write_text("z")
        scope.touch("dead-peer", "untouched_by_mine.py", cwd=str(repo))

        monkeypatch.setattr(
            scope.liveness, "live_session_ids", lambda cwd=None: frozenset({"mine"})
        )
        result = scope.compute_scope("mine", cwd=str(repo))

        assert "untouched_by_mine.py" in result.orphans
        assert "untouched_by_mine.py" not in result.my_scope
        skipped_paths = {p for p, _owner in result.skipped}
        assert "untouched_by_mine.py" not in skipped_paths

    @pytest.mark.pending_fix
    def test_real_live_session_ids_no_monkeypatch_dead_peer_claim_releases(
        self, tmp_path
    ):
        """Review: staff-eng F3 — one no-monkeypatch integration test
        routing through the REAL `liveness.live_session_ids`, per
        `state/lessons/2026-07-12-mock-the-bridge-tests-can-t-catch-vacuou-
        5146e5a025e5.yaml`: every other test in this class stubs
        `live_session_ids`, which cannot catch a total delegation no-op.
        Forces one session dead via an old `last_activity` and no
        `stable_pid` in its meta.json (Layer-2 recency, unmocked).

        RED AT HEAD, and correctly so (re-cut 2026-08-11,
        docs/plans/2026-08-11-kill-on-staleness-and-a-way-past-the-gate.md
        § B2): this test's own `core.init` gives "dead-peer" a real, live
        PID witness (the test process itself), and Layer 1 (PPID-
        authoritative) outranks the doctored Layer-2 recency this test
        relies on to force death — so `live_session_ids` still reports
        "dead-peer" live and its claim is never released here. This is a
        real defect, NOT this plan's to fix: `liveness.py` is anti-scope
        for this plan (PM ruling), and the defect is owned by
        `state/bug-backlog/2026-08-11-session-live-layer-1-ppid-witness-perman-6c1272db353f.yaml`
        (P1). Do NOT edit this test to pass — its redness is the correct,
        documented state.

        Review: coordinator:code-reviewer P3 — this docstring previously
        named `TestReleaseCommittedClaims::
        test_b1_stale_peer_claim_on_a_clean_path_releases` as pinning B1's
        release-path deliverable separately; that test no longer exists
        (removed by chunk C5, which deleted `release_committed_claims`'s
        peer-release machinery and its tests entirely). This test's own
        assertion does NOT depend on `release_committed_claims` — it pins
        `compute_scope`'s own Step 3/3b liveness gate (a distinct, C5-
        untouched mechanism; see that function's docstring), which is what
        the still-open Layer-1 PPID witness bug above actually blocks. No
        replacement cross-reference is owed: nothing in this file pins the
        now-deleted peer-release path, since C5 removed that capability
        outright rather than leaving it under test.

        MARKED `pending_fix` 2026-08-19. Its redness was correct and
        documented, but it carried NO marker, so it failed every raw run of
        this suite and each EM rediscovered it from scratch — the marker
        contract in `pyproject.toml` exists precisely so a known-broken path
        assumption is excluded from the tiers until fixed, instead of
        spending someone's attention every time. This does NOT weaken the
        do-not-edit-to-pass rule above: the marker records that the redness
        is expected and owned, it does not retire the P1 that owns it."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("dead-peer", cwd=str(repo))
        (repo / "shared.py").write_text("z")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("dead-peer", "shared.py", cwd=str(repo))

        # Force "dead-peer" dead via real Layer-2 recency: no stable_pid,
        # last_activity far in the past (> 30 min idle threshold).
        core.update_meta_field(
            str(_sdir(repo, "dead-peer")),
            "last_activity",
            "2000-01-01T00:00:00Z",
        )

        # "mine" stays live via the real path: core.init just set its own
        # last_activity to now, so live_session_ids() (unmocked) sees it as
        # recently active under the same Layer-2 recency check.
        result = scope.compute_scope("mine", cwd=str(repo))

        assert "shared.py" in result.my_scope
        skipped_paths = {p for p, _owner in result.skipped}
        assert "shared.py" not in skipped_paths

    def test_abandoned_live_peer_claim_releases(self, tmp_path, monkeypatch):
        """C4/AC6 — abandonment is a SECOND, independent release condition
        from liveness. `live_session_ids` is stubbed to report the peer
        LIVE (so the pre-existing liveness gate alone would still contest
        this path) -- the release must come from the real, unmocked
        `liveness.session_abandoned` finding the peer's positive-activity
        signals genuinely stale, not from a doctored `last_activity` alone
        (`session_abandoned`'s >= 2 independently-stale-signal floor):
        both `last_activity` (meta.json) and the `touched.txt` mtime are
        pushed outside `_ABANDONMENT_WINDOW_SEC`."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("abandoned-peer", cwd=str(repo))
        (repo / "shared.py").write_text("z")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("abandoned-peer", "shared.py", cwd=str(repo))

        monkeypatch.setattr(
            scope.liveness,
            "live_session_ids",
            lambda cwd=None: frozenset({"mine", "abandoned-peer"}),
        )

        peer_sdir = _sdir(repo, "abandoned-peer")
        stale_epoch = (
            core.now_epoch() - scope.liveness._ABANDONMENT_WINDOW_SEC - 3600
        )
        core.update_meta_field(
            str(peer_sdir), "last_activity", "2000-01-01T00:00:00Z"
        )
        _age_session_dir_records(peer_sdir, stale_epoch)

        result = scope.compute_scope("mine", cwd=str(repo))

        assert "shared.py" in result.my_scope
        skipped_paths = {p for p, _owner in result.skipped}
        assert "shared.py" not in skipped_paths

    def test_live_recently_active_peer_claim_still_withheld(
        self, tmp_path, monkeypatch
    ):
        """C4/AC5 — the direction that matters most: a live, recently-
        active peer's claim must still be withheld, asserted explicitly
        rather than inferred from AC6's release test (a predicate that
        released everything would satisfy AC6 alone, but not this one)."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("live-peer", cwd=str(repo))
        (repo / "shared.py").write_text("z")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("live-peer", "shared.py", cwd=str(repo))

        monkeypatch.setattr(
            scope.liveness,
            "live_session_ids",
            lambda cwd=None: frozenset({"mine", "live-peer"}),
        )

        result = scope.compute_scope("mine", cwd=str(repo))

        assert "shared.py" not in result.my_scope
        assert ("shared.py", "live-peer") in result.skipped

    def test_undetermined_liveness_abandoned_peer_claim_still_withheld(
        self, tmp_path, monkeypatch
    ):
        """Review: coordinator:code-reviewer P1 (coordinatorcode-reviewer-
        1da5144e.md), Step 3 site. `live_ids is None` (undetermined
        liveness) must NEVER reach `session_abandoned` — a peer whose dir
        reads abandoned while liveness itself could not be resolved must
        still be treated as contested, not released. `live_session_ids` is
        stubbed to an EMPTY frozenset while a real peer session dir exists
        on disk -- the same indeterminacy shape
        `test_attribution_indeterminate_liveness_renders_undetermined_not_
        live` uses -- so `compute_scope` disables liveness gating
        (`live_ids = None`) for this call. The peer's own signals (real,
        unmocked `session_abandoned`) are pushed genuinely stale, exactly
        as `test_abandoned_live_peer_claim_releases` does for the
        determined-liveness case -- the ONLY difference here is that
        liveness itself is undetermined, which must withhold the claim
        regardless."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))
        (repo / "shared.py").write_text("z")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("peer", "shared.py", cwd=str(repo))

        monkeypatch.setattr(
            scope.liveness, "live_session_ids", lambda cwd=None: frozenset()
        )

        peer_sdir = _sdir(repo, "peer")
        stale_epoch = (
            core.now_epoch() - scope.liveness._ABANDONMENT_WINDOW_SEC - 3600
        )
        core.update_meta_field(
            str(peer_sdir), "last_activity", "2000-01-01T00:00:00Z"
        )
        _age_session_dir_records(peer_sdir, stale_epoch)

        # Sanity: the peer really does read abandoned in isolation -- the
        # test is pinning that an UNDETERMINED live_ids still withholds it.
        assert scope.liveness.session_abandoned("peer", cwd=str(repo)) is True

        result = scope.compute_scope("mine", cwd=str(repo))

        assert "shared.py" not in result.my_scope
        assert ("shared.py", "peer") in result.skipped

    def test_undetermined_liveness_abandoned_peer_agent_claim_still_withheld(
        self, tmp_path, monkeypatch
    ):
        """Review: coordinator:code-reviewer P1 (coordinatorcode-reviewer-
        1da5144e.md), Step 3b site -- the sub-agent-claim twin of the test
        above, keyed on the back-pointed owning `em_sid` rather than a
        direct session claim. Same shape: `live_session_ids` stubbed empty
        while the owning EM session's own dir exists on disk (indeterminate
        liveness -> `live_ids = None`), and the EM session's OWN signals
        (not the agent dir's) pushed genuinely stale via the real,
        unmocked `session_abandoned`. The claim must still be withheld."""
        repo = _make_repo(tmp_path)
        core.init("em-owner", cwd=str(repo))
        core.init("bystander", cwd=str(repo))

        base = Path(core.sessions_dir(cwd=str(repo)))
        agent_dir = base / ".agents" / "agent-xyz"
        agent_dir.mkdir(parents=True)
        (agent_dir / "em-session-id.txt").write_text("em-owner\n", encoding="utf-8")
        _agent_claim(agent_dir, "coordinator/agent_owned.py")
        (repo / "coordinator").mkdir()
        (repo / "coordinator" / "agent_owned.py").write_text("z")

        monkeypatch.setattr(
            scope.liveness, "live_session_ids", lambda cwd=None: frozenset()
        )

        em_sdir = _sdir(repo, "em-owner")
        # Give the EM session its own touched.txt (session_abandoned's
        # meta-carrying branch needs >= 2 independently stale signals) --
        # unrelated to the agent dir's separate touched.txt above.
        scope.touch("em-owner", "unrelated.py", cwd=str(repo))
        stale_epoch = (
            core.now_epoch() - scope.liveness._ABANDONMENT_WINDOW_SEC - 3600
        )
        core.update_meta_field(
            str(em_sdir), "last_activity", "2000-01-01T00:00:00Z"
        )
        _age_session_dir_records(em_sdir, stale_epoch)

        assert (
            scope.liveness.session_abandoned("em-owner", cwd=str(repo)) is True
        )

        result = scope.compute_scope("bystander", cwd=str(repo))

        assert "coordinator/agent_owned.py" not in result.my_scope
        assert (
            "coordinator/agent_owned.py",
            "em-owner",
        ) in result.skipped


class TestAttribution:
    """C2: pin C1's ``ScopeResult.attribution`` sidecar (``dict[str,
    OwnerFact]``) against the live-peer, dead-peer, peer-agent, and
    indeterminate-liveness cases named in the plan (AC1/AC1a/AC3/AC4/AC5/
    AC7/AC12 at this layer).

    The whole point of the attribution/subtraction split (see
    ``scope.OwnerFact``'s own docstring): ``other_owner`` (and therefore
    ``skipped``/``my_scope``) drops a dead peer's claim entirely (the
    release path), but ``attribution`` must STILL name that peer — it is an
    ungated, reporting-only record of every claim the Step 3/3b loop read,
    never a projection of what survived the liveness/clean-path gates.
    """

    def test_attribution_live_peer_recorded_as_live_session_claim(
        self, tmp_path, monkeypatch
    ):
        """Live peer: attribution names the peer with liveness "live" and
        claim_source "session" — matching the ordinary skipped/other_owner
        disposition for a live, contesting claim."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("live-peer", cwd=str(repo))
        (repo / "shared.py").write_text("z")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("live-peer", "shared.py", cwd=str(repo))

        monkeypatch.setattr(
            scope.liveness,
            "live_session_ids",
            lambda cwd=None: frozenset({"mine", "live-peer"}),
        )
        result = scope.compute_scope("mine", cwd=str(repo))

        assert "shared.py" not in result.my_scope
        assert ("shared.py", "live-peer") in result.skipped
        assert result.attribution["shared.py"] == scope.OwnerFact(
            "live-peer", "live", "session"
        )

    def test_attribution_dead_peer_still_names_the_peer(
        self, tmp_path, monkeypatch
    ):
        """AC1a named this "attribution must still name a dead peer" --
        C4/AC16(a) knowingly narrows that guarantee for the specific case
        this test exercises. AC16(a)'s reorder skips a CONFIRMED-dead
        peer's record read entirely (never opened/read/parsed, on the
        `live_session_ids()` membership test alone) -- there is no longer
        a per-path claim to attribute for that peer, by construction, not
        by oversight (see `compute_scope`'s Step 3 comment on this exact
        reorder). The subtraction-side guarantee (a dead peer's claim is
        RELEASED, the path lands in my_scope uncontested) is unaffected --
        only the attribution SIDE narrows, and only for a peer this call
        can positively prove dead up front."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("dead-peer", cwd=str(repo))
        (repo / "shared.py").write_text("z")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("dead-peer", "shared.py", cwd=str(repo))

        monkeypatch.setattr(
            scope.liveness, "live_session_ids", lambda cwd=None: frozenset({"mine"})
        )
        result = scope.compute_scope("mine", cwd=str(repo))

        # Subtraction side: released, uncontested -- lands in my_scope.
        assert "shared.py" in result.my_scope
        skipped_paths = {p for p, _owner in result.skipped}
        assert "shared.py" not in skipped_paths

        # Attribution side (C4/AC16(a)): a CONFIRMED-dead peer's record is
        # never read, so no per-path attribution for it survives -- this is
        # the accepted, documented consequence of the reorder, not a silent
        # drop (nothing in `other_owner`/`skipped` claims otherwise either).
        assert "shared.py" not in result.attribution

    def test_attribution_peer_agent_via_dot_agents_backpointer(
        self, tmp_path, monkeypatch
    ):
        """Peer's dispatched agent via the ``.agents`` back-pointer:
        attribution is keyed to the BACK-POINTED owning em-session-id, not
        the raw agent-directory id, with claim_source "agent"."""
        repo = _make_repo(tmp_path)
        core.init("em-owner", cwd=str(repo))
        core.init("bystander", cwd=str(repo))

        base = Path(core.sessions_dir(cwd=str(repo)))
        agent_dir = base / ".agents" / "agent-xyz"
        agent_dir.mkdir(parents=True)
        (agent_dir / "em-session-id.txt").write_text("em-owner\n", encoding="utf-8")
        _agent_claim(agent_dir, "coordinator/agent_owned.py")
        (repo / "coordinator").mkdir()
        (repo / "coordinator" / "agent_owned.py").write_text("z")

        monkeypatch.setattr(
            scope.liveness,
            "live_session_ids",
            lambda cwd=None: frozenset({"bystander", "em-owner"}),
        )
        result = scope.compute_scope("bystander", cwd=str(repo))

        assert "coordinator/agent_owned.py" not in result.my_scope
        assert (
            "coordinator/agent_owned.py",
            "em-owner",
        ) in result.skipped
        assert result.attribution["coordinator/agent_owned.py"] == scope.OwnerFact(
            "em-owner", "live", "agent"
        )

    def test_attribution_indeterminate_liveness_renders_undetermined_not_live(
        self, tmp_path, monkeypatch
    ):
        """AC7 — an unresolvable liveness verdict (``live_ids = None``: here
        via the empty-live-set-with-peer-evidence-on-disk indeterminacy
        fallback) must resolve attribution toward the CONTESTED rendering
        ("undetermined" liveness), NEVER toward an assertion of liveness
        ("live"). ``live_session_ids`` returning an empty frozenset while a
        real peer session directory exists on disk is the indeterminate
        shape (same as "everyone is dead") that disables liveness gating
        for this call entirely -- ``_peer_liveness_str`` reads
        ``live_ids is None`` as "undetermined", not "live"."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))
        (repo / "shared.py").write_text("z")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("peer", "shared.py", cwd=str(repo))

        monkeypatch.setattr(
            scope.liveness, "live_session_ids", lambda cwd=None: frozenset()
        )
        result = scope.compute_scope("mine", cwd=str(repo))

        # Subtraction side: gating disabled -> pre-existing unconditional
        # exclusion still applies (still skipped, not released).
        assert "shared.py" not in result.my_scope
        assert ("shared.py", "peer") in result.skipped

        # Attribution side: liveness renders "undetermined" -- NOT "live" --
        # the CONTESTED rendering the indeterminate verdict must resolve
        # toward, never an assertion of liveness.
        assert result.attribution["shared.py"] == scope.OwnerFact(
            "peer", "undetermined", "session"
        )
        assert result.attribution["shared.py"].liveness != "live"

    def test_attribution_survives_clean_path_release_from_subtraction(
        self, tmp_path, monkeypatch
    ):
        """AC12: releasing a peer claim from the SUBTRACTION (the clean-path
        prune -- ``state/handoffs/2026-08-03-scope-guard-peer-claim-
        release.md`` -- a live peer's claim on a now-committed, no-longer-
        dirty path drops out of ``other_owner``/``skipped``) must NOT remove
        that peer from the ATTRIBUTION map. Same fixture shape as
        ``test_clean_path_claim_is_pruned_dirty_path_claim_survives`` (the
        peer is live and the path is clean, so the release fires), but
        asserted at the attribution layer that sibling test never touches:
        attribution is recorded (Step 3/3b) BEFORE the clean-path prune
        runs, so it must still name the peer here."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))

        (repo / "clean.py").write_text("z")
        subprocess.run(["git", "add", "clean.py"], cwd=repo, check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-q", "-m", "add clean.py"], cwd=repo, check=True,
            **no_console_passthrough_kwargs(),
        )
        scope.touch("mine", "clean.py", cwd=str(repo))
        scope.touch("peer", "clean.py", cwd=str(repo))

        monkeypatch.setattr(
            scope.liveness,
            "live_session_ids",
            lambda cwd=None: frozenset({"mine", "peer"}),
        )
        result = scope.compute_scope("mine", cwd=str(repo))

        # Subtraction side: released -- clean.py lands in my_scope, not
        # skipped (mirrors test_clean_path_claim_is_pruned_dirty_path_
        # claim_survives).
        assert "clean.py" in result.my_scope
        skipped_paths = {p for p, _owner in result.skipped}
        assert "clean.py" not in skipped_paths

        # Attribution side: STILL names the peer, with liveness "live" --
        # this is the property AC12 pins.
        assert result.attribution["clean.py"] == scope.OwnerFact(
            "peer", "live", "session"
        )

    def test_attribution_runtime_type_is_uniform_across_code_paths(
        self, tmp_path
    ):
        """Staff-eng P2 (2026-08-03, pass 3) — the regression a divergent-
        type fix would let through. `ScopeResult.attribution`'s default (the
        out-of-repo/no-sessions-dir early-return path) and the REAL
        attribution dict `compute_scope` builds (the normal path) must be
        the SAME runtime type, ``types.MappingProxyType``, not a
        ``mappingproxy`` on one path and a plain ``dict`` on the other — a
        divergence that would only surface on the rarely-exercised
        early-return path (see that field's own docstring for why this is
        the worse shape than the shared-mutable-default hazard the
        original fix closed)."""
        import types as types_module

        out_of_repo_dir = tmp_path / "not_a_repo"
        out_of_repo_dir.mkdir()
        out_of_repo = scope.compute_scope("sid", cwd=str(out_of_repo_dir))
        assert type(out_of_repo.attribution) is types_module.MappingProxyType

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        repo = _make_repo(repo_dir)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))
        (repo / "shared.py").write_text("z")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("peer", "shared.py", cwd=str(repo))

        normal = scope.compute_scope("mine", cwd=str(repo))
        assert type(normal.attribution) is types_module.MappingProxyType
        assert type(normal.attribution) is type(out_of_repo.attribution)
        # And genuinely read-only on the normal path too, not merely typed
        # that way -- an in-place mutation attempt must raise.
        with pytest.raises(TypeError):
            normal.attribution["shared.py"] = None  # type: ignore[index]


# ---------------------------------------------------------------------------
# compute_scope() — real on-disk session registry corpus (no-raise + type)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def frozen_corpus_repo(tmp_path_factory):
    """A self-contained git repo seeded with every frozen session directory
    (touched.txt + started_at, verbatim from the snapshot) as a sibling —
    built once per module so the Step 3 other-session-claims scan exercises
    real cross-session shapes, not a single session in isolation."""
    repo = tmp_path_factory.mktemp("frozen-corpus-repo")
    _make_repo(repo)
    for entry in _FROZEN_CORPUS_ENTRIES:
        sid = entry["sid"]
        core.init(sid, cwd=str(repo))
        touched_text = "".join(line + "\n" for line in entry["touched_lines"])
        (_sdir(repo, sid) / "touched.txt").write_text(touched_text, encoding="utf-8")
        if entry["started_at"] is not None:
            (_sdir(repo, sid) / "started_at").write_text(
                entry["started_at"], encoding="utf-8"
            )
    return repo


@pytest.mark.parametrize(
    "entry",
    _FROZEN_CORPUS_ENTRIES,
    ids=lambda e: e["sid"],
)
def test_compute_scope_over_real_registry_corpus(entry, frozen_corpus_repo):
    """Golden-diff against a FROZEN copy of reality (see module docstring):
    compute_scope must never raise and must return a well-typed ScopeResult
    for every real session id in the frozen registry snapshot (exercises the
    real git diff/ls-files + mtime path, with every other frozen session
    present as a sibling claim-set)."""
    result = scope.compute_scope(entry["sid"], cwd=str(frozen_corpus_repo))
    assert isinstance(result, scope.ScopeResult)
    assert isinstance(result.my_scope, list)
    assert all(isinstance(p, str) for p in result.my_scope)
    assert all(
        isinstance(t, tuple) and len(t) == 2 for t in result.skipped
    )
    assert all(isinstance(p, str) for p in result.orphans)


# ---------------------------------------------------------------------------
# AC8 — defensive read-side normalization (C7): compute_scope's Step 1
# candidates AND Step 3/3b other_owner key space are BOTH run through the
# SAME strip-one-'../'-then-verify-containment transform C6 applies to the
# historical corpus. Belt-and-braces behind C6, not a substitute for it —
# these tests inject already-poisoned entries DIRECTLY into touched.txt
# (bypassing touch()'s own on-write normalization) to simulate a future
# writer regression reintroducing the poisoned dialect.
# ---------------------------------------------------------------------------


class TestAC8DefensiveHistoricalNormalization:
    def test_poisoned_own_and_peer_touched_txt_degrades_to_clean_equivalent_verdict(
        self, tmp_path, monkeypatch
    ):
        """The degradation property AC8 exists for: a single-leading-'../'
        poisoned entry naming the SAME real file, injected into both this
        session's touched.txt and a LIVE peer's touched.txt, must produce
        the EXACT SAME ownership verdict (skipped, owned by the peer) that
        the clean-entry equivalent produces — no false ownership, no
        dropped claim."""
        monkeypatch.setattr(
            scope.liveness,
            "live_session_ids",
            lambda cwd=None: frozenset({"mine", "peer"}),
        )

        poisoned_dir = tmp_path / "poisoned"
        poisoned_dir.mkdir()
        poisoned_repo = _make_repo(poisoned_dir)
        core.init("mine", cwd=str(poisoned_repo))
        core.init("peer", cwd=str(poisoned_repo))
        (poisoned_repo / "shared.py").write_text("z")
        _write_touch_record(
            _sdir(poisoned_repo, "mine") / "touch-record.jsonl",
            session_id="mine",
            entries=[(touch_record.VERB_TOUCH, "../shared.py")],
        )
        _write_touch_record(
            _sdir(poisoned_repo, "peer") / "touch-record.jsonl",
            session_id="peer",
            entries=[(touch_record.VERB_TOUCH, "../shared.py")],
        )
        poisoned_result = scope.compute_scope("mine", cwd=str(poisoned_repo))

        clean_dir = tmp_path / "clean-control"
        clean_dir.mkdir()
        clean_repo = _make_repo(clean_dir)
        core.init("mine", cwd=str(clean_repo))
        core.init("peer", cwd=str(clean_repo))
        (clean_repo / "shared.py").write_text("z")
        scope.touch("mine", "shared.py", cwd=str(clean_repo))
        scope.touch("peer", "shared.py", cwd=str(clean_repo))
        clean_result = scope.compute_scope("mine", cwd=str(clean_repo))

        assert poisoned_result.my_scope == clean_result.my_scope
        assert poisoned_result.skipped == clean_result.skipped
        assert poisoned_result.orphans == clean_result.orphans
        assert "shared.py" not in poisoned_result.my_scope
        assert ("shared.py", "peer") in poisoned_result.skipped

    def test_poisoned_own_touched_txt_uncontested_becomes_mtime_orphan_not_my_scope(
        self, tmp_path, monkeypatch
    ):
        """Reversed under C1 (docs/plans/2026-08-05-touched-sibling-escape-
        and-suppressed-trailer.md), same posture as this file's
        TestClassifyTouchEntry pins. This test used to assert an
        uncontested single-level-poisoned entry still rescues into
        my_scope under its normalized form — that was C1's OLD (pre-C1)
        contract. C1 replaces the depth-based rescue with a uniform
        containment drop: the entry is dropped from the Step 1 candidate
        set regardless of contest. It is NOT silently lost, though — the
        Key-decision section names this exact narrowing case as visible,
        not invisible: the file is still dirty on disk, so compute_scope
        Step 2 re-adds it as an `mtime_only` orphan, and it is reported
        there rather than auto-committed. Assert the new, narrower
        contract: absent from my_scope, present in orphans."""
        monkeypatch.setattr(
            scope.liveness, "live_session_ids", lambda cwd=None: frozenset({"mine"})
        )
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "mine_only.py").write_text("z")
        (_sdir(repo, "mine") / "touched.txt").write_text("../mine_only.py\n")

        result = scope.compute_scope("mine", cwd=str(repo))

        assert "mine_only.py" not in result.my_scope
        assert "mine_only.py" in result.orphans
        assert not any(p.startswith("../") for p in result.my_scope)

    def test_multi_level_poisoned_entry_not_fabricated_into_in_repo_path(
        self, tmp_path, monkeypatch
    ):
        """A `../../…` entry cannot be resolved by a single strip without
        escaping the worktree — it must be DROPPED from both the Step 1
        candidate set and the Step 3 other_owner key space, never
        fabricated into an in-repo path (the non-negotiable invariant: an
        out-of-worktree entry must never become a fabricated in-repo one)."""
        monkeypatch.setattr(
            scope.liveness,
            "live_session_ids",
            lambda cwd=None: frozenset({"mine", "peer"}),
        )
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))
        (_sdir(repo, "mine") / "touched.txt").write_text("../../outside/foo.py\n")
        (_sdir(repo, "peer") / "touched.txt").write_text("../../outside/foo.py\n")

        result = scope.compute_scope("mine", cwd=str(repo))

        assert result.my_scope == []
        assert all("outside" not in p for p in result.my_scope)
        skipped_paths = {p for p, _owner in result.skipped}
        assert "outside/foo.py" not in skipped_paths
        assert not any("outside" in p for p in skipped_paths)

    def test_asymmetric_multi_level_peer_poisoning_does_not_silently_drop_live_claim(
        self, tmp_path, monkeypatch
    ):
        """Review: code-reviewer Finding 1 (sidecar
        coordinatorcode-reviewer-359b224b.md) — the asymmetric case the
        symmetric-drop argument missed: THIS session's own candidate for a
        real in-tree file is clean, while a LIVE peer's entry for the SAME
        real file is poisoned at a depth (2-level '../') a single strip
        cannot resolve. A symmetric drop would silently vanish the peer's
        claim from other_owner while the clean candidate survives, letting
        this session sweep the peer's live file into its own scope
        uncontested. Assert that does NOT happen: the path must be skipped
        (owned by the peer), never land in my_scope."""
        monkeypatch.setattr(
            scope.liveness,
            "live_session_ids",
            lambda cwd=None: frozenset({"mine", "peer"}),
        )
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))
        (repo / "foo.py").write_text("z")  # dirty (untracked) — same real file
        scope.touch("mine", "foo.py", cwd=str(repo))
        _write_touch_record(
            _sdir(repo, "peer") / "touch-record.jsonl",
            session_id="peer",
            entries=[(touch_record.VERB_TOUCH, "../../foo.py")],
        )

        result = scope.compute_scope("mine", cwd=str(repo))

        assert "foo.py" not in result.my_scope
        assert ("foo.py", "peer") in result.skipped

    def test_compute_scope_idempotent_on_clean_touched_txt(self, tmp_path):
        """The common path (today's corpus is clean) must be a true no-op:
        running compute_scope twice against unchanged, already-clean
        touched.txt content yields identical results."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        scope.touch("mine", "already_clean.py", cwd=str(repo))
        (repo / "already_clean.py").write_text("z")

        first = scope.compute_scope("mine", cwd=str(repo))
        second = scope.compute_scope("mine", cwd=str(repo))

        assert first == second
        assert "already_clean.py" in first.my_scope


class TestClassifyTouchEntry:
    """Direct unit coverage of the canonical transform underlying AC8's
    compute_scope normalization — see scope.classify_touch_entry's own
    docstring for the full class taxonomy this mirrors.

    C1 (docs/plans/2026-08-05-touched-sibling-escape-and-suppressed-
    trailer.md) replaces the leading-'../'-token-depth branch split with one
    posixpath-normalize-then-contain predicate applied uniformly to every
    non-absolute entry. `stripped_one_level` and `multi_level` are BOTH
    retired into a single `dropped` outcome — this class's pins below are
    updated to match (AC4), plus new pins for the containment-escape shapes
    the old branch structure missed entirely (AC1) and the corrected
    `clean`-return ruling (C1's PM ruling on canonical-value return)."""

    def test_clean_entry_is_idempotent_noop(self, tmp_path):
        repo = _make_repo(tmp_path)
        first = scope.classify_touch_entry("clean/path.py", repo)
        assert first.entry_class == "clean"
        assert first.new_value == "clean/path.py"
        second = scope.classify_touch_entry(first.new_value, repo)
        assert second == first

    def test_single_leading_dotdot_entry_is_now_dropped_not_rescued(self, tmp_path):
        """AC4 — deliberately REVERSED from the pre-C1 pin. This test used
        to assert `../inside.py` -> `stripped_one_level` -> `inside.py` and
        pinned the strip-then-verify as intended behaviour, including
        idempotence on the stripped result. That assertion was WRONG: the
        containment check it encoded ran only inside the (now-retired)
        `leading == 1` branch and never saw the `leading == 0` case at all
        — i.e. the check it exercised was vacuous by construction (a
        remainder can only still start with `../` if the original had two
        or more leading tokens, which already returned `multi_level` on the
        line above), so this test never actually verified containment. C1
        replaces the whole depth-counting branch with one canonical-value
        containment predicate applied to every non-absolute entry, under
        which a genuine single-level `../` escape (indistinguishable at
        read time from a common-dir-poisoned real in-repo touch) now drops
        rather than being rescued — the fail-safe, narrowing direction."""
        repo = _make_repo(tmp_path)
        outcome = scope.classify_touch_entry("../inside.py", repo)
        assert outcome.entry_class == "dropped"
        assert outcome.new_value is None

    def test_multi_level_entry_is_dropped_not_fabricated(self, tmp_path):
        repo = _make_repo(tmp_path)
        outcome = scope.classify_touch_entry("../../outside/foo.py", repo)
        assert outcome.entry_class == "dropped"
        assert outcome.new_value is None

    @pytest.mark.parametrize(
        "entry",
        [
            "docs/../../peer/x.md",  # embedded '..' after a clean-looking prefix
            "./../peer/x.md",  # leading './' defeats the old leading-token loop
            "..\\peer\\x.md",  # backslash separator; the old loop matched '../' only
            "../docs/peer.md",  # the one shape the old branch structure did cover
        ],
        ids=[
            "embedded-dotdot-after-clean-prefix",
            "leading-dot-slash",
            "backslash-separator",
            "leading-dotdot-original-shape",
        ],
    )
    def test_containment_escape_shapes_are_dropped(self, tmp_path, entry):
        """AC1 — four distinct containment-escape shapes C1's body names,
        each pinned individually (not covered incidentally by one another).
        Before C1 every one of the first three classified `clean` and
        survived UNCHANGED — the old leading-token loop only ever ran its
        containment check inside the `leading == 1` branch, so an entry
        with zero leading '../' tokens (or one defeated by a './' prefix or
        a backslash separator) never reached any containment check at all.
        The fourth shape is the one the pre-C1 branch structure DID cover
        (as `stripped_one_level`); it is included here so a reader working
        from this table pins all four, not three."""
        repo = _make_repo(tmp_path)
        outcome = scope.classify_touch_entry(entry, repo)
        assert outcome.entry_class == "dropped"
        assert outcome.new_value is None

    def test_leading_backslash_entry_is_dropped_via_separator_normalize(
        self, tmp_path
    ):
        """A fifth escape shape, distinct from the four above: a
        leading-backslash entry ('\\peer\\x.md') does NOT match HEAD's real
        `_ABSOLUTE_RE` (`^(?:/|[A-Za-z]:)`) — no leading '/', no drive
        letter — so it never reaches `classify_touch_entry`'s absolute
        branch at all; before C1 it returned `clean` UNCHANGED. Under C1 it
        drops only via the separator-normalize step turning it into
        '/peer/x.md', which `posixpath.isabs` then catches — the one escape
        shape neither branch handled before C1. Drive-letter forms
        ('C:\\peer\\x.md', 'C:/peer/x.md' -- abs-path-ok: a test-data string
        naming an escape SHAPE for classify_touch_entry, not a real host
        path) already route through the
        pre-existing absolute branch and are deliberately NOT re-pinned
        here."""
        repo = _make_repo(tmp_path)
        outcome = scope.classify_touch_entry("\\peer\\x.md", repo)
        assert outcome.entry_class == "dropped"
        assert outcome.new_value is None

    def test_non_escaping_backslash_entry_is_clean_and_returns_canonical_form(
        self, tmp_path
    ):
        """THE MOST IMPORTANT TEST IN THIS SLATE. A non-escaping backslash
        entry ('state\\x.md') is CONTAINED (its canonical form is
        'state/x.md'), so it must classify `clean` — but per the corrected
        `clean`-return ruling it must return the CANONICAL form
        ('state/x.md'), NOT the raw ('state\\x.md') entry. This is the
        shape the ruling's v1 wording silently mishandled: comparing
        `posixpath.normpath(entry) == entry` against the RAW entry was true
        for this exact input (posixpath does not treat '\\' as a
        separator), so it returned the raw, un-normalized value. It failed
        SILENTLY with no test before this pin — load-bearing, not
        incidental coverage."""
        repo = _make_repo(tmp_path)
        outcome = scope.classify_touch_entry("state\\x.md", repo)
        assert outcome.entry_class == "clean"
        assert outcome.new_value == "state/x.md"

    def test_ac12_depth1_peer_claim_key_is_byte_identical_before_and_after_c1(
        self, tmp_path
    ):
        """AC12 — peer-side equivalence. C1 moves a depth-1 '../' entry's
        peer-key resolution from `classify_touch_entry` itself (pre-C1:
        `stripped_one_level`, whose `new_value` fed `normalize_peer_claim_
        key` directly) to `_maximal_strip_peer_fallback` (post-C1:
        `classify_touch_entry` now returns `dropped`, so
        `normalize_peer_claim_key` falls through to the strip-ALL
        fallback). For a depth-1 entry "strip all" and "strip one leading
        token" are the IDENTICAL string, and both apply the identical
        containment test, so `other_owner`'s returned key is byte-identical
        either way. Assert the KEY VALUE, not which branch produced it, so
        a future edit to either transform cannot silently break this
        equivalence without a red test naming the break."""
        repo = _make_repo(tmp_path)
        key = scope.normalize_peer_claim_key("../foo.py", repo)
        assert key == "foo.py"


class TestClassifyTouchEntryAC1EndToEnd:
    """AC1's P1 end-to-end assertion: a session whose touched.txt carries a
    HAND-WRITTEN '../' entry (never routed through touch()) must not reach
    safe_paths. Hand-writing the entry — rather than driving it through
    touch() — is deliberate: this test must still fail if only C2 (the
    writer fix) had landed, which is what makes it a real pin on C1 rather
    than a restatement of C2."""

    def test_hand_written_relative_escape_yields_empty_safe_paths(self, tmp_path):
        from coordinator_core.ops.session import safe_commit_offer

        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        # A sibling-dir collision: an in-repo top-level "docs/" dir with a
        # real, dirty (untracked) file this session never touched, matching
        # the exact P1 probe transcript shape.
        (repo / "docs").mkdir()
        (repo / "docs" / "peer_owned.md").write_text("z")
        (_sdir(repo, "mine") / "touched.txt").write_text(
            "../docs/peer_owned.md\n"
        )

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))

        assert offer["safe_paths"] == []


# ---------------------------------------------------------------------------
# archive()
# ---------------------------------------------------------------------------


class TestPostCutoverSelfClaimNeedsNoLegacyUnion:
    """C8 (docs/plans/2026-08-25-the-legacy-touch-record-is-retired-by-
    repointing-its-writers.md) post-cutover invariant, replacing
    ``TestAC21SelfClaimStaysVisibleUntilTheWriterMigrates`` BY NAME.

    That class pinned a fixture where `self_claim` wrote ONLY a legacy
    `touched.txt`, on the premise that `session/claims.py :: self_claim`
    was still an old-dialect writer. It has not been since C6 (`self_claim`
    now appends through `touch_record.append_event`, the same seam
    `scope.touch()` uses) -- a fixture forcing a legacy-only `touched.txt`
    out of `self_claim` no longer represents anything `self_claim` can
    itself produce, so the old pin would now pass vacuously against a
    hand-written fixture, not against the writer's own behaviour.

    The replacement below asserts the two things that actually changed:
    `self_claim` itself never touches the old dialect (AC9's "no [in-scope]
    writer emits the old dialect" premise, proven directly rather than
    inferred), and (AC4, C8 half) `compute_scope` sees a `self_claim` write
    with NO `touched.txt` anywhere in the fixture at all -- only meaningful
    now that the union-read no longer needs one to pass.
    """

    def test_self_claim_writes_only_the_jsonl_dialect(self, tmp_path, monkeypatch):
        from coordinator_core.session import claims

        repo = _make_repo(tmp_path)
        core.init("sc1", cwd=str(repo))
        target = repo / "edited_outside_hook.py"
        target.write_text("y")

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sc1")
        claims.self_claim(str(target), cwd=str(repo))

        sdir = _sdir(repo, "sc1")
        # The fact this chunk's brief asks to establish: self_claim, the
        # writer the retired AC21 pin depended on, no longer emits the old
        # bare-line dialect at all -- not "the union still covers it if it
        # did", but "it never does".
        assert not (sdir / "touched.txt").exists()
        assert (sdir / scope._TOUCH_RECORD_FILENAME).is_file()

    def test_self_claim_visible_via_compute_scope_with_no_touched_txt_anywhere(
        self, tmp_path, monkeypatch
    ):
        from coordinator_core.session import claims

        repo = _make_repo(tmp_path)
        core.init("sc2", cwd=str(repo))
        target = repo / "edited_outside_hook_2.py"
        target.write_text("y")

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sc2")
        claims.self_claim(str(target), cwd=str(repo))

        sdir = _sdir(repo, "sc2")
        assert not (sdir / "touched.txt").exists()

        # AC4, C8 half: no `touched.txt` anywhere in this fixture (self or
        # peer side) -- the self_claim write is visible to compute_scope
        # through the jsonl seam alone.
        result = scope.compute_scope("sc2", cwd=str(repo))
        assert "edited_outside_hook_2.py" in result.my_scope
        assert "edited_outside_hook_2.py" not in result.orphans


class TestArchive:
    def test_requires_sid(self):
        with pytest.raises(ValueError):
            scope.archive("")

    def test_out_of_repo_returns_false(self, tmp_path):
        assert scope.archive("sid", cwd=str(tmp_path)) is False

    def test_missing_session_dir_is_idempotent_true(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert scope.archive("never-existed", cwd=str(repo)) is True

    def test_moves_session_dir_under_archive_with_date(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("done-sid", cwd=str(repo))
        sdir = _sdir(repo, "done-sid")
        assert sdir.is_dir()
        assert scope.archive("done-sid", cwd=str(repo)) is True
        assert not sdir.exists()
        base = Path(core.sessions_dir(cwd=str(repo)))
        archived = list((base / ".archive").glob("done-sid-*"))
        assert len(archived) == 1
        # date-suffix shape YYYY-MM-DD
        import re as _re

        assert _re.search(r"done-sid-\d{4}-\d{2}-\d{2}$", str(archived[0]))

    def test_archive_is_idempotent_on_second_call(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("twice", cwd=str(repo))
        assert scope.archive("twice", cwd=str(repo)) is True
        # second call: session dir already gone -> idempotent True
        assert scope.archive("twice", cwd=str(repo)) is True

    def _write_roster(self, sdir: Path, rows) -> None:
        sdir.mkdir(parents=True, exist_ok=True)
        with open(sdir / "dispatched-agents.txt", "a", encoding="utf-8") as fh:
            for agent_id in rows:
                fh.write(f"{agent_id}\tsome-model\tsome-type\n")

    def _write_backptr(self, base: Path, agent_id: str, em_sid: str, stale: bool = True) -> Path:
        agent_dir = base / ".agents" / agent_id
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "em-session-id.txt").write_text(em_sid + "\n", encoding="utf-8")
        touched = agent_dir / "touched.txt"
        touched.write_text("T x\n", encoding="utf-8")
        if stale:
            # Older than `_AGENT_DROP_RECENCY_SECONDS` so the liveness
            # guard doesn't mask the ownership-logic these tests exercise.
            import os as _os
            import time as _time

            old = _time.time() - scope._AGENT_DROP_RECENCY_SECONDS - 60
            _os.utime(touched, (old, old))
        return agent_dir

    def test_drops_owned_agent_dirs_on_clean_archive(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("owner-sid", cwd=str(repo))
        sdir = _sdir(repo, "owner-sid")
        base = sdir.parent
        self._write_roster(sdir, ["agent-1"])
        agent_dir = self._write_backptr(base, "agent-1", "owner-sid")

        assert scope.archive("owner-sid", cwd=str(repo)) is True
        assert not agent_dir.exists()
        archived = list((base / ".archive").glob("_agents-agent-1-*"))
        assert len(archived) == 1

    def test_agent_owned_by_different_session_is_left_alone(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("owner-sid", cwd=str(repo))
        sdir = _sdir(repo, "owner-sid")
        base = sdir.parent
        self._write_roster(sdir, ["agent-2"])
        agent_dir = self._write_backptr(base, "agent-2", "some-other-sid")

        assert scope.archive("owner-sid", cwd=str(repo)) is True
        assert agent_dir.exists()
        assert not list((base / ".archive").glob("_agents-agent-2-*"))

    def test_missing_roster_file_is_a_noop(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("no-roster-sid", cwd=str(repo))
        sdir = _sdir(repo, "no-roster-sid")
        base = sdir.parent
        # No dispatched-agents.txt written at all.
        assert scope.archive("no-roster-sid", cwd=str(repo)) is True
        assert not (base / ".archive" / "no-roster-sid").exists()  # session itself archives fine

    def test_failing_entry_does_not_prevent_session_archive(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        core.init("owner-sid2", cwd=str(repo))
        sdir = _sdir(repo, "owner-sid2")
        base = sdir.parent
        self._write_roster(sdir, ["agent-3"])
        self._write_backptr(base, "agent-3", "owner-sid2")

        real_rename = scope.os.rename

        def _selective_boom(src, dst, *args, **kwargs):
            if "agent-3" in str(src):
                raise OSError("simulated move failure")
            return real_rename(src, dst, *args, **kwargs)

        monkeypatch.setattr(scope.os, "rename", _selective_boom)
        assert scope.archive("owner-sid2", cwd=str(repo)) is True
        assert not sdir.exists()

    def test_recently_touched_owned_agent_dir_is_left_alone(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("owner-sid3", cwd=str(repo))
        sdir = _sdir(repo, "owner-sid3")
        base = sdir.parent
        self._write_roster(sdir, ["agent-fresh"])
        agent_dir = self._write_backptr(base, "agent-fresh", "owner-sid3", stale=False)

        assert scope.archive("owner-sid3", cwd=str(repo)) is True
        assert agent_dir.exists()
        assert not list((base / ".archive").glob("_agents-agent-fresh-*"))

    def test_stale_touched_owned_agent_dir_is_moved(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("owner-sid4", cwd=str(repo))
        sdir = _sdir(repo, "owner-sid4")
        base = sdir.parent
        self._write_roster(sdir, ["agent-stale"])
        agent_dir = self._write_backptr(base, "agent-stale", "owner-sid4", stale=True)

        assert scope.archive("owner-sid4", cwd=str(repo)) is True
        assert not agent_dir.exists()
        archived = list((base / ".archive").glob("_agents-agent-stale-*"))
        assert len(archived) == 1

    def test_existing_archive_dest_absorbed_idempotently_via_rename_race(self, tmp_path, monkeypatch):
        # Simulate a concurrent cadence-reaper race: `os.rename` raises
        # OSError, but the destination already exists by the time we
        # re-check — the function must absorb this idempotently, not raise.
        repo = _make_repo(tmp_path)
        core.init("owner-sid5", cwd=str(repo))
        sdir = _sdir(repo, "owner-sid5")
        base = sdir.parent
        self._write_roster(sdir, ["agent-race"])
        agent_dir = self._write_backptr(base, "agent-race", "owner-sid5", stale=True)

        archive_root = base / ".archive"
        archive_root.mkdir(parents=True, exist_ok=True)

        real_rename = scope.os.rename

        def _race_rename(src, dst, *args, **kwargs):
            if "agent-race" in str(src):
                Path(dst).mkdir(parents=True, exist_ok=True)
                raise OSError("simulated concurrent-reap race")
            return real_rename(src, dst, *args, **kwargs)

        monkeypatch.setattr(scope.os, "rename", _race_rename)
        assert scope.archive("owner-sid5", cwd=str(repo)) is True
        # The race-created destination stands in for the concurrent
        # reaper's own move — absorbed idempotently, not raised.
        archived = list(archive_root.glob("_agents-agent-race-*"))
        assert len(archived) == 1


# ---------------------------------------------------------------------------
# parse_touch_event() / format_touch_event() — P1, the frozen event-record
# format. Additive only: no existing assertion above is touched.
# ---------------------------------------------------------------------------


class TestParseFormatTouchEvent:
    def test_well_formed_t_line_parses(self):
        verb, ts, path = scope.parse_touch_event(
            "T 2026-08-03T14:37:37.412088+00:00 coordinator_core/foo.py"
        )
        assert verb == "T"
        assert ts is not None
        assert path == "coordinator_core/foo.py"

    def test_well_formed_r_line_parses(self):
        verb, ts, path = scope.parse_touch_event(
            "R 2026-08-03T14:37:37.412088+00:00 coordinator_core/foo.py"
        )
        assert verb == "R"
        assert ts is not None
        assert path == "coordinator_core/foo.py"

    def test_path_containing_space_survives(self):
        verb, ts, path = scope.parse_touch_event(
            "T 2026-08-03T14:37:37.412088+00:00 some dir/has space.py"
        )
        assert verb == "T"
        assert path == "some dir/has space.py"

    def test_bare_legacy_line_projects_t_unknown_time(self):
        verb, ts, path = scope.parse_touch_event("coordinator_core/legacy.py")
        assert verb == "T"
        assert ts is None
        assert path == "coordinator_core/legacy.py"

    def test_truncated_line_is_fail_safe_claimed(self):
        verb, ts, path = scope.parse_touch_event("T 2026-08-03T14:37:37.412088+00:00")
        assert verb == "T"
        assert ts is None

    def test_unknown_verb_is_fail_safe_claimed(self):
        verb, ts, path = scope.parse_touch_event(
            "X 2026-08-03T14:37:37.412088+00:00 coordinator_core/foo.py"
        )
        assert verb == "T"
        assert ts is None

    def test_tab_bearing_line_still_parses_via_whitespace_split(self):
        # str.split(None, 2) treats a tab as whitespace too, so a
        # tab-delimited line parses structurally the same as a
        # space-delimited one — the frozen format itself must never EMIT
        # a tab (see test_format_never_emits_tab), but the parser degrades
        # gracefully rather than choking on one it encounters.
        line = "T\t2026-08-03T14:37:37.412088+00:00\tcoordinator_core/foo.py"
        verb, ts, path = scope.parse_touch_event(line)
        assert verb == "T"
        assert ts is not None
        assert path == "coordinator_core/foo.py"

    def test_never_raises_on_garbage_input(self):
        for garbage in ("", "   ", "\n", "T R path extra tokens here"):
            verb, ts, path = scope.parse_touch_event(garbage)
            assert verb in ("T", "R")

    def test_unknown_time_sentinel_sorts_earlier_than_real_timestamp(self):
        _, none_ts, _ = scope.parse_touch_event("legacy/bare/path.py")
        _, real_ts, _ = scope.parse_touch_event(
            "R 2026-08-03T14:37:37.412088+00:00 coordinator_core/foo.py"
        )
        assert scope._touch_event_sort_key(none_ts) < scope._touch_event_sort_key(real_ts)

    def test_format_round_trips_through_parse(self):
        line = scope.format_touch_event("T", "coordinator_core/foo.py")
        verb, ts, path = scope.parse_touch_event(line)
        assert verb == "T"
        assert ts is not None
        assert path == "coordinator_core/foo.py"

    def test_format_rejects_bad_verb(self):
        with pytest.raises(ValueError):
            scope.format_touch_event("Q", "coordinator_core/foo.py")

    def test_format_never_emits_tab(self):
        line = scope.format_touch_event("R", "coordinator_core/foo bar.py")
        assert "\t" not in line

    def test_format_uses_explicit_when(self):
        from datetime import datetime, timezone

        when = datetime(2026, 8, 3, 14, 37, 37, 412088, tzinfo=timezone.utc)
        line = scope.format_touch_event("T", "coordinator_core/foo.py", when=when)
        assert line == "T 2026-08-03T14:37:37.412088Z coordinator_core/foo.py"

    def test_format_keeps_microseconds_when_zero(self):
        """`isoformat()` omits the microsecond field entirely when it is zero,
        which silently drops the resolution the `>=` mtime comparison depends
        on and breaks the exact-line schema contract DoE re-pins against."""
        from datetime import datetime, timezone

        when = datetime(2026, 8, 3, 14, 37, 37, 0, tzinfo=timezone.utc)
        line = scope.format_touch_event("R", "coordinator_core/foo.py", when=when)
        assert line == "R 2026-08-03T14:37:37.000000Z coordinator_core/foo.py"

    def test_naive_timestamp_fails_safe_to_claimed(self):
        """A tz-less timestamp must not parse into a naive datetime: every
        consumer compares these against aware instants, and mixing the two
        raises TypeError inside the projection, past where the never-raise
        contract can absorb it."""
        verb, ts, _path = scope.parse_touch_event(
            "T 2026-08-03T14:37:37.412088 coordinator_core/foo.py"
        )
        assert (verb, ts) == ("T", None)


class TestProjectSelfScope:
    """Self-facing projection (P3) — never applies the peer-facing mtime
    re-claim; this is the arm that must not widen `my_scope`."""

    def test_last_event_t_is_claimed(self):
        assert scope.project_self_scope(
            ["T 2026-08-03T10:00:00+00:00 a.py"]
        ) == {"a.py"}

    def test_last_event_r_is_released_even_if_dirty(self):
        # No mtime data is even accepted by this function's signature — the
        # policy is unconditional: last event R means excluded, period.
        lines = [
            "T 2026-08-03T09:00:00+00:00 a.py",
            "R 2026-08-03T10:00:00+00:00 a.py",
        ]
        assert scope.project_self_scope(lines) == set()

    def test_t_after_r_reclaims_normally(self):
        lines = [
            "T 2026-08-03T09:00:00+00:00 a.py",
            "R 2026-08-03T10:00:00+00:00 a.py",
            "T 2026-08-03T11:00:00+00:00 a.py",
        ]
        assert scope.project_self_scope(lines) == {"a.py"}

    def test_legacy_bare_line_is_claimed_at_unknown_time(self):
        assert scope.project_self_scope(["legacy/bare/path.py"]) == {
            "legacy/bare/path.py"
        }

    def test_blank_lines_ignored(self):
        assert scope.project_self_scope(
            ["", "T 2026-08-03T10:00:00+00:00 a.py", ""]
        ) == {"a.py"}


class TestProjectPeerClaims:
    """Peer-facing projection (P3) — the guard's input. A released path
    re-projects to CLAIMED under the mtime-re-claim rule UNLESS a real
    challenger T post-dates the R (inference loses to evidence)."""

    def test_last_event_t_is_claimed(self):
        result = scope.project_peer_claims(
            ["T 2026-08-03T10:00:00+00:00 a.py"], {}
        )
        assert "a.py" in result

    def test_last_event_r_not_dirty_stays_released(self):
        lines = [
            "T 2026-08-03T09:00:00+00:00 a.py",
            "R 2026-08-03T10:00:00+00:00 a.py",
        ]
        # path_mtimes has no entry for a.py -> "not currently dirty".
        result = scope.project_peer_claims(lines, {})
        assert "a.py" not in result

    def test_last_event_r_dirty_since_reclaims_to_claimed(self):
        from datetime import datetime, timezone

        r_ts = datetime(2026, 8, 3, 10, 0, 0, tzinfo=timezone.utc)
        lines = [scope.format_touch_event("R", "a.py", when=r_ts)]
        path_mtimes = {"a.py": r_ts.timestamp() + 100}  # dirty AFTER the R
        result = scope.project_peer_claims(lines, path_mtimes)
        assert "a.py" in result

    def test_last_event_r_mtime_before_r_stays_released(self):
        from datetime import datetime, timezone

        r_ts = datetime(2026, 8, 3, 10, 0, 0, tzinfo=timezone.utc)
        lines = [scope.format_touch_event("R", "a.py", when=r_ts)]
        path_mtimes = {"a.py": r_ts.timestamp() - 100}  # stale mtime, before R
        result = scope.project_peer_claims(lines, path_mtimes)
        assert "a.py" not in result

    def test_challenger_t_postdating_r_blocks_reclaim(self):
        # AC13(b): a REAL T for this path, post-dating the R, means
        # inference (mtime re-claim) loses to evidence (the challenger T).
        from datetime import datetime, timezone

        r_ts = datetime(2026, 8, 3, 10, 0, 0, tzinfo=timezone.utc)
        challenger_ts = datetime(2026, 8, 3, 11, 0, 0, tzinfo=timezone.utc)
        lines = [scope.format_touch_event("R", "a.py", when=r_ts)]
        path_mtimes = {"a.py": r_ts.timestamp() + 100}
        result = scope.project_peer_claims(
            lines, path_mtimes, challenger_t_events={"a.py": challenger_ts}
        )
        assert "a.py" not in result

    def test_challenger_t_predating_r_does_not_block_reclaim(self):
        # AC13(a): a challenger T that does NOT post-date the R is not real
        # evidence against the reclaim — the mtime rule still fires.
        from datetime import datetime, timezone

        challenger_ts = datetime(2026, 8, 3, 9, 0, 0, tzinfo=timezone.utc)
        r_ts = datetime(2026, 8, 3, 10, 0, 0, tzinfo=timezone.utc)
        lines = [scope.format_touch_event("R", "a.py", when=r_ts)]
        path_mtimes = {"a.py": r_ts.timestamp() + 100}
        result = scope.project_peer_claims(
            lines, path_mtimes, challenger_t_events={"a.py": challenger_ts}
        )
        assert "a.py" in result

    def test_legacy_bare_line_is_claimed_unconditionally(self):
        result = scope.project_peer_claims(["legacy/bare.py"], {})
        assert "legacy/bare.py" in result

    def test_blank_lines_ignored(self):
        result = scope.project_peer_claims(
            ["", "T 2026-08-03T10:00:00+00:00 a.py", ""], {}
        )
        assert "a.py" in result
        assert "" not in result


class TestComputeScopeEventProjectionAC13:
    """Integration coverage for AC13 both directions, wired through
    `compute_scope`'s Step 3 peer scan (`project_peer_claims`) — a bare-
    line-only corpus is covered exhaustively by TestComputeScope's existing
    (unmodified) suite; these tests exercise the event-log dialect
    directly, since no writer in this repo emits it yet (P2 pending)."""

    def test_unattributed_redirty_after_release_projects_back_to_claimed(
        self, tmp_path
    ):
        from datetime import datetime, timedelta, timezone

        repo = _make_repo(tmp_path)
        core.init("mine13a", cwd=str(repo))
        core.init("peer13a", cwd=str(repo))
        mine_sdir = _sdir(repo, "mine13a")
        (mine_sdir / "started_at").write_text("2000-01-01T00:00:00Z")

        r_ts = datetime.now(timezone.utc) - timedelta(seconds=5)
        peer_sdir = _sdir(repo, "peer13a")
        _write_touch_record(
            peer_sdir / "touch-record.jsonl",
            session_id="peer13a",
            entries=[
                (touch_record.VERB_TOUCH, "reclaim.py", (r_ts - timedelta(seconds=10)).timestamp()),
                (touch_record.VERB_RELEASE, "reclaim.py", r_ts.timestamp()),
            ],
        )

        (repo / "reclaim.py").write_text("z")  # dirty NOW, after the R above

        result = scope.compute_scope("mine13a", cwd=str(repo))
        assert "reclaim.py" not in result.my_scope
        assert ("reclaim.py", "peer13a") in result.skipped

    def test_attributed_redirty_with_challenger_t_stays_released(self, tmp_path):
        # AC13(b) worked example: peer A releases publish.py, then the
        # CALLING session (mine) itself edits it — the reclaim must NOT
        # fire; the path belongs uncontested to `mine`.
        from datetime import datetime, timedelta, timezone

        repo = _make_repo(tmp_path)
        core.init("peer13b", cwd=str(repo))
        core.init("mine13b", cwd=str(repo))
        mine_sdir = _sdir(repo, "mine13b")
        (mine_sdir / "started_at").write_text("2000-01-01T00:00:00Z")

        r_ts = datetime.now(timezone.utc) - timedelta(seconds=10)
        peer_sdir = _sdir(repo, "peer13b")
        # C7b: both sides write through the record seam. Step 1's legacy
        # self-read and AC21's transitional union are gone, so a
        # `touched.txt` fixture is no longer visible to `compute_scope` at
        # all -- writing one here would make this test assert the reader's
        # blindness rather than AC13(b)'s challenger rule.
        _write_touch_record(
            peer_sdir / scope._TOUCH_RECORD_FILENAME,
            session_id="peer13b",
            entries=[
                ("T", "publish.py", (r_ts - timedelta(seconds=20)).timestamp()),
                ("R", "publish.py", r_ts.timestamp()),
            ],
        )

        challenger_ts = r_ts + timedelta(seconds=5)  # post-dates the peer's R
        _write_touch_record(
            mine_sdir / scope._TOUCH_RECORD_FILENAME,
            session_id="mine13b",
            entries=[("T", "publish.py", challenger_ts.timestamp())],
        )
        (repo / "publish.py").write_text("z")  # dirty now

        result = scope.compute_scope("mine13b", cwd=str(repo))
        assert "publish.py" in result.my_scope
        assert not any(p == "publish.py" for p, _owner in result.skipped)


# ---------------------------------------------------------------------------
# C1 :: release_committed_claims — C2 pins AC2, AC4, AC7, AC8 at the helper
# layer, against a real git repo (not the pure-projection unit level above).
# ---------------------------------------------------------------------------


class TestReleaseCommittedClaims:
    """Existing assertions elsewhere in this file encode incident history
    (AC5) -- if any of THOSE fail because of this change, C1 is wrong; this
    class only pins the NEW `release_committed_claims` helper itself."""

    def test_clean_committed_path_is_released(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("s-rel1", cwd=str(repo))
        (repo / "foo.py").write_text("x")
        scope.touch("s-rel1", "foo.py", cwd=str(repo))
        subprocess.run(["git", "add", "foo.py"], cwd=repo, check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-q", "-m", "commit foo"], cwd=repo, check=True,
            **no_console_passthrough_kwargs(),
        )

        scope.release_committed_claims("s-rel1", ["foo.py"], cwd=str(repo))

        record = _sdir(repo, "s-rel1") / "touch-record.jsonl"
        events = _decode_events(record)
        assert len(events) == 2
        assert (events[1].verb, events[1].path) == (touch_record.VERB_RELEASE, "foo.py")
        projection = touch_record.project_live_claims(record, cwd=str(repo))
        assert "foo.py" not in projection.claims

    def test_dirty_uncommitted_path_still_releases(self, tmp_path):
        """PM ruling 2026-08-26: the cleanliness term is deleted, not
        weakened -- release no longer checks the worktree at all. A path
        never committed, still dirty, still releases if the caller names
        it: the caller-named-it-plus-still-T-claimed test is the whole
        contract now (see `release_committed_claims`'s own docstring for
        the overrule). This replaces the old AC2 "still dirty -> retained"
        pin, which encoded the retired rule."""
        repo = _make_repo(tmp_path)
        core.init("s-rel2", cwd=str(repo))
        (repo / "bar.py").write_text("x")
        scope.touch("s-rel2", "bar.py", cwd=str(repo))
        # Never committed -- irrelevant now, release does not consult git.

        scope.release_committed_claims("s-rel2", ["bar.py"], cwd=str(repo))

        record = _sdir(repo, "s-rel2") / "touch-record.jsonl"
        events = _decode_events(record)
        assert len(events) == 2
        assert (events[1].verb, events[1].path) == (touch_record.VERB_RELEASE, "bar.py")
        projection = touch_record.project_live_claims(record, cwd=str(repo))
        assert "bar.py" not in projection.claims

    def test_ac4_committed_in_earlier_hunk_with_further_uncommitted_edit_still_releases(
        self, tmp_path
    ):
        """Former AC4 shape: a path already committed once (has git
        history) that STILL carries a further, currently-uncommitted edit
        at call time. Under the PM ruling this is no longer retained --
        the caller named it, it is still T-claimed, so it releases; the
        further uncommitted edit is a fact about the worktree this
        function no longer consults. Rewritten from the retired
        retain-on-dirty pin rather than dropped, since the fixture (a
        committed path with a LATER uncommitted edit) is still a real
        situation worth coverage under the new rule."""
        repo = _make_repo(tmp_path)
        core.init("s-rel4", cwd=str(repo))
        (repo / "hunked.py").write_text("v1")
        scope.touch("s-rel4", "hunked.py", cwd=str(repo))
        subprocess.run(["git", "add", "hunked.py"], cwd=repo, check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-q", "-m", "first hunk"], cwd=repo, check=True,
            **no_console_passthrough_kwargs(),
        )
        # A second, uncommitted edit lands on the SAME already-committed path.
        (repo / "hunked.py").write_text("v2")

        scope.release_committed_claims("s-rel4", ["hunked.py"], cwd=str(repo))

        record = _sdir(repo, "s-rel4") / "touch-record.jsonl"
        events = _decode_events(record)
        assert len(events) == 2
        assert (events[1].verb, events[1].path) == (
            touch_record.VERB_RELEASE,
            "hunked.py",
        )
        projection = touch_record.project_live_claims(record, cwd=str(repo))
        assert "hunked.py" not in projection.claims

    def test_ac7_unreadable_touch_record_leaves_file_byte_identical(
        self, tmp_path, monkeypatch
    ):
        """C4: session-side release now reads the ``touch-record.jsonl``
        sink via ``touch_record._read_stream_claims`` (``Path.read_bytes``),
        not the retired ``touched.txt``/``Path.read_text`` seam -- an
        unreadable sink must still leave it byte-identical (fail-safe
        RETAIN)."""
        repo = _make_repo(tmp_path)
        core.init("s-rel7a", cwd=str(repo))
        (repo / "clean.py").write_text("x")
        scope.touch("s-rel7a", "clean.py", cwd=str(repo))
        subprocess.run(["git", "add", "clean.py"], cwd=repo, check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-q", "-m", "commit clean"], cwd=repo, check=True,
            **no_console_passthrough_kwargs(),
        )

        record = _sdir(repo, "s-rel7a") / "touch-record.jsonl"
        before = record.read_bytes()

        orig_read_bytes = Path.read_bytes

        def _boom(self, *a, **k):
            if self == record:
                raise OSError("simulated read failure")
            return orig_read_bytes(self, *a, **k)

        monkeypatch.setattr(Path, "read_bytes", _boom)
        scope.release_committed_claims("s-rel7a", ["clean.py"], cwd=str(repo))
        monkeypatch.undo()

        assert record.read_bytes() == before

    # RETIRED (PM ruling 2026-08-26): `release_committed_claims` no longer
    # calls git at all -- the cleanliness term this test pinned a git
    # failure fail-safe for is deleted, not weakened. There is no longer
    # a git call on this path to fail; `test_ac7_unreadable_touch_record_
    # leaves_file_byte_identical` above already pins this function's ONLY
    # remaining fail-safe surface (an unreadable touch-record sink).

    def test_ac8_path_redirtied_between_commit_and_release_still_releases(
        self, tmp_path
    ):
        """Former AC8 shape: a path released once, re-dirtied, then
        re-claimed with a fresh `T`, then a SECOND release call while the
        worktree is still dirty. Under the retired rule the second call
        was required to retain (worktree cleanliness gated it); under the
        PM ruling there is no worktree check left to gate on -- the second
        call sees a caller-named path that is still T-claimed in-process
        and releases it, dirty worktree or not. Rewritten rather than
        dropped: the redirty-then-reclaim-then-release-again fixture is
        still the real sequence worth pinning, just with the opposite
        outcome."""
        repo = _make_repo(tmp_path)
        core.init("s-rel8", cwd=str(repo))
        (repo / "cycle.py").write_text("v1")
        scope.touch("s-rel8", "cycle.py", cwd=str(repo))
        subprocess.run(["git", "add", "cycle.py"], cwd=repo, check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-q", "-m", "commit cycle"], cwd=repo, check=True,
            **no_console_passthrough_kwargs(),
        )

        scope.release_committed_claims("s-rel8", ["cycle.py"], cwd=str(repo))
        record = _sdir(repo, "s-rel8") / "touch-record.jsonl"
        after_first = _decode_events(record)
        assert len(after_first) == 2
        assert after_first[1].verb == touch_record.VERB_RELEASE

        # Re-dirty AFTER the release, then re-claim it (mirrors touch()'s
        # own AC8: last event R -> a fresh edit gets a new T).
        (repo / "cycle.py").write_text("v2")
        scope.touch("s-rel8", "cycle.py", cwd=str(repo))
        after_touch = _decode_events(record)
        assert len(after_touch) == 3  # fresh T appended post-release
        projection = touch_record.project_live_claims(record, cwd=str(repo))
        assert "cycle.py" in projection.claims

        # Release again while still dirty -- releases anyway; no
        # worktree-cleanliness term left to retain on.
        scope.release_committed_claims("s-rel8", ["cycle.py"], cwd=str(repo))

        after_second = _decode_events(record)
        assert len(after_second) == 4
        assert after_second[3].verb == touch_record.VERB_RELEASE
        projection2 = touch_record.project_live_claims(record, cwd=str(repo))
        assert "cycle.py" not in projection2.claims

    def test_renamed_path_is_releasable(self, tmp_path):
        """Review: code-reviewer Finding 3 -- a real `git mv` + commit must
        release cleanly through the RENAMED (new) path. Retained post PM
        ruling 2026-08-26 as a plain releasability pin: there is no more
        worktree-cleanliness check to spuriously fail via the old name --
        the rename shape is still worth coverage as ordinary caller-named-
        path-plus-still-T-claimed behaviour."""
        repo = _make_repo(tmp_path)
        core.init("s-rename", cwd=str(repo))
        (repo / "old_name.py").write_text("x")
        scope.touch("s-rename", "old_name.py", cwd=str(repo))
        subprocess.run(["git", "add", "old_name.py"], cwd=repo, check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-q", "-m", "commit old_name"], cwd=repo, check=True,
            **no_console_passthrough_kwargs(),
        )

        subprocess.run(
            ["git", "mv", "old_name.py", "new_name.py"], cwd=repo, check=True,
            **no_console_passthrough_kwargs(),
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "rename to new_name"], cwd=repo, check=True,
            **no_console_passthrough_kwargs(),
        )
        # Re-touch under the new name -- a rename is a fresh file identity
        # from touched.txt's own perspective; nothing renames the entry.
        scope.touch("s-rename", "new_name.py", cwd=str(repo))

        scope.release_committed_claims("s-rename", ["new_name.py"], cwd=str(repo))

        record = _sdir(repo, "s-rename") / "touch-record.jsonl"
        projection = touch_record.project_live_claims(record, cwd=str(repo))
        assert "new_name.py" not in projection.claims

    # RETIRED (PM ruling 2026-08-26): `_porcelain_dirty_paths` is deleted --
    # `release_committed_claims` no longer parses `git status --porcelain`
    # output at all, so there is no unparseable-line fail-safe left to pin
    # here. `test_ac7_unreadable_touch_record_leaves_file_byte_identical`
    # above pins this function's one remaining fail-safe surface.

    def test_peer_agent_dir_not_back_pointed_at_sid_is_untouched(self, tmp_path):
        """The worst failure this helper could have: silently pruning a
        peer's record. A peer's .agents dir, back-pointed at a DIFFERENT
        session, must never be touched by a release call for this sid --
        this function is structurally incapable of releasing a peer's
        claim (C5, docs/plans/2026-08-13-claim-release-deadlock-and-the-
        doctrine-that-rejects-it.md, deleted B1's stale-peer release step
        outright); "peer-owner" here pins that negative case."""
        repo = _make_repo(tmp_path)
        core.init("s-releaser", cwd=str(repo))
        core.init("peer-owner", cwd=str(repo))

        (repo / "shared.py").write_text("x")
        scope.touch("s-releaser", "shared.py", cwd=str(repo))
        subprocess.run(["git", "add", "shared.py"], cwd=repo, check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-q", "-m", "commit shared"], cwd=repo, check=True,
            **no_console_passthrough_kwargs(),
        )

        base = Path(core.sessions_dir(cwd=str(repo)))
        peer_agent_dir = base / ".agents" / "agent-peer"
        peer_agent_dir.mkdir(parents=True)
        (peer_agent_dir / "em-session-id.txt").write_text(
            "peer-owner\n", encoding="utf-8"
        )
        peer_touched = peer_agent_dir / "touched.txt"
        peer_touched.write_text("shared.py\n", encoding="utf-8")
        before = peer_touched.read_bytes()

        scope.release_committed_claims("s-releaser", ["shared.py"], cwd=str(repo))

        assert peer_touched.read_bytes() == before

    def test_ac10_no_op_release_performs_no_write(self, tmp_path):
        """AC10 survives the PM ruling under a different fixture: the old
        no-op trigger was "never committed, stays dirty" -- that no longer
        applies since cleanliness is no longer consulted. The genuinely
        no-op case now is a path this session never claimed (never a `T`
        in its own record), so `_release_from_touch_record`'s `claimed ∩
        release_set` intersection is empty and nothing is written."""
        repo = _make_repo(tmp_path)
        core.init("s-rel10", cwd=str(repo))
        (repo / "untouched.py").write_text("x")
        scope.touch("s-rel10", "other.py", cwd=str(repo))

        record = _sdir(repo, "s-rel10") / "touch-record.jsonl"
        mtime_before = record.stat().st_mtime_ns

        scope.release_committed_claims("s-rel10", ["untouched.py"], cwd=str(repo))
        assert record.stat().st_mtime_ns == mtime_before


class TestCrossDialectClaimCancellation:
    """C1 (docs/plans/2026-08-11-claim-release-and-the-gate-that-cannot-
    clear.md), A2/A3 -- a release written under one path dialect (backslashed
    or forward-slashed relative pathspec) must cancel a claim written under
    the OTHER dialect. Asserted through claim_index's own lookup (the real
    consumer of both writers' output), never by comparing normalized strings
    directly -- a string-equality pin alone does not exercise the actual
    claim-index read path a commit-gate check goes through."""

    def test_release_under_forward_slash_cancels_claim_under_backslash(
        self, tmp_path
    ):
        from coordinator_core.session import claim_index

        repo = _make_repo(tmp_path)
        core.init("s-xd1", cwd=str(repo))
        (repo / "state").mkdir()
        (repo / "state" / "x.md").write_text("y")

        # Claim written under the backslashed relative dialect.
        scope.touch("s-xd1", "state\\x.md", cwd=str(repo))
        sessions_dir = core.sessions_dir(cwd=str(repo))
        before = claim_index.lookup(["state/x.md"], sessions_dir=sessions_dir)
        assert before == {"state/x.md": ["s-xd1"]}

        # Commit it so release_committed_claims sees it clean, then release
        # under the forward-slashed dialect via the real release helper.
        subprocess.run(["git", "add", "state/x.md"], cwd=repo, check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-q", "-m", "commit state/x.md"], cwd=repo, check=True,
            **no_console_passthrough_kwargs(),
        )
        scope.release_committed_claims("s-xd1", ["state/x.md"], cwd=str(repo))

        after = claim_index.lookup(["state/x.md"], sessions_dir=sessions_dir)
        assert after == {"state/x.md": []}

    def test_release_under_backslash_cancels_claim_under_forward_slash(
        self, tmp_path
    ):
        """Reverse direction: a claim written forward-slashed, released by
        passing the BACKSLASHED pathspec straight into
        ``release_committed_claims`` -- the real release-side call shape,
        now that its own ``requested``/``clean`` set-diff canonicalizes both
        sides before comparing (C1 follow-up)."""
        from coordinator_core.session import claim_index

        repo = _make_repo(tmp_path)
        core.init("s-xd2", cwd=str(repo))
        (repo / "state").mkdir()
        (repo / "state" / "y.md").write_text("y")

        # Claim written under the forward-slashed dialect.
        scope.touch("s-xd2", "state/y.md", cwd=str(repo))
        sessions_dir = core.sessions_dir(cwd=str(repo))
        before = claim_index.lookup(["state\\y.md"], sessions_dir=sessions_dir)
        assert before == {"state\\y.md": ["s-xd2"]}

        subprocess.run(["git", "add", "state/y.md"], cwd=repo, check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-q", "-m", "commit state/y.md"], cwd=repo, check=True,
            **no_console_passthrough_kwargs(),
        )
        scope.release_committed_claims("s-xd2", ["state\\y.md"], cwd=str(repo))

        after = claim_index.lookup(["state/y.md"], sessions_dir=sessions_dir)
        assert after == {"state/y.md": []}


class TestBackslashedRelativePathspecCommitClearsClaimEndToEnd:
    """C1, A2/A3 -- end-to-end shape the plan explicitly requires: a claim
    written via a backslashed relative pathspec, committed, and released
    through release_committed_claims (the mechanism
    ops/ceremony/scoped_git_commit.py's real commit op calls post-commit)
    must clear -- read back through claim_index.lookup, the commit gate's
    own read path."""

    def test_backslashed_relative_pathspec_claim_clears_after_commit_and_release(
        self, tmp_path
    ):
        from coordinator_core.session import claim_index

        repo = _make_repo(tmp_path)
        core.init("s-e2e", cwd=str(repo))
        (repo / "pkg").mkdir()
        (repo / "pkg" / "mod.py").write_text("v1")

        scope.touch("s-e2e", "pkg\\mod.py", cwd=str(repo))
        sessions_dir = core.sessions_dir(cwd=str(repo))
        assert claim_index.lookup(["pkg/mod.py"], sessions_dir=sessions_dir) == {
            "pkg/mod.py": ["s-e2e"]
        }

        subprocess.run(["git", "add", "pkg/mod.py"], cwd=repo, check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-q", "-m", "commit pkg/mod.py"], cwd=repo, check=True,
            **no_console_passthrough_kwargs(),
        )
        scope.release_committed_claims("s-e2e", ["pkg\\mod.py"], cwd=str(repo))

        assert claim_index.lookup(["pkg/mod.py"], sessions_dir=sessions_dir) == {
            "pkg/mod.py": []
        }

    def test_forward_slashed_relative_pathspec_claim_clears_after_commit_and_release(
        self, tmp_path
    ):
        """Sibling pin for the forward-slashed dialect, alongside the
        backslashed case above -- both dialects pinned end-to-end through
        the same claim -> commit -> release_committed_claims -> claim_index
        path."""
        from coordinator_core.session import claim_index

        repo = _make_repo(tmp_path)
        core.init("s-e2e-fwd", cwd=str(repo))
        (repo / "pkg2").mkdir()
        (repo / "pkg2" / "mod.py").write_text("v1")

        scope.touch("s-e2e-fwd", "pkg2/mod.py", cwd=str(repo))
        sessions_dir = core.sessions_dir(cwd=str(repo))
        assert claim_index.lookup(["pkg2/mod.py"], sessions_dir=sessions_dir) == {
            "pkg2/mod.py": ["s-e2e-fwd"]
        }

        subprocess.run(["git", "add", "pkg2/mod.py"], cwd=repo, check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-q", "-m", "commit pkg2/mod.py"], cwd=repo, check=True,
            **no_console_passthrough_kwargs(),
        )
        scope.release_committed_claims("s-e2e-fwd", ["pkg2/mod.py"], cwd=str(repo))

        assert claim_index.lookup(["pkg2/mod.py"], sessions_dir=sessions_dir) == {
            "pkg2/mod.py": []
        }


# RETIRED (PM ruling 2026-08-26): `TestReleaseCommittedClaimsArgvBounded::
# test_status_argv_stays_bounded_at_scale` pinned that the chunked `git
# status --porcelain` argv this function used to build stayed under the
# Windows 32767-char command-line limit at multi-thousand-path scale. With
# the cleanliness term deleted, `release_committed_claims` issues no
# `status` call and builds no such argv at all -- the protection this test
# pinned is moot, not merely untested. Checked for any OTHER large argv
# `release_committed_claims` might still build: the function's remaining
# work per call is (1) one touch-record read/append per sink via
# `_release_from_touch_record` (no subprocess), and (2) an `os.scandir` walk
# of `.agents/*` reading one line of `em-session-id.txt` per dir (no
# subprocess, no argv). Neither builds a path-list argv, so there is nothing
# left in this function for an argv-bound test to cover; the class and test
# are deleted rather than rewritten against a call shape that no longer
# exists. `_tracked_at_head`/`_staged_in_index` (this module, further down)
# still build chunked git argvs via `_chunk_paths` for `release_phantom_
# claims`'s separate discriminator -- unaffected by this ruling and out of
# this test's original scope (it named `release_committed_claims`
# specifically).

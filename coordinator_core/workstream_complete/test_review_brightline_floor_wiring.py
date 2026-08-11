"""
coordinator_core.workstream_complete.test_review_brightline_floor_wiring —
production-caller-side wiring for `directives_review.
build_review_brightline_gate_directive`'s four range-floor kwargs
(`trail_records`/`chain_tip_sha`/`is_ancestor`/`session_start_sha`).

Spec backlink: docs/plans/2026-08-08-the-second-close-re-measures-the-first-c.md.
The predecessor session in this plan's chain landed the builder-side
capability (`directives_review.build_review_brightline_gate_directive`'s
four optional kwargs, delegating to its own `resolve_mid_chain_review_
scope`) with NO producer — AC1 was left deliberately open. This file covers
the producer: `coordinator_core.workstream_complete.
_resolve_review_brightline_floor_kwargs` (the trail-record fetch + git
resolution) and `build_directives`'s new `session_start_time` kwarg that
carries it into `directives_review`'s call site, both wired through
`brief()`.

Uses real git repos + the REAL `coordinator_core.ops.review_brightline_gate`
CLI module (AC4's "prove composition by running it, not by reading it") —
`review_brightline_gate.main()` shells out to `git` against the PROCESS cwd
(it has no `repo_root` parameter of its own), so the AC4 tests
`monkeypatch.chdir` into the temp repo rather than mocking git.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import coordinator_core.workstream_complete as wsc
from coordinator_core.ops import review_brightline_gate
from coordinator_core.ops.ceremony.wsc_disposition import SINGLE_SESSION

import pytest

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.spawns_process]

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_PRE_SESSION_COMMIT_DATE = "2000-01-01T00:00:00+00:00"
_SID = "4839fcc4-7544-4ca6-bb5f-2cf0977e4620"


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, **_NO_CONSOLE)


def _commit(cwd: Path, message: str, filename: str = "a.py", content: str = "x = 1\n", when: str | None = None) -> str:
    (cwd / filename).write_text(content, encoding="utf-8")
    _run_git(["add", filename], cwd)
    env = dict(os.environ)
    if when is not None:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=str(cwd), check=True, capture_output=True, env=env, **_NO_CONSOLE,
    )
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(cwd), check=True, capture_output=True, text=True, **_NO_CONSOLE
    )
    return out.stdout.strip()


def _init_repo(root: Path) -> None:
    _run_git(["init", "-q"], root)
    _run_git(["config", "user.email", "t@example.com"], root)
    _run_git(["config", "user.name", "t"], root)
    _commit(root, "init", when=_PRE_SESSION_COMMIT_DATE)


def _write_trail_record(root: Path, name: str, session_id: str, sha_range: str) -> Path:
    """Real on-disk record shape (verified against `state/review-trail/`
    live records) — `sha_range`, never `sha_range_head`/`head`."""
    trail_dir = root / "state" / "review-trail"
    trail_dir.mkdir(parents=True, exist_ok=True)
    path = trail_dir / name
    path.write_text(
        json.dumps(
            {
                "sha_range": sha_range,
                "reviewer": "code-reviewer",
                "scope": "session",
                "scope_kind": "diff",
                "verdict": "single-reviewer-ok",
                "diff_loc": 10,
                "session_id": session_id,
                "workstream": None,
                "reviewed_paths": None,
            }
        ),
        encoding="utf-8",
    )
    return path


def _gate(sid: str = _SID) -> wsc.SessionShapeGate:
    return wsc.SessionShapeGate(
        sid=sid, disposition=SINGLE_SESSION, consumed_handoff="",
        diagnostics=[], consumed_handoff_paths=(),
    )


def _brightline_directive(directives: list[dict]) -> dict:
    return next(d for d in directives if d["id"] == "d-run-review-brightline-gate")


# ---------------------------------------------------------------------------
# AC2 — the no-prior-record path (nearly every close) must stay byte-
# identical. Written first per the dispatch brief's own instruction.
# ---------------------------------------------------------------------------


def test_session_start_time_omitted_reproduces_todays_call(tmp_path):
    """Every EXISTING caller (including every pre-existing test in
    `test_workstream_complete.py`) omits `session_start_time` — must
    reproduce today's exact two-element argv with zero new I/O."""
    directives = wsc.build_directives(_gate(), {}, tmp_path)
    assert _brightline_directive(directives)["args"] == ["--session-id", _SID]


def test_no_prior_trail_record_emits_byte_identical_argv(monkeypatch, tmp_path):
    """A resolvable `session_start_time` but zero own trail records — the
    ordinary single-close case — must still emit the plain two-element
    argv, not an empty-but-present range."""
    _init_repo(tmp_path)
    # No trail-record files written under tmp_path/state/review-trail — the
    # real (root-honouring) scan naturally finds nothing, no monkeypatch
    # needed. Review: code-reviewer (P2 #1).
    session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    directives = wsc.build_directives(_gate(), {}, tmp_path, session_start_time=session_start_time)
    assert _brightline_directive(directives)["args"] == ["--session-id", _SID]


def test_peer_session_record_never_floors_this_session(monkeypatch, tmp_path):
    """A trail record belonging to a DIFFERENT session_id must never
    contribute a floor — the plan's Anti-scope forbids widening/shifting
    this session's range over a peer's reviewed span."""
    _init_repo(tmp_path)
    first_sha = _commit(tmp_path, "close 1", filename="a1.py", content="a=1\n")
    _write_trail_record(tmp_path, "2026-08-08-peer.json", "some-other-session-id", f"{first_sha}^..{first_sha}")
    session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    directives = wsc.build_directives(_gate(), {}, tmp_path, session_start_time=session_start_time)
    assert _brightline_directive(directives)["args"] == ["--session-id", _SID]


def test_unresolvable_session_start_sha_falls_back_to_todays_call(monkeypatch, tmp_path):
    """Own trail record(s) exist, but `session_start_sha` cannot be
    resolved (no git repo at all here) — a resolution FAILURE, not the
    zero-records AC2 path, but must fail open to the same plain call, never
    a guessed floor."""
    _write_trail_record(tmp_path, "2026-08-08-rec.json", _SID, "aaaaaaa^..aaaaaaa")
    session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    directives = wsc.build_directives(_gate(), {}, tmp_path, session_start_time=session_start_time)
    assert _brightline_directive(directives)["args"] == ["--session-id", _SID]


# ---------------------------------------------------------------------------
# AC1/AC5 — a prior trail record floors the range; two records in one
# session floor at the LAST (most recent) one, reproducing the live
# incident shape (this session's own second close re-measuring the first
# close's diff).
# ---------------------------------------------------------------------------


def test_prior_own_record_floors_the_range(monkeypatch, tmp_path):
    _init_repo(tmp_path)
    session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    first_sha = _commit(tmp_path, "first close\n\nSession-Id: %s" % _SID, filename="a1.py", content="a=1\n")
    _commit(tmp_path, "second close\n\nSession-Id: %s" % _SID, filename="a2.py", content="a=2\n")

    _write_trail_record(tmp_path, "2026-08-08-000001-rec.json", _SID, f"{first_sha}^..{first_sha}")

    directives = wsc.build_directives(_gate(), {}, tmp_path, session_start_time=session_start_time)
    assert _brightline_directive(directives)["args"] == ["--session-id", _SID, f"{first_sha}..HEAD"]


def test_two_trail_records_floors_at_the_last_not_the_first(monkeypatch, tmp_path):
    """The live failure shape this plan's Problem section reconstructs:
    this session closed twice already; the THIRD close's gate must be
    floored at the SECOND close's reviewed tip, not the first — a floor at
    the first would still re-measure the second close's own diff."""
    _init_repo(tmp_path)
    session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    close1_sha = _commit(tmp_path, "close 1 commit", filename="a1.py", content="a=1\n")
    close2_sha = _commit(tmp_path, "close 2 commit", filename="a2.py", content="a=2\n")
    _commit(tmp_path, "close 3 commit (under test)", filename="a3.py", content="a=3\n")

    _write_trail_record(tmp_path, "2026-08-08-000001-rec1.json", _SID, f"{close1_sha}^..{close1_sha}")
    _write_trail_record(tmp_path, "2026-08-08-000002-rec2.json", _SID, f"{close2_sha}^..{close2_sha}")

    directives = wsc.build_directives(_gate(), {}, tmp_path, session_start_time=session_start_time)
    assert _brightline_directive(directives)["args"] == ["--session-id", _SID, f"{close2_sha}..HEAD"]


def test_untrustworthy_record_tip_omitted_falls_back_to_session_start_sha(monkeypatch, tmp_path):
    """A record whose `sha_range` ends in a live `..HEAD` (never terminated
    at a concrete sha) cannot be trusted as a floor — omitted, same as
    `resolve_mid_chain_review_scope`'s own `if not head: continue` reads
    it, so the floor falls through to `session_start_sha` instead of a
    stale/re-resolving `HEAD`."""
    _init_repo(tmp_path)
    session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    _commit(tmp_path, "session commit", filename="a1.py", content="a=1\n")
    _write_trail_record(tmp_path, "2026-08-08-000001-rec.json", _SID, "aaaaaaa..HEAD")

    directives = wsc.build_directives(_gate(), {}, tmp_path, session_start_time=session_start_time)
    args = _brightline_directive(directives)["args"]
    assert args[:2] == ["--session-id", _SID]
    assert len(args) == 3
    assert args[2] != "HEAD..HEAD"  # a real base was resolved, not the degenerate empty-range fallback


def test_root_honoured_even_when_cwd_differs(monkeypatch, tmp_path):
    """Review: code-reviewer (P2 #1) — `_resolve_review_brightline_floor_
    kwargs` must scan `root`'s own `state/review-trail/`, never the
    process cwd's. Chdir into an UNRELATED directory (with its own,
    different `state/review-trail/` record for the same session id) and
    confirm the floor still comes from `tmp_path` (the explicit `root`),
    not from cwd — the class of bug six other tests in this file
    previously could not see because they monkeypatched `list_paths`
    directly, bypassing both cwd resolution and the dropped `root` arg."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    first_sha = _commit(repo, "first close\n\nSession-Id: %s" % _SID, filename="a1.py", content="a=1\n")
    _commit(repo, "second close\n\nSession-Id: %s" % _SID, filename="a2.py", content="a=2\n")
    _write_trail_record(repo, "2026-08-08-000001-rec.json", _SID, f"{first_sha}^..{first_sha}")

    other_cwd = tmp_path / "unrelated-cwd"
    other_cwd.mkdir()
    _write_trail_record(other_cwd, "2026-08-08-000001-decoy.json", _SID, "deadbee^..deadbee")
    monkeypatch.chdir(other_cwd)

    directives = wsc.build_directives(_gate(), {}, repo, session_start_time=session_start_time)
    assert _brightline_directive(directives)["args"] == ["--session-id", _SID, f"{first_sha}..HEAD"]


def test_own_records_present_zero_commits_since_start_falls_back_to_todays_call(tmp_path):
    """Review: code-reviewer (P2 #2) — own trail record(s) exist, but ZERO
    commits have landed since `session_start_time` (a trail record written
    to disk before this session's own commit lands, or a clock-skew edge).
    `_resolve_session_start_sha` must not return the literal `"HEAD"` here
    — that value previously flowed through to the emitted argv as a
    well-formed but EMPTY `HEAD..HEAD` range instead of falling back to
    today's plain two-element call. `test_untrustworthy_record_tip_
    omitted_falls_back_to_session_start_sha` only covers the case where a
    real commit already landed; this covers the zero-commits combination
    that test does not."""
    _init_repo(tmp_path)
    _write_trail_record(tmp_path, "2026-08-08-000001-rec.json", _SID, "aaaaaaa^..aaaaaaa")
    # session_start_time is in the FUTURE relative to the repo's only
    # (init) commit, so `git log --since=<future>` finds zero commits.
    session_start_time = datetime.now(timezone.utc) + timedelta(hours=1)

    directives = wsc.build_directives(_gate(), {}, tmp_path, session_start_time=session_start_time)
    args = _brightline_directive(directives)["args"]
    assert args == ["--session-id", _SID]
    assert "HEAD..HEAD" not in args


# ---------------------------------------------------------------------------
# AC4 — composition with `aff5b6efd`'s session-aware floor retry, proven by
# actually running `coordinator_core.ops.review_brightline_gate.main()`,
# not by reasoning about it.
# ---------------------------------------------------------------------------


def test_caller_floor_with_zero_trailer_matches_still_retries_session_floor(monkeypatch, tmp_path, capsys):
    """A caller-supplied floor whose `floor..HEAD` range matches zero of
    this session's trailer-tagged commits (this session's own commit sits
    BEFORE the floor, e.g. because the trail record's tip already advanced
    past it) must still fall through to `_resolve_session_floor`'s
    unbounded retry — the exact composition `aff5b6efd` added."""
    _init_repo(tmp_path)
    session_a = _commit(tmp_path, "session commit\n\nSession-Id: %s" % _SID, filename="a1.py", content="a=1\n")
    floor_sha = _commit(tmp_path, "floor commit (no trailer)", filename="a2.py", content="a=2\n")
    assert floor_sha != session_a

    monkeypatch.chdir(tmp_path)
    rc = review_brightline_gate.main(["--session-id", _SID, f"{floor_sha}..HEAD"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "recovered via session-aware floor" in captured.err
    assert "VERDICT=indeterminate" not in captured.out


def test_genuinely_no_commits_still_resolves_indeterminate(monkeypatch, tmp_path, capsys):
    """A session with NO commits anywhere reachable from HEAD carrying its
    trailer — the session-aware floor retry itself turns up nothing — must
    resolve `VERDICT=indeterminate`, never a manufactured `single-reviewer-
    ok` or a forced partition (the `aff5b6efd` fix this composition must
    not regress)."""
    _init_repo(tmp_path)
    head_sha = _commit(tmp_path, "unrelated commit, no trailer", filename="a1.py", content="a=1\n")

    monkeypatch.chdir(tmp_path)
    # An explicit range, not the bare `[--session-id <id>]` form: this repo
    # has no `origin/main` to fall back to, and that resolution failure
    # (rc=1, "cannot resolve origin/main") is a DIFFERENT, uninteresting
    # failure mode this test does not exercise — `_resolve_range`'s own
    # positional-range branch is exercised elsewhere.
    rc = review_brightline_gate.main(["--session-id", "session-with-zero-commits", f"{head_sha}..HEAD"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "VERDICT=indeterminate" in captured.out

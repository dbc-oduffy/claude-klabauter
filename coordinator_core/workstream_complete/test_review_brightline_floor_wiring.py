
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

# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

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


def _write_trail_record(
    root: Path,
    name: str,
    session_id: str,
    sha_range: str,
    name_sid: str | None = None,
) -> Path:
    """Real on-disk record shape (verified against `state/review-trail/`
    live records) — `sha_range`, never `sha_range_head`/`head`.

    Real record FILENAMES also carry their own `session_id[:8]`:
    `review_trail_write` names every record `{TIMESTAMP}-{SESSION_ID[:8]}.json`,
    and `_list_review_trail_paths_for_root` pre-filters on that at the
    `os.scandir` name level BEFORE opening anything. Audited over the live
    corpus: 4710 of 4711 records with a `session_id` carry it in the filename
    (the one exception, `2026-08-01T175000Z-mise-close-origin-stub.json`, was
    hand-written).

    This helper therefore INSERTS `-{session_id[:8]}` before `.json` when the
    supplied `name` lacks it. Without that, every fixture here was invisible to
    the name filter, `_resolve_review_brightline_floor_kwargs` bailed to `None`,
    and six tests in this file compared a floored range against an unfloored
    one. Deriving the suffix here rather than spelling it into each call site is
    deliberate: a hand-written name is what drifted from the producer's
    convention in the first place.

    `name_sid` overrides the suffix independently of `session_id` — for the
    peer-record case, which needs a filename that PASSES the name filter while
    its `session_id` field still fails the exact-match check. That is the only
    way to exercise the field check at all; a peer record whose name also fails
    the filter is excluded one stage too early and proves nothing about it.
    """
    trail_dir = root / "state" / "review-trail"
    trail_dir.mkdir(parents=True, exist_ok=True)
    suffix = (name_sid if name_sid is not None else session_id)[:8]
    if suffix and f"-{suffix}" not in name:
        name = f"{name[: -len('.json')] if name.endswith('.json') else name}-{suffix}.json"
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


def _head_sha(cwd: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(cwd), check=True, capture_output=True, text=True, **_NO_CONSOLE
    )
    return out.stdout.strip()


def test_session_start_time_omitted_reproduces_todays_call(tmp_path):
    """Every EXISTING caller (including every pre-existing test in
    `test_workstream_complete.py`) omits `session_start_time` — must
    reproduce today's exact two-element argv with zero new I/O."""
    directives = wsc.build_directives(_gate(), {}, tmp_path)
    assert _brightline_directive(directives)["args"] == ["--session-id", _SID]


def test_no_prior_trail_record_emits_byte_identical_argv(monkeypatch, tmp_path):
    _init_repo(tmp_path)
    session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    directives = wsc.build_directives(_gate(), {}, tmp_path, session_start_time=session_start_time)
    assert _brightline_directive(directives)["args"] == ["--session-id", _SID]


def test_peer_session_record_never_floors_this_session(monkeypatch, tmp_path):
    """A trail record belonging to a DIFFERENT session_id must never
    contribute a floor — the plan's Anti-scope forbids widening/shifting
    this session's range over a peer's reviewed span.

    The peer's FILENAME deliberately carries THIS session's `sid[:8]`
    (`name_sid=_SID`) while its `session_id` FIELD is a peer's. That is the
    8-char-collision case, and it is the only shape that actually tests the
    exact-match field check in `_resolve_review_brightline_floor_kwargs`:
    a peer record named after its own session is thrown out one stage earlier,
    by the `os.scandir` name filter, so it would pass this test even if the
    field check were deleted. Before this was fixed the test was doing exactly
    that — passing for the wrong reason.
    """
    _init_repo(tmp_path)
    first_sha = _commit(tmp_path, "close 1", filename="a1.py", content="a=1\n")
    _write_trail_record(
        tmp_path,
        "2026-08-08-000001-peer.json",
        "some-other-session-id",
        f"{first_sha}^..{first_sha}",
        name_sid=_SID,
    )
    assert any(
        "peer" in p for p in wsc._list_review_trail_paths_for_root(tmp_path, sid_short=_SID[:8])
    ), "peer record must survive the name filter so the session_id field check is what excludes it"

    session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    directives = wsc.build_directives(_gate(), {}, tmp_path, session_start_time=session_start_time)
    assert _brightline_directive(directives)["args"] == ["--session-id", _SID]


def test_unresolvable_session_start_sha_falls_back_to_todays_call(monkeypatch, tmp_path):
    _write_trail_record(tmp_path, "2026-08-08-rec.json", _SID, "aaaaaaa^..aaaaaaa")
    session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    directives = wsc.build_directives(_gate(), {}, tmp_path, session_start_time=session_start_time)
    assert _brightline_directive(directives)["args"] == ["--session-id", _SID]


def test_prior_own_record_floors_the_range(monkeypatch, tmp_path):
    """2026-08-11: `chain_tip_sha` is now resolved to a CONCRETE sha
    (`_resolve_head_sha`), not the literal `"HEAD"` string — see
    `_resolve_review_brightline_floor_kwargs`'s own `chain_tip_sha`
    docstring paragraph. The tip half of the emitted range is this repo's
    real current HEAD sha at build time."""
    _init_repo(tmp_path)
    session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    first_sha = _commit(tmp_path, "first close\n\nSession-Id: %s" % _SID, filename="a1.py", content="a=1\n")
    second_sha = _commit(tmp_path, "second close\n\nSession-Id: %s" % _SID, filename="a2.py", content="a=2\n")
    assert second_sha == _head_sha(tmp_path)

    _write_trail_record(tmp_path, "2026-08-08-000001-rec.json", _SID, f"{first_sha}^..{first_sha}")

    directives = wsc.build_directives(_gate(), {}, tmp_path, session_start_time=session_start_time)
    assert _brightline_directive(directives)["args"] == ["--session-id", _SID, f"{first_sha}..{second_sha}"]


def test_two_trail_records_floors_at_the_last_not_the_first(monkeypatch, tmp_path):
    _init_repo(tmp_path)
    session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    close1_sha = _commit(tmp_path, "close 1 commit", filename="a1.py", content="a=1\n")
    close2_sha = _commit(tmp_path, "close 2 commit", filename="a2.py", content="a=2\n")
    close3_sha = _commit(tmp_path, "close 3 commit (under test)", filename="a3.py", content="a=3\n")
    assert close3_sha == _head_sha(tmp_path)

    _write_trail_record(tmp_path, "2026-08-08-000001-rec1.json", _SID, f"{close1_sha}^..{close1_sha}")
    _write_trail_record(tmp_path, "2026-08-08-000002-rec2.json", _SID, f"{close2_sha}^..{close2_sha}")

    directives = wsc.build_directives(_gate(), {}, tmp_path, session_start_time=session_start_time)
    assert _brightline_directive(directives)["args"] == ["--session-id", _SID, f"{close2_sha}..{close3_sha}"]


def test_untrustworthy_record_tip_omitted_falls_back_to_session_start_sha(monkeypatch, tmp_path):
    _init_repo(tmp_path)
    session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    _commit(tmp_path, "session commit", filename="a1.py", content="a=1\n")
    _write_trail_record(tmp_path, "2026-08-08-000001-rec.json", _SID, "aaaaaaa..HEAD")

    directives = wsc.build_directives(_gate(), {}, tmp_path, session_start_time=session_start_time)
    args = _brightline_directive(directives)["args"]
    assert args[:2] == ["--session-id", _SID]
    assert len(args) == 3
    assert args[2] != "HEAD..HEAD"


def test_root_honoured_even_when_cwd_differs(monkeypatch, tmp_path):
    """`_resolve_review_brightline_floor_
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

    repo_head_sha = _head_sha(repo)
    directives = wsc.build_directives(_gate(), {}, repo, session_start_time=session_start_time)
    assert _brightline_directive(directives)["args"] == ["--session-id", _SID, f"{first_sha}..{repo_head_sha}"]


def test_own_records_present_zero_commits_since_start_falls_back_to_todays_call(tmp_path):
    _init_repo(tmp_path)
    _write_trail_record(tmp_path, "2026-08-08-000001-rec.json", _SID, "aaaaaaa^..aaaaaaa")
    session_start_time = datetime.now(timezone.utc) + timedelta(hours=1)

    directives = wsc.build_directives(_gate(), {}, tmp_path, session_start_time=session_start_time)
    args = _brightline_directive(directives)["args"]
    assert args == ["--session-id", _SID]
    assert "HEAD..HEAD" not in args


def test_caller_floor_with_zero_trailer_matches_still_retries_session_floor(monkeypatch, tmp_path, capsys):
    _init_repo(tmp_path)
    session_a = _commit(tmp_path, "session commit\n\nSession-Id: %s" % _SID, filename="a1.py", content="a=1\n")
    floor_sha = _commit(tmp_path, "floor commit (no trailer)", filename="a2.py", content="a=2\n")
    assert floor_sha != session_a

    monkeypatch.chdir(tmp_path)
    rc = review_brightline_gate.main(["--session-id", _SID, f"{floor_sha}..HEAD"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "recovered via session-aware floor" in captured.err
    # the trailer-tagged commit, and REPORTED ON THE RETRY RANGE rather than
    assert f"range={session_a}^..HEAD" in captured.out
    assert "filtered_to=1" in captured.out


# `directives_review.record_gate_verdict_if_passed`'s KEY-STALENESS section.


def test_resolve_head_sha_returns_concrete_sha_for_a_real_repo(tmp_path):
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "a commit", filename="a1.py", content="a=1\n")
    assert wsc._resolve_head_sha(tmp_path) == sha


def test_resolve_head_sha_degrades_to_none_on_a_non_repo(tmp_path):
    assert wsc._resolve_head_sha(tmp_path) is None


def test_resolve_head_sha_degrades_to_none_on_subprocess_failure(monkeypatch, tmp_path):
    class _FakeCompletedProcess:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(
        wsc.subprocess, "run", lambda *a, **k: _FakeCompletedProcess()
    )
    assert wsc._resolve_head_sha(tmp_path) is None


def test_floor_path_falls_back_to_literal_head_when_tip_resolution_fails(monkeypatch, tmp_path):
    _init_repo(tmp_path)
    session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    first_sha = _commit(tmp_path, "first close\n\nSession-Id: %s" % _SID, filename="a1.py", content="a=1\n")
    _commit(tmp_path, "second close\n\nSession-Id: %s" % _SID, filename="a2.py", content="a=2\n")
    _write_trail_record(tmp_path, "2026-08-08-000001-rec.json", _SID, f"{first_sha}^..{first_sha}")

    monkeypatch.setattr(wsc, "_resolve_head_sha", lambda root: None)
    directives = wsc.build_directives(_gate(), {}, tmp_path, session_start_time=session_start_time)
    assert _brightline_directive(directives)["args"] == ["--session-id", _SID, f"{first_sha}..HEAD"]


def test_floor_resolved_concrete_tip_hits_the_gate_memo_on_a_second_pass(tmp_path):
    from coordinator_core.workstream_complete import directives_review

    _init_repo(tmp_path)
    session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    first_sha = _commit(tmp_path, "first close\n\nSession-Id: %s" % _SID, filename="a1.py", content="a=1\n")
    second_sha = _commit(tmp_path, "second close\n\nSession-Id: %s" % _SID, filename="a2.py", content="a=2\n")
    _write_trail_record(tmp_path, "2026-08-08-000001-rec.json", _SID, f"{first_sha}^..{first_sha}")

    directives = wsc.build_directives(_gate(), {}, tmp_path, session_start_time=session_start_time)
    directive = _brightline_directive(directives)
    assert directive["args"] == ["--session-id", _SID, f"{first_sha}..{second_sha}"]
    assert directive["already_satisfied"] is False

    directives_review.record_gate_verdict_if_passed(tmp_path, directive, exit_code=0, stdout="")

    directives_2 = wsc.build_directives(_gate(), {}, tmp_path, session_start_time=session_start_time)
    directive_2 = _brightline_directive(directives_2)
    assert directive_2["args"] == directive["args"]
    assert directive_2["already_satisfied"] is True


def test_genuinely_no_commits_still_resolves_indeterminate(monkeypatch, tmp_path, capsys):
    _init_repo(tmp_path)
    head_sha = _commit(tmp_path, "unrelated commit, no trailer", filename="a1.py", content="a=1\n")

    monkeypatch.chdir(tmp_path)
    # (rc=1, "cannot resolve origin/main") is a DIFFERENT, uninteresting
    rc = review_brightline_gate.main(["--session-id", "session-with-zero-commits", f"{head_sha}..HEAD"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "VERDICT=indeterminate" in captured.out

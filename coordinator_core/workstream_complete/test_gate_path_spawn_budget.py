"""test_gate_path_spawn_budget — the standing gate for `workstream_
complete.brief()`'s process budget: git spawns counted, never observed.

`brief()` reads one `SessionHistory` (one `git log`, floored at the session's
`head_at_start`) and threads it to the session-shape gate, the review scale,
the ship stamp and the mid-chain floor. The unpatched-gate test below drives a
live call and asserts at most 2 git spawns, at most 1 history walk and no
non-git process; with `head_at_start` absent it asserts zero walks.

Counts GIT spawns specifically (argv[0] basename `git`/`git.exe`), not
every subprocess `brief()` might reach -- an unrelated non-git spawn on
the path (there is none known today) must not make this assertion fail
for the wrong reason. Patches `subprocess.Popen.__init__` only (never
`subprocess.run` too): `subprocess.run` itself constructs a `Popen`
internally, so patching both would double-count every spawn issued via
`subprocess.run` -- `Popen.__init__` is the single choke point both
`subprocess.run` and a bare `Popen(...)` call funnel through, and is
therefore the only vantage point that counts each real process exactly
once regardless of which calling idiom a given module uses.

ASSERTS THE BUDGET, NOT THE ARGV: no assertion here pins an exact command
string. A later change that costs no spawn (a new flag, a reordered arg) must
not turn this file red -- only a NEW spawn crossing the ceiling should.

Run: python3 -m pytest coordinator_core/workstream_complete/test_gate_path_spawn_budget.py -q

Negative-spec:
    - Does NOT assert on `directives[]` content, envelope shape, or any
      judgment/decision `brief()` returns -- `test_workstream_complete.py`
      already owns that surface. This file owns exactly one property: git
      process count.
    - Does NOT stub out the history reader or the session-shape gate in the
      budget test to fake a low count -- the whole point is a LIVE call.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import coordinator_core.workstream_complete as wsc
import coordinator_core.ops.gate_dimension_review as gate_dimension_review
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(*args: str, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False,
        **no_console_creationflags(),
    )


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    branch = _git("symbolic-ref", "--short", "HEAD", cwd=path).stdout.strip()
    return branch or "master"


def _commit(path: Path, filename: str, content: str, message: str) -> str:
    (path / filename).write_text(content, encoding="utf-8")
    _git("add", filename, cwd=path)
    _git("commit", "-qm", message, cwd=path)
    return _git("rev-parse", "HEAD", cwd=path).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    sha = _commit(root, "seed.txt", "seed\n", "seed")
    if not sha:
        pytest.skip("git unavailable — cannot build a fixture repo with history")
    return root


def _wrap_popen_for_git_spawn_count(monkeypatch: pytest.MonkeyPatch) -> "list[list[str]]":
    calls: "list[list[str]]" = []
    real_init = subprocess.Popen.__init__

    def _fake_init(self, args, *a, **kw):
        argv = args if isinstance(args, (list, tuple)) else [args]
        if argv and Path(str(argv[0])).name in ("git", "git.exe"):
            calls.append([str(a) for a in argv])
        return real_init(self, args, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", _fake_init)
    return calls


def _patch_gate_with_sid(monkeypatch: pytest.MonkeyPatch, sid: str) -> None:
    monkeypatch.setattr(
        wsc,
        "compute_session_shape_gate",
        lambda root, history=None: wsc.SessionShapeGate(
            sid=sid,
            disposition="single-session",
            consumed_handoff="",
            diagnostics=[],
            consumed_handoff_paths=(),
            detection={},
        ),
    )


def _wrap_popen_for_all_spawns(monkeypatch: pytest.MonkeyPatch) -> "list[list[str]]":
    calls: "list[list[str]]" = []
    real_init = subprocess.Popen.__init__

    def _fake_init(self, args, *a, **kw):
        argv = args if isinstance(args, (list, tuple)) else [args]
        calls.append([str(x) for x in argv])
        return real_init(self, args, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", _fake_init)
    return calls


def _is_git(argv: "list[str]") -> bool:
    return bool(argv) and Path(argv[0]).name in ("git", "git.exe")


def _seed_session_with_peer(repo: Path, sid: str, peer_sid: str, *, with_floor: bool) -> None:
    """Own + peer commits with trailers; `head_at_start` is the seed commit when `with_floor`."""
    claim_dir = repo / ".git" / "coordinator-sessions" / sid
    claim_dir.mkdir(parents=True)
    (repo / ".git" / "coordinator-sessions" / peer_sid).mkdir(parents=True)
    if with_floor:
        (claim_dir / "head_at_start").write_text(
            _git("rev-parse", "HEAD", cwd=repo).stdout.strip(), encoding="utf-8"
        )

    def _commit_with_trailer(filename: str, content: str, committing_sid: str) -> None:
        (repo / filename).write_text(content, encoding="utf-8")
        _git("add", filename, cwd=repo)
        _git("commit", "-qm", f"work\n\nSession-Id: {committing_sid}", cwd=repo)

    _commit_with_trailer("peer-file.txt", "peer\n", peer_sid)
    _commit_with_trailer("own-file-1.txt", "own1\n", sid)
    _commit_with_trailer("own-file-2.txt", "own2\n", sid)
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")


_UNPATCHED_SID = "22222222-2222-2222-2222-222222222222"


def _name_the_session(monkeypatch: pytest.MonkeyPatch, sid: str) -> None:
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)


def test_brief_with_the_gate_unpatched_spawns_at_most_two_git_and_walks_once(repo, monkeypatch):
    """One live `brief()` call, session-shape gate NOT patched: at most 2 git spawns, at most
    1 history walk (`git log` with the reader's record separator), no non-git process.

    The fixture reaches the reader: `head_at_start` is the seed commit and the window holds a
    peer commit and two own commits, so the walk really runs and the gate, the review scale and
    the ship stamp all read that one result."""
    sid, peer_sid = _UNPATCHED_SID, "33333333-3333-3333-3333-333333333333"
    _seed_session_with_peer(repo, sid, peer_sid, with_floor=True)
    _name_the_session(monkeypatch, sid)
    calls = _wrap_popen_for_all_spawns(monkeypatch)

    envelope = wsc.brief(decisions={}, repo_root=repo)

    assert envelope["gates"]["session_shape"]["disposition"], "the real gate must have run"
    git_calls = [c for c in calls if _is_git(c)]
    other = [c for c in calls if not _is_git(c)]
    walks = [c for c in git_calls if "log" in c]
    assert len(git_calls) <= 2, f"expected at most 2 git spawns, got {len(git_calls)}: {git_calls}"
    assert len(walks) == 1, f"expected exactly 1 history walk (the reader ran, once), got {len(walks)}: {walks}"
    assert other == [], f"a non-git process on the brief path: {other}"


def test_brief_with_no_head_at_start_walks_nothing_and_names_the_floor(repo, monkeypatch):
    """No `head_at_start`: the reader walks nothing (there is no full-history fallback), and
    the review scale is unresolved with the missing floor in its reason."""
    sid, peer_sid = _UNPATCHED_SID, "33333333-3333-3333-3333-333333333333"
    _seed_session_with_peer(repo, sid, peer_sid, with_floor=False)
    _name_the_session(monkeypatch, sid)
    calls = _wrap_popen_for_all_spawns(monkeypatch)

    envelope = wsc.brief(decisions={"stage_paths": []}, repo_root=repo)

    walks = [c for c in calls if _is_git(c) and "log" in c]
    assert walks == [], f"an absent floor must not walk history: {walks}"
    review_scale = envelope["gates"]["review_scale"]
    assert review_scale["resolved"] is False
    assert "floor" in review_scale["reason"], review_scale["reason"]


def test_brief_call_one_builds_no_review_scope_and_so_cannot_truncate_one(repo, monkeypatch):
    """SUCCESSOR to `test_brief_spends_a_fifth_spawn_when_the_window_cannot_
    cover_us`, retired 2026-08-26 with the mechanism it pinned.

    That test bought an authoritative `git log --grep=Session-Id` walk to stop
    the peer trailer map silently TRUNCATING the review scope: the map is built
    over `--since=<earliest live peer start>`, so a session older than that
    peer may own commits the window never held, and a truncated `commit_slices`
    surfaces not as a smaller measurement but as a partitioned review claiming
    full coverage while the dropped commits go unreviewed and unnamed (measured
    on session 8bb305c5: 7 owned commits, the map held 4, the 3 it dropped
    carried the code).

    That hazard is now retired at its source rather than paid for. `brief()`
    call 1 -- `stage_paths` absent, the caller has not yet named its file set --
    no longer reconstructs the session's own paths or shas at all, so there is
    no scope for the window to truncate. The caller names the set on call 2 and
    the measurement runs against that.

    THE FIXTURE IS THE OLD TEST'S, DELIBERATELY UNCHANGED: this session's claim
    dir is backdated an hour behind its live peer, which is precisely the
    unprovable-window case the old test existed for. The assertion is that the
    dangerous state is now unreachable, on the exact input that used to reach
    it.

    Asserts the honest-unresolved shape, not merely a low count: a present-but-
    empty `commit_slices` would read as "this session owns zero commits" -- an
    answer -- where the truth is "not measured here". The key must be ABSENT.
    """
    sid = "22222222-2222-2222-2222-222222222222"
    peer_sid = "33333333-3333-3333-3333-333333333333"
    claim_dir = repo / ".git" / "coordinator-sessions" / sid
    claim_dir.mkdir(parents=True)
    peer_claim_dir = repo / ".git" / "coordinator-sessions" / peer_sid
    peer_claim_dir.mkdir(parents=True)
    import os as _os

    _hour_ago = _os.path.getmtime(peer_claim_dir) - 3600
    _os.utime(claim_dir, (_hour_ago, _hour_ago))

    def _commit_with_trailer(filename: str, content: str, committing_sid: str) -> str:
        (repo / filename).write_text(content, encoding="utf-8")
        _git("add", filename, cwd=repo)
        _git("commit", "-qm", f"work\n\nSession-Id: {committing_sid}", cwd=repo)
        return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    _commit_with_trailer("peer-file.txt", "peer\n", peer_sid)
    _commit_with_trailer("own-file-1.txt", "own1\n", sid)
    _commit_with_trailer("own-file-2.txt", "own2\n", sid)
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    _patch_gate_with_sid(monkeypatch, sid)
    calls = _wrap_popen_for_git_spawn_count(monkeypatch)

    envelope = wsc.brief(decisions={}, repo_root=repo)

    assert calls == [], (
        f"call 1 must issue no git spawn at all; got {len(calls)}: {calls}"
    )

    review_scale = envelope["gates"]["review_scale"]
    assert review_scale["resolved"] is False, (
        "call 1 must report the review scale UNRESOLVED, never a verdict built "
        f"from inputs it did not measure: {review_scale}"
    )
    assert "commit_slices" not in review_scale, (
        "an absent measurement must OMIT commit_slices, never emit it empty -- "
        "an empty list reads as 'this session owns zero commits', which is an "
        f"answer, and the wrong one: {review_scale}"
    )


def test_close_coverage_advisory_reaches_the_dimension_on_the_ordinary_close(repo, monkeypatch):
    """AC1 over AC6, the conflict resolved 2026-08-28 mid-execution.

    The ordinary single-close path has no prior review-trail record, so the
    review-brightline-gate directive's argv carries no range to reuse. The
    ORIGINAL C1 build treated that as "silent, zero spawns" -- which made the
    advisory inert on precisely the path AC1 was funded for. This test pins
    the opposite: given a `head_at_start` on disk, the advisory resolves its
    own range and DOES reach `_review_dimension_check`.

    The spawn budget survives as a ceiling, not as zero: range resolution
    stays free (a disk read plus the literal `HEAD` token, never a
    `rev-parse`), so the only added subprocess is the dimension's own
    `git log`. Asserted as at most one call, never as none."""
    sid = "22222222-2222-2222-2222-222222222222"
    claim_dir = repo / ".git" / "coordinator-sessions" / sid
    claim_dir.mkdir(parents=True)
    (claim_dir / "head_at_start").write_text("a" * 40, encoding="utf-8")
    _patch_gate_with_sid(monkeypatch, sid)

    calls = []

    def _record(changed_files, diff_base, repo_root):
        calls.append(diff_base)
        raise RuntimeError("dimension unavailable -- D2 says degrade to silence")

    monkeypatch.setattr(gate_dimension_review, "_review_dimension_check", _record)

    envelope = wsc.brief(decisions={"stage_paths": ["own-file-1.txt"]}, repo_root=repo)

    assert len(calls) <= 1, f"advisory must add at most one dimension call: {calls}"
    assert calls == ["{}..HEAD".format("a" * 40)], (
        "advisory must resolve its own range from head_at_start on the "
        f"ordinary close path: {calls}"
    )
    advisory = [d for d in envelope["directives"] if d["id"] == "d-close-coverage-advisory"]
    assert advisory, "the raising dimension must not remove the directive (D2)"


def test_close_coverage_advisory_stays_silent_without_head_at_start(repo, monkeypatch):
    sid = "22222222-2222-2222-2222-222222222222"
    (repo / ".git" / "coordinator-sessions" / sid).mkdir(parents=True)
    _patch_gate_with_sid(monkeypatch, sid)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("no range is resolvable; the dimension must not be reached")

    monkeypatch.setattr(gate_dimension_review, "_review_dimension_check", _fail_if_called)

    envelope = wsc.brief(decisions={"stage_paths": ["own-file-1.txt"]}, repo_root=repo)
    assert [d for d in envelope["directives"] if d["id"] == "d-close-coverage-advisory"]


def test_close_coverage_advisory_directive_is_always_already_satisfied_and_ungated(repo, monkeypatch):
    sid = "22222222-2222-2222-2222-222222222222"
    claim_dir = repo / ".git" / "coordinator-sessions" / sid
    claim_dir.mkdir(parents=True)
    _patch_gate_with_sid(monkeypatch, sid)

    envelope = wsc.brief(decisions={"stage_paths": ["own-file-1.txt"]}, repo_root=repo)
    directives = envelope["directives"]
    advisory = next(d for d in directives if d["id"] == "d-close-coverage-advisory")

    assert advisory["already_satisfied"] is True
    assert advisory["depends_on"] is None
    assert not any(d.get("depends_on") == "d-close-coverage-advisory" for d in directives)


def test_call_one_unresolved_scale_names_stage_paths_as_its_unlock(repo, monkeypatch):
    """An unresolved review scale must name the input that RESOLVES it.

    `decide_review_scale`'s `reason` names `code_loc`/`commit_count`/
    `surface_count` — all three are OUTPUTS of the measurement gated on
    `decisions["stage_paths"]`, and none is a value a caller can supply. An
    EM told to "supply the missing input named in `reason`" therefore has
    nothing to supply, and hand-sums the diff instead. That is not merely
    slower: a hand count uses different definitions than the engine's (gross
    LOC over a commit span, versus noise-excluded `code_loc` per owned sha),
    so the two paths can return different verdicts for the same tree.

    This pins the remediation onto the gate itself rather than into SKILL.md
    prose, so the route travels with the envelope that reports the problem.
    """
    sid = "44444444-4444-4444-4444-444444444444"
    (repo / ".git" / "coordinator-sessions" / sid).mkdir(parents=True)
    _patch_gate_with_sid(monkeypatch, sid)

    review_scale = wsc.brief(decisions={}, repo_root=repo)["gates"]["review_scale"]

    assert review_scale["resolved"] is False
    remediation = review_scale.get("remediation")
    assert remediation, (
        "an unresolved review scale must carry a remediation naming its unlock; "
        f"got: {review_scale}"
    )
    assert "stage_paths" in remediation, (
        f"remediation must name the input that actually resolves the gate: {remediation}"
    )


def test_resolved_scale_carries_no_remediation(repo, monkeypatch):
    sid = "55555555-5555-5555-5555-555555555555"
    (repo / ".git" / "coordinator-sessions" / sid).mkdir(parents=True)
    _patch_gate_with_sid(monkeypatch, sid)

    review_scale = wsc.brief(
        decisions={"stage_paths": []}, repo_root=repo
    )["gates"]["review_scale"]

    assert "remediation" not in review_scale, (
        f"a gate whose measurement ran carries no remediation: {review_scale}"
    )


def _bare_session_start_spawns(repo: Path, monkeypatch, sid: str) -> "tuple[object, list[list[str]]]":
    from coordinator_core.workstream_complete import directives_memo_lifecycle as lifecycle

    calls = _wrap_popen_for_git_spawn_count(monkeypatch)
    return lifecycle.resolve_session_start_time(repo, sid), calls


def test_session_start_ladder_without_claim_dir_costs_two_spawns_not_seven(repo, monkeypatch):
    """No claim dir -> the ref ladder runs. With an `origin/main` ref present
    it must be one `for-each-ref` plus one `log`, never a `merge-base` probe
    per candidate ref."""
    base_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    _git("update-ref", "refs/remotes/origin/main", base_sha, cwd=repo)
    _commit(repo, "work.txt", "w\n", "work")

    start, calls = _bare_session_start_spawns(repo, monkeypatch, "44444444-4444-4444-4444-444444444444")

    assert start is not None
    assert len(calls) <= 2, [c[1:] if c[1] != '-C' else c[3:] for c in calls]
    assert not any("merge-base" in c for c in calls), [c[3:] for c in calls]


def test_session_start_ladder_with_no_candidate_refs_costs_two_spawns(repo, monkeypatch):
    """A repo whose only branch is neither main nor master and has no remote:
    one `for-each-ref`, then the whole-history `log` fallback."""
    _git("branch", "-m", "feature", cwd=repo)

    start, calls = _bare_session_start_spawns(repo, monkeypatch, "55555555-5555-5555-5555-555555555555")

    assert start is not None
    assert len(calls) <= 2, [c[1:] if c[1] != '-C' else c[3:] for c in calls]


def test_session_start_ladder_anchors_on_first_commit_past_upstream(repo, monkeypatch):
    """Semantics preserved: the anchor is the earliest commit on HEAD not
    reachable from the resolved base."""
    base_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    _git("update-ref", "refs/remotes/origin/main", base_sha, cwd=repo)
    first_own_sha = _commit(repo, "a.txt", "a\n", "first-own")
    _commit(repo, "b.txt", "b\n", "second-own")
    first_own = _git("log", "-1", "--format=%cI", first_own_sha, cwd=repo).stdout.strip()

    start, _ = _bare_session_start_spawns(repo, monkeypatch, "66666666-6666-6666-6666-666666666666")

    assert start == datetime.fromisoformat(first_own)

"""test_gate_path_spawn_budget — AC1's own standing gate for `workstream_
complete.brief()`'s git-spawn budget, closed here for the first time: no
spawn-count assertion existed anywhere in `coordinator_core/workstream_
complete/` before this file (C4, docs/plans/2026-08-26-the-gate-paths-six-
spawns-collapse-to-four.md § C4). AC1 was previously an OBSERVED number
(measured live: 6 -> 5 after C1, still 5 after C2's net-zero reorder),
never an asserted one -- this file is what turns it into a red-on-regress
gate rather than a claim a future change can silently invalidate.

Spec backlink: docs/plans/2026-08-26-the-gate-paths-six-spawns-collapse-
to-four.md, chunk C4. C4 folded this session's own commit shas into the
SAME `show --numstat` union `resolve_known_concurrent_paths` already
spawns for peer attribution (`directives_commit_tail._committed_paths_
for_sids`'s new `this_session_id`/`own_session_numstat_out` pair), closing
the circular dependency C2's reorder introduced without reintroducing a
per-item spawn. AC1 is THIS file's bar: `brief()` at most 4 git spawns for
a live call, asserted by process count, not eyeballed off a comment.

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

ASSERTS THE BUDGET, NOT THE ARGV (per this chunk's own brief): no
assertion here pins an exact command string. A later change that costs no
spawn (a new flag, a reordered arg) must not turn this file red -- only a
NEW spawn crossing the 4-call ceiling should.

Run: python3 -m pytest coordinator_core/workstream_complete/test_gate_path_spawn_budget.py -q

Negative-spec:
    - Does NOT assert on `directives[]` content, envelope shape, or any
      judgment/decision `brief()` returns -- `test_workstream_complete.py`
      already owns that surface. This file owns exactly one property: git
      process count.
    - Does NOT reintroduce a per-item spawn to hit the budget, and does
      NOT stub out `_committed_paths_for_sids`/`resolve_known_concurrent_
      paths` to fake a low count -- the whole point is a LIVE call through
      the real spawn-collapse path this chunk landed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.workstream_complete as wsc

# Declared, not excused: this file spawns real `git` processes because the
# property under test IS process count -- no fixture stands in for it. Same
# precedent as `test_directives_commit_tail_peer_committed_paths.py`'s own
# `pytestmark` comment.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(*args: str, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


def _init_repo(path: Path) -> str:
    """git-inits a fixture repo, returns the checked-out branch name --
    never assumed, since `init.defaultBranch` varies across git installs.
    Same idiom as `test_directives_commit_tail_peer_committed_paths.py`'s
    own `_init_repo`, reproduced here rather than imported (a test-only
    fixture helper, not a production shape worth a shared module)."""
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
    """Wraps the ONE choke point both `subprocess.run` and a bare
    `Popen(...)` call funnel through (see this module's own docstring for
    why only `Popen.__init__`, never `subprocess.run` too, is patched).
    Returns the live list this function keeps appending to — read it AFTER
    the call under test, not before."""
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
        lambda root: wsc.SessionShapeGate(
            sid=sid,
            disposition="single-session",
            consumed_handoff="",
            diagnostics=[],
            consumed_handoff_paths=(),
            detection={},
        ),
    )


def test_brief_git_spawn_budget_is_at_most_four(repo, monkeypatch):
    """AC1: one live `brief()` call, git spawns counted (not observed),
    ceiling asserted at 4 -- see this file's own module docstring for the
    row's own accounting of what those 4 spawns are (#1 trailer map, one
    merged numstat, #3 diff-filter=A, #4 status).

    Seeds this session's own `.git/coordinator-sessions/<sid>/` claim dir
    (`directives_memo_lifecycle.resolve_session_start_time`'s FIRST rung,
    read via a bare `stat()`, zero git spawns) so this test exercises the
    production fast path every real session actually takes -- a session
    without a claim dir falls through that function's own 5-candidate
    `merge-base`/`log` ladder (`@{upstream}`, `origin/main`, `origin/
    master`, `main`, `master`), which is orthogonal pre-existing cost this
    row's brief does not touch and does not license re-measuring here.

    C5 (docs/plans/2026-08-26-the-gate-paths-six-spawns-collapse-to-four.md
    § C5, Defect 1): the fixture MUST reach the optimised path, or this
    assertion passes for the wrong reason -- against a `tmp_path` fixture
    with no peer session and no session-owned commits,
    `resolve_known_concurrent_paths` short-circuits before spawning at all
    and `_session_owned_shas` never reaches `resolve_session_commits`, so
    NONE of C1/C2/C4's spawn-collapse code is on the asserted path. Seeded
    here: one PEER session claim dir with a commit carrying ITS OWN
    `Session-Id:` trailer (so the bulk trailer walk + peer numstat spawn
    actually run), and two commits carrying THIS session's own sid (so
    `_session_owned_shas`/the committed-LOC leg have real data to fold)."""
    sid = "22222222-2222-2222-2222-222222222222"
    peer_sid = "33333333-3333-3333-3333-333333333333"
    claim_dir = repo / ".git" / "coordinator-sessions" / sid
    claim_dir.mkdir(parents=True)
    peer_claim_dir = repo / ".git" / "coordinator-sessions" / peer_sid
    peer_claim_dir.mkdir(parents=True)

    def _commit_with_trailer(filename: str, content: str, committing_sid: str) -> str:
        (repo / filename).write_text(content, encoding="utf-8")
        _git("add", filename, cwd=repo)
        _git(
            "commit", "-qm", f"work\n\nSession-Id: {committing_sid}", cwd=repo
        )
        return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    _commit_with_trailer("peer-file.txt", "peer\n", peer_sid)
    _commit_with_trailer("own-file-1.txt", "own1\n", sid)
    _commit_with_trailer("own-file-2.txt", "own2\n", sid)

    # C6 (docs/plans/2026-08-26-the-gate-paths-six-spawns-collapse-to-four.md
    # § C6): a real session's worktree is never clean -- a dirty path here is
    # what makes the `ls-files` spawn a real session always pays actually
    # fire on this fixture's asserted path, rather than the assertion passing
    # only because the condition that spawn exists for is absent.
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    _patch_gate_with_sid(monkeypatch, sid)
    calls = _wrap_popen_for_git_spawn_count(monkeypatch)

    wsc.brief(decisions={}, repo_root=repo)

    assert len(calls) <= 4, (
        f"expected at most 4 git spawns from one brief() call, got "
        f"{len(calls)}: {calls}"
    )


def test_brief_spends_a_fifth_spawn_when_the_window_cannot_cover_us(repo, monkeypatch):
    """The other branch of the same contract, and the reason it is worth a spawn.

    `brief()` reads this session's owned shas from the peer trailer map when
    that map provably saw all of them, and pays an authoritative
    `git log --grep=Session-Id` walk when it cannot prove that. The proof is
    `_map_window_covers_session`: the map is built over
    `--since=<earliest live peer start>`, so a session that started BEFORE
    that peer may have commits the window never held.

    Why the extra spawn is the right purchase: these shas become
    `commit_slices`, which IS the review scope. A truncated list does not
    surface as a smaller measurement -- it surfaces as a partitioned review
    reporting full coverage while the commits outside the window go
    unreviewed and unnamed. Measured on session 8bb305c5 (2026-08-26): 7
    owned commits, the map held 4, and the 3 it dropped carried the code.

    Fixture inverts the sibling test's ordering deliberately: this session's
    claim dir is created FIRST, so the session predates its own live peer and
    the window cannot be proven to cover it. Asserting the walk FIRED (not
    merely that the count rose) is what keeps this from passing for an
    unrelated reason.
    """
    sid = "22222222-2222-2222-2222-222222222222"
    peer_sid = "33333333-3333-3333-3333-333333333333"
    claim_dir = repo / ".git" / "coordinator-sessions" / sid
    claim_dir.mkdir(parents=True)
    peer_claim_dir = repo / ".git" / "coordinator-sessions" / peer_sid
    peer_claim_dir.mkdir(parents=True)
    # Backdate THIS session an hour. Creation order alone cannot express
    # "older than the window": both dirs land in the same clock tick, the two
    # timestamps compare equal, and `>=` reads that as covered. Setting the
    # time explicitly is what makes this fixture the unprovable case rather
    # than a race that usually resolves the other way.
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

    wsc.brief(decisions={}, repo_root=repo)

    walked = [c for c in calls if any("--grep=^Session-Id:" in str(a) for a in c)]
    assert walked, (
        "the authoritative sha walk did not fire for a session the map's window "
        "cannot be proven to cover -- review scope may be silently truncated: "
        f"{calls}"
    )
    assert len(calls) <= 5, (
        f"expected at most 5 git spawns on the unprovable-window path, got "
        f"{len(calls)}: {calls}"
    )

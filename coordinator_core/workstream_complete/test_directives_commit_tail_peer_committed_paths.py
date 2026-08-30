"""test_directives_commit_tail_peer_committed_paths — path-scoped regression
suite for `directives_commit_tail._committed_paths_for_sids`, the sole
peer-attribution entry point production calls (via `resolve_known_
concurrent_paths`).

Also carries C5's AC5 pin (docs/plans/2026-08-25-the-close-ceremony-
rebuilt-from-the-requirement.md, C5, DR-358 `d-release-plan-claim` ruling)
for `run_close_commit_and_release_claims` — the wave-1 pathspec
(`docs/plans/2026-08-25-the-close-ceremony-rebuilt-from-the-requirement.
workflow.mjs`) lists this exact file for C5's test, alongside
`directives_commit_tail.py` itself; see the `TestRunCloseCommitAndRelease
Claims*` section below for that pin, kept in this module rather than a new
file per the declared `writes:` scope.

Spec backlink: docs/plans/2026-08-10-commit-event-5s-cap-and-the-silent-
tail.md, chunk C1. Originally authored against `docs/plans/2026-08-07-n-
plus-one-git-spawn-class-and-amplification-gate.md`'s C18 ("`_peer_committed_
paths` migrates onto `bulk_trailer_session_map`"), which was REVERTED —
`bulk_trailer_session_map` hardcoded `--no-merges`, silently dropping a
peer's merge-commit contribution (UNDER-exclusion; see
`test_merge_commit_authored_by_peer_is_included` below). C1 lands the fix
that C18 could not: `_committed_paths_for_sids` now calls
`bulk_trailer_session_map(..., include_merges=True)` — a parameterized
merge filter, default unchanged for every OTHER caller — so this file's
merge-inclusion pin still holds while the N+1 spawn shape it originally
also covered is now gone. `test_spawn_count_does_not_scale_with_commit_
count` is the new pin for that half.

Repointed off the former `_peer_committed_paths` single-sid convenience
wrapper (deleted — no production caller; see
state/debt-backlog/2026-08-11-peer-committed-paths-is-a-second-peer-at-
c596b80fa5db.yaml) onto `_committed_paths_for_sids` directly, so the tested
path and the shipped path (`resolve_known_concurrent_paths`'s own call) are
the same function. The single-sid convenience is reproduced in this file's
own `_peer_committed_paths` test helper below, which resolves `sid`'s start
time and unwraps the one-sid result — same call shape every test in this
file already used, no assertion intent changed.

Run: python3 -m pytest coordinator_core/workstream_complete/test_directives_commit_tail_peer_committed_paths.py -q
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.lifecycle import git_common_dir
from coordinator_core.session import claims as _session_claims
from coordinator_core.session import core as _session_core_mod
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workstream_complete import directives_commit_tail

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _git(*args: str, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False,
        **no_console_creationflags(),
    )


def _init_repo(path) -> str:
    """git-inits a fixture repo, returns the checked-out branch name (never
    assumed — `init.defaultBranch` varies across git installs/configs, and a
    hardcoded "main"/"master" here would make this fixture flaky exactly the
    way the plan's own § Anti-scope 19 warns against for unpinned state)."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    branch = _git("symbolic-ref", "--short", "HEAD", cwd=path).stdout.strip()
    return branch or "master"


def _commit(path, filename: str, content: str, message: str) -> str:
    (path / filename).write_text(content, encoding="utf-8")
    _git("add", filename, cwd=path)
    _git("commit", "-qm", message, cwd=path)
    return _git("rev-parse", "HEAD", cwd=path).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    branch = _init_repo(root)
    sha = _commit(root, "seed.txt", "seed\n", "seed")
    if not sha:
        pytest.skip("git unavailable — cannot build a fixture repo with history")
    return root, branch


def _peer_committed_paths(repo_root, sid: str, monkeypatch, since_before) -> set:
    """Calls `_committed_paths_for_sids` for a single sid — the shipped
    entry point `resolve_known_concurrent_paths` itself calls — with
    `resolve_session_start_time` monkeypatched to a fixed instant before
    every fixture commit, so the `--since=` window covers the whole fixture
    history regardless of wall-clock skew between fixture setup and the
    git-log call. Mirrors the former `_peer_committed_paths` single-sid
    convenience wrapper's own two steps (resolve start time, unwrap the
    one-sid result) so every existing assertion keeps its intent."""
    monkeypatch.setattr(
        directives_commit_tail._memo_lifecycle,
        "resolve_session_start_time",
        lambda _repo_root, _sid: since_before,
    )
    start = directives_commit_tail._memo_lifecycle.resolve_session_start_time(repo_root, sid)
    return directives_commit_tail._committed_paths_for_sids(repo_root, {sid: start}).get(sid, set())


# ---------------------------------------------------------------------------
# Happy path — a plain (non-merge) commit trailered to the target sid
# ---------------------------------------------------------------------------


def test_trailered_commit_is_included(repo, monkeypatch):
    root, _branch = repo
    sid = "11111111-1111-1111-1111-111111111111"
    since_before = datetime.now(timezone.utc) - timedelta(hours=1)
    _commit(root, "mine.txt", "mine\n", f"peer work\n\nSession-Id: {sid}\n")

    result = _peer_committed_paths(root, sid, monkeypatch, since_before)

    assert "mine.txt" in result


# ---------------------------------------------------------------------------
# Untrailered commits are never attributed — the exclusion-based posture
# `trailer_foreign_shas`/`bulk_trailer_session_map` also document
# ---------------------------------------------------------------------------


def test_untrailered_commit_is_excluded(repo, monkeypatch):
    root, _branch = repo
    sid = "22222222-2222-2222-2222-222222222222"
    since_before = datetime.now(timezone.utc) - timedelta(hours=1)
    _commit(root, "untrailered.txt", "x\n", "no trailer here")

    result = _peer_committed_paths(root, sid, monkeypatch, since_before)

    assert "untrailered.txt" not in result


# ---------------------------------------------------------------------------
# A commit trailered to a DIFFERENT session is not attributed to sid
# ---------------------------------------------------------------------------


def test_other_session_commit_is_excluded(repo, monkeypatch):
    root, _branch = repo
    sid = "33333333-3333-3333-3333-333333333333"
    other_sid = "44444444-4444-4444-4444-444444444444"
    since_before = datetime.now(timezone.utc) - timedelta(hours=1)
    _commit(root, "theirs.txt", "theirs\n", f"other peer work\n\nSession-Id: {other_sid}\n")

    result = _peer_committed_paths(root, sid, monkeypatch, since_before)

    assert "theirs.txt" not in result


# ---------------------------------------------------------------------------
# UUID-shape validation (archive_stamp._commit_session_id) is load-bearing
# here even though the comparison itself is plain equality — a trailer that
# textually matches sid but is not UUID/hex-dash shaped must still be
# rejected. Distinguishes this call site's fail-closed reader from a naive
# equality-only comparison, per § Anti-scope 7's shape-validation warning
# for this primitive family.
# ---------------------------------------------------------------------------


def test_shape_invalid_trailer_is_excluded_despite_textual_match(repo, monkeypatch):
    root, _branch = repo
    # Underscore is not in `_SESSION_ID_UUID_RE`'s `[0-9a-fA-F-]` character
    # class, so this sid is intentionally shape-invalid.
    sid = "not_a_uuid_shape"
    since_before = datetime.now(timezone.utc) - timedelta(hours=1)
    _commit(root, "shape-invalid.txt", "x\n", f"peer work\n\nSession-Id: {sid}\n")

    result = _peer_committed_paths(root, sid, monkeypatch, since_before)

    assert "shape-invalid.txt" not in result


# ---------------------------------------------------------------------------
# THE key divergence test — a MERGE commit trailered to sid, with a real
# combined-diff-visible touched path (a resolved merge conflict), must be
# included under `_committed_paths_for_sids`'s current (`git log --since=...`,
# no `--no-merges`) behavior. `bulk_trailer_session_map` walks `--no-merges`
# and would silently drop this commit's contribution entirely — the finding
# this whole file backs. See the module docstring above.
# ---------------------------------------------------------------------------


def test_merge_commit_authored_by_peer_is_included(repo, monkeypatch):
    root, branch = repo
    sid = "55555555-5555-5555-5555-555555555555"
    since_before = datetime.now(timezone.utc) - timedelta(hours=1)

    _commit(root, "shared.txt", "A\n", "base shared")
    _git("checkout", "-qb", "feature", cwd=root)
    _commit(root, "shared.txt", "B\n", "feature edit")
    _git("checkout", "-q", branch, cwd=root)
    _commit(root, "shared.txt", "C\n", "base edit")

    merge_proc = _git("merge", "--no-ff", "-q", "feature", cwd=root)
    assert merge_proc.returncode != 0, "expected a merge conflict to set up the fixture"

    (root / "shared.txt").write_text("D\n", encoding="utf-8")
    _git("add", "shared.txt", cwd=root)
    commit_proc = _git(
        "commit", "-qm", f"resolve merge\n\nSession-Id: {sid}\n", cwd=root
    )
    assert commit_proc.returncode == 0, commit_proc.stderr

    merge_sha = _git("rev-parse", "HEAD", cwd=root).stdout.strip()
    parents = _git("log", "-1", "--format=%P", merge_sha, cwd=root).stdout.strip()
    assert len(parents.split()) > 1, "fixture HEAD must be a real 2-parent merge commit"

    result = _peer_committed_paths(root, sid, monkeypatch, since_before)

    assert "shared.txt" in result


# ---------------------------------------------------------------------------
# AC1 — the git-spawn count is bounded, independent of commit count. The
# pre-fix implementation spawned up to 2N processes (one `archive_stamp.
# _commit_session_id` per candidate sha, one `git show --name-only` per
# attributed sha) for a peer's own `--since=` window. This pins the fix:
# regardless of how many trailered commits sid authored, the number of real
# `subprocess.run` calls this module's own code makes must not grow with
# commit count.
# ---------------------------------------------------------------------------


def test_spawn_count_does_not_scale_with_commit_count(repo, monkeypatch):
    root, _branch = repo
    sid = "66666666-6666-6666-6666-666666666666"
    since_before = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(12):
        _commit(root, f"mine{i}.txt", f"mine{i}\n", f"peer work {i}\n\nSession-Id: {sid}\n")

    spawn_calls = []
    real_run = subprocess.run

    def _counting_run(*args, **kwargs):
        spawn_calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)

    result = _peer_committed_paths(root, sid, monkeypatch, since_before)

    assert all(f"mine{i}.txt" in result for i in range(12))
    # Exactly two real `git` spawns for this call regardless of the 12
    # trailered commits above (one bulk trailer walk, one batched touched-
    # paths walk) — never one spawn per candidate/attributed sha.
    assert len(spawn_calls) == 2, [c[0] for c in spawn_calls]


# ---------------------------------------------------------------------------
# Overflow — a sha count ABOVE the chunking threshold still returns the
# COMPLETE union, split across multiple chunked spawns rather than one
# unchunked call or a per-sha spawn. Chunk size is monkeypatched down to a
# small value so the test does not need to build hundreds of real fixture
# commits to cross the threshold; the spawn-count assertion is what proves
# chunking actually happened (would fail if chunking were removed and the
# call collapsed back onto a single unchunked spawn).
# ---------------------------------------------------------------------------


def test_overflow_sha_count_returns_complete_union_via_chunked_spawns(repo, monkeypatch):
    root, _branch = repo
    sid = "77777777-7777-7777-7777-777777777777"
    since_before = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(directives_commit_tail, "_COMMITTED_PATHS_CHUNK", 2)
    commit_count = 5
    for i in range(commit_count):
        _commit(root, f"overflow{i}.txt", f"overflow{i}\n", f"peer work {i}\n\nSession-Id: {sid}\n")

    spawn_calls = []
    real_run = subprocess.run

    def _counting_run(*args, **kwargs):
        spawn_calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)

    result = _peer_committed_paths(root, sid, monkeypatch, since_before)

    assert all(f"overflow{i}.txt" in result for i in range(commit_count))
    # 1 bulk trailer spawn + ceil(5 commits / chunk-of-2) == 3 chunked
    # touched-paths spawns == 4 total. A regression that removed chunking
    # (reverting to one unchunked spawn-2 call) would collapse this to 2.
    assert len(spawn_calls) == 4, [c[0] for c in spawn_calls]


# ---------------------------------------------------------------------------
# Fail-closed — a git failure on the batched touched-paths walk must NOT
# silently degrade to an empty peer-exclusion set (the pre-fix per-sha loop's
# fail-open posture). It must raise, surfacing the failure to the caller
# rather than reading as "confirmed no peer owns anything here".
# ---------------------------------------------------------------------------


def test_git_failure_on_touched_paths_walk_raises_not_empty(repo, monkeypatch):
    root, _branch = repo
    sid = "88888888-8888-8888-8888-888888888888"
    since_before = datetime.now(timezone.utc) - timedelta(hours=1)
    _commit(root, "willfail.txt", "x\n", f"peer work\n\nSession-Id: {sid}\n")

    # `_chunked_committed_paths` now calls `_run_git_ok_retrying`, not
    # `_run_git_ok` directly (bounded retry wrapper added for routine lock
    # contention — see that function's own docstring). Patched at that
    # layer so this fail-closed pin exercises the actual call site.
    monkeypatch.setattr(directives_commit_tail, "_run_git_ok_retrying", lambda *_a, **_k: None)

    with pytest.raises(directives_commit_tail.PeerAttributionUnavailable):
        _peer_committed_paths(root, sid, monkeypatch, since_before)


# ---------------------------------------------------------------------------
# AC5 (C5, docs/plans/2026-08-25-the-close-ceremony-rebuilt-from-the-
# requirement.md) — `run_close_commit_and_release_claims` releases the
# GOVERNING-PLAN ARTIFACT claim (`cs_release_artifact`, "plan" class), a
# different mechanism entirely from this file's own `_committed_paths_for_
# sids` PATH-attribution machinery above and from hard constraint 4's
# per-path `release_committed_claims` (see `directives_commit_tail.py`'s
# own module docstring for the two-mechanism split). This is a
# STATE-OBSERVATION test, not a call-observation one — `cs_release_artifact`
# is documented "always best-effort, always returns (never raises)", so a
# spy asserting the call happened would pass on a release that silently did
# nothing (staff-eng review, finding 2). Asserts the claim path exists with
# this session as holder BEFORE close and is absent AFTER it.
#
# Real repo, hooks live throughout (hard constraint 2, `state/audits/2026-
# 08-25-close-ceremony-floor-probe.md`'s own recipe): a `git init` scratch
# fixture carries no hooks, and the whole point of routing this rebuilt
# close step through `run_commit_pipeline` (never `git_native.commit_scoped`
# directly — hard constraint 6) is that the gated route the hooks live in is
# what the ceremony actually runs in production. This suite clones THIS
# repo's own checkout (`--depth 1`, resolved via `git rev-parse
# --show-toplevel`, never hand-typed) and copies `.git/hooks/{pre-commit,
# prepare-commit-msg,post-commit}` verbatim into the fixture before driving
# a real close through it.
# ---------------------------------------------------------------------------


def _this_repo_toplevel() -> Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(Path(__file__).resolve().parent),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    return Path(proc.stdout.strip())


@pytest.fixture
def hooks_live_repo(tmp_path, monkeypatch):
    """A real, throwaway clone of THIS repo's own checkout, with THIS
    repo's own `.git/hooks/{pre-commit,prepare-commit-msg,post-commit}`
    copied in verbatim — see this section's own header comment for why a
    `git init` scratch fixture (no hooks) does not stand in for this test.

    `-c core.longpaths=true` is explicit and load-bearing, not decorative:
    the suite-wide autouse `HOME`/`USERPROFILE` quarantine (`coordinator_core/
    conftest.py`) points git at a fresh profile with no global gitconfig,
    so a real machine's `core.longpaths=true` (needed for this repo's own
    longest tracked paths under a Windows `MAX_PATH` checkout) is silently
    lost under test — `git clone` here fails `fatal: unable to checkout
    working tree` without this flag, even though the identical command
    succeeds outside pytest where the real profile's gitconfig applies.
    Latent-bug carve-out (§ Core Behavior 4): the failure is in this new
    fixture's own environment, not a change to any other file.

    `COORDINATOR_ENGINE_ROOT` is pinned to THIS repo's own checkout (`src`,
    never the throwaway clone `dest`) for the same reason: any pre-commit
    hook actually installed in `src`'s `.git/hooks/` (this fixture copies
    whatever is there, skipping any hook that isn't installed -- see the loop
    below; historically that meant the now-deleted `detect-staged-rollback`
    gate, claude-klabauter ends with no pre-commit hook as of 2026-08-25) resolves the
    claude-klabauter engine root via `coordinator/bin/lib/cc_invoke.py`'s registry
    ladder, which has nothing to resolve a freshly-cloned, unregistered
    `dest` against and fails closed (`engine-root resolution failed`)
    without an explicit rung-1 override. `dest` runs the identical
    `coordinator_core` code either way (it is a clone of `src`), so pointing
    any such gate's own import at `src` changes nothing about which code
    executes."""
    src = _this_repo_toplevel()
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(src))
    dest = tmp_path / "hooks-live-repo"
    subprocess.run(
        ["git", "-c", "core.longpaths=true", "clone", "--depth", "1", str(src), str(dest)],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    _git("config", "user.email", "t@example.com", cwd=dest)
    _git("config", "user.name", "t", cwd=dest)
    for hook_name in ("pre-commit", "prepare-commit-msg", "post-commit"):
        src_hook = src / ".git" / "hooks" / hook_name
        if not src_hook.is_file():
            continue
        dest_hook = dest / ".git" / "hooks" / hook_name
        shutil.copy2(src_hook, dest_hook)
        dest_hook.chmod(0o755)
    return dest


def test_run_close_commit_and_release_claims_releases_governing_plan_claim(
    hooks_live_repo, monkeypatch
):
    root = hooks_live_repo
    sid = "c5-claim-release-test-session"
    slug = "c5-claim-release-test-plan"

    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    _session_core_mod.init(sid, cwd=str(root))
    assert _session_claims.claim_plan(slug, cwd=str(root)) is True

    common_dir = git_common_dir(root)
    claim_dir = common_dir / "coordinator-sessions" / "plan-claims" / slug

    # BEFORE close: the claim exists, held by this session.
    assert claim_dir.is_dir()
    assert (claim_dir / "session_id").read_text(encoding="utf-8").strip() == sid

    rel = "c5-claim-release-fixture.txt"
    (root / rel).write_text("close ceremony claim-release fixture\n", encoding="utf-8")

    result = directives_commit_tail.run_close_commit_and_release_claims(
        root,
        session_id=sid,
        subject="C5 claim-release fixture commit",
        stage_paths=[rel],
        caller_paths={rel},
        governing_plan_slug=slug,
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None

    # AFTER close: the governing-plan claim is gone.
    assert not claim_dir.exists()


def test_run_close_commit_and_release_claims_releases_on_commit_failure_too(
    hooks_live_repo, monkeypatch
):
    """DR-358's failure-path ruling: release fires unconditionally, whether
    the wrapped commit step succeeded or failed — a claim held by a session
    that failed to commit is exactly as abandoned as one held by a session
    that committed cleanly. Forces `run_close_commit` to RAISE (mirrors the
    "or raises outright" branch `run_close_commit_and_release_claims`'s own
    docstring names — a `finally`, not an `if result.ok:` branch, is what
    this pins) by monkeypatching `run_commit_pipeline` itself, and asserts
    the claim is released and the exception still propagates unmodified."""
    root = hooks_live_repo
    sid = "c5-claim-release-failure-test-session"
    slug = "c5-claim-release-failure-test-plan"

    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    _session_core_mod.init(sid, cwd=str(root))
    assert _session_claims.claim_plan(slug, cwd=str(root)) is True

    common_dir = git_common_dir(root)
    claim_dir = common_dir / "coordinator-sessions" / "plan-claims" / slug
    assert claim_dir.is_dir()

    def _raise(*_args, **_kwargs):
        raise RuntimeError("simulated commit-step failure")

    monkeypatch.setattr(
        "coordinator_core.ops.ceremony.commit_pipeline.run_commit_pipeline", _raise
    )

    with pytest.raises(RuntimeError, match="simulated commit-step failure"):
        directives_commit_tail.run_close_commit_and_release_claims(
            root,
            session_id=sid,
            subject="C5 claim-release failure fixture",
            stage_paths=["irrelevant.txt"],
            governing_plan_slug=slug,
        )

    assert not claim_dir.exists()

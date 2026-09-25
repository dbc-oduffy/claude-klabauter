"""Behavioral tests for
coordinator_core.write_guards.guard_doctrine_surface_edits._sentinel_state --
the read side of the doctrine-surface approval guard.

This file exists specifically to close a gap left after the 2026-07-30
forge-closure fix: the *write*-side creation guard
(coordinator_core.bash_guards.block_approval_sentinel_creation) already had
116 passing tests, but the *read* side that turned an uncaught creation
command into a working approval had none. `_sentinel_state()` used to grant
approval via a bare `os.path.getmtime()` call, which succeeds on a
DIRECTORY exactly as it does on a regular file -- so `mkdir
.coordinator-doctrine-edit-approved` produced a real, honoured 30-minute
approval window with no Bash guard in the loop at all. The fix requires
`os.path.isfile()`; these tests pin that requirement directly against
`_sentinel_state`, independent of the Bash surface, so a future edit cannot
silently reintroduce the `getmtime`-only check.

Spec backlink: DoE-claude
  coordinator/tests/test_guard_doctrine_surface_edits.py (sibling coverage,
  reached via `check()` rather than `_sentinel_state()` directly)
"""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from coordinator_core.session import harness_registry as hr
from coordinator_core.write_guards import guard_doctrine_surface_edits as guard
from coordinator_core.win_portability import no_console_creationflags

# Real git is load-bearing for two tests below (test_sentinel_is_gitignored_
# in_this_repo, test_sentinel_is_not_tracked_in_this_repo): they assert on
# THIS repo's actual .gitignore/tracked-file state via `git check-ignore` /
# `git ls-files`, which a mocked git object model cannot reproduce. The
# scratch_repo fixture above no longer spawns git -- see its own docstring.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SENTINEL_NAME = guard._SENTINEL_NAME


@pytest.fixture
def scratch_repo(tmp_path):
    """A scratch root, isolated from any real checkout. `_sentinel_state`
    only ever consults `os.path.join(repo_root, _SENTINEL_NAME)`, so no real
    git repo is spawned here -- every test in this file that needs
    `_git_root()` in the loop (below) monkeypatches it directly rather than
    relying on this fixture being a real repo.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _fresh(path) -> None:
    path.write_text("", encoding="utf-8")


def _age(path, seconds_ago: float) -> None:
    old_time = time.time() - seconds_ago
    os.utime(path, (old_time, old_time))


# ---------------------------------------------------------------------------
# The forge this whole file exists to close.
# ---------------------------------------------------------------------------


def test_directory_at_sentinel_path_never_approves(scratch_repo):
    """THE FORGE: `mkdir .coordinator-doctrine-edit-approved` must never
    read as a real approval. `os.path.getmtime()` succeeds on a directory
    exactly as it does on a regular file -- a bare-getmtime read side (the
    pre-fix state of this function) would return "allow" here. Do not
    "simplify" the `os.path.isfile()` check in `_sentinel_state` back out
    to a bare `getmtime`/`exists` check; that reopens this exact forge.
    """
    sentinel_dir = scratch_repo / _SENTINEL_NAME
    sentinel_dir.mkdir()
    assert guard._sentinel_state(str(scratch_repo)) == "deny-absent"


# ---------------------------------------------------------------------------
# Baseline shapes.
# ---------------------------------------------------------------------------


def test_fresh_regular_file_allows(scratch_repo):
    _fresh(scratch_repo / _SENTINEL_NAME)
    assert guard._sentinel_state(str(scratch_repo)) == "allow"


def test_regular_file_older_than_window_denies_expired(scratch_repo):
    sentinel = scratch_repo / _SENTINEL_NAME
    _fresh(sentinel)
    _age(sentinel, 31 * 60)
    assert guard._sentinel_state(str(scratch_repo)) == "deny-expired"


def test_absent_sentinel_denies(scratch_repo):
    assert guard._sentinel_state(str(scratch_repo)) == "deny-absent"


# ---------------------------------------------------------------------------
# Symlinks -- `isfile()` follows symlinks, deliberately: a symlink to a
# regular file the PM created still approves. A symlink to a directory (or
# anything else that isn't a regular file at the resolved end) must not.
# ---------------------------------------------------------------------------


def _symlink_or_skip(alias, real_target) -> None:
    try:
        alias.symlink_to(real_target)
    except OSError as exc:
        pytest.skip(f"could not create symlink in this environment: {exc}")


def test_symlink_to_regular_file_within_window_allows(scratch_repo):
    """Deliberate: the PM could plausibly create the approval as a symlink
    to a regular file elsewhere, and `os.path.isfile()` follows symlinks."""
    real_file = scratch_repo / "real-approval.txt"
    _fresh(real_file)
    alias = scratch_repo / _SENTINEL_NAME
    _symlink_or_skip(alias, real_file)
    assert guard._sentinel_state(str(scratch_repo)) == "allow"


def test_symlink_to_directory_denies_absent(scratch_repo):
    real_dir = scratch_repo / "real-dir"
    real_dir.mkdir()
    alias = scratch_repo / _SENTINEL_NAME
    _symlink_or_skip(alias, real_dir)
    assert guard._sentinel_state(str(scratch_repo)) == "deny-absent"


# ---------------------------------------------------------------------------
# Other non-regular-file node types.
# ---------------------------------------------------------------------------


def test_fifo_at_sentinel_path_denies_absent(scratch_repo):
    if not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo unavailable on this platform")
    fifo_path = scratch_repo / _SENTINEL_NAME
    try:
        os.mkfifo(str(fifo_path))
    except OSError as exc:
        pytest.skip(f"could not create a fifo in this environment: {exc}")
    assert guard._sentinel_state(str(scratch_repo)) == "deny-absent"


# ---------------------------------------------------------------------------
# No repo root resolvable -> deny-absent, per the guard's fail-closed
# posture (module docstring: "Fail-closed is DELIBERATE").
# ---------------------------------------------------------------------------


def test_no_repo_root_denies_absent():
    assert guard._sentinel_state(None) == "deny-absent"


# ---------------------------------------------------------------------------
# The sentinel must never become a TRACKED file in this repo.
#
# `_sentinel_state`'s whole semantic is the sentinel's MTIME: present-and-
# younger-than `_APPROVAL_WINDOW_SECONDS` is a live operator approval to
# rewrite always-loaded doctrine. Git does not preserve mtime -- a checkout
# stamps every file it writes with the checkout time -- so a sentinel that
# ever got committed would arrive FRESH on every clone, pull, and branch
# switch, handing every session in the fleet a standing 30-minute approval
# that no operator granted. The Bash-side creation guard
# (bash_guards.block_approval_sentinel_creation) governs how the file is
# MADE and is blind to this route entirely: `git checkout` is not a
# sentinel-creating command in its rules, yet it materialises the file with
# a qualifying mtime.
#
# Ignored rather than merely untracked, so the ceremony whole-worktree
# dirty-tree gates do not hard-fail on the sentinel an approving operator
# legitimately just created -- the same reasoning .gitignore records for the
# housekeeping-liveness and doctor-sentinel entries.
# ---------------------------------------------------------------------------


def test_sentinel_is_gitignored_in_this_repo():
    repo_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    **no_console_creationflags()).stdout.strip()
    result = subprocess.run(
        ["git", "check-ignore", "-q", _SENTINEL_NAME],
        cwd=repo_root, capture_output=True,
    **no_console_creationflags())
    assert result.returncode == 0, (
        f"{_SENTINEL_NAME} is not gitignored -- a committed sentinel arrives "
        "with a fresh mtime on every checkout, which _sentinel_state reads as "
        "a live doctrine-edit approval nobody granted."
    )


def test_sentinel_is_not_tracked_in_this_repo():
    repo_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    **no_console_creationflags()).stdout.strip()
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", _SENTINEL_NAME],
        cwd=repo_root, capture_output=True, text=True,
    **no_console_creationflags())
    assert tracked.returncode != 0, (
        f"{_SENTINEL_NAME} is TRACKED -- gitignore does not untrack an "
        "already-committed path. Run `git rm --cached` on it."
    )


# ---------------------------------------------------------------------------
# Deny-message class split (cross-repo/inbox/2026-08-08-example-store-repo-em-...).
#
# A sibling repo's PM read "always-loaded doctrine" against their own
# coordinator.local.md, correctly judged it false, and proposed ungating the
# file. The premise was right and the conclusion inverted -- the frontmatter
# is the executed/authority half. These pin the message that says so, because
# a deny reason a reader can falsify is one they route around.
# ---------------------------------------------------------------------------


def test_local_config_denial_does_not_claim_always_loaded_doctrine():
    reason = guard._deny_reason("coordinator.local.md", is_local_config=True)
    assert "always-loaded doctrine" not in reason
    assert "reaches every session" not in reason


def test_local_config_denial_names_the_execution_and_authority_surface():
    reason = guard._deny_reason("coordinator.local.md", is_local_config=True)
    for key in ("fast_test_cmd", "_post_command", "fast_tier_unscoped_reason"):
        assert key in reason, f"deny message must name {key} as the real reason"


def test_claude_md_denial_keeps_the_always_loaded_rationale():
    reason = guard._deny_reason("CLAUDE.md")
    assert "always-loaded doctrine" in reason
    assert "fast_test_cmd" not in reason


def test_local_config_path_matches_the_protected_entry(scratch_repo):
    """`_local_config_path` must resolve identically to the protected-list
    entry it is compared against in `check()` -- a drift between the two
    silently reverts every coordinator.local.md denial to the class-1
    message without failing anything else.
    """
    root = str(scratch_repo)
    assert guard._local_config_path(root) in guard._protected_paths(root)


def test_local_config_path_is_none_without_a_repo_root():
    assert guard._local_config_path(None) is None


# ---------------------------------------------------------------------------
# C4 — advisory `gates.repo_identity` recording
# (docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md § C4).
#
# DR-277: this guard is advisory-by-default and clears no hard-deny
# carve-out for the repo-identity gate. These tests construct a REAL
# MISMATCH via real harness-registry files on disk (C1's fixture pattern,
# `pickup_assemble/tests/test_repo_identity_gate.py`) rather than
# monkeypatching `compute_repo_identity_gate`'s own return value, and pin
# the load-bearing property: the verdict is RECORDED, and the guard's
# ALLOW/DENY decision is UNCHANGED by it.
# ---------------------------------------------------------------------------


def _epoch_to_filetime_ticks(epoch: float) -> int:
    return int((epoch + hr._FILETIME_EPOCH_OFFSET_SEC) * hr._FILETIME_TICKS_PER_SEC)


def _write_registry_record(sessions_dir, filename, session_id, pid, cwd, epoch=None):
    sessions_dir.mkdir(parents=True, exist_ok=True)
    if epoch is None:
        epoch = time.time() - 60
    payload = {
        "sessionId": session_id,
        "pid": pid,
        "procStart": _epoch_to_filetime_ticks(epoch),
        "cwd": str(cwd),
    }
    (sessions_dir / filename).write_text(json.dumps(payload), encoding="utf-8")
    return epoch


def _make_repo(root) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(parents=True, exist_ok=True)


def _patch_pid_env(monkeypatch, pid, create_time=0.0, hit=True):
    if hit:
        monkeypatch.setattr(
            "coordinator_core.session.core._resolve_claude_pid_from_env",
            lambda: ((pid, create_time), "env-hit"),
        )
    else:
        monkeypatch.setattr(
            "coordinator_core.session.core._resolve_claude_pid_from_env",
            lambda: (None, "env-miss:absent"),
        )


def test_advisory_log_records_mismatch_and_decision_is_unchanged(tmp_path, monkeypatch):
    """THE LOAD-BEARING TEST: a real repo-identity MISMATCH is recorded to
    the advisory log AND `check()`'s decision (ALLOW, for an unprotected
    file) is unchanged by it -- proving the guard is advisory-only, not
    merely intended to be. A test that only asserted the verdict was
    emitted would pass against a version that also refused."""
    repo_root = tmp_path / "repo"
    foreign_root = tmp_path / "foreign"
    _make_repo(repo_root)
    _make_repo(foreign_root)
    sessions_dir = tmp_path / "sessions"
    _write_registry_record(sessions_dir, "9001.json", "sess-mismatch", 9001, foreign_root)
    monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
    _patch_pid_env(monkeypatch, 9001)
    monkeypatch.setattr(
        "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
        lambda pid, stored_start_epoch="": True,
    )
    monkeypatch.setattr(guard, "_git_root", lambda: str(repo_root))

    unprotected = repo_root / "some_file.py"
    unprotected.write_text("x = 1\n", encoding="utf-8")

    # A real session's hub directory is created by `core.init`; the advisory
    # logger no longer creates one itself (it used to `mkdir(parents=True)`,
    # which let an advisory line mint a phantom session for any id). Stand one
    # up here so this test still exercises the logging path it exists to pin.
    (repo_root / ".git" / "coordinator-sessions" / "sess-mismatch").mkdir(parents=True)

    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(unprotected)},
        "session_id": "sess-mismatch",
    }
    result = guard.check(payload)

    # Decision unchanged: this file is not a protected doctrine surface, so
    # the guard must ALLOW regardless of the MISMATCH verdict computed.
    assert result is None

    log_path = repo_root / ".git" / "coordinator-sessions" / "sess-mismatch" / "repo-identity-gate.log"
    assert log_path.is_file(), "advisory gates.repo_identity fact was not recorded"
    contents = log_path.read_text(encoding="utf-8")
    assert "gates.repo_identity" in contents
    assert "verdict=MISMATCH" in contents


def test_advisory_log_mismatch_does_not_add_a_second_deny_on_protected_file(tmp_path, monkeypatch):
    """A protected doctrine surface with NO approval sentinel already denies
    for its own (unrelated) reason. This pins that a MISMATCH verdict does
    not change *why* it denies -- the deny reason stays the doctrine-surface
    message, never a repo-identity refusal."""
    repo_root = tmp_path / "repo"
    foreign_root = tmp_path / "foreign"
    _make_repo(repo_root)
    _make_repo(foreign_root)
    sessions_dir = tmp_path / "sessions"
    _write_registry_record(sessions_dir, "9002.json", "sess-mismatch-2", 9002, foreign_root)
    monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
    _patch_pid_env(monkeypatch, 9002)
    monkeypatch.setattr(
        "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
        lambda pid, stored_start_epoch="": True,
    )
    monkeypatch.setattr(guard, "_git_root", lambda: str(repo_root))

    protected = repo_root / "CLAUDE.md"
    protected.write_text("doctrine\n", encoding="utf-8")

    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(protected)},
        "session_id": "sess-mismatch-2",
    }
    result = guard.check(payload)

    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "doctrine" in reason.lower()
    assert "repo" not in reason.lower() or "repository" not in reason.lower()


def test_advisory_log_not_written_without_repo_root(monkeypatch):
    """No repo root resolvable -> the gate is never called and no advisory
    log write is attempted (mirrors the guard's own fail-open-on-repo-root
    posture for this advisory addendum -- see `check()`)."""
    calls = []
    monkeypatch.setattr(
        "coordinator_core.write_guards.guard_doctrine_surface_edits.compute_repo_identity_gate",
        lambda *a, **k: calls.append((a, k)) or {"verdict": "UNRESOLVED"},
    )
    monkeypatch.setattr(guard, "_git_root", lambda: None)

    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/whatever.py"},
        "session_id": "sess-no-root",
    }
    guard.check(payload)
    assert calls == []


def test_advisory_log_never_creates_a_session_dir(tmp_path, monkeypatch):
    """An advisory log line must never MINT session state.

    `_log_repo_identity_gate` used to `mkdir(parents=True, exist_ok=True)` its
    target, so any `session_id` it was handed -- including the fixture ids
    tests exercise this guard with -- became a real directory under
    `.git/coordinator-sessions/`. `liveness.live_session_ids` enumerates every
    non-denylisted child there as a SESSION, so those dirs entered the corpus
    that claim attribution and scope computation read. Nine had accumulated in
    this repo's own hub by 2026-08-19 and were still accruing writes, which is
    why deleting them without fixing this writer would not have held.

    The decision must stay ALLOW either way -- dropping the line is not
    allowed to change what the guard permits."""
    repo_root = tmp_path / "repo"
    foreign_root = tmp_path / "foreign"
    _make_repo(repo_root)
    _make_repo(foreign_root)
    sessions_dir = tmp_path / "sessions"
    _write_registry_record(sessions_dir, "9002.json", "sess-phantom", 9002, foreign_root)
    monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
    _patch_pid_env(monkeypatch, 9002)
    monkeypatch.setattr(
        "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
        lambda pid, stored_start_epoch="": True,
    )
    monkeypatch.setattr(guard, "_git_root", lambda: str(repo_root))

    unprotected = repo_root / "some_file.py"
    unprotected.write_text("x = 1\n", encoding="utf-8")

    result = guard.check(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(unprotected)},
            "session_id": "sess-phantom",
        }
    )

    assert result is None, "dropping the advisory line must not change the decision"
    assert not (repo_root / ".git" / "coordinator-sessions" / "sess-phantom").exists(), (
        "the advisory logger minted a session directory for a session that has "
        "none -- live_session_ids will enumerate it as a phantom session"
    )


# ---------------------------------------------------------------------------
# Cross-repo mis-anchor: the two DoE-only surfaces must follow the DoE root,
# not the session's cwd. Measured 2026-09-17 from a session whose cwd was
# claude-klabauter: both resolved to nonexistent claude-klabauter paths while the
# real DoE files edited clean.
# ---------------------------------------------------------------------------

_DOE_ONLY_RELATIVES = [
    ("coordinator", "snippets", "em-operating-doctrine.md"),
    ("global-doctrine", "CLAUDE.md"),
]


@pytest.fixture
def two_roots(tmp_path, monkeypatch):
    """A DoE root and an unrelated session repo, with the pointer resolved to
    the former and `_git_root()` to the latter -- the shape the mis-anchor
    needed to be visible at all. Clears the module's per-process DoE-root
    memo both before and after, so ordering against any other test that has
    already resolved it cannot decide this one.
    """
    doe = tmp_path / "DoE-claude"
    (doe / "coordinator" / "snippets").mkdir(parents=True)
    (doe / "global-doctrine").mkdir(parents=True)
    session_repo = tmp_path / "project-peer"
    session_repo.mkdir()

    guard._doe_root_memo.clear()
    monkeypatch.setattr(guard, "read_doe_root_pointer", lambda: str(doe))
    monkeypatch.setattr(guard, "_git_root", lambda: str(session_repo))
    yield doe, session_repo
    guard._doe_root_memo.clear()


def _verdict(target) -> str:
    result = guard.check(
        {"tool_name": "Edit", "session_id": "t", "tool_input": {"file_path": str(target)}}
    )
    return "allow" if result is None else "deny"


@pytest.mark.parametrize("relative", _DOE_ONLY_RELATIVES)
def test_doe_only_surfaces_are_protected_from_a_foreign_session(two_roots, relative):
    doe, _session_repo = two_roots
    assert _verdict(doe.joinpath(*relative)) == "deny"


@pytest.mark.parametrize("relative", _DOE_ONLY_RELATIVES)
def test_doe_only_surface_approval_is_read_at_the_doe_root(two_roots, relative):
    """The sentinel follows the OWNING repo. An approval in the editing
    session's own repo must not authorize an edit to DoE's doctrine --
    otherwise any repo on the box approves fleet-wide doctrine changes.
    """
    doe, session_repo = two_roots
    target = doe.joinpath(*relative)

    _fresh(session_repo / _SENTINEL_NAME)
    assert _verdict(target) == "deny", (
        "an approval created in the editing session's repo authorized an edit "
        "to another repo's doctrine surface"
    )

    _fresh(doe / _SENTINEL_NAME)
    assert _verdict(target) == "allow"


def test_unresolvable_doe_pointer_lands_on_the_pre_fix_protected_set(
    two_roots, monkeypatch
):
    """Fail-quiet, not fail-error. The DoE-anchored entries are ADDITIVE, so
    an unresolvable pointer must simply drop them and leave every path the
    guard already protected still protected.
    """
    doe, session_repo = two_roots
    guard._doe_root_memo.clear()
    monkeypatch.setattr(guard, "read_doe_root_pointer", lambda: "")

    paths = [path for path, _ in guard._protected_entries(str(session_repo))]
    assert guard._norm(str(doe.joinpath(*_DOE_ONLY_RELATIVES[0]))) not in paths
    assert guard._norm(str(session_repo / "CLAUDE.md")) in paths
    assert guard._norm(str(session_repo / "coordinator.local.md")) in paths
    assert guard._home_claude_md() in paths


@pytest.mark.parametrize("name", ["CLAUDE.md", "coordinator.local.md"])
def test_per_repo_surfaces_still_anchor_on_the_session_repo(two_roots, name):
    """The per-repo family is NOT the mis-anchor and must not move: every
    repo carries its own copy, each governing that repo's own sessions.
    """
    _doe, session_repo = two_roots
    assert _verdict(session_repo / name) == "deny"
    entry_roots = {
        owning
        for path, owning in guard._protected_entries(str(session_repo))
        if path == guard._norm(str(session_repo / name))
    }
    assert entry_roots == {None}, (
        f"{name} gained a hard-coded owning root -- it must keep reading the "
        "session's own repo root"
    )


# ---------------------------------------------------------------------------
# Foreign-repo (non-DoE) root doctrine files -- bug-blitz issue #88.
# `_protected_entries` only ever anchors on the session's own repo_root and
# the registry-pointed DoE root, so a THIRD repo's own root CLAUDE.md (e.g.
# a example-retrieval-repo session editing example-retrieval-repo-ue-addon/CLAUDE.md) was
# invisible to the guard even though it is the same always-loaded-per-its-
# own-sessions class of file DoE's surfaces are.
# ---------------------------------------------------------------------------


@pytest.fixture
def foreign_third_repo(tmp_path, monkeypatch):
    """A session repo and an unrelated THIRD repo -- neither the DoE root
    nor the session's own repo_root -- to isolate the generic
    `_foreign_repo_root_surface` fallback from the DoE-specific anchor.
    """
    session_repo = tmp_path / "example-retrieval-repo"
    third_repo = tmp_path / "example-retrieval-repo-ue-addon"
    _make_repo(session_repo)
    _make_repo(third_repo)

    guard._doe_root_memo.clear()
    monkeypatch.setattr(guard, "read_doe_root_pointer", lambda: "")
    monkeypatch.setattr(guard, "_git_root", lambda: str(session_repo))
    yield session_repo, third_repo
    guard._doe_root_memo.clear()


@pytest.mark.parametrize("name", ["CLAUDE.md", "coordinator.local.md"])
def test_foreign_third_repo_root_doctrine_is_protected(foreign_third_repo, name):
    """The consistency bug itself: editing another repo's own root doctrine
    file from a session rooted elsewhere must deny, exactly as editing the
    session's own root doctrine file does.
    """
    _session_repo, third_repo = foreign_third_repo
    assert _verdict(third_repo / name) == "deny"


def test_foreign_third_repo_approval_is_read_at_its_own_root(foreign_third_repo):
    """Sentinel-follows-owning-repo holds for the generic fallback too: an
    approval sitting in the editing session's repo must not authorize an
    edit to a third repo's doctrine.
    """
    session_repo, third_repo = foreign_third_repo
    target = third_repo / "CLAUDE.md"

    _fresh(session_repo / _SENTINEL_NAME)
    assert _verdict(target) == "deny"

    _fresh(third_repo / _SENTINEL_NAME)
    assert _verdict(target) == "allow"


def test_nested_claude_md_in_a_foreign_repo_stays_unprotected(foreign_third_repo):
    """The exact-match, not substring/basename, contract survives the
    generalization: a CLAUDE.md nested below a foreign repo's root (not
    the root itself) must not match."""
    _session_repo, third_repo = foreign_third_repo
    nested = third_repo / "coordinator" / "tests" / "CLAUDE.md"
    nested.parent.mkdir(parents=True)
    assert _verdict(nested) == "allow"


# ---------------------------------------------------------------------------
# C3 (docs/plans/2026-09-23-guard-dispatch-residuals.md): the EM-audience
# override-keys-doc pointer names an approval route (create the sentinel at
# its owning root) that a cloud session cannot reach -- the operator who
# grants approval must do so from a workstation session. Only the
# resolves_em_audience branch, only under CLAUDE_CODE_REMOTE=="true" (the
# same read as hooks/repin_cloud_engine_root.py's REMOTE_ENV_VAR), swaps the
# doc pointer for one fact plus one terse alternative. Subagent/unresolved
# audience text must stay byte-identical under both venues.
# ---------------------------------------------------------------------------


def test_em_audience_remote_true_names_the_venue_not_the_doc(monkeypatch):
    monkeypatch.setattr(guard, "resolves_em_audience", lambda payload, root: True)
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    reason = guard._deny_reason("CLAUDE.md", payload={})
    assert "cannot be granted from a cloud session" in reason
    assert "workstation session" in reason
    assert "guard-override-keys.md" not in reason
    assert guard._SENTINEL_NAME not in reason


def test_em_audience_remote_unset_keeps_todays_doc_pointer(monkeypatch):
    monkeypatch.setattr(guard, "resolves_em_audience", lambda payload, root: True)
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    reason = guard._deny_reason("CLAUDE.md", payload={})
    assert "guard-override-keys.md" in reason
    assert "cloud session" not in reason
    assert guard._SENTINEL_NAME not in reason


@pytest.mark.parametrize("remote", ["true", None])
def test_subagent_audience_text_is_byte_identical_across_venue(monkeypatch, remote):
    monkeypatch.setattr(guard, "resolves_em_audience", lambda payload, root: False)
    if remote is None:
        monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CODE_REMOTE", remote)
    reason = guard._deny_reason("CLAUDE.md", payload={})
    assert "cloud session" not in reason
    assert "guard-override-keys.md" not in reason
    assert guard._SENTINEL_NAME not in reason


def test_subagent_audience_unchanged_regardless_of_remote_env(monkeypatch):
    """Pin byte-identity directly, not just absence of the new strings."""
    monkeypatch.setattr(guard, "resolves_em_audience", lambda payload, root: False)
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    without_remote = guard._deny_reason("CLAUDE.md", payload={})
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    with_remote = guard._deny_reason("CLAUDE.md", payload={})
    assert without_remote == with_remote


def test_in_cloud_session_reads_the_same_env_var_as_repin_cloud_engine_root(monkeypatch):
    from coordinator_core.hooks import repin_cloud_engine_root as venue

    monkeypatch.setenv(venue.REMOTE_ENV_VAR, venue.REMOTE_ENV_TRUE)
    assert guard._in_cloud_session() is True
    monkeypatch.setenv(venue.REMOTE_ENV_VAR, "false")
    assert guard._in_cloud_session() is False
    monkeypatch.delenv(venue.REMOTE_ENV_VAR, raising=False)
    assert guard._in_cloud_session() is False


def test_doe_root_is_resolved_once_per_process(two_roots, monkeypatch):
    """The resolver runs on the PreToolUse path for every Write/Edit. The memo
    is what keeps a registry read plus two file reads off that path per call.
    """
    doe, session_repo = two_roots
    guard._doe_root_memo.clear()
    calls = []

    def _counted():
        calls.append(1)
        return str(doe)

    monkeypatch.setattr(guard, "read_doe_root_pointer", _counted)
    for _ in range(4):
        guard._protected_entries(str(session_repo))
    assert len(calls) == 1

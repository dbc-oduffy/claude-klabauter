"""Tests for coordinator_core.bash_guards._write_bump_session_start -- the
write-confinement speed bump's SessionStart anchor record.

Spec backlink: DoE-claude:pln-write-confinement-guards-cross-996567, chunk C0.
Extended by: docs/plans/2026-08-03-write-bump-anchor-outside-the-guarded-repo.md, chunk C1
    (the settings-home anchor hub, its env-injection contract, and its session-end cleanup).
Covers: record creation on SessionStart, read-back after a `cd`-equivalent (resolving
from a different subdirectory), fail-open on an unwritable/unreadable target, the
liveness-probe verdict pinned as a fixture so C2's "primary vs corroborating anchor"
docstring claim stays honest as the harness evolves, the settings-home hub's write/read/
delete, its `env`-injection contract, and its settings-home-first read precedence over the
in-repo record.

Test isolation (binds AC13): every test here that exercises the anchor runs under the
`_isolated_settings_home` autouse fixture below, which `setenv`s `COORDINATOR_SETTINGS_HOME`
to a `tmp_path` subdirectory rather than leaving the developer's real
`~/.coordinator-claude-settings/claude-klabauter/` reachable -- this module's
`write_session_start_record` now ALWAYS attempts a settings-home write as a side effect
(see that function's own docstring), so every test that calls it must be isolated, not only
the ones that assert on the settings-home hub directly.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _write_bump_session_start as session_start

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture(autouse=True)
def _isolated_settings_home(tmp_path, monkeypatch):
    """AC13: isolate every test in this module from the developer's real settings home.

    `setenv`s `COORDINATOR_SETTINGS_HOME` to a `tmp_path` subdirectory -- NOT a `delenv` of
    `HOME`/`CLAUDE_HOME`/`USERPROFILE`, which (per AC13) would make
    `_settings_home_dir_from_env` return `""` and silently no-op the settings-home anchor in
    exactly the suite meant to test it. `COORDINATOR_SETTINGS_HOME` is the FIRST rung
    `_settings_home_dir_from_env` checks, so setting it here is sufficient in isolation --
    it does not need HOME/CLAUDE_HOME/USERPROFILE to be scrubbed as well.
    """
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(str(root), "init", "-q")
    _git(str(root), "config", "user.email", "t@example.com")
    _git(str(root), "config", "user.name", "Test")
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(str(root), "add", "README.md")
    _git(str(root), "commit", "-q", "-m", "init")
    return root


# ---------------------------------------------------------------------------
# write_session_start_record -- creation on SessionStart, idempotence, fail-open
# ---------------------------------------------------------------------------


def test_write_creates_record_with_default_cwd(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    monkeypatch.chdir(root)

    assert session_start.write_session_start_record("sess-1") is True

    record = root / ".git" / "coordinator-sessions" / "sess-1" / "write_bump_launch_cwd"
    assert record.is_file()
    assert record.read_text(encoding="utf-8").strip() == str(root)


def test_write_creates_record_with_explicit_launch_cwd(tmp_path):
    root = _init_repo(tmp_path)

    assert session_start.write_session_start_record("sess-2", launch_cwd=str(root)) is True

    record = root / ".git" / "coordinator-sessions" / "sess-2" / "write_bump_launch_cwd"
    assert record.read_text(encoding="utf-8").strip() == str(root)


def test_write_is_idempotent_across_repeat_calls(tmp_path):
    root = _init_repo(tmp_path)

    assert session_start.write_session_start_record("sess-3", launch_cwd=str(root)) is True
    assert session_start.write_session_start_record("sess-3", launch_cwd=str(root)) is True

    record = root / ".git" / "coordinator-sessions" / "sess-3" / "write_bump_launch_cwd"
    assert record.read_text(encoding="utf-8").strip() == str(root)


def test_write_fails_open_when_session_id_empty(tmp_path):
    root = _init_repo(tmp_path)
    assert session_start.write_session_start_record("", launch_cwd=str(root)) is False


def test_write_fails_open_when_not_in_a_git_repo(tmp_path):
    scratch = tmp_path / "not-a-repo"
    scratch.mkdir()
    assert session_start.write_session_start_record("sess-4", launch_cwd=str(scratch)) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
def test_write_fails_open_on_unwritable_sessions_dir(tmp_path):
    root = _init_repo(tmp_path)
    sessions_hub = root / ".git" / "coordinator-sessions"
    sessions_hub.mkdir(parents=True)

    original_mode = sessions_hub.stat().st_mode
    try:
        os.chmod(sessions_hub, 0o500)  # read+execute only, no write
        # Must not raise -- an unwritable hub is a missed bump for this session, never
        # a dead end for the calling hook.
        assert session_start.write_session_start_record("sess-5", launch_cwd=str(root)) is False
    finally:
        os.chmod(sessions_hub, original_mode)


# ---------------------------------------------------------------------------
# read_session_start_record -- read-back survives a `cd`, absence, fail-open
# ---------------------------------------------------------------------------


def test_read_returns_none_when_absent(tmp_path):
    root = _init_repo(tmp_path)
    assert session_start.read_session_start_record("no-such-session", cwd=str(root)) is None


def test_read_returns_none_when_session_id_empty(tmp_path):
    root = _init_repo(tmp_path)
    assert session_start.read_session_start_record("", cwd=str(root)) is None


def test_read_returns_none_when_not_in_a_git_repo(tmp_path):
    scratch = tmp_path / "not-a-repo"
    scratch.mkdir()
    assert session_start.read_session_start_record("sess-1", cwd=str(scratch)) is None


def test_read_round_trips_the_written_launch_cwd(tmp_path):
    root = _init_repo(tmp_path)
    session_start.write_session_start_record("sess-6", launch_cwd=str(root))

    assert session_start.read_session_start_record("sess-6", cwd=str(root)) == str(root)


def test_read_survives_resolution_from_a_different_subdirectory(tmp_path):
    """The whole point of this record: an intervening `cd` moves the live payload cwd,
    but the session hub -- and therefore this record -- is reachable from anywhere in
    the same repo, unaffected by where the read is issued from."""
    root = _init_repo(tmp_path)
    subdir = root / "nested" / "deeper"
    subdir.mkdir(parents=True)

    session_start.write_session_start_record("sess-7", launch_cwd=str(root))

    assert session_start.read_session_start_record("sess-7", cwd=str(subdir)) == str(root)


def test_read_returns_none_for_empty_record_file(tmp_path):
    root = _init_repo(tmp_path)
    session_dir = root / ".git" / "coordinator-sessions" / "sess-8"
    session_dir.mkdir(parents=True)
    (session_dir / "write_bump_launch_cwd").write_text("", encoding="utf-8")

    assert session_start.read_session_start_record("sess-8", cwd=str(root)) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
def test_read_fails_open_on_unreadable_record(tmp_path):
    root = _init_repo(tmp_path)
    session_dir = root / ".git" / "coordinator-sessions" / "sess-9"
    session_dir.mkdir(parents=True)
    record = session_dir / "write_bump_launch_cwd"
    record.write_text(str(root), encoding="utf-8")

    original_mode = record.stat().st_mode
    try:
        os.chmod(record, 0o000)
        # Must not raise -- an unreadable record is treated exactly like an absent one.
        assert session_start.read_session_start_record("sess-9", cwd=str(root)) is None
    finally:
        os.chmod(record, original_mode)


# ---------------------------------------------------------------------------
# liveness probe -- CLAUDE_PROJECT_DIR in a real hook subprocess environment
# ---------------------------------------------------------------------------


def test_liveness_probe_verdict_is_pinned():
    """Pins the C0 dispatch's liveness-probe finding: `CLAUDE_PROJECT_DIR` was ABSENT
    from the environment of a live confined Bash-tool subprocess (the closest
    first-party evidence obtainable for this dispatch -- see the module docstring's
    "Anchor status" paragraph for the full evidentiary basis, including the three
    non-hook call sites elsewhere in this codebase that all fall back to PWD/getcwd()).

    This is a doctrine-honesty pin, not a live re-probe: it fails loud the moment
    someone flips the module constant without re-deriving the docstring/C2 claim
    alongside it, rather than letting the two silently drift apart. A genuine future
    re-probe (ideally against a real PreToolUse(Bash) hook subprocess, not merely a
    Bash-tool subprocess) that finds a different verdict must update the constant,
    this fixture, AND this module's + C2's docstrings in the same change.
    """
    assert session_start.CLAUDE_PROJECT_DIR_LIVE_IN_HOOK_ENV is False


def test_environment_probe_fixture_matches_dispatch_evidence(monkeypatch):
    """Direct re-run of the C0 dispatch's own probe shape, isolated from whatever the
    ambient test-runner's environment happens to carry: with CLAUDE_PROJECT_DIR
    deliberately unset, `os.environ.get` returns None, matching the ABSENT verdict."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert os.environ.get("CLAUDE_PROJECT_DIR") is None


# ---------------------------------------------------------------------------
# settings-home anchor hub -- C1 (write-bump-anchor-outside-the-guarded-repo)
# ---------------------------------------------------------------------------


def test_write_lands_settings_home_record_at_the_forward_bound_path(tmp_path, monkeypatch):
    """AC11: the settings-home anchor record lives under
    `$(coordinator-settings-home)/claude-klabauter/write-bump-anchor/`, never the settings-home
    root or another ad-hoc `~/` path."""
    root = _init_repo(tmp_path)
    settings_home = os.environ["COORDINATOR_SETTINGS_HOME"]

    assert session_start.write_session_start_record("sess-h1", launch_cwd=str(root)) is True

    record = (
        Path(settings_home)
        / "claude-klabauter"
        / "write-bump-anchor"
        / "sess-h1"
        / "write_bump_launch_cwd"
    )
    assert record.is_file()
    assert record.read_text(encoding="utf-8").strip() == str(root)


def test_write_lands_settings_home_record_even_when_not_in_a_git_repo(tmp_path):
    """The settings-home hub is cwd-independent by construction (no git involved) -- unlike
    the in-repo record, it must land even when `launch_cwd` names a directory with no git
    root at all. The overall return value still reflects ONLY the in-repo write (unchanged
    semantics -- see the next test), but the settings-home side effect must not be skipped."""
    settings_home = os.environ["COORDINATOR_SETTINGS_HOME"]
    scratch = Path(settings_home).parent / "not-a-repo"
    scratch.mkdir(parents=True)

    session_start.write_session_start_record("sess-h2", launch_cwd=str(scratch))

    record = (
        Path(settings_home)
        / "claude-klabauter"
        / "write-bump-anchor"
        / "sess-h2"
        / "write_bump_launch_cwd"
    )
    assert record.is_file()
    assert record.read_text(encoding="utf-8").strip() == str(scratch)


def test_write_return_value_reflects_only_in_repo_write_when_not_in_a_git_repo(tmp_path):
    """Pins the "RETURN-VALUE SEMANTICS ARE UNCHANGED" contract: a settings-home write
    succeeding must not flip an in-repo failure (no git root) into a `True` return."""
    scratch = tmp_path / "not-a-repo"
    scratch.mkdir()
    assert session_start.write_session_start_record("sess-h3", launch_cwd=str(scratch)) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
def test_write_fails_open_when_settings_home_unwritable(tmp_path):
    """Fail-open on an unwritable settings home: the SessionStart hook must not crash, and
    the in-repo write's own success/failure must be unaffected."""
    root = _init_repo(tmp_path)
    settings_home = Path(os.environ["COORDINATOR_SETTINGS_HOME"])
    settings_home.mkdir(parents=True, exist_ok=True)

    original_mode = settings_home.stat().st_mode
    try:
        os.chmod(settings_home, 0o500)  # read+execute only, no write
        # Must not raise, and the in-repo write still succeeds independently.
        assert (
            session_start.write_session_start_record("sess-h4", launch_cwd=str(root)) is True
        )
    finally:
        os.chmod(settings_home, original_mode)

    record = root / ".git" / "coordinator-sessions" / "sess-h4" / "write_bump_launch_cwd"
    assert record.is_file()


def test_read_prefers_settings_home_record_over_in_repo_record(tmp_path):
    """Settings-home-first read order: when both records exist for a session id but disagree
    (simulating a foreign-repo `cwd` at read time whose in-repo record, if any, would be
    someone else's), the settings-home value wins."""
    root = _init_repo(tmp_path)
    session_start.write_session_start_record("sess-h5", launch_cwd=str(root))

    # Overwrite ONLY the in-repo record with a different value, leaving the settings-home
    # record as the original launch cwd -- the read must still return the settings-home value.
    in_repo_record = root / ".git" / "coordinator-sessions" / "sess-h5" / "write_bump_launch_cwd"
    in_repo_record.write_text("/some/other/foreign/repo", encoding="utf-8")

    assert session_start.read_session_start_record("sess-h5", cwd=str(root)) == str(root)


def test_read_falls_back_to_in_repo_record_when_settings_home_absent(tmp_path, monkeypatch):
    """When the settings-home hub has no record for this session id (e.g. it was written
    before this chunk landed, or the settings home was unwritable at write time), the read
    falls back to the in-repo record -- unchanged behaviour for the same-repo case."""
    root = _init_repo(tmp_path)
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)

    session_dir = root / ".git" / "coordinator-sessions" / "sess-h6"
    session_dir.mkdir(parents=True)
    (session_dir / "write_bump_launch_cwd").write_text(str(root), encoding="utf-8")

    assert session_start.read_session_start_record("sess-h6", cwd=str(root)) == str(root)


def test_write_and_read_honour_injected_env_over_process_environ(tmp_path, monkeypatch):
    """The `env` parameter is honoured for BOTH write and read -- an injected mapping
    resolves the settings home independently of `os.environ`, so a caller (e.g. C2's
    `bump_applies`) can thread one consistent mapping through every settings-home-anchored
    read in a single verdict."""
    root = _init_repo(tmp_path)
    injected_home = tmp_path / "injected-settings-home"
    injected_env = {"COORDINATOR_SETTINGS_HOME": str(injected_home)}

    # A different value lives in the process-environ settings home (set by the autouse
    # fixture) versus the injected one -- proves the injected mapping, not os.environ, wins.
    assert (
        session_start.write_session_start_record(
            "sess-h7", launch_cwd=str(root), env=injected_env
        )
        is True
    )

    injected_record = (
        injected_home / "claude-klabauter" / "write-bump-anchor" / "sess-h7" / "write_bump_launch_cwd"
    )
    assert injected_record.is_file()

    process_env_record = (
        Path(os.environ["COORDINATOR_SETTINGS_HOME"])
        / "claude-klabauter"
        / "write-bump-anchor"
        / "sess-h7"
    )
    assert not process_env_record.exists()

    assert (
        session_start.read_session_start_record("sess-h7", cwd=str(root), env=injected_env)
        == str(root)
    )


def test_delete_settings_home_session_record_removes_the_record(tmp_path):
    """AC12: `delete_settings_home_session_record` removes the settings-home anchor by an
    exact `session_id` match."""
    root = _init_repo(tmp_path)
    session_start.write_session_start_record("sess-h8", launch_cwd=str(root))
    settings_home = Path(os.environ["COORDINATOR_SETTINGS_HOME"])
    record_dir = settings_home / "claude-klabauter" / "write-bump-anchor" / "sess-h8"
    assert record_dir.exists()

    assert session_start.delete_settings_home_session_record("sess-h8") is True
    assert not record_dir.exists()


def test_delete_settings_home_session_record_does_not_prefix_match(tmp_path):
    """AC12 explicitly names this: deleting `sess-h9` must NOT remove a session whose id
    merely shares `sess-h9` as a prefix -- the settings-home hub is keyed one-record-per-
    exact-session-id, so no prefix-collision hazard should exist even accidentally."""
    root = _init_repo(tmp_path)
    session_start.write_session_start_record("sess-h9", launch_cwd=str(root))
    session_start.write_session_start_record("sess-h9-longer", launch_cwd=str(root))
    settings_home = Path(os.environ["COORDINATOR_SETTINGS_HOME"])
    other_record_dir = settings_home / "claude-klabauter" / "write-bump-anchor" / "sess-h9-longer"
    assert other_record_dir.exists()

    session_start.delete_settings_home_session_record("sess-h9")

    assert other_record_dir.exists()
    assert (other_record_dir / "write_bump_launch_cwd").is_file()


def test_delete_settings_home_session_record_is_idempotent_when_absent(tmp_path):
    """Idempotent: deleting a session id with no settings-home record is success, not
    failure -- "already gone or never written" is not an error condition."""
    assert session_start.delete_settings_home_session_record("no-such-session-h10") is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
def test_delete_settings_home_session_record_fails_open_on_permission_error(tmp_path):
    """Fail-open: a delete that cannot actually remove the record returns `False` rather
    than raising."""
    root = _init_repo(tmp_path)
    session_start.write_session_start_record("sess-h11", launch_cwd=str(root))
    settings_home = Path(os.environ["COORDINATOR_SETTINGS_HOME"])
    anchor_dir = settings_home / "claude-klabauter" / "write-bump-anchor"

    original_mode = anchor_dir.stat().st_mode
    try:
        os.chmod(anchor_dir, 0o500)  # read+execute only -- rmtree of a child dir will fail
        assert session_start.delete_settings_home_session_record("sess-h11") is False
    finally:
        os.chmod(anchor_dir, original_mode)


def test_read_session_start_record_docstring_corrects_the_false_safety_claim():
    """Pins the docstring correction this chunk makes: the docstring must no longer ASSERT
    the false "SAME hub regardless of which subdirectory of the repo it's resolved from"
    safety claim as true (it was silently false once `cwd` crosses a repo boundary) -- it
    may still quote/reference the old wording while explaining why it was wrong, but must
    now state the settings-home hub is the actual cross-repo-safe primary read."""
    doc = session_start.read_session_start_record.__doc__ or ""
    assert "SILENTLY FALSE" in doc
    assert "settings-home" in doc.lower()
    assert "PRIMARY read for the" in doc


@pytest.mark.parametrize(
    "traversal_id",
    ["../escaped", "..\\escaped", "a/../../escaped", "..", ".", "a/b", "a\\b"],
)
def test_settings_home_write_rejects_traversal_shaped_session_id(tmp_path, traversal_id):
    """Review: code-reviewer (`e2a586f9`) -- a traversal-shaped `session_id` must not escape
    the settings-home anchor hub. Fail-open: the write returns whatever the in-repo write
    would return on its own, never raises, and lands no settings-home record at all."""
    root = _init_repo(tmp_path)
    settings_home = Path(os.environ["COORDINATOR_SETTINGS_HOME"])

    assert session_start.write_session_start_record(traversal_id, launch_cwd=str(root)) is True

    anchor_root = settings_home / "claude-klabauter" / "write-bump-anchor"
    # No record must exist anywhere -- neither inside the hub (as a literal, unresolved
    # sub-path) nor escaped outside it (the settings-home root or its parent).
    for candidate in (
        anchor_root / traversal_id,
        settings_home / "escaped",
        settings_home.parent / "escaped",
    ):
        assert not candidate.exists()


def test_settings_home_read_rejects_traversal_shaped_session_id(tmp_path):
    """A traversal-shaped `session_id` must read back as absent, not raise and not resolve
    outside the hub."""
    assert session_start.read_session_start_record("../escaped", cwd=str(tmp_path)) is None


def test_settings_home_delete_rejects_traversal_shaped_session_id_and_is_idempotent():
    """A traversal-shaped `session_id` must be treated as "nothing to clean up" (idempotent
    success), never attempt a delete outside the hub."""
    assert session_start.delete_settings_home_session_record("../escaped") is True


def test_settings_home_still_accepts_a_plain_short_session_id(tmp_path):
    """AC8: short test session ids like `abc` remain valid at this sink -- the traversal
    guard must not regress the deliberately-short session ids this subsystem relies on
    elsewhere (see this module's own prefix-collision tests above)."""
    root = _init_repo(tmp_path)
    settings_home = Path(os.environ["COORDINATOR_SETTINGS_HOME"])

    assert session_start.write_session_start_record("abc", launch_cwd=str(root)) is True

    record = settings_home / "claude-klabauter" / "write-bump-anchor" / "abc" / "write_bump_launch_cwd"
    assert record.is_file()
    assert record.read_text(encoding="utf-8").strip() == str(root)
    assert session_start.read_session_start_record("abc", cwd=str(root)) == str(root)
    assert session_start.delete_settings_home_session_record("abc") is True
    assert not record.exists()


# ---------------------------------------------------------------------------
# The SessionStart anchor is a session-directory CONSTRUCTOR, and must never
# leave a half-initialised one (K-006 F0 / no_meta_json)
# ---------------------------------------------------------------------------


def test_write_leaves_no_session_dir_without_meta_json(tmp_path, monkeypatch):
    """The anchor write is the FIRST thing to reach the per-session hub in a
    session's life, and `session/core.py::init` has had no SessionStart caller
    since `session-init.py` was deleted (2026-07-15 hook kill). Before this was
    routed through `init`, a bare `mkdir` here minted a directory carrying
    `write_bump_launch_cwd` and nothing else -- no `stable_pid` (K-006's F0
    hazard), no `started_at`/`head_at_start`, and every later
    `update_meta_field` write silently no-opping. Measured live on this box
    2026-08-26: three peer sessions in exactly that shape.

    Fails against the bare-`mkdir` shape: the record is written, so the assert
    on the anchor still passes, but `meta.json` is absent.
    """
    root = _init_repo(tmp_path)
    monkeypatch.chdir(root)

    assert session_start.write_session_start_record("sid-init-constructor") is True

    sdir = root / ".git" / "coordinator-sessions" / "sid-init-constructor"
    assert (sdir / session_start._RECORD_FILENAME).is_file()
    assert (sdir / "meta.json").is_file(), (
        "the anchor minted a session directory with no meta.json -- "
        "the no_meta_json shape K-006's watch counts"
    )


def test_write_declines_to_mint_a_dir_when_init_cannot_write_the_record(
    tmp_path, monkeypatch
):
    """Fail-open direction: when `init` cannot leave a `meta.json`, the in-repo
    leg writes NOTHING rather than minting the record-less directory. The
    settings-home twin -- which `read_session_start_record` consults FIRST -- is
    unaffected, so the anchor still resolves.
    """
    root = _init_repo(tmp_path)
    monkeypatch.chdir(root)

    monkeypatch.setattr(
        session_start,
        "_session_init",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only hub")),
    )

    assert session_start.write_session_start_record("sid-init-fails") is False

    sdir = root / ".git" / "coordinator-sessions" / "sid-init-fails"
    assert not (sdir / session_start._RECORD_FILENAME).exists()
    assert not (sdir / "meta.json").exists()
    # The settings-home leg is independent and still carries the anchor.
    assert session_start.read_session_start_record("sid-init-fails") == str(root)

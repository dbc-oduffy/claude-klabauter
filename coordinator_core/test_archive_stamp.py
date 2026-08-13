"""
coordinator_core.test_archive_stamp — co-located pytest for coordinator_core.archive_stamp.

Independently re-derives expected outcomes from the underlying ops' own documented
contracts (handoff.stamp / handoff.transition / memo.transition), rather than
transcribing the bash oracle's behavior — the ops are the single source of truth this
module wraps, so these tests assert the wrapper's orchestration (SHA resolution,
session-id resolution, param building, exit-code propagation, ownership gating), not
the frontmatter-mutation logic itself (that's covered by the ops' own test suites).

Run: cd /Users/example-operator/X/claude-klabauter && python3 -m pytest coordinator_core/test_archive_stamp.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

# Import guards — fire @register_op side effects before any handler is invoked
# in-process by archive_stamp.
import coordinator_core.ops.handoff_archive_transition  # noqa: F401
import coordinator_core.ops.handoff_stamp  # noqa: F401
import coordinator_core.ops.handoff_transition  # noqa: F401
import coordinator_core.ops.memo_transition  # noqa: F401
import coordinator_core.ops.session.record_pickup  # noqa: F401

import coordinator_core.archive_stamp as arstamp

# Real-git spawn is load-bearing: archive_stamp's SHA resolution, session-id
# resolution, and ownership gating orchestrate over ACTUAL git-tracked repo
# state, and TestArchiveStampColdImport spawns a fresh interpreter to prove
# real cold-import behaviour — no mock stands in for either. Fixtures spin up
# per-test repos (mutation-heavy: stamps/transitions per test), so the git
# fixture is not hoisted to module scope.

# TestArchiveStampColdImport below spawns a fresh interpreter that imports
# coordinator_core. That child inherits cwd but NOT pytest's rootdir sys.path
# insertion, so it can only resolve the package when cwd is (or is under) the
# repo root -- from any other cwd it dies with ModuleNotFoundError before it
# can write anything to stdout. Pinning cwd to the repo root derived from this
# file's own path makes the subprocess resolvable regardless of the invoking
# shell's cwd.
_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}

# Ownership guard (2026-07-22): stamp_shipped_in now refuses to stamp a DERIVED
# sha unless its Session-Id trailer matches the CALLING session's own id (see
# archive_stamp._ownership_block_reason). This is the default "own" session id
# for every commit this file's `_git()` helper makes — the `_default_caller_
# session_id` autouse fixture below sets CLAUDE_SESSION_ID to this SAME value,
# so every existing happy-path test (none of which care about ownership) keeps
# stamping without per-test boilerplate. Tests that DO care about ownership
# (TestOwnershipGuard) pass an explicit `session_id=` override to seed a PEER
# commit instead.
_DEFAULT_TEST_SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _git(
    repo: Path, *args: str, session_id: Optional[str] = _DEFAULT_TEST_SESSION_ID
) -> subprocess.CompletedProcess:
    """Runs a git command in `repo`.

    `commit -m <msg>` calls get a `Session-Id: <session_id>` trailer auto-appended
    as a second paragraph — UNLESS the message already contains "Session-Id:"
    (an explicit trailer in the message always wins, letting a test seed a PEER
    commit by passing its own `Session-Id: <uuid>` in the `-m` text directly), OR
    `session_id=None` is passed explicitly (produces a genuinely trailer-less
    commit — for testing the "candidate has no trailer at all" guard path).
    Every other git subcommand (init, add, config, rev-parse, log, ...) is
    unaffected. `session_id` lets a caller seed a specific commit as a named
    OTHER session without hand-writing the trailer text — see TestOwnershipGuard.
    """
    args_list = list(args)
    if (
        len(args_list) >= 3
        and args_list[0] == "commit"
        and args_list[1] == "-m"
        and session_id is not None
        and "Session-Id:" not in args_list[2]
    ):
        args_list[2] = f"{args_list[2]}\n\nSession-Id: {session_id}"
    return subprocess.run(
        ["git", "-C", str(repo), *args_list],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )


@pytest.fixture(autouse=True)
def _default_caller_session_id(monkeypatch):
    """Ownership guard (2026-07-22): resolves the CALLING session's id via
    CLAUDE_SESSION_ID for every test in this file, matching the default trailer
    `_git()` stamps onto commits it makes (see `_DEFAULT_TEST_SESSION_ID` above).
    Autouse so this file's existing happy-path tests need no per-test setup;
    ownership-specific tests override per-commit via `_git(..., session_id=...)`."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", _DEFAULT_TEST_SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _seed_handoff(repo: Path, name: str, status: str, deployment_state: str, extra: str = "") -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        f"status: {status}\n"
        'predecessor: "none"\n'
        f"deployment_state: {deployment_state}\n"
    )
    if extra:
        fm += extra
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _seed_ledger_claim(
    repo: Path, handoff: Path, session_id: str, claimed_at: str, pid: str = "1"
) -> Path:
    """Seed a durable claim-ledger record for `handoff` under the repo's git dir.

    The ledger is branch-independent (it lives inside `.git/`, not the
    worktree), which is exactly why it survives the branch-switch revert that
    empties the tracked frontmatter mirror — see
    `coordinator_core.claim_state`'s module docstring for the incident.
    """
    from coordinator_core.claim_state import handoff_claim_dir

    claim_dir = handoff_claim_dir(repo / ".git", handoff)
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")
    (claim_dir / "pid").write_text(pid, encoding="utf-8")
    return claim_dir


def _seed_memo(repo: Path, name: str, status: str, extra: str = "") -> Path:
    path = repo / "cross-repo" / "inbox" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        "kind: fyi\n"
        f"status: {status}\n"
        "from: sender-session\n"
        "summary: A test memo.\n"
        "created: 2026-01-01\n"
    )
    if extra:
        fm += extra
    path.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _assert_shipped_in(path: Path, expected_sha: str) -> None:
    """Assert shipped_in == expected_sha[:8], parsing the YAML value rather than
    substring-matching the raw line.

    A raw `f"shipped_in: {sha[:8]}" in text` check is FLAKY: stamp_shipped_in
    writes SHAs with numeric_quoting=True, so an all-digit or
    scientific-notation-looking sha8 (e.g. 23983115, 3e770274) is emitted
    single-quoted and the bare substring match misses. Git produces such a sha8
    often enough to fail ~13% of runs (measured 2026-07-20, 2/15 on HEAD).

    read_fm_field alone is NOT sufficient either: it is a raw regex extractor
    that does NOT unquote, so it returns "'23983115'" (quotes included) for a
    quoted value — asymmetric with its serialize_yaml_scalar write counterpart.

    2026-07-21: the product-side asymmetry is fixed — read_fm_field_unquoted is
    the shared comparison-safe reader (frontmatter/primitives.py), and
    handoff_ship_archive.py's replay check now uses it. The explicit local
    unquote workaround is retired in favour of that primitive. The assertion
    itself is unchanged and remains exact-equality — it is NOT loosened.
    """
    from coordinator_core.frontmatter.primitives import (
        read_fm_field_unquoted,
        split_frontmatter,
    )

    split = split_frontmatter(path.read_text(encoding="utf-8"))
    assert split is not None
    raw = read_fm_field_unquoted(split.fm_text, "shipped_in")
    assert raw is not None, "shipped_in absent"
    assert raw == expected_sha[:8]


def _seed_handoff_with_predecessor(repo: Path, name: str, predecessor: str) -> Path:
    """Seeds a live, non-terminal handoff naming `predecessor` (bare filename,
    mirrors coordinator_core/ops/tests/conftest.py's HandoffRepo.seed_handoff
    predecessor convention) — used to make the reverse-membership guard
    (handoff.has_live_children) report the candidate as still referenced.

    Writes raw frontmatter directly (rather than composing with _seed_handoff,
    which hardcodes its own `predecessor: "none"` line) to avoid a duplicate-key
    frontmatter block."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        f'predecessor: "{predecessor}"\n'
        "deployment_state: active\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


# ---------------------------------------------------------------------------
# stamp_shipped_in
# ---------------------------------------------------------------------------

class TestStampShippedIn:
    def test_scope_paths_resolve_sha_and_stamp(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch widget")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        hp = _seed_handoff(
            repo, "h1.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )
        rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived')
        assert rc.exit_code == 0
        _assert_shipped_in(hp, expected_sha)

    def test_no_scope_no_fallback_is_noop(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h2.md", "claimed", "shipped")
        rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived')
        assert rc.exit_code == 0
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_no_scope_no_fallback_is_noop_prints_reason_to_stderr(self, tmp_path):
        """The 'nothing resolved' skip (line ~458) previously returned rc=0 with
        zero stderr output — indistinguishable from a deliberate no-stamp-needed
        success to a direct `archive-stamp-cli stamp-shipped-in` caller. Must now
        name the handoff and explain shipped_in was left unset."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h2b.md", "claimed", "shipped")

        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived')
        assert rc.exit_code == 0
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")
        out = buf.getvalue()
        assert "stamp_shipped_in:" in out
        assert "no commit found" in out
        assert str(hp) in out

    def test_branch_tip_fallback(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h3.md", "claimed", "shipped")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived', allow_branch_tip_fallback=True)
        assert rc.exit_code == 0
        _assert_shipped_in(hp, expected_sha)

    def test_scope_parser_skips_leading_comment(self, tmp_path):
        """A leading <!-- --> provenance comment ahead of frontmatter must not be
        mistaken for the frontmatter opener (mirrors the oracle's incmt awk guard)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "x.txt"
        touched.write_text("x\n", encoding="utf-8")
        _git(repo, "add", "x.txt")
        _git(repo, "commit", "-m", "touch x")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        path = repo / "state" / "handoffs" / "h4.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "<!-- provenance: installer-seeded -->\n"
            "---\n"
            'title: "T"\ncreated: 2026-01-01\nbranch: work/test/2026-01-01\n'
            'status: claimed\npredecessor: "none"\ndeployment_state: shipped\n'
            "scope:\n  - x.txt\n"
            "---\n\nBody.\n"
        )
        path.write_text(content, encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "add h4")

        rc = arstamp.stamp_shipped_in(str(path), kind='scope-derived')
        assert rc.exit_code == 0
        _assert_shipped_in(path, expected_sha)

    def test_explicit_sha_override_bypasses_noop(self, tmp_path):
        """A caller-supplied sha is ALWAYS stamped, even in the no-scope,
        would-otherwise-be-a-no-op case (mirrors test_no_scope_no_fallback_is_noop's
        seed, but with an explicit override)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h5.md", "claimed", "shipped")
        override_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        rc = arstamp.stamp_shipped_in(str(hp), kind='ship-commit', sha=override_sha)
        assert rc.exit_code == 0
        _assert_shipped_in(hp, override_sha)

    def test_explicit_sha_overrides_scope_derivation(self, tmp_path):
        """A caller-supplied sha wins over scope-derivation, even when scope-derivation
        would otherwise resolve a different, valid SHA."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget2.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch widget2")
        scope_derived_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        hp = _seed_handoff(
            repo, "h6.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )

        another = repo / "another.txt"
        another.write_text("y\n", encoding="utf-8")
        _git(repo, "add", "another.txt")
        _git(repo, "commit", "-m", "unrelated commit")
        override_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        assert override_sha != scope_derived_sha

        rc = arstamp.stamp_shipped_in(str(hp), kind='ship-commit', sha=override_sha)
        assert rc.exit_code == 0
        _assert_shipped_in(hp, override_sha)

    def test_empty_sha_falls_through_to_selfderive(self, tmp_path):
        """An empty-string sha is normalized to None and treated as omitted —
        backward-compat: no scope, no fallback, no stamp."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h7.md", "claimed", "shipped")
        rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived', sha="")
        assert rc.exit_code == 0
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_whitespace_only_sha_falls_through_to_selfderive(self, tmp_path):
        """A whitespace-only sha exercises the `.strip()` call itself (not just the
        falsy-empty-string short-circuit) and is normalized to None the same way."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h7b.md", "claimed", "shipped")
        rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived', sha="   ")
        assert rc.exit_code == 0
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_empty_sha_still_selfderives_on_happy_path(self, tmp_path):
        """An explicit sha="" call is truly indistinguishable from omitting the arg
        on the happy path too, not just the no-op path — scope-derived sha still
        gets stamped."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget3.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch widget3")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        hp = _seed_handoff(
            repo, "h7c.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )
        rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived', sha="")
        assert rc.exit_code == 0
        _assert_shipped_in(hp, expected_sha)

    def test_malformed_sha_override_rejected(self, tmp_path):
        """A non-hex override fails loud (rc==1) and writes nothing — the
        override path must not silently truncate and stamp an unvalidated
        caller-supplied string."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h8.md", "claimed", "shipped")
        rc = arstamp.stamp_shipped_in(str(hp), kind='ship-commit', sha="not-a-sha")
        assert rc.exit_code == 1
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_too_short_sha_override_rejected(self, tmp_path):
        """A too-short (< 7 hex chars) override also fails loud and writes nothing."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h9.md", "claimed", "shipped")
        rc = arstamp.stamp_shipped_in(str(hp), kind='ship-commit', sha="abc")
        assert rc.exit_code == 1
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_scalar_scope_resolves_sha_and_stamp(self, tmp_path):
        """A bare scalar `scope: <path>` (no list) must resolve identically to
        list form. 2026-07-22: the prior hand-rolled scanner silently returned
        [] for scalar scope, treating it as "no commit found" — this fixture
        gap is what hid that. `_parse_scope_paths` now delegates to
        `dag._read_meta`, which handles both forms."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_scalar.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch widget_scalar")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        hp = _seed_handoff(
            repo, "h13.md", "claimed", "shipped",
            extra=f"scope: {touched.relative_to(repo)}\n",
        )
        rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived')
        assert rc.exit_code == 0
        _assert_shipped_in(hp, expected_sha)

    def test_force_true_without_sha_rejected(self, tmp_path):
        """force=True with no sha override is rejected before any resolution —
        force must never trigger its own resolve-and-overwrite."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h10.md", "claimed", "shipped")
        rc = arstamp.stamp_shipped_in(str(hp), kind='ship-commit', force=True)
        assert rc.exit_code == 1
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_force_true_with_sha_replaces_existing(self, tmp_path):
        """force=True + sha replaces an already-present shipped_in value."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h11.md", "claimed", "shipped")
        first_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        rc1 = arstamp.stamp_shipped_in(str(hp), kind='ship-commit', sha=first_sha)
        assert rc1.exit_code == 0
        _assert_shipped_in(hp, first_sha)

        another = repo / "another2.txt"
        another.write_text("q\n", encoding="utf-8")
        _git(repo, "add", "another2.txt")
        _git(repo, "commit", "-m", "another commit")
        second_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert second_sha != first_sha

        rc2 = arstamp.stamp_shipped_in(str(hp), kind='ship-commit', sha=second_sha, force=True)
        assert rc2.exit_code == 0
        _assert_shipped_in(hp, second_sha)

    def test_force_false_default_against_already_stamped_is_noop(self, tmp_path):
        """Default force=False against an already-stamped handoff is unchanged:
        the silent idempotent no-op, rc 0, value untouched."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h12.md", "claimed", "shipped")
        first_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        rc1 = arstamp.stamp_shipped_in(str(hp), kind='ship-commit', sha=first_sha)
        assert rc1.exit_code == 0

        another = repo / "another3.txt"
        another.write_text("w\n", encoding="utf-8")
        _git(repo, "add", "another3.txt")
        _git(repo, "commit", "-m", "yet another commit")
        second_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        rc2 = arstamp.stamp_shipped_in(str(hp), kind='ship-commit', sha=second_sha)
        assert rc2.exit_code == 0
        _assert_shipped_in(hp, first_sha)


# ---------------------------------------------------------------------------
# shipped_in_kind — DR-096 (2026-07-26 ruling). stamp_shipped_in is the single
# choke point that owns the shipped_in value grammar (SHA shape OR the
# sanctioned no-commit token) AND the kind/sha cross-validation — this class
# covers that surface specifically (the enum, the required-kind contract, the
# kind<->override compatibility matrix, the no-commit token path, and the
# widened 7-64 hex ceiling). TestStampShippedIn above already exercises every
# existing resolution guard with a kind threaded through; this class is
# additive, not a duplicate of that coverage.
# ---------------------------------------------------------------------------


class TestShippedInKind:
    def test_kind_is_required_keyword_only(self, tmp_path):
        """kind has no default — omitting it is a TypeError, not a silent guess.
        This is the load-bearing assertion for 'required at the seam, not
        defaulted silently' (DR-096) — a regression here would recreate the
        exact five-meanings failure mode this pass exists to close."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "kind-req.md", "claimed", "shipped")
        with pytest.raises(TypeError):
            arstamp.stamp_shipped_in(str(hp))  # type: ignore[call-arg]

    def test_unknown_kind_rejected(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "kind-unknown.md", "claimed", "shipped")
        rc = arstamp.stamp_shipped_in(str(hp), kind="guessed")
        assert rc.exit_code == 1
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_scope_derived_writes_kind_in_lockstep(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_kind1.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch widget_kind1")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        hp = _seed_handoff(
            repo, "kind-scope.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )
        rc = arstamp.stamp_shipped_in(str(hp), kind="scope-derived")
        assert rc.exit_code == 0
        _assert_shipped_in(hp, expected_sha)
        assert "shipped_in_kind: scope-derived" in hp.read_text(encoding="utf-8")

    def test_ship_commit_override_writes_kind_in_lockstep(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "kind-shipcommit.md", "claimed", "shipped")
        override_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        rc = arstamp.stamp_shipped_in(str(hp), kind="ship-commit", sha=override_sha)
        assert rc.exit_code == 0
        _assert_shipped_in(hp, override_sha)
        assert "shipped_in_kind: ship-commit" in hp.read_text(encoding="utf-8")

    def test_successor_override_writes_kind_in_lockstep(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "kind-successor.md", "claimed", "shipped")
        override_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        rc = arstamp.stamp_shipped_in(str(hp), kind="successor", sha=override_sha)
        assert rc.exit_code == 0
        _assert_shipped_in(hp, override_sha)
        assert "shipped_in_kind: successor" in hp.read_text(encoding="utf-8")

    def test_no_commit_kind_stamps_token_verbatim_not_truncated(self, tmp_path):
        """The no-commit token must survive intact — `resolved[:8]`-style
        truncation (the format contract every hex SHA gets) would corrupt it."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "kind-nocommit.md", "claimed", "shipped")
        token = "substantively-shipped-no-commit:2026-07-26"

        rc = arstamp.stamp_shipped_in(str(hp), kind="no-commit", sha=token)
        assert rc.exit_code == 0
        from coordinator_core.frontmatter.primitives import (
            read_fm_field_unquoted,
            split_frontmatter,
        )
        split = split_frontmatter(hp.read_text(encoding="utf-8"))
        assert split is not None
        assert read_fm_field_unquoted(split.fm_text, "shipped_in") == token
        assert "shipped_in_kind: no-commit" in hp.read_text(encoding="utf-8")

    def test_ship_commit_kind_requires_override(self, tmp_path):
        """kind='ship-commit' with no sha override is rejected — this kind means
        'the caller already has a specific commit', which is never something
        this function derives on the caller's behalf."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "kind-shipcommit-noverride.md", "claimed", "shipped")
        rc = arstamp.stamp_shipped_in(str(hp), kind="ship-commit")
        assert rc.exit_code == 1
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_no_commit_kind_requires_override(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "kind-nocommit-noverride.md", "claimed", "shipped")
        rc = arstamp.stamp_shipped_in(str(hp), kind="no-commit")
        assert rc.exit_code == 1
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_scope_derived_kind_rejects_explicit_override(self, tmp_path):
        """kind='scope-derived' paired with an explicit sha override is a
        self-contradiction (self-derivation vs a caller-supplied commit) and is
        rejected fail-loud rather than silently picking one meaning."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "kind-scopederived-override.md", "claimed", "shipped")
        override_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        rc = arstamp.stamp_shipped_in(str(hp), kind="scope-derived", sha=override_sha)
        assert rc.exit_code == 1
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_no_commit_kind_rejects_hex_override(self, tmp_path):
        """kind='no-commit' paired with a hex-shaped override (not the token) is
        rejected — the two shapes may never cross-pollinate a kind they don't
        actually match."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "kind-nocommit-hex.md", "claimed", "shipped")
        override_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        rc = arstamp.stamp_shipped_in(str(hp), kind="no-commit", sha=override_sha)
        assert rc.exit_code == 1
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_ship_commit_kind_rejects_no_commit_token(self, tmp_path):
        """kind='ship-commit' paired with the no-commit token (not a hex sha) is
        rejected the same way, in the other direction."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "kind-shipcommit-token.md", "claimed", "shipped")
        rc = arstamp.stamp_shipped_in(
            str(hp), kind="ship-commit",
            sha="substantively-shipped-no-commit:2026-07-26",
        )
        assert rc.exit_code == 1
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_widened_64char_hex_override_accepted(self, tmp_path):
        """The override shape ceiling widened from 7-40 to 7-64 hex chars (DR-096)
        to match the ratified schema pattern — a SHA-256-shaped 64-char value
        must now be accepted, not just 7-40."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "kind-64char.md", "claimed", "shipped")
        sha64 = "a" * 64
        rc = arstamp.stamp_shipped_in(str(hp), kind="ship-commit", sha=sha64)
        assert rc.exit_code == 0
        _assert_shipped_in(hp, sha64)

    def test_65char_hex_override_still_rejected(self, tmp_path):
        """65 hex chars exceeds even the widened ceiling — still malformed."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "kind-65char.md", "claimed", "shipped")
        sha65 = "a" * 65
        rc = arstamp.stamp_shipped_in(str(hp), kind="ship-commit", sha=sha65)
        assert rc.exit_code == 1
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Co-commit guard (2026-07-26) — a scope-path-derived sha can never be the
# ship commit while the ship itself is still sitting uncommitted in the
# worktree: `_resolve_scope_sha` only ever sees git history that ALREADY
# exists, so "most recent commit touching scope" is necessarily the commit
# BEFORE the pending one the stamp write is about to be swept into.
# Reproduction of the originating incident (a handoff stamped 49 seconds
# before its actual ship commit).
# ---------------------------------------------------------------------------


class TestCoCommitGuard:
    def test_dirty_scope_path_refuses_derived_stamp(self, tmp_path):
        """Scope path has a genuine prior commit (so `_resolve_scope_sha` would
        otherwise resolve it), but the SAME path is currently dirty in the
        worktree — the structural signature of the ship not having landed yet.
        Must leave shipped_in UNSET, rc 0 (never fail loud, never re-resolve to
        a different candidate)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_cc.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch widget_cc")
        stale_candidate_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        # The actual ship: further, still-uncommitted edit to the scope path —
        # this is what stamp_shipped_in's write is about to be co-committed
        # alongside, and it does not exist as a commit yet.
        touched.write_text("#!/bin/sh\necho ship\n", encoding="utf-8")

        hp = _seed_handoff(
            repo, "cc1.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )
        rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived')
        assert rc.exit_code == 0
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")
        # Negative-spec check: the guard must not have silently substituted the
        # stale candidate either.
        assert stale_candidate_sha not in hp.read_text(encoding="utf-8")

    def test_dirty_scope_path_refusal_prints_reason_to_stderr(self, tmp_path):
        """Mirrors 794deb1d's convention (make an rc==0 decline audible on
        stderr) — the co-commit refusal must be detectable by a caller that
        parses stderr, the same way the ownership guard's refusal already is."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_cc2.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch widget_cc2")
        touched.write_text("#!/bin/sh\necho ship\n", encoding="utf-8")

        hp = _seed_handoff(
            repo, "cc2.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )

        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived')
        assert rc.exit_code == 0
        out = buf.getvalue()
        assert "stamp_shipped_in:" in out
        assert "co-commit guard" in out
        assert str(hp) in out

    def test_untracked_scope_path_refuses_derived_stamp(self, tmp_path):
        """An untracked (never-committed, never-added) new file at the scope
        path is also 'dirty' by this guard's definition — `git status
        --porcelain` reports it regardless of staged/unstaged/untracked."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_cc3.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch widget_cc3")

        untracked = repo / "coordinator" / "bin" / "widget_cc3_new.sh"
        untracked.write_text("#!/bin/sh\necho new\n", encoding="utf-8")

        hp = _seed_handoff(
            repo, "cc3.md", "claimed", "shipped",
            extra=(
                "scope:\n  - "
                f"{touched.relative_to(repo)}\n  - {untracked.relative_to(repo)}\n"
            ),
        )
        rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived')
        assert rc.exit_code == 0
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_clean_scope_path_unaffected_regression(self, tmp_path):
        """Regression guard: a scope path with NO uncommitted changes must
        keep stamping exactly as before this fix (mirrors
        test_scope_paths_resolve_sha_and_stamp)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_cc4.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch widget_cc4")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        hp = _seed_handoff(
            repo, "cc4.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )
        rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived')
        assert rc.exit_code == 0
        _assert_shipped_in(hp, expected_sha)

    def test_explicit_sha_override_bypasses_dirty_scope_guard(self, tmp_path):
        """An explicit `sha=` override is the caller's own ownership assertion
        (per `_ownership_block_reason`'s docstring) and must bypass this guard
        entirely too — mirrors `test_explicit_sha_override_bypasses_noop`."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_cc5.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch widget_cc5")
        override_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        touched.write_text("#!/bin/sh\necho ship\n", encoding="utf-8")

        hp = _seed_handoff(
            repo, "cc5.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )
        rc = arstamp.stamp_shipped_in(str(hp), kind='ship-commit', sha=override_sha)
        assert rc.exit_code == 0
        _assert_shipped_in(hp, override_sha)

    def test_force_path_bypasses_dirty_scope_guard(self, tmp_path):
        """force=True + an explicit sha (the provenance-repair path) must also
        bypass this guard entirely — force never triggers its own resolution,
        so a dirty scope path is irrelevant to it."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "cc6.md", "claimed", "shipped")
        first_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        rc1 = arstamp.stamp_shipped_in(str(hp), kind='ship-commit', sha=first_sha)
        assert rc1.exit_code == 0

        touched = repo / "coordinator" / "bin" / "widget_cc6.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch widget_cc6")
        second_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        touched.write_text("#!/bin/sh\necho dirty\n", encoding="utf-8")

        rc2 = arstamp.stamp_shipped_in(str(hp), kind='ship-commit', sha=second_sha, force=True)
        assert rc2.exit_code == 0
        _assert_shipped_in(hp, second_sha)


def _git_dated(
    repo: Path, msg: str, committer_date_iso: str, session_id: Optional[str] = _DEFAULT_TEST_SESSION_ID
) -> subprocess.CompletedProcess:
    """Mirrors `_git`'s Session-Id-trailer auto-append, but also pins BOTH the
    author and committer dates via env vars (`git commit --date=` only pins
    the author date; `_commit_committer_date`/`%cI` reads the committer date)
    — used to construct a candidate commit with a controlled, deterministic
    date for `TestNotAfterGuard`/`TestResolveSourceShipSha` without depending
    on wall-clock timing."""
    msg_full = msg
    if session_id is not None and "Session-Id:" not in msg:
        msg_full = f"{msg}\n\nSession-Id: {session_id}"
    env = {
        **_GIT_ENV,
        "GIT_AUTHOR_DATE": committer_date_iso,
        "GIT_COMMITTER_DATE": committer_date_iso,
    }
    return subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", msg_full],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# not_after guard (2026-08-04) — a scope-path-derived sha whose committer
# date postdates the cascade's own trigger timestamp is refused rather than
# stamped. See archive_stamp._scope_sha_postdates_trigger's docstring for the
# incident this closes.
# ---------------------------------------------------------------------------


class TestNotAfterGuard:
    def test_postdating_candidate_refuses_stamp(self, tmp_path):
        """Candidate commit is dated AFTER not_after (the cascade's own trigger
        timestamp) — must leave shipped_in UNSET, rc 0, never stamp it."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_na1.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git_dated(repo, "touch widget_na1", "2026-08-04T12:00:00+00:00")

        hp = _seed_handoff(
            repo, "na1.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )
        rc = arstamp.stamp_shipped_in(
            str(hp), kind="scope-derived", not_after="2026-07-01T00:00:00+00:00"
        )
        assert rc.exit_code == 0
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_postdating_refusal_prints_reason_to_stderr(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_na2.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git_dated(repo, "touch widget_na2", "2026-08-04T12:00:00+00:00")

        hp = _seed_handoff(
            repo, "na2.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )

        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.stamp_shipped_in(
                str(hp), kind="scope-derived", not_after="2026-07-01T00:00:00+00:00"
            )
        assert rc.exit_code == 0
        out = buf.getvalue()
        assert "stamp_shipped_in:" in out
        assert "not-after guard" in out
        assert str(hp) in out

    def test_predating_candidate_still_stamps(self, tmp_path):
        """A genuine scope-derived match that PREDATES not_after resolves
        exactly as before this guard existed — no regression."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_na3.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git_dated(repo, "touch widget_na3", "2026-06-01T00:00:00+00:00")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        hp = _seed_handoff(
            repo, "na3.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )
        rc = arstamp.stamp_shipped_in(
            str(hp), kind="scope-derived", not_after="2026-08-04T00:00:00+00:00"
        )
        assert rc.exit_code == 0
        _assert_shipped_in(hp, expected_sha)

    def test_not_after_omitted_is_prior_behaviour(self, tmp_path):
        """not_after=None (default) — every pre-existing caller — is a no-op
        for this guard regardless of the candidate's date."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_na4.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git_dated(repo, "touch widget_na4", "2026-08-04T12:00:00+00:00")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        hp = _seed_handoff(
            repo, "na4.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )
        rc = arstamp.stamp_shipped_in(str(hp), kind="scope-derived")
        assert rc.exit_code == 0
        _assert_shipped_in(hp, expected_sha)

    def test_unparseable_not_after_fails_closed(self, tmp_path):
        """A supplied but unparseable not_after cannot be evaluated — routes
        to refuse (fail-closed), never to assumed-plausible."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_na5.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch widget_na5")

        hp = _seed_handoff(
            repo, "na5.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )
        rc = arstamp.stamp_shipped_in(str(hp), kind="scope-derived", not_after="not-a-date")
        assert rc.exit_code == 0
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")


def _seed_handoff_with_created(repo: Path, name: str, created: str, scope_line: str) -> Path:
    """Like `_seed_handoff`, but with a caller-controlled `created:` field —
    `_seed_handoff` hardcodes `created: 2026-01-01`, which cannot exercise
    `TestCreatedDateGuard`'s pre/post-date scenarios."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        f"created: {created}\n"
        "branch: work/test/2026-01-01\n"
        "status: claimed\n"
        'predecessor: "none"\n'
        "deployment_state: shipped\n"
        f"{scope_line}"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


# ---------------------------------------------------------------------------
# Mechanical-commit walk-back (2026-08-05) — a scope-path's most recent
# toucher is very often housekeeping/archival machinery (`fleet: archive N
# ... handoff(s)`, `archive handoff: ...`, a corpus-wide DR-084 vocabulary
# migration), not the artifact's own ship commit. Confirmed live across two
# reverted `deliverable.cascade_terminal` drains — see
# `archive_stamp._resolve_scope_sha`'s own docstring for the incident.
# ---------------------------------------------------------------------------


class TestMechanicalCommitWalkBack:
    def test_fleet_archive_toucher_is_skipped_walks_back_to_real_commit(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_mc1.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "implement widget_mc1 feature")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        touched.write_text("#!/bin/sh\necho archived\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "fleet: archive 4 completed handoff(s)")

        hp = _seed_handoff(
            repo, "mc1.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )
        rc = arstamp.stamp_shipped_in(str(hp), kind="scope-derived")
        assert rc.exit_code == 0
        _assert_shipped_in(hp, expected_sha)

    def test_archive_handoff_prefix_is_skipped(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_mc1b.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "implement widget_mc1b feature")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        touched.write_text("#!/bin/sh\necho archived\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "archive handoff: state/handoffs/widget_mc1b.md")

        hp = _seed_handoff(
            repo, "mc1b.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )
        rc = arstamp.stamp_shipped_in(str(hp), kind="scope-derived")
        assert rc.exit_code == 0
        _assert_shipped_in(hp, expected_sha)

    def test_every_toucher_mechanical_leaves_shipped_in_unset(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_mc2.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "fleet: archive 1 completed handoff(s)")

        touched.write_text("#!/bin/sh\necho x\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "archive handoff: state/handoffs/widget_mc2.md")

        hp = _seed_handoff(
            repo, "mc2.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )
        rc = arstamp.stamp_shipped_in(str(hp), kind="scope-derived")
        assert rc.exit_code == 0
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_two_unrelated_batons_never_share_a_bulk_migration_sha(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        a = repo / "state" / "widget_mc3a.txt"
        b = repo / "state" / "widget_mc3b.txt"
        a.parent.mkdir(parents=True, exist_ok=True)
        a.write_text("a\n", encoding="utf-8")
        b.write_text("b\n", encoding="utf-8")
        _git(repo, "add", str(a.relative_to(repo)), str(b.relative_to(repo)))
        _git(
            repo, "commit", "-m",
            "change_kind: retire 29 coined work-shape tokens, and stop minting invalid refs",
        )

        hp_a = _seed_handoff(
            repo, "mc3a.md", "claimed", "shipped", extra=f"scope:\n  - {a.relative_to(repo)}\n"
        )
        hp_b = _seed_handoff(
            repo, "mc3b.md", "claimed", "shipped", extra=f"scope:\n  - {b.relative_to(repo)}\n"
        )

        rc_a = arstamp.stamp_shipped_in(str(hp_a), kind="scope-derived")
        rc_b = arstamp.stamp_shipped_in(str(hp_b), kind="scope-derived")
        assert rc_a.exit_code == 0
        assert rc_b.exit_code == 0
        assert "shipped_in:" not in hp_a.read_text(encoding="utf-8")
        assert "shipped_in:" not in hp_b.read_text(encoding="utf-8")

    def test_genuine_stamp_implemented_commit_still_resolves(self, tmp_path):
        """close-out:/stamp: are genuine ship signals, never denylisted."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_mc4.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "stamp: plan-orphan ownership resolver implemented")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        hp = _seed_handoff(
            repo, "mc4.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )
        rc = arstamp.stamp_shipped_in(str(hp), kind="scope-derived")
        assert rc.exit_code == 0
        _assert_shipped_in(hp, expected_sha)

    def test_close_out_commit_still_resolves(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_mc5.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "close-out: docs/plans/2026-08-01-example-plan.md")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        hp = _seed_handoff(
            repo, "mc5.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )
        rc = arstamp.stamp_shipped_in(str(hp), kind="scope-derived")
        assert rc.exit_code == 0
        _assert_shipped_in(hp, expected_sha)


# ---------------------------------------------------------------------------
# created-date lower bound (2026-08-05) — a scope-derived candidate whose
# committer date predates the handoff's own `created` frontmatter field
# cannot be its ship commit. See `archive_stamp._scope_sha_predates_creation`'s
# docstring for the incident this closes (a handoff stamped with a commit six
# days before it existed).
# ---------------------------------------------------------------------------


class TestCreatedDateGuard:
    def test_predating_candidate_refuses_stamp(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_cd1.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git_dated(repo, "touch widget_cd1", "2026-07-23T00:00:00+00:00")

        hp = _seed_handoff_with_created(
            repo, "cd1.md", "2026-07-29", f"scope:\n  - {touched.relative_to(repo)}\n"
        )
        rc = arstamp.stamp_shipped_in(str(hp), kind="scope-derived")
        assert rc.exit_code == 0
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_predating_refusal_prints_reason_to_stderr(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_cd1b.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git_dated(repo, "touch widget_cd1b", "2026-07-23T00:00:00+00:00")

        hp = _seed_handoff_with_created(
            repo, "cd1b.md", "2026-07-29", f"scope:\n  - {touched.relative_to(repo)}\n"
        )

        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.stamp_shipped_in(str(hp), kind="scope-derived")
        assert rc.exit_code == 0
        out = buf.getvalue()
        assert "created-date guard" in out
        assert str(hp) in out

    def test_postdating_candidate_still_stamps(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_cd2.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git_dated(repo, "touch widget_cd2", "2026-08-01T00:00:00+00:00")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        hp = _seed_handoff_with_created(
            repo, "cd2.md", "2026-07-29", f"scope:\n  - {touched.relative_to(repo)}\n"
        )
        rc = arstamp.stamp_shipped_in(str(hp), kind="scope-derived")
        assert rc.exit_code == 0
        _assert_shipped_in(hp, expected_sha)

    def test_no_created_field_is_noop(self, tmp_path):
        """A handoff with no `created:` field at all gets prior (guard-absent)
        behaviour regardless of the candidate's date."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget_cd3.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git_dated(repo, "touch widget_cd3", "2020-01-01T00:00:00+00:00")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        path = repo / "state" / "handoffs" / "cd3.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            'title: "Test Handoff cd3.md"\n'
            "branch: work/test/2026-01-01\n"
            "status: claimed\n"
            'predecessor: "none"\n'
            "deployment_state: shipped\n"
            f"scope:\n  - {touched.relative_to(repo)}\n"
        )
        path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "add cd3.md")

        rc = arstamp.stamp_shipped_in(str(path), kind="scope-derived")
        assert rc.exit_code == 0
        _assert_shipped_in(path, expected_sha)


# ---------------------------------------------------------------------------
# resolve_source_ship_sha (2026-08-04) — source-artifact-derived shipped_in
# evidence, the PRIMARY resolution path `deliverable_cascade._advance_one`
# now tries before falling back to Position A scope-derived resolution.
# ---------------------------------------------------------------------------


class TestResolveSourceShipSha:
    def test_resolves_commit_touching_source_path(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan = repo / "docs" / "plans" / "p1.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("status: implemented\n", encoding="utf-8")
        _git(repo, "add", str(plan.relative_to(repo)))
        _git(repo, "commit", "-m", "flip p1 to implemented")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        resolved = arstamp.resolve_source_ship_sha(
            str(plan.relative_to(repo)), worktree=repo
        )
        assert resolved == expected_sha

    def test_returns_none_for_untouched_source_path(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        resolved = arstamp.resolve_source_ship_sha(
            "docs/plans/never-existed.md", worktree=repo
        )
        assert resolved is None

    def test_returns_none_for_empty_source_path(self, tmp_path):
        assert arstamp.resolve_source_ship_sha("") is None

    def test_refuses_uncommitted_source_path(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan = repo / "docs" / "plans" / "p2.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("status: implemented\n", encoding="utf-8")
        _git(repo, "add", str(plan.relative_to(repo)))
        _git(repo, "commit", "-m", "add p2 (not yet implemented)")
        plan.write_text("status: implemented\nnewly dirty\n", encoding="utf-8")

        resolved = arstamp.resolve_source_ship_sha(
            str(plan.relative_to(repo)), worktree=repo
        )
        assert resolved is None

    def test_refuses_postdating_not_after(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan = repo / "docs" / "plans" / "p3.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("status: implemented\n", encoding="utf-8")
        _git(repo, "add", str(plan.relative_to(repo)))
        _git_dated(repo, "flip p3 to implemented", "2026-08-04T12:00:00+00:00")

        resolved = arstamp.resolve_source_ship_sha(
            str(plan.relative_to(repo)),
            not_after="2026-07-01T00:00:00+00:00",
            worktree=repo,
        )
        assert resolved is None

    def test_predating_not_after_still_resolves(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan = repo / "docs" / "plans" / "p4.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("status: implemented\n", encoding="utf-8")
        _git(repo, "add", str(plan.relative_to(repo)))
        _git_dated(repo, "flip p4 to implemented", "2026-06-01T00:00:00+00:00")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        resolved = arstamp.resolve_source_ship_sha(
            str(plan.relative_to(repo)),
            not_after="2026-08-04T00:00:00+00:00",
            worktree=repo,
        )
        assert resolved == expected_sha

    def test_stamps_via_ship_commit_kind(self, tmp_path):
        """End-to-end: a resolved source sha is stamped through
        stamp_shipped_in(kind="ship-commit", sha=...), which is NEVER subject
        to the ownership guard (an explicit sha override is the caller's own
        assertion) — resolve a PEER-owned commit and confirm it still stamps."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan = repo / "docs" / "plans" / "p5.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("status: implemented\n", encoding="utf-8")
        _git(repo, "add", str(plan.relative_to(repo)))
        _git(repo, "commit", "-m", "flip p5 to implemented", session_id=_PEER_SESSION_ID)
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        resolved = arstamp.resolve_source_ship_sha(
            str(plan.relative_to(repo)), worktree=repo
        )
        assert resolved == expected_sha

        hp = _seed_handoff(repo, "src1.md", "claimed", "shipped")
        rc = arstamp.stamp_shipped_in(str(hp), kind="ship-commit", sha=resolved)
        assert rc.exit_code == 0
        _assert_shipped_in(hp, expected_sha)

    def test_walks_back_past_mechanical_toucher(self, tmp_path):
        """Source's newest toucher is a bulk vocabulary migration — must NOT
        resolve to it; walks back to the genuine ship commit instead."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan = repo / "docs" / "plans" / "p6.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("status: implemented\n", encoding="utf-8")
        _git(repo, "add", str(plan.relative_to(repo)))
        _git(repo, "commit", "-m", "flip p6 to implemented")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        plan.write_text("status: implemented\nmigrated\n", encoding="utf-8")
        _git(repo, "add", str(plan.relative_to(repo)))
        _git(repo, "commit", "-m", "migrate_handoff_vocabulary: sweep docs/plans corpus")

        resolved = arstamp.resolve_source_ship_sha(str(plan.relative_to(repo)), worktree=repo)
        assert resolved == expected_sha

    def test_all_touchers_mechanical_returns_none(self, tmp_path):
        """Every commit touching source_path is mechanical — refuses (returns
        None) rather than resolving to housekeeping machinery."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan = repo / "docs" / "plans" / "p7.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("status: implemented\n", encoding="utf-8")
        _git(repo, "add", str(plan.relative_to(repo)))
        _git(repo, "commit", "-m", "fleet: archive 1 completed handoff(s)")

        resolved = arstamp.resolve_source_ship_sha(str(plan.relative_to(repo)), worktree=repo)
        assert resolved is None


# ---------------------------------------------------------------------------
# Ownership guard (2026-07-22, the P1 fix) — never stamp a DERIVED sha that
# cannot be established as the calling session's. Reproduction of the
# originating incident (peer-later, shared scope path) plus the full guard
# matrix per archive_stamp._ownership_block_reason.
# ---------------------------------------------------------------------------

_PEER_SESSION_ID = "22222222-2222-2222-2222-222222222222"


class TestOwnershipGuard:
    def test_peer_later_shared_scope_leaves_unset(self, tmp_path):
        """Reproduction: own commit T1, then a PEER commit T2 touching the same
        scope path — T2 resolves as the newest, but it isn't the caller's. Must
        leave shipped_in UNSET, rc 0 (never fail loud)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "shared.txt"
        touched.write_text("v1\n", encoding="utf-8")
        _git(repo, "add", "shared.txt")
        _git(repo, "commit", "-m", "own T1")  # own (default session id)

        hp = _seed_handoff(
            repo, "og1.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )

        touched.write_text("v2\n", encoding="utf-8")
        _git(repo, "add", "shared.txt")
        _git(repo, "commit", "-m", "peer T2", session_id=_PEER_SESSION_ID)

        rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived')
        assert rc.exit_code == 0
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_own_later_reverse_order_stamps(self, tmp_path):
        """Reverse of the reproduction: PEER commit T1, then OWN commit T2
        touching the same scope path — T2 (the caller's own) must stamp
        normally. Without this, test_peer_later... would pass trivially under
        a blanket-refusal guard that never actually checks ownership."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "shared2.txt"
        touched.write_text("v1\n", encoding="utf-8")
        _git(repo, "add", "shared2.txt")
        _git(repo, "commit", "-m", "peer T1", session_id=_PEER_SESSION_ID)

        hp = _seed_handoff(
            repo, "og2.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )

        touched.write_text("v2\n", encoding="utf-8")
        _git(repo, "add", "shared2.txt")
        _git(repo, "commit", "-m", "own T2")  # own (default session id)
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived')
        assert rc.exit_code == 0
        _assert_shipped_in(hp, expected_sha)

    def test_same_session_two_commits_newest_stamped(self, tmp_path):
        """Normal path: both commits touching scope belong to the SAME (own)
        session — the newest must stamp, guarding that the ownership check
        doesn't interfere with ordinary same-session resolution."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "shared3.txt"
        touched.write_text("v1\n", encoding="utf-8")
        _git(repo, "add", "shared3.txt")
        _git(repo, "commit", "-m", "own T1")

        hp = _seed_handoff(
            repo, "og3.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )

        touched.write_text("v2\n", encoding="utf-8")
        _git(repo, "add", "shared3.txt")
        _git(repo, "commit", "-m", "own T2")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived')
        assert rc.exit_code == 0
        _assert_shipped_in(hp, expected_sha)

    def test_candidate_no_trailer_distinct_warning(self, tmp_path):
        """A candidate commit with NO Session-Id trailer at all must leave
        shipped_in UNSET with a warning DISTINCT from "no scope commit found" —
        the two must not read identically (a reader must be able to tell
        "nothing resolved" from "something resolved but ownership is unknown")."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "no_trailer.txt"
        touched.write_text("v1\n", encoding="utf-8")
        _git(repo, "add", "no_trailer.txt")
        _git(repo, "commit", "-m", "no trailer at all", session_id=None)

        hp = _seed_handoff(
            repo, "og4.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )

        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived')
        assert rc.exit_code == 0
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")
        stderr = buf.getvalue()
        assert "ownership unestablished" in stderr
        assert "no valid Session-Id trailer" in stderr

    def test_malformed_trailer_treated_as_unestablished(self, tmp_path):
        """A non-UUID-shaped Session-Id trailer must be rejected the same way
        as a missing one (mirrors coverage.py's own fidelity guard 1 — a
        malformed trailer must never be treated as a match)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "malformed_trailer.txt"
        touched.write_text("v1\n", encoding="utf-8")
        _git(repo, "add", "malformed_trailer.txt")
        _git(
            repo, "commit", "-m",
            "malformed trailer\n\nSession-Id: not a uuid at all!!",
        )

        hp = _seed_handoff(
            repo, "og5.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )

        rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived')
        assert rc.exit_code == 0
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_caller_session_unresolvable_leaves_unset(self, tmp_path, monkeypatch):
        """When the CALLER's own session id cannot be resolved (no env var, no
        sentinel), nothing is safe to stamp — must NOT fail-open and stamp
        anyway (that would restore the bug precisely in the CI/hook
        environments where it is least observable)."""
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "unresolvable_caller.txt"
        touched.write_text("v1\n", encoding="utf-8")
        _git(repo, "add", "unresolvable_caller.txt")
        _git(repo, "commit", "-m", "own commit, but caller id unresolvable")

        hp = _seed_handoff(
            repo, "og6.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )

        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived')
        assert rc.exit_code == 0
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")
        assert "caller session-id unresolvable" in buf.getvalue()

    def test_explicit_sha_override_bypasses_guard_entirely(self, tmp_path):
        """An explicit `sha=` override is the caller's OWN assertion of
        ownership — the guard must not second-guess it, even against a
        peer-owned commit history."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "peer_owned.txt"
        touched.write_text("v1\n", encoding="utf-8")
        _git(repo, "add", "peer_owned.txt")
        _git(repo, "commit", "-m", "peer commit", session_id=_PEER_SESSION_ID)
        peer_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        hp = _seed_handoff(repo, "og7.md", "claimed", "shipped")

        rc = arstamp.stamp_shipped_in(str(hp), kind='ship-commit', sha=peer_sha)
        assert rc.exit_code == 0
        _assert_shipped_in(hp, peer_sha)

    def test_branch_tip_fallback_with_foreign_tip_refused(self, tmp_path):
        """`allow_branch_tip_fallback=True` resolves the branch tip when no
        scope commit is found — the branch tip is the MOST likely candidate to
        be a peer's commit on a shared branch, so it must be guarded too."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "og8.md", "claimed", "shipped")
        # Branch tip after seeding is the handoff-add commit itself (own) — make
        # a REAL peer commit (content must actually change, or git rejects an
        # empty commit and the tip silently stays "own") to move the tip.
        with hp.open("a", encoding="utf-8") as f:
            f.write("\nPeer edit.\n")
        _git(repo, "add", str(hp.relative_to(repo)))
        _git(repo, "commit", "-m", "peer re-touch to move tip", session_id=_PEER_SESSION_ID)

        rc = arstamp.stamp_shipped_in(str(hp), kind='scope-derived', allow_branch_tip_fallback=True)
        assert rc.exit_code == 0
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

    def test_recoverable_after_own_commit_lands(self, tmp_path):
        """Unset-by-guard is NOT sticky — once the caller lands their own
        commit touching scope, a re-run stamps correctly."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "recoverable.txt"
        touched.write_text("v1\n", encoding="utf-8")
        _git(repo, "add", "recoverable.txt")
        _git(repo, "commit", "-m", "peer commit", session_id=_PEER_SESSION_ID)

        hp = _seed_handoff(
            repo, "og9.md", "claimed", "shipped",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )

        rc1 = arstamp.stamp_shipped_in(str(hp), kind='scope-derived')
        assert rc1.exit_code == 0
        assert "shipped_in:" not in hp.read_text(encoding="utf-8")

        touched.write_text("v2\n", encoding="utf-8")
        _git(repo, "add", "recoverable.txt")
        _git(repo, "commit", "-m", "own commit lands")
        expected_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        rc2 = arstamp.stamp_shipped_in(str(hp), kind='scope-derived')
        assert rc2.exit_code == 0
        _assert_shipped_in(hp, expected_sha)

    def test_op_level_peer_later_stamped_false_ship_refuses_flip(self, tmp_path):
        """Op-level (handoff.archive_transition, mode=stamp_shipped via
        cs_ship_handoff(archive=True)): under a peer-later scope commit, the
        ownership guard must leave shipped_in unset — and, per the 2026-07-22
        Defect-2 fix (handoff_archive_transition._handler's flip-refusal
        gate), an unset shipped_in must now ALSO refuse the
        deployment_state:shipped flip and the subsequent archival move,
        fail-loud, naming --sha as the fix. (Renamed from
        test_op_level_peer_later_stamped_false_but_ship_proceeds — its old
        rc==0 assertion described exactly the shipped_in-less
        deployment_state:shipped half-state this fix closes: that write is
        knowably schema-invalid on/after the 2026-05-29 shipped_in-required
        cutoff, and refusing it before the fact beats a guaranteed
        downstream validator rejection.)"""
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "op_shared.txt"
        touched.write_text("v1\n", encoding="utf-8")
        _git(repo, "add", "op_shared.txt")
        _git(repo, "commit", "-m", "own T1")

        hp = _seed_handoff(
            repo, "og10.md", "claimed", "in_flight",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )

        touched.write_text("v2\n", encoding="utf-8")
        _git(repo, "add", "op_shared.txt")
        _git(repo, "commit", "-m", "peer T2", session_id=_PEER_SESSION_ID)

        rc = arstamp.cs_ship_handoff(str(hp), archive=True)
        assert rc == 1
        text = hp.read_text(encoding="utf-8")
        assert "deployment_state: in_flight" in text
        assert "shipped_in:" not in text
        assert hp.exists()
        assert not any((repo / "archive" / "handoffs").rglob("og10.md"))


# ---------------------------------------------------------------------------
# handoff.transition verb wrappers
# ---------------------------------------------------------------------------

class TestHandoffTransitionWrappers:
    def test_gate_recheck_cleared(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "g1.md", "open", "awaiting_gate",
            extra="gate_dependency: some-thing\n",
        )
        rc = arstamp.cs_gate_recheck_handoff(str(hp), "2026-02-01", cleared=True)
        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "deployment_state: ready_to_fire" in text
        assert "gate_dependency:" not in text
        assert "last_gate_recheck: 2026-02-01" in text

    def test_gate_recheck_fails_loud_on_non_awaiting(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        # ready_to_fire is the IDEMPOTENT no-op target for --cleared (already there);
        # a genuinely non-awaiting_gate, non-ready_to_fire state (in_flight) is what
        # fails loud — gate-recheck is defined only as the awaiting_gate re-check/clear.
        hp = _seed_handoff(repo, "g2.md", "claimed", "in_flight")
        rc = arstamp.cs_gate_recheck_handoff(str(hp), "2026-02-01", cleared=True)
        assert rc != 0

    def test_repark(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "r1.md", "claimed", "in_flight")
        rc = arstamp.cs_repark_handoff(str(hp))
        assert rc == 0
        assert "deployment_state: ready_to_fire" in hp.read_text(encoding="utf-8")

    def test_unclaim(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "u1.md", "claimed", "in_flight",
            extra="claimed_at: '2026-01-01T00:00:00Z'\nclaimed_by: sess-x\n",
        )
        rc = arstamp.cs_unclaim_handoff(str(hp))
        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "status: open" in text
        assert "deployment_state: ready_to_fire" in text
        assert "claimed_at:" not in text
        assert "claimed_by:" not in text

    def test_claim_requires_session_id(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "c1.md", "open", "ready_to_fire")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        rc = arstamp.cs_claim_handoff(str(hp))
        assert rc == 1

    def test_claim_with_session_id_env(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "c2.md", "open", "ready_to_fire")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        rc = arstamp.cs_claim_handoff(str(hp))
        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "status: claimed" in text
        assert "deployment_state: in_flight" in text
        assert "claimed_by: sess-abc" in text

    def test_claim_return_result_landed_vs_no_op_vs_rejection(self, tmp_path, monkeypatch):
        """C2/AC-4/AC-7: return_result=True distinguishes landed
        (exit_code 0, applied True), no-op (exit_code 0, applied False, with a
        message), and rejection (exit_code 1, error carrying the validator's
        own text) — and a rejection from a rule OTHER than the summary cap
        produces its own message, unchanged, not special-cased."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-return-result")

        # landed
        hp_landed = _seed_handoff(repo, "rr-landed.md", "open", "ready_to_fire")
        result = arstamp.cs_claim_handoff(str(hp_landed), return_result=True)
        assert isinstance(result, dict)
        assert result["exit_code"] == 0
        assert result.get("applied") is True

        # no-op (already claimed — idempotent re-claim)
        result2 = arstamp.cs_claim_handoff(str(hp_landed), return_result=True)
        assert result2["exit_code"] == 0
        assert result2.get("applied") is False

        # rejection — from a rule OTHER than the summary cap (simulated via
        # the underlying op handler, not pattern-matched on message content)
        hp_reject = _seed_handoff(repo, "rr-reject.md", "open", "ready_to_fire")

        async def _fake_transition(params, repo_root=None):
            return {
                "exit_code": 1,
                "error": "handoff frontmatter validation failed: some other "
                "cross-field rule was violated (not the summary cap)",
            }

        monkeypatch.setattr(
            "coordinator_core.ops.handoff_transition._handler", _fake_transition
        )
        result3 = arstamp.cs_claim_handoff(str(hp_reject), return_result=True)
        assert result3["exit_code"] == 1
        assert (
            result3["error"]
            == "handoff frontmatter validation failed: some other "
            "cross-field rule was violated (not the summary cap)"
        )

        # bare-int default is unaffected
        monkeypatch.undo()
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-return-result-2")
        hp_default = _seed_handoff(repo, "rr-default.md", "open", "ready_to_fire")
        rc = arstamp.cs_claim_handoff(str(hp_default))
        assert rc == 0

    def test_claim_threads_deliverable_id_into_session_shape(self, tmp_path, monkeypatch):
        """C2 write-moment: a claimed handoff carrying deliverable_id frontmatter
        gets it recorded into session-shape.json's pickup object (single cheap
        read_frontmatter_field read, no full YAML parse)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "c4.md", "open", "ready_to_fire",
            extra="deliverable_id: dlv-claim-test\n",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-dlv")
        rc = arstamp.cs_claim_handoff(str(hp))
        assert rc == 0

        shape_path = repo / ".git" / "coordinator-sessions" / "sess-dlv" / "session-shape.json"
        data = json.loads(shape_path.read_text(encoding="utf-8"))
        assert data["pickup"]["deliverable_id"] == "dlv-claim-test"
        assert data["pickup_history"][0]["deliverable_id"] == "dlv-claim-test"

    def test_claim_omits_deliverable_id_when_handoff_has_none(self, tmp_path, monkeypatch):
        """A handoff with no deliverable_id frontmatter records nothing for that
        field — absent, not null, not empty-string."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "c5.md", "open", "ready_to_fire")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-no-dlv")
        rc = arstamp.cs_claim_handoff(str(hp))
        assert rc == 0

        shape_path = repo / ".git" / "coordinator-sessions" / "sess-no-dlv" / "session-shape.json"
        data = json.loads(shape_path.read_text(encoding="utf-8"))
        assert "deliverable_id" not in data["pickup"]
        assert "deliverable_id" not in data["pickup_history"][0]

    def test_claim_stays_nonfatal_when_record_pickup_fails(self, tmp_path, monkeypatch):
        """C2 write-moment (DR-059): a session.record_pickup FAILURE must not fail
        cs_claim_handoff — the transition itself (C1) already succeeded and is the
        primary concern; the pickup-ledger write is best-effort."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "c3.md", "open", "ready_to_fire")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-nonfatal")

        async def _boom(params, repo_root=None):
            return {"exit_code": 1, "error": "simulated op failure"}

        monkeypatch.setattr(
            "coordinator_core.ops.session.record_pickup._handler", _boom
        )

        rc, warning_seen = 1, False
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_claim_handoff(str(hp))
        warning_seen = "session.record_pickup op did not complete" in buf.getvalue()

        assert rc == 0
        assert warning_seen
        text = hp.read_text(encoding="utf-8")
        assert "status: claimed" in text
        assert "deployment_state: in_flight" in text

    def test_claim_populates_session_goal_from_handoff_title(self, tmp_path, monkeypatch):
        """2026-08-13 session-goal-field-has-no-writer: claiming a handoff
        writes the claiming session's meta.json `goal`, sourced from the
        handoff's own title, carrying the `pickup: ` provenance prefix."""
        from coordinator_core.session import core as session_core

        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "goal1.md", "open", "ready_to_fire")
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-goal-1")
        session_core.init("sess-goal-1", cwd=str(repo))

        rc = arstamp.cs_claim_handoff(str(hp))
        assert rc == 0

        meta_path = repo / ".git" / "coordinator-sessions" / "sess-goal-1" / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["goal"] == "pickup: Test Handoff goal1.md"

    def test_claim_populates_session_goal_from_summary_when_title_absent(self, tmp_path, monkeypatch):
        """Title absent/empty falls back to `summary`."""
        from coordinator_core.session import core as session_core

        repo = tmp_path / "repo"
        _init_repo(repo)
        path = repo / "state" / "handoffs" / "goal2.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            'title: ""\n'
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            'predecessor: "none"\n'
            "deployment_state: ready_to_fire\n"
            'summary: "Fallback summary text"\n'
        )
        path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "add goal2")
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-goal-2")
        session_core.init("sess-goal-2", cwd=str(repo))

        rc = arstamp.cs_claim_handoff(str(path))
        assert rc == 0

        meta_path = repo / ".git" / "coordinator-sessions" / "sess-goal-2" / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["goal"] == "pickup: Fallback summary text"

    def test_init_writes_no_goal_placeholder(self, tmp_path, monkeypatch):
        """AC4: session init leaves `goal` empty — nothing derives a slug or
        placeholder at boot."""
        from coordinator_core.session import core as session_core

        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        repo = tmp_path / "repo"
        _init_repo(repo)
        ok = session_core.init("sess-init-only", cwd=str(repo))
        assert ok is True

        meta_path = repo / ".git" / "coordinator-sessions" / "sess-init-only" / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta.get("goal", "") == ""

    def test_claim_stays_nonfatal_when_session_goal_write_fails(self, tmp_path, monkeypatch):
        """AC5: the goal writer failing must not fail the claim."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "goal3.md", "open", "ready_to_fire")
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-goal-nonfatal")

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated update_meta_field failure")

        monkeypatch.setattr(
            "coordinator_core.session.core.update_meta_field", _boom
        )

        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        assert "session goal write did not complete" in buf.getvalue()
        text = hp.read_text(encoding="utf-8")
        assert "status: claimed" in text

    def test_claim_warns_when_update_meta_field_returns_false(self, tmp_path, monkeypatch):
        """update_meta_field returning False (no exception — the realistic
        failure mode when meta.json is absent/unreadable/not-an-object) must
        still surface a WARNING, not vanish silently; the claim stays
        non-fatal regardless."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "goal4.md", "open", "ready_to_fire")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-goal-false")

        monkeypatch.setattr(
            "coordinator_core.session.core.update_meta_field",
            lambda *args, **kwargs: False,
        )

        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        assert "session goal write did not complete" in buf.getvalue()

    def test_consume_unconsume_deprecated_aliases_still_work(self, tmp_path, monkeypatch):
        """DR-084 verb rename (consume->claim, unconsume->unclaim): the OLD
        spellings must keep working as plain aliases of the SAME function
        objects, not a second reimplementation — this is the load-bearing
        property of the rename, so it gets its own dedicated assertion
        rather than incidental coverage from the renamed tests above."""
        assert arstamp.cs_consume_handoff is arstamp.cs_claim_handoff
        assert arstamp.cs_unconsume_handoff is arstamp.cs_unclaim_handoff

        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "alias1.md", "open", "ready_to_fire")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-alias")

        rc = arstamp.cs_consume_handoff(str(hp))
        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "status: claimed" in text
        assert "deployment_state: in_flight" in text

        rc = arstamp.cs_unconsume_handoff(str(hp))
        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "status: open" in text
        assert "deployment_state: ready_to_fire" in text


# ---------------------------------------------------------------------------
# cs_ship_handoff — handoff.archive_transition composition (stamp_only /
# stamp_shipped modes) — closes the shipped_in-present/deployment_state-
# in_flight incoherent half-state a standalone stamp_shipped_in() call left
# on state/handoffs/2026-07-15_150901_auto-push-windows-spike-and-doe-cutover.md.
# ---------------------------------------------------------------------------

class TestShipHandoff:
    def test_default_flips_deployment_state_and_stamps_without_moving(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "ship1.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/ship1.md\n",
        )
        rc = arstamp.cs_ship_handoff(str(hp))
        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "deployment_state: shipped" in text
        assert "shipped_in:" in text
        assert hp.exists()
        assert not (repo / "archive" / "handoffs").exists() or not any(
            (repo / "archive" / "handoffs").rglob("ship1.md")
        )

    def test_archive_true_moves_file_under_archive_handoffs(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "ship2.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/ship2.md\n",
        )
        rc = arstamp.cs_ship_handoff(str(hp), archive=True)
        assert rc == 0
        assert not hp.exists()
        archived = list((repo / "archive" / "handoffs").rglob("ship2.md"))
        assert len(archived) == 1
        text = archived[0].read_text(encoding="utf-8")
        assert "shipped_in:" in text

    def test_idempotent_second_call_on_already_shipped_is_clean_noop(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "ship3.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/ship3.md\n",
        )
        rc1 = arstamp.cs_ship_handoff(str(hp))
        assert rc1 == 0
        text_after_first = hp.read_text(encoding="utf-8")
        assert "deployment_state: shipped" in text_after_first

        rc2 = arstamp.cs_ship_handoff(str(hp))
        assert rc2 == 0
        text_after_second = hp.read_text(encoding="utf-8")
        # Idempotent: no corruption — frontmatter unchanged by the second call.
        assert text_after_second == text_after_first
        assert "deployment_state: shipped" in text_after_second

    def test_live_children_retention_is_graceful_and_leaves_file_in_place(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "ship4.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/ship4.md\n",
        )
        # A live, non-terminal child naming ship4.md as its predecessor —
        # handoff.has_live_children reports referenced=True (exit_code 0),
        # which handoff.archive_transition surfaces as a graceful retain.
        _seed_handoff_with_predecessor(repo, "ship4-child.md", "ship4.md")

        rc = arstamp.cs_ship_handoff(str(hp))
        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        # Guard runs BEFORE the stamp_only mutation — no flip, no stamp.
        assert "deployment_state: in_flight" in text
        assert "shipped_in:" not in text
        assert hp.exists()

    def test_explicit_sha_stamps_that_sha_without_resolving(self, tmp_path):
        """A caller-supplied sha is stamped verbatim — the scope-path git log
        resolution never runs (mirrors stamp_shipped_in's own override contract,
        threaded here through cs_ship_handoff's new sha= param, 2026-07-22)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "ship5.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/ship5.md\n",
        )
        # An unrelated commit whose sha is deliberately NOT what scope-path
        # resolution would find — if the override were ignored, the stamped
        # value would differ from this.
        another = repo / "unrelated.txt"
        another.write_text("z\n", encoding="utf-8")
        _git(repo, "add", "unrelated.txt")
        _git(repo, "commit", "-m", "unrelated commit")
        override_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        rc = arstamp.cs_ship_handoff(str(hp), sha=override_sha)
        assert rc == 0
        _assert_shipped_in(hp, override_sha)

    def test_force_with_explicit_sha_replaces_and_reports_prior_value(self, tmp_path):
        """force=True + sha= REPLACES an already-stamped shipped_in — the
        provenance-repair escape (2026-07-22 incident: a sibling ship-handoff
        stamped a concurrent peer session's sha with no authorized repair path)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "ship6.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/ship6.md\n",
        )
        rc1 = arstamp.cs_ship_handoff(str(hp))
        assert rc1 == 0
        wrong_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _assert_shipped_in(hp, wrong_sha)

        another = repo / "correction.txt"
        another.write_text("c\n", encoding="utf-8")
        _git(repo, "add", "correction.txt")
        _git(repo, "commit", "-m", "the actually-correct commit")
        correct_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert correct_sha != wrong_sha

        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc2 = arstamp.cs_ship_handoff(str(hp), sha=correct_sha, force=True)
        assert rc2 == 0
        _assert_shipped_in(hp, correct_sha)
        assert wrong_sha[:8] in buf.getvalue()

    def test_force_without_sha_rejected_fail_loud(self, tmp_path):
        """force=True with no sha is rejected before any stamp call — a
        force-overwrite that then resolves its own sha is the exact hazard
        this escape exists to repair, not repeat."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "ship7.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/ship7.md\n",
        )
        rc1 = arstamp.cs_ship_handoff(str(hp))
        assert rc1 == 0
        stamped_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _assert_shipped_in(hp, stamped_sha)

        rc2 = arstamp.cs_ship_handoff(str(hp), force=True)
        assert rc2 == 1
        # Rejected before any mutation — shipped_in unchanged.
        _assert_shipped_in(hp, stamped_sha)

    def test_default_no_force_against_already_stamped_now_refuses_loudly(self, tmp_path):
        """2026-07-28 (§ S11/AC6, chunk C0): omitting force (the default) against
        an already-stamped handoff, while supplying a DIFFERENT --sha, no longer
        silently no-ops (rc 0) — it refuses loudly (non-zero), naming --force
        as the remedy, and leaves the frontmatter untouched. Renamed from
        test_default_no_force_against_already_stamped_is_still_silent_noop,
        whose rc2==0 assertion described exactly the silent-discard behaviour
        AC6 retires."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "ship8.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/ship8.md\n",
        )
        rc1 = arstamp.cs_ship_handoff(str(hp))
        assert rc1 == 0
        stamped_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        text_after_first = hp.read_text(encoding="utf-8")

        another = repo / "should-be-ignored.txt"
        another.write_text("i\n", encoding="utf-8")
        _git(repo, "add", "should-be-ignored.txt")
        _git(repo, "commit", "-m", "should not be stamped")
        different_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        rc2 = arstamp.cs_ship_handoff(str(hp), sha=different_sha)
        assert rc2 != 0
        _assert_shipped_in(hp, stamped_sha)
        assert hp.read_text(encoding="utf-8") == text_after_first

    def test_same_commit_resupply_is_legitimate_noop_not_refused(self, tmp_path):
        """§ S11/AC6b: re-supplying the SAME commit's full-length sha (the
        one already stamped, truncated to 8 chars in storage) is a legitimate
        no-op — canonical prefix match against prior_value — and must NOT
        refuse."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "ship8b.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/ship8b.md\n",
        )
        rc1 = arstamp.cs_ship_handoff(str(hp))
        assert rc1 == 0
        stamped_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        text_after_first = hp.read_text(encoding="utf-8")

        rc2 = arstamp.cs_ship_handoff(str(hp), sha=stamped_sha)
        assert rc2 == 0
        _assert_shipped_in(hp, stamped_sha)
        assert hp.read_text(encoding="utf-8") == text_after_first


# ---------------------------------------------------------------------------
# cs_chain_archive_handoff / cs_supersede_archive_handoff — the C8 (cockpit
# §6.2) CLI-reachability gap: 'chain' and supersede-with-continued_into/
# exclude were reachable in-process (handoff.archive_transition's own modes)
# but had no archive_stamp verb.
# ---------------------------------------------------------------------------

class TestChainArchiveHandoff:
    def test_moves_file_with_no_stamp(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "chain1.md", "claimed", "shipped",
            extra="shipped_in: 'abcdef12'\nscope:\n  - state/handoffs/chain1.md\n",
        )
        rc = arstamp.cs_chain_archive_handoff(str(hp))
        assert rc == 0
        assert not hp.exists()
        archived = list((repo / "archive" / "handoffs").rglob("chain1.md"))
        assert len(archived) == 1
        text = archived[0].read_text(encoding="utf-8")
        # Chain mode never stamps — the pre-seeded value is untouched.
        assert "shipped_in: 'abcdef12'" in text or "shipped_in: abcdef12" in text

    def test_live_children_retention_is_graceful_and_leaves_file_in_place(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "chain2.md", "claimed", "shipped",
            extra="scope:\n  - state/handoffs/chain2.md\n",
        )
        _seed_handoff_with_predecessor(repo, "chain2-child.md", "chain2.md")

        rc = arstamp.cs_chain_archive_handoff(str(hp))
        assert rc == 0
        assert hp.exists()
        assert not (repo / "archive" / "handoffs").exists() or not any(
            (repo / "archive" / "handoffs").rglob("chain2.md")
        )

    def test_live_children_retention_prints_reason_to_stderr(self, tmp_path):
        """The guard-retained outcome is a legitimate exit_code:0 non-move —
        but was previously silent on this path (see
        test_live_children_retention_is_graceful_and_leaves_file_in_place
        above), indistinguishable from a successful archive to the caller.
        Must now print the op's own retain_reason."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "chain2b.md", "claimed", "shipped",
            extra="scope:\n  - state/handoffs/chain2b.md\n",
        )
        _seed_handoff_with_predecessor(repo, "chain2b-child.md", "chain2b.md")

        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_chain_archive_handoff(str(hp))
        assert rc == 0
        assert hp.exists()
        out = buf.getvalue()
        assert "cs_chain_archive_handoff:" in out
        assert "retain_reason=" in out
        assert "live" in out

    def test_exclude_lets_guard_ignore_the_named_child_and_archive_proceeds(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "chain3.md", "claimed", "shipped",
            extra="scope:\n  - state/handoffs/chain3.md\n",
        )
        child = _seed_handoff_with_predecessor(repo, "chain3-child.md", "chain3.md")
        # exclude is abspath-resolved against CWD (dag.referenced_by), not the
        # candidate's repo root — an absolute path is the only form guaranteed
        # to match here.
        rc = arstamp.cs_chain_archive_handoff(str(hp), exclude=[str(child)])
        assert rc == 0
        assert not hp.exists()
        archived = list((repo / "archive" / "handoffs").rglob("chain3.md"))
        assert len(archived) == 1

    def test_nonexistent_handoff_is_op_error(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        missing = repo / "state" / "handoffs" / "does-not-exist.md"

        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_chain_archive_handoff(str(missing))
        assert rc == 1
        assert "cs_chain_archive_handoff:" in buf.getvalue()

    def test_forced_commit_failure_returns_nonzero_not_retained_zero(self, tmp_path, monkeypatch):
        """§ C2/AC2 (`docs/plans/2026-07-28-handoff-close-path-fail-loud.md`):
        a `git mv`/commit failure that is NOT a deliberate guard retention
        must surface as non-zero — this is § S6's ref-lock-race reproduction,
        forced here via a monkeypatched `archive_and_commit` that fails
        (simulating a read-only path / ref-lock contention) while the
        live-children guard clears (no retention). Pre-C2, this wrapper
        relayed the op's own `exit_code:0` verbatim (the op's git-mv-failure
        branch is deliberately non-fatal), making a genuinely failed move
        indistinguishable from success at this wrapper's own rc."""
        import coordinator_core.ops.handoff_archive_transition as hat

        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "chain-forced-fail.md", "claimed", "shipped",
            extra="shipped_in: 'abcdef12'\n",
        )

        async def _boom_archive_and_commit(worktree, moves, subject):
            return (
                [],
                [{"candidate_id": moves[0].candidate_id, "reason": "simulated ref-lock contention"}],
            )

        monkeypatch.setattr(hat, "archive_and_commit", _boom_archive_and_commit)

        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_chain_archive_handoff(str(hp))
        assert rc == 1, "a git-mv/commit failure must not be relayed as the op's own retained-shape 0"
        assert hp.exists(), "the source must still be present — the move did not land"
        assert "did not land" in buf.getvalue()

    def test_guard_retention_still_returns_zero_after_ac2_fix(self, tmp_path):
        """The AC2 fix (independent on-disk re-verification) must NEVER
        downgrade a legitimate guard-retention to non-zero — this is the
        regression guard against the noisy-success bug a naive "any non-move
        is a failure" patch would introduce. Companion to
        test_forced_commit_failure_returns_nonzero_not_retained_zero above:
        that test proves an ATTEMPTED-and-unlanded move fails; this one
        proves a DELIBERATE retain (live children, no move attempted at all)
        still succeeds."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "chain-retain-still-zero.md", "claimed", "shipped",
            extra="scope:\n  - state/handoffs/chain-retain-still-zero.md\n",
        )
        _seed_handoff_with_predecessor(repo, "chain-retain-still-zero-child.md", "chain-retain-still-zero.md")

        rc = arstamp.cs_chain_archive_handoff(str(hp))
        assert rc == 0
        assert hp.exists()


class TestSupersedeArchiveHandoff:
    def test_stamps_continued_into_and_moves_file(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "sup1.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/sup1.md\n",
        )
        rc = arstamp.cs_supersede_archive_handoff(str(hp), "sup1-successor.md")
        assert rc == 0
        assert not hp.exists()
        archived = list((repo / "archive" / "handoffs").rglob("sup1.md"))
        assert len(archived) == 1
        text = archived[0].read_text(encoding="utf-8")
        assert "status: claimed" in text
        assert "deployment_state: continued" in text
        assert "continued_into: sup1-successor.md" in text
        assert "shipped_in:" in text

    def test_missing_continued_into_is_usage_error_before_any_op_call(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "sup2.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/sup2.md\n",
        )

        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_supersede_archive_handoff(str(hp), "")
        assert rc == 2
        assert "continued_into" in buf.getvalue()
        # No mutation on the usage-error path.
        text = hp.read_text(encoding="utf-8")
        assert "deployment_state: in_flight" in text
        assert hp.exists()

    def test_live_children_retention_is_graceful_and_leaves_file_in_place(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "sup3.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/sup3.md\n",
        )
        _seed_handoff_with_predecessor(repo, "sup3-child.md", "sup3.md")

        rc = arstamp.cs_supersede_archive_handoff(str(hp), "sup3-successor.md")
        assert rc == 0
        assert hp.exists()
        text = hp.read_text(encoding="utf-8")
        # Status-flip-precedes-guard fix (2026-07-27, handoff_archive_transition
        # module docstring § Status-flip-precedes-guard fix): the supersede
        # mutation is UNCONDITIONAL and lands before the live-children guard —
        # a live child (here, the successor itself) governs ONLY the archival
        # git-mv, never the status flip. Retention leaves the flip landed.
        assert "deployment_state: continued" in text
        assert "continued_into: sup3-successor.md" in text

    def test_live_children_retention_prints_reason_to_stderr(self, tmp_path):
        """Mirrors TestChainArchiveHandoff's sibling test — the guard-retained
        outcome on supersede is also a legitimate exit_code:0 non-write, and
        was previously silent (reproduced live on 2026-07-26: a retained
        handoff exited 0 with no stdout/stderr and an unchanged frontmatter).
        Must now print the op's own retain_reason."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "sup3b.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/sup3b.md\n",
        )
        _seed_handoff_with_predecessor(repo, "sup3b-child.md", "sup3b.md")

        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_supersede_archive_handoff(str(hp), "sup3b-successor.md")
        assert rc == 0
        assert hp.exists()
        text = hp.read_text(encoding="utf-8")
        # See test_live_children_retention_is_graceful_and_leaves_file_in_place
        # above — the status flip is unconditional (2026-07-27 fix) and lands
        # even on a guard-retained (not-yet-archived) call.
        assert "continued_into: sup3b-successor.md" in text
        out = buf.getvalue()
        assert "cs_supersede_archive_handoff:" in out
        assert "retain_reason=" in out
        assert "live" in out

    def test_exclude_lets_guard_ignore_the_named_child_and_supersede_proceeds(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "sup4.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/sup4.md\n",
        )
        child = _seed_handoff_with_predecessor(repo, "sup4-child.md", "sup4.md")
        rc = arstamp.cs_supersede_archive_handoff(
            str(hp), "sup4-successor.md", exclude=[str(child)]
        )
        assert rc == 0
        assert not hp.exists()
        archived = list((repo / "archive" / "handoffs").rglob("sup4.md"))
        assert len(archived) == 1
        text = archived[0].read_text(encoding="utf-8")
        assert "continued_into: sup4-successor.md" in text

    def test_shipped_predecessor_with_ledger_claim_is_stamped_with_its_holder(self, tmp_path):
        """The holder-less `status: claimed` mirror, at the observed shape.

        Measured 2026-08-10 in example-retrieval-repo: 34 of 231 archived `status: claimed`
        handoffs carried NO holder field at all (and no `claimed_at`, which is
        why the schema's `claimed + claimed_at => claimed_by` cross-field rule
        never fired on them). Reproduced here: the op's DR-242 gate admits a
        predecessor that is claimed OR SHIPPED, so a shipped predecessor whose
        frontmatter mirror never received the claiming commit (the branch-
        dependence desync `claim_state`'s module docstring opens with) reached
        the supersede status flip with no holder on either side, and the flip
        wrote `status: claimed` naming nobody. Once the ledger holder goes
        non-live, such a record is unattributable to any consumer forever.

        The durable ledger is the evidence that was already on disk the whole
        time. Nothing here is inferred from the calling session.
        """
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "ledger-holder.md", "open", "shipped",
            extra="scope:\n  - state/handoffs/ledger-holder.md\n",
        )
        _seed_ledger_claim(repo, hp, "cf725a50-e1be-443e-957c-1c4be5ff964b", "2026-08-10T16:25:36Z")

        rc = arstamp.cs_supersede_archive_handoff(str(hp), "ledger-holder-successor.md")
        assert rc == 0
        archived = list((repo / "archive" / "handoffs").rglob("ledger-holder.md"))
        assert len(archived) == 1
        text = archived[0].read_text(encoding="utf-8")
        assert "status: claimed" in text
        assert "claimed_by: cf725a50-e1be-443e-957c-1c4be5ff964b" in text
        assert "2026-08-10T16:25:36Z" in text

    def test_dead_ledger_holder_still_attributes_the_superseded_record(self, tmp_path):
        """Superseding is retrospective — the holder's liveness is irrelevant to
        who consumed the baton. A crashed/exited session is exactly the
        population that produced the corpus defect, so the attribution must
        survive it (`claim_state.resolve_historical_claim`'s own Negative-spec
        explains why this does NOT weaken `resolve_claim_state`'s liveness
        gate, which governs live-work decisions and is untouched)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "dead-holder.md", "open", "shipped",
            extra="scope:\n  - state/handoffs/dead-holder.md\n",
        )
        _seed_ledger_claim(repo, hp, "dead-session-id", "2026-08-10T16:25:36Z", pid="999999999")

        rc = arstamp.cs_supersede_archive_handoff(str(hp), "dead-holder-successor.md")
        assert rc == 0
        archived = list((repo / "archive" / "handoffs").rglob("dead-holder.md"))
        text = archived[0].read_text(encoding="utf-8")
        assert "claimed_by: dead-session-id" in text

    def test_existing_mirror_holder_is_never_overwritten_by_the_ledger(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "mirror-wins.md", "claimed", "shipped",
            extra=(
                "claimed_by: mirror-session\n"
                "claimed_at: 2026-08-01T00:00:00Z\n"
                "scope:\n  - state/handoffs/mirror-wins.md\n"
            ),
        )
        _seed_ledger_claim(repo, hp, "ledger-session", "2026-08-10T16:25:36Z")

        rc = arstamp.cs_supersede_archive_handoff(str(hp), "mirror-wins-successor.md")
        assert rc == 0
        archived = list((repo / "archive" / "handoffs").rglob("mirror-wins.md"))
        text = archived[0].read_text(encoding="utf-8")
        assert "claimed_by: mirror-session" in text
        assert "ledger-session" not in text

    def test_silent_ledger_invents_no_holder_and_warns(self, tmp_path):
        """A predecessor that was genuinely never claimed (shipped only) has no
        honest holder to name. The calling session, and the `shipped_in`
        commit's own `Session-Id:` trailer, are both available here and both
        answer a DIFFERENT question — neither may be stamped as `claimed_by`.
        The residual holder-less record is surfaced as a warning instead."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "no-evidence.md", "open", "shipped",
            extra="scope:\n  - state/handoffs/no-evidence.md\n",
        )

        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_supersede_archive_handoff(str(hp), "no-evidence-successor.md")
        assert rc == 0
        archived = list((repo / "archive" / "handoffs").rglob("no-evidence.md"))
        text = archived[0].read_text(encoding="utf-8")
        assert "claimed_by:" not in text
        assert _DEFAULT_TEST_SESSION_ID not in text
        assert "no claimed_by" in buf.getvalue()

    def test_never_claimed_parent_with_successor_named_children_is_not_stamped(self, tmp_path):
        """AC10 (§ C5/C8, docs/plans/2026-07-28-handoff-close-path-fail-loud.md):
        the discriminator-safety test. Models the real corpus instance the DR
        (DR-242) cites — 2026-07-24_140030_a3983c55-... carried two
        speculative successor-named children pointing at it, but was NEVER
        itself picked up (status stayed 'open') or shipped. A successor-named
        child's existence alone is NOT evidence of succession (§ S1's
        discriminator hazard) — cs_supersede_archive_handoff gates on
        coordinator_core.archival.claimed_or_shipped_at_path
        BEFORE calling the op at all, so this exercises the fixed supersede
        path (C2, landed) via the real DR-242 gate (C5), not a mock."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        # status="open" (never claimed/consumed), deployment_state a
        # non-terminal value, no shipped_in, no claimed_at/claimed_by —
        # claimed_or_shipped_at_path must read this as False.
        hp = _seed_handoff(repo, "never-claimed-parent.md", "open", "active")
        # Two successor-named children, both naming the parent as
        # predecessor — the exact "speculative successor-named child"
        # shape § S1/DR-242 names, proven insufficient on its own.
        _seed_handoff_with_predecessor(
            repo, "never-claimed-parent-successor-a.md", "never-claimed-parent.md"
        )
        _seed_handoff_with_predecessor(
            repo, "never-claimed-parent-successor-b.md", "never-claimed-parent.md"
        )
        text_before = hp.read_text(encoding="utf-8")

        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_supersede_archive_handoff(
                str(hp), "never-claimed-parent-successor-a.md"
            )

        assert rc != 0
        assert "never claimed or shipped" in buf.getvalue()
        # Disk state (AC4/AC10): the parent is untouched — no supersede
        # mutation, no archival move — never merely "params reached
        # route_mutation".
        assert hp.exists()
        assert hp.read_text(encoding="utf-8") == text_before
        archived = list((repo / "archive" / "handoffs").rglob("never-claimed-parent.md"))
        assert len(archived) == 0


# ---------------------------------------------------------------------------
# memo.transition verb wrappers
# ---------------------------------------------------------------------------

class TestMemoTransitionWrappers:
    def test_claim(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "m1.md", "open")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        rc = arstamp.cs_claim_memo_stamp(str(mp))
        assert rc == 0
        text = mp.read_text(encoding="utf-8")
        assert "status: in_progress" in text
        assert "picked_up_by: sess-abc" in text

    def test_claim_requires_session_id(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "m2.md", "open")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        rc = arstamp.cs_claim_memo_stamp(str(mp))
        assert rc == 1

    def test_release(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(
            repo, "m3.md", "in_progress",
            extra="picked_up_at: '2026-01-01T00:00:00Z'\npicked_up_by: sess-x\n",
        )
        rc = arstamp.cs_release_memo_revert(str(mp))
        assert rc == 0
        text = mp.read_text(encoding="utf-8")
        assert "status: open" in text
        assert "picked_up_by:" not in text

    def test_action_no_claim_dir_proceeds(self, tmp_path, monkeypatch):
        """Guard 1: claim dir absent -> PROCEED (no ownership infra to check)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "m4.md", "in_progress")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        rc = arstamp.cs_action_memo(str(mp), "--decision", "declined", "--decision-note", "no")
        assert rc == 0
        assert "status: actioned" in mp.read_text(encoding="utf-8")

    def test_action_owner_closing_own_claim(self, tmp_path, monkeypatch):
        """Guard 3: holder == caller -> PROCEED, even with a live claim dir."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "m5.md", "in_progress")
        claim_dir = repo / ".git" / "coordinator-sessions" / "memo-claims" / "m5.md"
        claim_dir.mkdir(parents=True)
        (claim_dir / "session_id").write_text("sess-abc", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        rc = arstamp.cs_action_memo(str(mp), "--actioned-note", "done")
        assert rc == 0
        assert "status: actioned" in mp.read_text(encoding="utf-8")

    def test_action_disposition_flag_parsing(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "m6.md", "in_progress")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        rc = arstamp.cs_action_memo(
            str(mp), "--decision", "accepted", "--realized-by", "commit-abc123",
            "--distill-fate", "commitment",
        )
        assert rc == 0
        text = mp.read_text(encoding="utf-8")
        assert "decision: accepted" in text
        assert "realized_by: commit-abc123" in text

    def test_action_correct_realization_flag_parsed_as_boolean(self, tmp_path, monkeypatch):
        """--correct-realization is a bare (no-value) flag — _DISPOSITION_BOOL_FLAGS,
        distinct from the value-taking _DISPOSITION_FLAGS. Corrects realized_by on
        an already-actioned memo whose decision: is unchanged."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(
            repo, "m7.md", "actioned",
            extra=(
                "decision: accepted\n"
                "decision_note: 'Prior rationale.'\n"
                "realized_by: oldsha0001\n"
            ),
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        rc = arstamp.cs_action_memo(
            str(mp), "--decision", "accepted", "--realized-by", "newsha0002",
            "--correct-realization",
        )
        assert rc == 0
        text = mp.read_text(encoding="utf-8")
        assert "realized_by: newsha0002" in text
        assert "oldsha0001" in text  # superseded SHA preserved in decision_note

    def test_action_in_linked_worktree_resolves_claim_dir_under_common_git(
        self, tmp_path, monkeypatch
    ):
        """Regression guard: cs_action_memo must resolve the claim dir via the
        COMMON git dir, not a literal <worktree>/.git join. A literal join
        resolves through <worktree>/.git, a gitdir-pointer FILE in a linked
        worktree — claim_dir.is_dir() reads that as absent, so Guard 1
        (claim absent -> PROCEED) would fire and silently bypass the live-
        holder refusal below. This is falsifying: a still-buggy literal-.git
        join returns rc=0 (wrongly proceeds); the fix returns rc=1 (correctly
        refuses)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        worktree = tmp_path / "wt1"
        _git(repo, "worktree", "add", "-q", str(worktree), "-b", "wt1-branch")
        mp = _seed_memo(worktree, "m7.md", "in_progress")

        claim_dir = repo / ".git" / "coordinator-sessions" / "memo-claims" / "m7.md"
        claim_dir.mkdir(parents=True)
        (claim_dir / "session_id").write_text("sess-owner", encoding="utf-8")

        monkeypatch.setattr(arstamp, "cs_claim_holder_live", lambda claim_path: True)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-caller")
        monkeypatch.chdir(worktree)  # Guard 4 requires cwd_git_root == memo_git_root
        rc = arstamp.cs_action_memo(str(mp), "--actioned-note", "done")
        assert rc == 1
        assert "status: in_progress" in mp.read_text(encoding="utf-8")

    def test_action_owner_in_linked_worktree_closes_own_claim(self, tmp_path, monkeypatch):
        """Companion to the falsifying refusal test above: the SAME resolution
        path must also let the actual claim owner proceed (Guard 3) from
        inside a linked worktree."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        worktree = tmp_path / "wt1"
        _git(repo, "worktree", "add", "-q", str(worktree), "-b", "wt1-branch")
        mp = _seed_memo(worktree, "m8.md", "in_progress")

        claim_dir = repo / ".git" / "coordinator-sessions" / "memo-claims" / "m8.md"
        claim_dir.mkdir(parents=True)
        (claim_dir / "session_id").write_text("sess-owner", encoding="utf-8")

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-owner")
        monkeypatch.chdir(worktree)
        rc = arstamp.cs_action_memo(str(mp), "--actioned-note", "done")
        assert rc == 0
        assert "status: actioned" in mp.read_text(encoding="utf-8")

    # -- --superseded-by (receiver-side supersession pair, AC1/AC2) -----------

    def test_action_superseded_by_writes_pair(self, tmp_path, monkeypatch):
        """AC1: --superseded-by writes status: superseded + superseded_by in
        one locked_rmw closure (through the memo.transition op this wrapper
        calls)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "m9.md", "in_progress")
        # The pointer must resolve — seed the named memo in cross-repo/inbox/.
        _seed_memo(repo, "successor.md", "open")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        rc = arstamp.cs_action_memo(str(mp), "--superseded-by", "successor.md")
        assert rc == 0
        text = mp.read_text(encoding="utf-8")
        assert "status: superseded" in text
        assert "superseded_by: successor.md" in text

    def test_action_superseded_by_and_decision_mutually_exclusive(self, tmp_path, monkeypatch):
        """AC2: --superseded-by and --decision together fail loud (exit 1),
        memo byte-unchanged — no op call is even made."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "m10.md", "in_progress")
        _seed_memo(repo, "successor2.md", "open")
        before = mp.read_bytes()
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        rc = arstamp.cs_action_memo(
            str(mp), "--superseded-by", "successor2.md", "--decision", "accepted",
        )
        assert rc == 1
        assert mp.read_bytes() == before

    def test_action_superseded_by_and_actioned_note_mutually_exclusive(self, tmp_path, monkeypatch):
        """AC2 sibling: --superseded-by + --actioned-note also refused."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "m11.md", "in_progress")
        _seed_memo(repo, "successor3.md", "open")
        before = mp.read_bytes()
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        rc = arstamp.cs_action_memo(
            str(mp), "--superseded-by", "successor3.md", "--actioned-note", "noted",
        )
        assert rc == 1
        assert mp.read_bytes() == before


# ---------------------------------------------------------------------------
# cs_resolve_memo — memo.transition verb `resolve`, the CLI's own trampoline
# (added alongside claim-memo-stamp/action-memo/release-memo-revert to close
# the unreachable-remediation defect the cross-repo memo of 2026-08-01
# reported: block_memo_status_hand_edit.py's deny text named a `resolve`
# verb with no CLI binding).
# ---------------------------------------------------------------------------

class TestResolveMemo:
    def test_open_to_actioned_in_one_call(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "r1.md", "open")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        rc = arstamp.cs_resolve_memo(str(mp), "--actioned-note", "done")
        assert rc == 0
        text = mp.read_text(encoding="utf-8")
        assert "status: actioned" in text
        assert "actioned_note: done" in text
        # No intermediate in_progress state should be observable post-call —
        # picked_up_by is still stamped (the collapsed claim step), but the
        # terminal status is what a single-call caller sees.
        assert "picked_up_by: sess-abc" in text

    def test_requires_session_id(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "r2.md", "open")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        rc = arstamp.cs_resolve_memo(str(mp), "--actioned-note", "done")
        assert rc == 1

    def test_disposition_flag_parsing(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "r3.md", "open")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        rc = arstamp.cs_resolve_memo(
            str(mp), "--decision", "accepted", "--realized-by", "commit-abc123",
        )
        assert rc == 0
        text = mp.read_text(encoding="utf-8")
        assert "decision: accepted" in text
        assert "realized_by: commit-abc123" in text

    def test_exactly_one_disposition_field_still_enforced(self, tmp_path, monkeypatch):
        """resolve's own disposition validation (shared with action) still fires —
        cs_resolve_memo does not loosen the exactly-one-disposition-field rule."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "r4.md", "open")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")

        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = arstamp.cs_resolve_memo(
                str(mp), "--decision", "accepted", "--realized-by", "sha1",
                "--actioned-note", "also this",
            )
        assert rc != 0
        assert "status: open" in mp.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# cs_stamp_plan_implemented — native in-process delegate to
# coordinator_core.ops.plan_status_transition.main; these tests assert the
# wrapper's exit-code propagation against the real contract (that module's
# own test suite covers its status-transition matrix in depth).
# ---------------------------------------------------------------------------

class TestStampPlanImplemented:
    def _seed_plan(self, tmp_path: Path, status: str) -> Path:
        path = tmp_path / "plan.md"
        path.write_text(
            f"---\nstatus: {status}\n---\n\nBody.\n",
            encoding="utf-8",
        )
        return path

    def test_flippable_status_flips_to_implemented(self, tmp_path):
        plan = self._seed_plan(tmp_path, "draft")
        rc = arstamp.cs_stamp_plan_implemented(str(plan))
        assert rc == 0
        assert "status: implemented" in plan.read_text(encoding="utf-8")

    def test_terminal_status_is_a_noop(self, tmp_path):
        plan = self._seed_plan(tmp_path, "implemented")
        rc = arstamp.cs_stamp_plan_implemented(str(plan))
        assert rc == 0
        assert "status: implemented" in plan.read_text(encoding="utf-8")

    def test_nonexistent_plan_path_returns_one(self, tmp_path):
        missing = tmp_path / "does-not-exist.md"
        rc = arstamp.cs_stamp_plan_implemented(str(missing))
        assert rc == 1


def _seed_archived_handoff(
    repo: Path, name: str, deployment_state: str = "in_flight", extra: str = ""
) -> Path:
    """Write an archive/handoffs/2026-07/<name>.md — the repair verb's own
    root, distinct from _seed_handoff's state/handoffs/. Committed (not just
    written) so `cs_repair_archived_deployment_state`'s own worktree
    resolution (`_resolve_repo_root_for`) has a real git ancestor to find."""
    path = repo / "archive" / "handoffs" / "2026-07" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Archived Handoff {name}"\n'
        "created: 2026-07-01\n"
        "branch: work/test/2026-07-01\n"
        "status: claimed\n"
        'predecessor: "none"\n'
        "kind: session-handoff\n"
        f"deployment_state: {deployment_state}\n"
        "claimed_at: '2026-07-01T00:00:00Z'\n"
        "claimed_by: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n"
    )
    if extra:
        fm += extra
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


class TestRepairArchivedDeploymentState:
    """Wrapper-level (cs_repair_archived_deployment_state) coverage: real
    worktree resolution (_resolve_repo_root_for) + real in-process call to
    handoff_stamp._repair_archived_deployment_state_handler — the actual path
    an archive-stamp-cli invocation takes end to end, minus the CLI argv
    parsing itself (covered separately, mock-only, in
    coordinator/bin/tests/test_archive_stamp_cli_repair_archived_deployment_state.py).

    Spec backlink: example-doctrine-repo cross-repo memo, 2026-07-26 — 13 archived
    handoffs stuck at deployment_state: in_flight, hand-edited because
    ship-handoff's state/handoffs/-only containment refuses archive/handoffs/
    paths. This is the AC the whole verb exists to satisfy: an archived
    handoff still carrying deployment_state: in_flight must be stampable
    through the op.
    """

    def test_in_flight_to_shipped_via_wrapper(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hpath = _seed_archived_handoff(repo, "wrap-shipped.md")

        rc = arstamp.cs_repair_archived_deployment_state(
            str(hpath), "test: repair stuck in_flight", "shipped"
        )

        assert rc == 0
        text = hpath.read_text(encoding="utf-8")
        assert "deployment_state: shipped" in text

    def test_continued_without_continued_into_fails_closed_via_wrapper(self, tmp_path):
        """Same regression as the handler-level test, exercised through the
        wrapper: a repair to continued with no continued_into must be
        rejected — the wrapper must not paper over the handler's cross-field
        guard."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hpath = _seed_archived_handoff(repo, "wrap-continued-bad.md")
        original = hpath.read_text(encoding="utf-8")

        rc = arstamp.cs_repair_archived_deployment_state(
            str(hpath), "test: missing continued_into", "continued"
        )

        assert rc == 1
        assert hpath.read_text(encoding="utf-8") == original

    def test_continued_with_continued_into_succeeds_via_wrapper(self, tmp_path):
        """continued_into is resolution-and-existence checked (2026-07-26) —
        the successor must resolve to a real handoff_id somewhere under the
        worktree, or the wrapper returns rc=1 (see the engine-level coverage
        in coordinator_core/ops/tests/test_handoff_stamp.py for the
        fabricated-value-rejected and override-path cases)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hpath = _seed_archived_handoff(repo, "wrap-continued-ok.md")
        _seed_archived_handoff(
            repo, "wrap-continued-successor.md",
            extra='handoff_id: "hnd-successor-abc123"\n',
        )

        rc = arstamp.cs_repair_archived_deployment_state(
            str(hpath),
            "test: repair with succession proof",
            "continued",
            continued_into="hnd-successor-abc123",
        )

        assert rc == 0
        text = hpath.read_text(encoding="utf-8")
        assert "deployment_state: continued" in text
        assert "continued_into: hnd-successor-abc123" in text

    def test_terminal_record_is_refused_and_left_untouched(self, tmp_path):
        """The wrapper inherits the door's self-extinguishing precondition.

        This test previously asserted a terminal record was a clean rc=0 no-op.
        That contract was deliberately retired: the repair door now refuses any
        record whose current deployment_state is already terminal, because a
        terminal state is what a validly-archived record looks like and mutating
        one is outside this door's reach. The only permitted transition is
        non-terminal -> terminal, which is what makes the door self-extinguishing
        — a repaired record leaves its reachable set forever.

        The surviving half of the old assertion is the load-bearing one and is
        kept: a refusal must not write.
        """
        repo = tmp_path / "repo"
        _init_repo(repo)
        hpath = _seed_archived_handoff(repo, "wrap-idem.md", deployment_state="shipped")
        original = hpath.read_text(encoding="utf-8")

        rc = arstamp.cs_repair_archived_deployment_state(
            str(hpath), "test: already shipped", "shipped"
        )

        assert rc != 0
        assert hpath.read_text(encoding="utf-8") == original

    def test_second_call_after_repair_refuses_self_extinguishing(self, tmp_path):
        """(2026-07-27 review Finding 4) Proves the two-call
        self-extinguishing property the `_TERMINAL_DEPLOYMENT_STATES`
        docstring in `coordinator_core/ops/handoff_stamp.py` describes: a
        record this door just repaired (non-terminal -> terminal) leaves its
        own reachable set immediately, so a follow-up call in the same
        session -- naming the SAME target state or a DIFFERENT one -- must
        be refused, not silently re-applied. The prior docstring citation
        (`test_repair_archived_deployment_state_refuses_second_call_after_
        repair`) named a test that did not exist; this is that test."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hpath = _seed_archived_handoff(repo, "wrap-two-call.md")

        rc1 = arstamp.cs_repair_archived_deployment_state(
            str(hpath), "test: first repair, non-terminal -> shipped", "shipped"
        )
        assert rc1 == 0
        repaired = hpath.read_text(encoding="utf-8")
        assert "deployment_state: shipped" in repaired

        # Same target state named again -> refused, no further write.
        rc2 = arstamp.cs_repair_archived_deployment_state(
            str(hpath), "test: second repair attempt, same target", "shipped"
        )
        assert rc2 != 0
        assert hpath.read_text(encoding="utf-8") == repaired

        # A DIFFERENT target state named on the now-terminal record -> also
        # refused, not silently re-mutated.
        rc3 = arstamp.cs_repair_archived_deployment_state(
            str(hpath), "test: second repair attempt, different target", "closed",
            closed_reason="stale",
        )
        assert rc3 != 0
        assert hpath.read_text(encoding="utf-8") == repaired


# ---------------------------------------------------------------------------
# Regression: archive_stamp <-> handoff_archive_transition circular import.
#
# archive_stamp imports coordinator_core.ops.session_context, whose package
# import (coordinator_core.ops/__init__.py's eager-import sweep, unless
# COORDINATOR_CORE_LAZY_OPS is set) reaches handoff_archive_transition in
# turn. If handoff_archive_transition imports archive_stamp's _run_git /
# stamp_shipped_in at module top-level, that back-edge fires while
# archive_stamp is still mid-import (its own _run_git isn't defined yet) —
# ImportError: cannot import name '_run_git' from partially initialized
# module. A fresh interpreter subprocess is required to observe this: within
# THIS pytest process other test modules have already imported
# coordinator_core.ops.* eagerly, which would mask the cycle.
# ---------------------------------------------------------------------------

class TestArchiveStampColdImport:
    def test_import_archive_stamp_cold(self):
        proc = subprocess.run(
            ["python3", "-c", "import coordinator_core.archive_stamp"],
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            cwd=_REPO_ROOT,
        )
        assert proc.returncode == 0, (
            f"cold import of coordinator_core.archive_stamp failed "
            f"(circular import with handoff_archive_transition?):\n{proc.stderr}"
        )

    def test_import_both_directions_cold(self):
        proc = subprocess.run(
            [
                "python3",
                "-c",
                "import coordinator_core.archive_stamp; "
                "import coordinator_core.ops.handoff_archive_transition; "
                "print('ok')",
            ],
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            cwd=_REPO_ROOT,
        )
        assert proc.returncode == 0, (
            f"cold import of both modules failed:\n{proc.stderr}"
        )
        assert "ok" in proc.stdout


class TestCorrectHandoffBody:
    """cs_correct_handoff_body — CLI veneer over the handoff.correct_body op.

    Spec: archive/specs/2026-07/2026-07-31-claimed-baton-body-correction-route.md,
    chunk C8. Asserts the WRAPPER's orchestration (repo-root resolution, param
    building, exit-code propagation) — the op's own full precondition matrix is
    already covered by coordinator_core/ops/tests/ (C3); this class exercises a
    representative slice (happy path plus a few refusal paths), mirroring the
    depth of TestHandoffTransitionWrappers/TestShipHandoff above rather than
    re-deriving the op's own test suite.
    """

    def test_happy_path_applies_correction_and_stamps_note(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "corr1.md", "claimed", "in_flight",
            extra=f"authoring_session: {_DEFAULT_TEST_SESSION_ID}\n",
        )
        rc = arstamp.cs_correct_handoff_body(str(hp), "Body.", "Corrected body.")
        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "Corrected body." in text
        assert "Body." not in text.split("---", 2)[2]  # replaced below frontmatter
        assert "## Correction Log (handoff.correct_body)" in text
        assert "<!-- handoff-correct-body-correction:" in text
        assert _DEFAULT_TEST_SESSION_ID in text

    def test_legacy_consumed_status_accepted(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "corr2.md", "consumed", "in_flight",
            extra=f"authoring_session: {_DEFAULT_TEST_SESSION_ID}\n",
        )
        rc = arstamp.cs_correct_handoff_body(str(hp), "Body.", "Corrected body.")
        assert rc == 0

    def test_refuses_when_authoring_session_absent(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "corr3.md", "claimed", "in_flight")
        rc = arstamp.cs_correct_handoff_body(str(hp), "Body.", "Corrected body.")
        assert rc == 1
        assert "Corrected body." not in hp.read_text(encoding="utf-8")

    def test_refuses_on_session_mismatch(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "corr4.md", "claimed", "in_flight",
            extra="authoring_session: 99999999-9999-9999-9999-999999999999\n",
        )
        rc = arstamp.cs_correct_handoff_body(str(hp), "Body.", "Corrected body.")
        assert rc == 1
        assert "Corrected body." not in hp.read_text(encoding="utf-8")

    def test_refuses_when_status_open(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "corr5.md", "open", "ready_to_fire",
            extra=f"authoring_session: {_DEFAULT_TEST_SESSION_ID}\n",
        )
        rc = arstamp.cs_correct_handoff_body(str(hp), "Body.", "Corrected body.")
        assert rc == 1

    def test_refuses_noop_replacement(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "corr6.md", "claimed", "in_flight",
            extra=f"authoring_session: {_DEFAULT_TEST_SESSION_ID}\n",
        )
        rc = arstamp.cs_correct_handoff_body(str(hp), "Body.", "Body.")
        assert rc == 1

    def test_refuses_old_string_zero_occurrences(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "corr7.md", "claimed", "in_flight",
            extra=f"authoring_session: {_DEFAULT_TEST_SESSION_ID}\n",
        )
        rc = arstamp.cs_correct_handoff_body(str(hp), "nonexistent-text", "replacement")
        assert rc == 1

    def test_error_printed_to_stderr_on_refusal(self, tmp_path, capsys):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "corr8.md", "claimed", "in_flight")
        rc = arstamp.cs_correct_handoff_body(str(hp), "Body.", "Corrected body.")
        assert rc == 1
        captured = capsys.readouterr()
        assert "cs_correct_handoff_body:" in captured.err
        assert "authoring_session" in captured.err

    def test_message_printed_to_stderr_on_success(self, tmp_path, capsys):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "corr9.md", "claimed", "in_flight",
            extra=f"authoring_session: {_DEFAULT_TEST_SESSION_ID}\n",
        )
        rc = arstamp.cs_correct_handoff_body(str(hp), "Body.", "Corrected body.")
        assert rc == 0
        captured = capsys.readouterr()
        assert "cs_correct_handoff_body:" in captured.err


class TestArchiveStampCliCorrectHandoffBodyDispatch:
    """One CLI-dispatch test for archive-stamp-cli main()'s new
    correct-handoff-body branch (spec C8) — flag scanning + wrapper-call
    wiring, not the op's own business logic (covered above)."""

    @staticmethod
    def _load_cli_module():
        import importlib.util
        from importlib.machinery import SourceFileLoader

        # archive-stamp-cli has no .py suffix — spec_from_file_location cannot
        # infer a loader from the extension alone, so an explicit
        # SourceFileLoader is required (mirrors how a shebang-only script gets
        # imported for testing elsewhere in this repo).
        cli_path = _REPO_ROOT / "coordinator" / "bin" / "archive-stamp-cli"
        loader = SourceFileLoader("archive_stamp_cli_under_test", str(cli_path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod

    def test_correct_handoff_body_dispatches_with_flags(self, tmp_path, monkeypatch):
        cli = self._load_cli_module()
        calls = []

        def _fake_correct(handoff_path, old_string, new_string):
            calls.append((handoff_path, old_string, new_string))
            return 0

        monkeypatch.setattr(arstamp, "cs_correct_handoff_body", _fake_correct)
        monkeypatch.setattr(cli, "_import_module", lambda: arstamp)

        rc = cli.main(
            [
                "correct-handoff-body",
                "state/handoffs/x.md",
                "--old-string",
                "was wrong",
                "--new-string",
                "is right",
            ]
        )
        assert rc == 0
        assert calls == [("state/handoffs/x.md", "was wrong", "is right")]

    def test_correct_handoff_body_requires_old_string(self, tmp_path, monkeypatch):
        cli = self._load_cli_module()
        monkeypatch.setattr(cli, "_import_module", lambda: arstamp)
        rc = cli.main(["correct-handoff-body", "state/handoffs/x.md", "--new-string", "is right"])
        assert rc == 2

    def test_correct_handoff_body_requires_new_string(self, tmp_path, monkeypatch):
        cli = self._load_cli_module()
        monkeypatch.setattr(cli, "_import_module", lambda: arstamp)
        rc = cli.main(["correct-handoff-body", "state/handoffs/x.md", "--old-string", "was wrong"])
        assert rc == 2

    def test_correct_handoff_body_help(self, capsys):
        cli = self._load_cli_module()
        rc = cli.main(["correct-handoff-body", "--help"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "ANTI-ACCIDENT" in captured.out
        assert "ANTI-ADVERSARY" in captured.out


class TestDiskReadHelpersDegradeOnUnicodeDecodeError:
    """Review: code-reviewer (nit F4) — `_read_current_shipped_in` and
    `_reread_supersede_frontmatter` promise "None on unreadable"; a non-UTF-8
    handoff file raises UnicodeDecodeError (a ValueError subclass, not an
    OSError), which must degrade the same as a missing file, not propagate."""

    def test_read_current_shipped_in_degrades_on_non_utf8_bytes(self, tmp_path):
        p = tmp_path / "bad.md"
        p.write_bytes(b"\xff\xfe not utf-8")
        assert arstamp._read_current_shipped_in(str(p)) is None

    def test_reread_supersede_frontmatter_degrades_on_non_utf8_bytes(self, tmp_path):
        p = tmp_path / "bad.md"
        p.write_bytes(b"\xff\xfe not utf-8")
        assert arstamp._reread_supersede_frontmatter(p) == (None, None)


# Negative spec -- `spawns_process` is CLASS-scoped here, never a module-level
# `pytestmark`. A module-level marker was tried and reverted (2026-08-13): it
# was written as `pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]`,
# and `cadence` is the fast-tier exclusion marker, so it silently dropped all
# ~148 tests in this file out of the fast tier to satisfy the spawn ratchet.
# Class scoping marks the only class that actually spawns, keeps the rest of
# this file in the fast tier, and keeps this file's entry in the ratchet's
# `_BASELINE` valid (the pre-existing unmarked spawn sites still register).
# See state/debt-backlog/DSR-2026-08-13-archive-stamp-import-order-drops-an-op-
# from-the-registry.yaml.
@pytest.mark.spawns_process
class TestArchiveStampFirstImportOrderDoesNotDropOps:
    """Regression: state/debt-backlog/DSR-2026-08-13-archive-stamp-import-order-
    drops-an-op-from-the-registry.yaml. Importing `coordinator_core.archive_stamp`
    BEFORE `coordinator_core.ops._eager_import_all()` used to trigger a genuine
    import cycle (archive_stamp -> pickup_assemble -> session_ledger.
    aggregate_chain_loe -> pickup_assemble, half-initialised), which raised
    ImportError inside `_eager_import_all`'s per-module try/except and silently
    dropped `session_ledger.aggregate_chain_loe` from the op registry while every
    other op module loaded fine.

    Must run in a FRESH interpreter: import order cannot be reset within a live
    process once `sys.modules` is populated, so a same-process assertion here
    would either always pass (module already cached) or corrupt every other
    test's import state.

    Asserts on the engine's own eager-import diagnostic, NOT on op resolution.
    Resolution is the WRONG oracle here and a vacuous one: `get_op_handler`
    retries a registry miss through `_lazy_import_and_lookup`, and by that
    point `archive_stamp` has finished initialising, so the retry succeeds and
    the op resolves even on the broken tree. `ops.get_poisoned_modules()` is
    equally vacuous — `_eager_import_all` pops the entry once a later pass
    succeeds. Both were measured against a clean pre-fix checkout and passed
    there, which is what makes them useless as regression oracles.

    What actually differs pre/post fix is the diagnostic: the broken tree emits
    2 `FAILED to import` lines plus an ImportError traceback during eager
    import; the fixed tree emits none. That noise is the real defect — the op
    self-heals, so nothing is permanently lost, but every process taking this
    import order printed a cycle traceback into hook and CLI output.

    Op resolution is still asserted alongside, as a floor: it would catch a
    future break severe enough to defeat the lazy retry as well."""

    def test_archive_stamp_first_leaves_aggregate_chain_loe_registered(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, '.')\n"
                    "import coordinator_core.archive_stamp\n"
                    "import coordinator_core.ops as o\n"
                    "o._eager_import_all()\n"
                    "from coordinator_core.ipc import get_op_handler\n"
                    "handler = get_op_handler('session_ledger.aggregate_chain_loe')\n"
                    "assert handler is not None, 'aggregate_chain_loe not registered'\n"
                    "print('OK')\n"
                ),
            ],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert proc.returncode == 0, (
            f"archive_stamp-first import order failed to leave "
            f"session_ledger.aggregate_chain_loe registered: "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        assert "OK" in proc.stdout
        assert "FAILED to import" not in proc.stderr, (
            f"eager import reported a module failure under the "
            f"archive_stamp-first order — the import cycle is back: "
            f"stderr={proc.stderr!r}"
        )

    def test_ops_first_reciprocal_order_still_leaves_it_registered(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, '.')\n"
                    "import coordinator_core.ops as o\n"
                    "o._eager_import_all()\n"
                    "import coordinator_core.archive_stamp\n"
                    "from coordinator_core.ipc import get_op_handler\n"
                    "handler = get_op_handler('session_ledger.aggregate_chain_loe')\n"
                    "assert handler is not None, 'aggregate_chain_loe not registered'\n"
                    "print('OK')\n"
                ),
            ],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert proc.returncode == 0, (
            f"ops-first reciprocal import order failed to leave "
            f"session_ledger.aggregate_chain_loe registered: "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        assert "OK" in proc.stdout

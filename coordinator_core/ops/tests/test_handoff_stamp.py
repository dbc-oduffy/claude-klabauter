"""
coordinator_core.ops.tests.test_handoff_stamp

Tests for the handoff.stamp op.

Import guard: coordinator_core.ops.handoff_stamp MUST be imported at module
load time so @register_op("handoff.stamp") fires and populates _REGISTRY.

Coverage:
  (a) registry assertion — op name present in _REGISTRY after import
  (b) insert            — inserts shipped_in after consumed_at; applied=True
  (c) insert-no-consumed-at — inserts shipped_in at end when consumed_at absent
  (d) idempotent-skip   — shipped_in already present → applied=False, exit_code 0,
                          file content unchanged
  (e) SHA quoting: '#'-in-SHA  → single-quoted in output
  (f) SHA quoting: all-numeric → single-quoted in output
  (g) SHA quoting: scientific-notation → single-quoted in output
  (h) missing handoff_path param  → exit_code 1
  (i) missing sha param           → exit_code 1
  (j) file not found              → exit_code 1
  (k) no valid frontmatter        → exit_code 1 (surfaces as MutateAbort internally)
  (m) LockTimeout                 → exit_code 1 with lock-timeout message
  (n) idempotent via locked_rmw   — byte-identical path; no write; applied=False

Spec backlink: coordinator_core/ops/handoff_stamp.py
Port source:   coordinator/bin/stamp-shipped-in.js
"""

from __future__ import annotations

import asyncio
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.handoff_stamp  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.locked_write import LockTimeout
from coordinator_core.ops.handoff_stamp import (
    _handler,
    _repair_archived_deployment_state_handler,
)

# Positive floor assertion: op must be registered before any test runs.
_OP_NAME = "handoff.stamp"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_stamp @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)



def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo and return its root.

    Produces:
    - git init -b main
    - user.email / user.name / commit.gpgsign=false
    - state/handoffs/ directory with .gitkeep, committed as initial skeleton.

    Returns repo_root (the main worktree root, NOT the .git dir).
    The caller should pass repo_root / ".git" as ``repo_root`` to the handler
    (P9 WORKTREE DERIVATION: handler receives common_dir = <worktree>/.git).
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "stamp-test@claude-klabauter.test")
    _git("config", "user.name", "Stamp Test")
    _git("config", "commit.gpgsign", "false")

    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return repo


def _seed_handoff(repo: Path, name: str, extra_fm: str = "") -> Path:
    """Write a state/handoffs/<name>.md with minimal YAML frontmatter.

    Returns the absolute path to the file.  Does NOT commit — for stamp tests
    the file does not need to be git-tracked (stamp only writes frontmatter).
    """
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    content = textwrap.dedent(f"""\
        ---
        title: "Test Handoff"
        status: claimed
        claimed_at: 2026-07-05T12:00:00Z
        {extra_fm.strip()}
        ---

        # Handoff body.
    """)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# (a) Registry assertion
# ---------------------------------------------------------------------------


def test_op_registered():
    """handoff.stamp must appear in _REGISTRY (import guard validates at module load)."""
    assert _OP_NAME in _REGISTRY


# ---------------------------------------------------------------------------
# (b) Insert — shipped_in after consumed_at
# ---------------------------------------------------------------------------


def test_insert_after_claimed_at(tmp_path):
    """shipped_in is inserted immediately after claimed_at when both present."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-07-05-test.md")

    result = _run(_handler(
        {"handoff_path": str(hpath), "sha": "abc123def456", "kind": "ship-commit"},        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    # Review: code-reviewer (F6) — message field must be present on exit_code 0 responses.
    assert "message" in result, f"exit_code=0 response must include 'message'; got {result!r}"

    lines = hpath.read_text(encoding="utf-8").splitlines()
    # Find claimed_at line; shipped_in must be the very next frontmatter line.
    claimed_idx = next(
        i for i, ln in enumerate(lines) if ln.startswith("claimed_at:")
    )
    shipped_line = lines[claimed_idx + 1]
    assert shipped_line == "shipped_in: abc123def456", (
        f"expected 'shipped_in: abc123def456' after claimed_at, got {shipped_line!r}"
    )


def test_insert_after_consumed_at_archived_schema(tmp_path):
    """Archived-schema fallback: shipped_in anchors on consumed_at when claimed_at
    is absent (pre-DR-084 record shape) — schema_validate/emit tolerate old-name
    inputs, so the writer's fallback path is deliberately exercised with old
    vocabulary here."""
    repo = _make_git_repo(tmp_path)
    path = repo / "state" / "handoffs" / "2026-07-05-archived.md"
    path.write_text(
        textwrap.dedent("""\
            ---
            title: "Archived Schema Handoff"
            status: superseded
            consumed_at: 2026-07-05T12:00:00Z
            ---

            # Handoff body.
        """),
        encoding="utf-8",
    )

    result = _run(_handler(
        {"handoff_path": str(path), "sha": "abc123def456", "kind": "ship-commit"},        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    lines = path.read_text(encoding="utf-8").splitlines()
    consumed_idx = next(
        i for i, ln in enumerate(lines) if ln.startswith("consumed_at:")
    )
    shipped_line = lines[consumed_idx + 1]
    assert shipped_line == "shipped_in: abc123def456", (
        f"expected 'shipped_in: abc123def456' after consumed_at, got {shipped_line!r}"
    )


# ---------------------------------------------------------------------------
# (c) Insert when consumed_at absent — appended to end of frontmatter
# ---------------------------------------------------------------------------


def test_insert_no_claimed_at(tmp_path):
    """When claimed_at is absent, shipped_in is appended to end of frontmatter."""
    repo = _make_git_repo(tmp_path)
    # Write a handoff WITHOUT claimed_at
    path = repo / "state" / "handoffs" / "2026-07-05-no-claimed.md"
    path.write_text(
        textwrap.dedent("""\
            ---
            title: "No claimed_at"
            status: claimed
            ---

            Body.
        """),
        encoding="utf-8",
    )

    result = _run(_handler(
        {"handoff_path": str(path), "sha": "deadbeef", "kind": "ship-commit"},        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    text = path.read_text(encoding="utf-8")
    # shipped_in must appear inside frontmatter (before closing ---)
    fm_end = text.index("\n---\n", text.index("---\n") + 4)
    fm_text = text[text.index("---\n") + 4: fm_end]
    assert "shipped_in: deadbeef" in fm_text, (
        f"shipped_in not found in frontmatter: {fm_text!r}"
    )


# ---------------------------------------------------------------------------
# (d) Idempotent skip — shipped_in already present
# ---------------------------------------------------------------------------


def test_idempotent_skip(tmp_path):
    """When shipped_in is already present, returns exit_code 0, applied False; file unchanged."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(
        repo, "2026-07-05-stamped.md", extra_fm="shipped_in: already999"
    )
    original = hpath.read_text(encoding="utf-8")

    result = _run(_handler(
        {"handoff_path": str(hpath), "sha": "newsha111", "kind": "ship-commit"},        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is False
    # Review: code-reviewer (F6) — message field must be present on idempotent-skip path.
    assert "message" in result, f"exit_code=0 idempotent response must include 'message'; got {result!r}"
    # File must be byte-identical — no write occurred.
    assert hpath.read_text(encoding="utf-8") == original, (
        "file was mutated despite shipped_in already present (idempotency violation)"
    )


# ---------------------------------------------------------------------------
# (kind) shipped_in_kind — DR-096 (2026-07-26 ruling)
# ---------------------------------------------------------------------------


def test_kind_inserted_in_lockstep_with_shipped_in(tmp_path):
    """kind is written as shipped_in_kind immediately after shipped_in on insert."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-07-26-kind-insert.md")

    result = _run(_handler(
        {"handoff_path": str(hpath), "sha": "abc123def456", "kind": "scope-derived"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["kind"] == "scope-derived"
    assert result["prior_kind"] is None

    lines = hpath.read_text(encoding="utf-8").splitlines()
    shipped_idx = next(i for i, ln in enumerate(lines) if ln.startswith("shipped_in:"))
    assert lines[shipped_idx + 1] == "shipped_in_kind: scope-derived", (
        f"expected shipped_in_kind immediately after shipped_in, got {lines[shipped_idx + 1]!r}"
    )


def test_kind_omitted_is_rejected(tmp_path):
    """Omitting kind entirely is now REJECTED (DR-096 choke-point closure,
    DoE-claude 2026-07-26/27 follow-up) — the op no longer accepts an
    unstated kind from any caller; no write occurs and the file is
    untouched. Supersedes the pre-follow-up
    ``test_kind_omitted_leaves_shipped_in_kind_absent`` (which asserted the
    OPPOSITE: that omitting kind was tolerated) — that assertion was exactly
    the bypass this follow-up closes."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-07-26-kind-omitted.md")
    original = hpath.read_text(encoding="utf-8")

    result = _run(_handler(
        {"handoff_path": str(hpath), "sha": "abc123def456"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert result["kind"] is None
    assert "kind" in result.get("error", "")
    assert hpath.read_text(encoding="utf-8") == original


def test_kind_rejected_when_unknown(tmp_path):
    """A kind outside the DR-096 enum is rejected fail-loud — no write occurs."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-07-26-kind-bad.md")
    original = hpath.read_text(encoding="utf-8")

    result = _run(_handler(
        {"handoff_path": str(hpath), "sha": "abc123def456", "kind": "guessed"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert result["kind"] == "guessed"
    assert "guessed" in result.get("error", "")
    assert hpath.read_text(encoding="utf-8") == original


def test_kind_idempotent_skip_leaves_kind_untouched(tmp_path):
    """shipped_in already present, force not set: the whole write (shipped_in AND
    shipped_in_kind) is skipped — kind is never applied as a partial write on its
    own when shipped_in itself is not being (re)written."""
    repo = _make_git_repo(tmp_path)
    hpath = repo / "state" / "handoffs" / "2026-07-26-kind-skip.md"
    hpath.write_text(
        textwrap.dedent("""\
            ---
            title: "Test Handoff"
            status: claimed
            claimed_at: 2026-07-05T12:00:00Z
            shipped_in: already999
            shipped_in_kind: ship-commit
            ---

            # Handoff body.
        """),
        encoding="utf-8",
    )
    original = hpath.read_text(encoding="utf-8")

    result = _run(_handler(
        {"handoff_path": str(hpath), "sha": "newsha111", "kind": "scope-derived"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original


def test_kind_force_replace_rewrites_kind_in_lockstep(tmp_path):
    """force=True replaces shipped_in AND shipped_in_kind together, reporting the
    prior kind for auditability — mirrors prior_value's own contract."""
    repo = _make_git_repo(tmp_path)
    hpath = repo / "state" / "handoffs" / "2026-07-26-kind-force.md"
    hpath.write_text(
        textwrap.dedent("""\
            ---
            title: "Test Handoff"
            status: claimed
            claimed_at: 2026-07-05T12:00:00Z
            shipped_in: peersha01
            shipped_in_kind: scope-derived
            ---

            # Handoff body.
        """),
        encoding="utf-8",
    )

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "sha": "ownsha001",
            "kind": "ship-commit",
            "force": True,
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["replaced"] is True
    assert result["kind"] == "ship-commit"
    assert result["prior_kind"] == "scope-derived"

    lines = hpath.read_text(encoding="utf-8").splitlines()
    shipped_idx = next(i for i, ln in enumerate(lines) if ln.startswith("shipped_in:"))
    assert lines[shipped_idx + 1] == "shipped_in_kind: ship-commit"


def test_kind_force_replace_without_kind_is_rejected(tmp_path):
    """force=True with NO kind param is REJECTED (DR-096 choke-point closure,
    DoE-claude 2026-07-26/27 follow-up) — kind is required unconditionally,
    including on the force-replace path; no write occurs. Supersedes the
    pre-follow-up ``test_kind_force_replace_without_kind_leaves_prior_kind_untouched``
    (which asserted the force-replace succeeded silently with kind omitted)."""
    repo = _make_git_repo(tmp_path)
    hpath = repo / "state" / "handoffs" / "2026-07-26-kind-force-nokind.md"
    hpath.write_text(
        textwrap.dedent("""\
            ---
            title: "Test Handoff"
            status: claimed
            claimed_at: 2026-07-05T12:00:00Z
            shipped_in: peersha02
            shipped_in_kind: scope-derived
            ---

            # Handoff body.
        """),
        encoding="utf-8",
    )
    original = hpath.read_text(encoding="utf-8")

    result = _run(_handler(
        {"handoff_path": str(hpath), "sha": "ownsha002", "force": True},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "kind" in result.get("error", "")
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# (l) Relative path — P9 worktree derivation (AC15 gap; Finding 3)
# ---------------------------------------------------------------------------


def test_insert_relative_path_resolves_from_worktree(tmp_path):
    """stamp accepts a repo-relative handoff_path and resolves against the worktree (P9).

    Review: code-reviewer (F3) — AC15 P9 gap: main_worktree_root is only called on the
    relative-path branch; all prior tests pass absolute paths. This test exercises that
    branch and verifies the file is written at <worktree>/state/handoffs/, not
    <worktree>/.git/state/handoffs/.
    """
    repo = _make_git_repo(tmp_path)
    _seed_handoff(repo, "2026-07-05-relative.md")
    rel_path = "state/handoffs/2026-07-05-relative.md"

    result = _run(_handler(
        {"handoff_path": rel_path, "sha": "abc123def456", "kind": "ship-commit"},        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert "message" in result

    # P9: file must exist at <worktree>/state/handoffs/ (not <worktree>/.git/state/handoffs/).
    real_path = repo / "state" / "handoffs" / "2026-07-05-relative.md"
    assert real_path.is_file(), (
        "P9 failure: stamp must resolve relative path to <worktree>/state/handoffs/, "
        "not <worktree>/.git/state/handoffs/"
    )
    text = real_path.read_text(encoding="utf-8")
    assert "shipped_in: abc123def456" in text, (
        f"shipped_in must be inserted in the resolved file; got:\n{text}"
    )


# ---------------------------------------------------------------------------
# SHA quoting cases — parity-critical (stamp-shipped-in.js serializeYamlScalar)
# ---------------------------------------------------------------------------


def test_sha_quoting_hash_in_sha(tmp_path):
    """SHA containing '#' is single-quoted to prevent YAML comment truncation (the Staff Engineer F0)."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-07-05-hash-sha.md")

    sha_with_hash = "abc#123"
    result = _run(_handler(
        {"handoff_path": str(hpath), "sha": sha_with_hash, "kind": "ship-commit"},        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    text = hpath.read_text(encoding="utf-8")
    # Quoting: YAML would truncate `abc#123` to `abc`; must be stored as `'abc#123'`
    assert "shipped_in: 'abc#123'" in text, (
        f"expected quoted shipped_in value; file content:\n{text}"
    )


def test_sha_quoting_all_numeric(tmp_path):
    """All-numeric SHA is single-quoted to prevent YAML integer coercion."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-07-05-numeric-sha.md")

    sha_numeric = "274671833"
    result = _run(_handler(
        {"handoff_path": str(hpath), "sha": sha_numeric, "kind": "ship-commit"},        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    text = hpath.read_text(encoding="utf-8")
    assert "shipped_in: '274671833'" in text, (
        f"expected quoted all-numeric SHA; file content:\n{text}"
    )


def test_sha_quoting_scientific_notation(tmp_path):
    """Scientific-notation SHA is single-quoted to prevent YAML 1.1 float coercion."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-07-05-sci-sha.md")

    sha_sci = "1958e194"
    result = _run(_handler(
        {"handoff_path": str(hpath), "sha": sha_sci, "kind": "ship-commit"},        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    text = hpath.read_text(encoding="utf-8")
    assert "shipped_in: '1958e194'" in text, (
        f"expected quoted scientific-notation SHA; file content:\n{text}"
    )


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_missing_handoff_path_param(tmp_path):
    """Missing handoff_path param → exit_code 1."""
    repo = _make_git_repo(tmp_path)
    result = _run(_handler(
        {"sha": "abc123"},        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "handoff_path" in result.get("error", "")


def test_missing_sha_param(tmp_path):
    """Missing sha param → exit_code 1."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-07-05-no-sha.md")
    result = _run(_handler(
        {"handoff_path": str(hpath)},        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "sha" in result.get("error", "")


def test_file_not_found(tmp_path):
    """Non-existent handoff path → exit_code 1."""
    repo = _make_git_repo(tmp_path)
    result = _run(_handler(
        {
            "handoff_path": str(repo / "state" / "handoffs" / "does-not-exist.md"),
            "sha": "abc123",
            "kind": "ship-commit",
        },        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert result["applied"] is False


def test_no_valid_frontmatter(tmp_path):
    """Handoff file with no YAML frontmatter block → exit_code 1."""
    repo = _make_git_repo(tmp_path)
    path = repo / "state" / "handoffs" / "2026-07-05-no-fm.md"
    path.write_text("# Just a heading\n\nNo frontmatter here.\n", encoding="utf-8")

    result = _run(_handler(
        {"handoff_path": str(path), "sha": "abc123", "kind": "ship-commit"},        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "frontmatter" in result.get("error", "").lower()


# ---------------------------------------------------------------------------
# (m) LockTimeout — lock contention surfaces as exit_code 1
# ---------------------------------------------------------------------------


def test_lock_timeout_returns_error(tmp_path):
    """LockTimeout from locked_rmw → exit_code 1 with informative error message.

    Mocks locked_rmw to raise LockTimeout so the handler's except-branch is
    exercised without requiring a real competing flock holder.
    """
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-07-05-lock-timeout.md")

    with patch(
        "coordinator_core.ops.handoff_stamp.locked_rmw",
        side_effect=LockTimeout("Could not acquire coordinator lock within 10s"),
    ):
        result = _run(_handler(
            {"handoff_path": str(hpath), "sha": "abc123", "kind": "ship-commit"},
            repo_root=repo / ".git",
        ))

    assert result["exit_code"] == 1, f"expected exit_code 1 on LockTimeout; got {result!r}"
    assert result["applied"] is False
    assert "lock" in result.get("error", "").lower(), (
        f"error message must mention lock; got {result.get('error')!r}"
    )


# ---------------------------------------------------------------------------
# (n) locked_rmw byte-identical path — idempotent write skip is observable
# ---------------------------------------------------------------------------


def test_rejects_traversal_path(tmp_path):
    """A handoff_path with '../' traversal segments escaping state/handoffs/ is
    rejected with exit_code=1; the out-of-tree target is NOT mutated."""
    repo = _make_git_repo(tmp_path)
    secret = repo / "secret.md"
    secret.write_text("---\ntitle: \"Secret\"\n---\n", encoding="utf-8")
    original = secret.read_text(encoding="utf-8")

    result = _run(_handler(
        {"handoff_path": "state/handoffs/../../secret.md", "sha": "abc123", "kind": "ship-commit"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, f"traversal path must be rejected; got {result!r}"
    assert result["applied"] is False
    assert secret.read_text(encoding="utf-8") == original


def test_rejects_out_of_tree_absolute_path(tmp_path):
    """An out-of-tree absolute handoff_path is rejected; target is NOT mutated."""
    repo = _make_git_repo(tmp_path)
    outside = tmp_path / "outside" / "secret.md"
    outside.parent.mkdir(parents=True)
    outside.write_text('---\ntitle: "Secret"\n---\n', encoding="utf-8")
    original = outside.read_text(encoding="utf-8")

    result = _run(_handler(
        {"handoff_path": str(outside), "sha": "abc123", "kind": "ship-commit"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert outside.read_text(encoding="utf-8") == original


def test_rejects_archive_handoffs_path(tmp_path):
    """A handoff_path under archive/handoffs/ is rejected — stamp is a mutation
    verb and its allowed root is state/handoffs/ ONLY (archived is out of scope)."""
    repo = _make_git_repo(tmp_path)
    archived = repo / "archive" / "handoffs" / "2026-01" / "old.md"
    archived.parent.mkdir(parents=True)
    archived.write_text(
        "---\ntitle: \"Old\"\nconsumed_at: 2026-01-01T00:00:00Z\n---\n\nBody.\n",
        encoding="utf-8",
    )
    original = archived.read_text(encoding="utf-8")

    result = _run(_handler(
        {"handoff_path": str(archived), "sha": "abc123", "kind": "ship-commit"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, (
        f"archive/handoffs/ path must be rejected for a mutation verb; got {result!r}"
    )
    assert result["applied"] is False
    assert archived.read_text(encoding="utf-8") == original


def test_rejects_absolute_path_with_no_repo_root(tmp_path):
    """An absolute handoff_path with repo_root=None is rejected outright — no
    containment fallback exists for the callerless-repo_root case.

    Review: code-reviewer (Finding 1/2) — before the fix, this branch fell
    through to an unguarded `p.resolve()` with zero containment check, reaching
    locked_rmw with an out-of-tree target. locked_rmw is mocked here (rather
    than left real) because with repo_root=None the *lock-sidecar* derivation
    (git_common_dir(None)) coincidentally raises OSError before the target is
    ever touched — that accident of the locking machinery would mask the
    containment bypass under test and make this test pass against BOTH the
    buggy and fixed code. Mocking locked_rmw to actually write isolates the
    containment-guard behavior: this test must FAIL against the pre-fix code
    (file gets mutated — proving the bypass) and PASS once repo_root is
    required up front (mirroring handoff_transition.py's
    `if repo_root is None: return _err(...)` gate) before locked_rmw is ever
    reached.
    """
    repo = _make_git_repo(tmp_path)
    outside = tmp_path / "outside" / "secret.md"
    outside.parent.mkdir(parents=True)
    outside.write_text('---\ntitle: "Secret"\n---\n', encoding="utf-8")
    original = outside.read_text(encoding="utf-8")

    def _fake_locked_rmw(target, mutate, *, repo_root, **kwargs):
        # Simulate a real RMW: read, mutate, write — proving the guard (not an
        # incidental lock-path crash) is what must stop this call.
        old_text = Path(target).read_text(encoding="utf-8")
        new_text = mutate(old_text)
        Path(target).write_text(new_text, encoding="utf-8")
        return new_text

    with patch(
        "coordinator_core.ops.handoff_stamp.locked_rmw",
        side_effect=_fake_locked_rmw,
    ):
        result = _run(_handler(
            {"handoff_path": str(outside), "sha": "abc123", "kind": "ship-commit"},
            repo_root=None,
        ))

    assert result["exit_code"] == 1, (
        f"absolute handoff_path with repo_root=None must be rejected; got {result!r}"
    )
    assert result["applied"] is False
    assert outside.read_text(encoding="utf-8") == original, (
        "file was mutated — repo_root=None absolute-path bypass was not closed"
    )


def test_idempotent_via_locked_rmw_no_write(tmp_path):
    """Idempotent path returns byte-identical text to locked_rmw; no write occurs.

    Verifies that when shipped_in is already present, the _mutate closure returns
    old_text unchanged (triggering locked_rmw's byte-identical skip), the handler
    reports applied=False, exit_code=0, and the file is unmodified on disk.

    This covers the locked_rmw integration: the no-write guarantee is now enforced
    by locked_rmw's byte-comparison rather than an early-return before locked_rmw.
    """
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-07-05-idem-rmw.md", extra_fm="shipped_in: existing999")
    original = hpath.read_text(encoding="utf-8")

    result = _run(_handler(
        {"handoff_path": str(hpath), "sha": "newsha999", "kind": "ship-commit"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is False
    assert "message" in result
    # File must be byte-identical — locked_rmw must have skipped the write.
    after = hpath.read_text(encoding="utf-8")
    assert after == original, (
        "file must be byte-identical after idempotent stamp — locked_rmw write-skip failed"
    )


# ---------------------------------------------------------------------------
# _repair_archived_deployment_state_handler — provenance-repair verb for an
# ALREADY-ARCHIVED handoff's deployment_state field.
#
# Spec backlink: docs/plans/... (DoE-claude cross-repo memo, 2026-07-26) — 13
# archived handoffs stuck at deployment_state: in_flight, hand-edited because
# ship-handoff's state/handoffs/-only containment refuses archive/handoffs/
# paths. Coverage:
#   (o) archived handoff, in_flight -> shipped               — the AC this
#       verb exists for: stampable through the op, not by hand.
#   (p) in_flight -> continued WITHOUT continued_into         -> exit_code 1
#       (cross-field guard — this is the EXACT defect 10 of the 13 DoE
#       hand-edits produced; this verb must never reproduce it)
#   (q) in_flight -> continued WITH continued_into             -> succeeds,
#       continued_into inserted immediately after deployment_state
#   (r) in_flight -> closed WITHOUT closed_reason               -> exit_code 1
#   (s) in_flight -> closed WITH a valid closed_reason           -> succeeds
#   (t) closed_reason value not in cancelled|displaced|stale     -> exit_code 1
#   (u) continued_into supplied but target state != continued    -> exit_code 1
#   (v) unknown deployment_state value                            -> exit_code 1
#   (w) missing reason                                             -> exit_code 1
#   (x) path under state/handoffs/ (not archive/handoffs/)         -> exit_code 1
#       (escape guard — this verb reaches ONLY archive/handoffs/)
#   (y) idempotent no-op — requested state already holds, current state
#       NON-TERMINAL                                                -> applied=False
#   (z) E1 enforced precondition — current state already TERMINAL
#       (shipped/continued/closed), same target state named          -> exit_code 1
#       (refuses rather than no-opping — an already-terminal record is
#       validly archived and out of this door's reach)
#   (aa) E1 self-extinguishing property — first call (in_flight -> shipped)
#       succeeds; a second call on that now-repaired record refuses,
#       whether it names the same target state or a different one
# ---------------------------------------------------------------------------


def _seed_archived_handoff(
    repo: Path, name: str, deployment_state: str = "in_flight", extra_fm: str = ""
) -> Path:
    """Write an archive/handoffs/2026-07/<name>.md with minimal YAML frontmatter,
    mirroring _seed_handoff but under the archived root. Does NOT commit — the
    repair handler mutates the file in place, no git interaction required.

    `deployment_state` is a dedicated param (not folded into `extra_fm`) so a
    caller overriding it (e.g. the idempotent-no-op test seeding an
    already-shipped fixture) never produces a duplicate `deployment_state:`
    key in the frontmatter block."""
    path = repo / "archive" / "handoffs" / "2026-07" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    content = textwrap.dedent(f"""\
        ---
        title: "Test Archived Handoff"
        created: 2026-07-01
        branch: "work/test/2026-07-01"
        status: claimed
        predecessor: none
        kind: session-handoff
        deployment_state: {deployment_state}
        claimed_at: '2026-07-01T00:00:00Z'
        claimed_by: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
        {extra_fm.strip()}
        ---

        # Handoff body.
    """)
    path.write_text(content, encoding="utf-8")
    return path


def test_repair_archived_deployment_state_in_flight_to_shipped(tmp_path):
    """(o) The AC this verb exists for: an archived handoff still carrying
    deployment_state: in_flight must be stampable through the op, not by hand."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-01_test-shipped.md")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: repair stuck in_flight",
            "deployment_state": "shipped",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["prior_state"] == "in_flight"
    assert result["new_state"] == "shipped"
    text = hpath.read_text(encoding="utf-8")
    assert "deployment_state: shipped" in text
    assert "deployment_state: in_flight" not in text


def test_repair_archived_deployment_state_continued_without_continued_into_rejected(tmp_path):
    """(p) THE regression: a repair to deployment_state: continued with no
    continued_into must be rejected — this is the exact defect 10 of the 13
    DoE-claude hand-edits produced (deployment_state: continued stamped with
    no continued_into, silently failing handoff-archived.schema.json's own
    cross-field rule). This verb must fail loud instead of reproducing it."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-01_test-continued-bad.md")
    original = hpath.read_text(encoding="utf-8")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: missing continued_into",
            "deployment_state": "continued",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert "continued_into" in result["error"]
    assert hpath.read_text(encoding="utf-8") == original, (
        "rejected repair must not write anything"
    )


def test_repair_archived_deployment_state_continued_with_continued_into(tmp_path):
    """(q) continued + continued_into succeeds when continued_into resolves to
    a REAL successor handoff (by handoff_id); continued_into is inserted
    immediately after deployment_state (matches the corpus convention).

    2026-07-26: continued_into is now resolution-and-existence checked (see
    _resolve_continued_into) — a fabricated slug that merely looks plausible
    (this test's own prior fixture value, 'hnd-successor-abc123', which
    resolved to nothing on disk) is exactly the gap that check closes. The
    successor here is seeded with a real, matching handoff_id."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-01_test-continued-ok.md")
    _seed_archived_handoff(
        repo,
        "2026-07-02_test-successor.md",
        extra_fm='handoff_id: "hnd-successor-abc123"',
    )

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: repair with succession proof",
            "deployment_state": "continued",
            "continued_into": "hnd-successor-abc123",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["continued_into_verified"] is True
    lines = hpath.read_text(encoding="utf-8").splitlines()
    ds_idx = next(i for i, ln in enumerate(lines) if ln.startswith("deployment_state:"))
    assert lines[ds_idx] == "deployment_state: continued"
    assert lines[ds_idx + 1] == "continued_into: hnd-successor-abc123"


def test_repair_archived_deployment_state_continued_into_fabricated_rejected(tmp_path):
    """(z1) THE regression this workstream closes: a well-formed but
    fabricated/guessed continued_into (no matching handoff_id anywhere, no
    matching path anywhere) is rejected — exit_code 1, file unchanged. Before
    this fix the verb accepted any non-empty string silently (rc=0); this is
    the exact defect an executor's independent handoff_id cross-check caught
    by hand on 2026-07-26."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-01_test-continued-fake.md")
    original = hpath.read_text(encoding="utf-8")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: fabricated successor slug",
            "deployment_state": "continued",
            "continued_into": "hnd-this-slug-was-guessed-not-real-000000",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert "continued_into" in result["error"]
    assert "override" in result["error"]
    assert hpath.read_text(encoding="utf-8") == original, (
        "rejected continued_into must not write anything"
    )


def test_repair_archived_deployment_state_continued_into_resolves_live_handoff(tmp_path):
    """(z2) continued_into resolving against a LIVE (state/handoffs/, not yet
    archived) successor is accepted — the resolver searches both roots, per
    the corpus convention that a successor is not always itself archived
    yet."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-01_test-continued-live-succ.md")
    _seed_handoff(
        repo, "2026-07-02-live-successor.md",
        extra_fm='handoff_id: "hnd-live-successor-def456"',
    )

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: successor still live",
            "deployment_state": "continued",
            "continued_into": "hnd-live-successor-def456",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["continued_into_verified"] is True


def test_repair_archived_deployment_state_continued_into_path_shaped_resolves(tmp_path):
    """(z3) continued_into given as a worktree-relative path (the
    'state/handoffs/<file>.md' corpus convention, not a bare handoff_id) is
    resolved by direct file existence, not just handoff_id search."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-01_test-continued-path.md")
    _seed_handoff(repo, "2026-07-02-path-successor.md")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: path-shaped successor reference",
            "deployment_state": "continued",
            "continued_into": "state/handoffs/2026-07-02-path-successor.md",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["continued_into_verified"] is True


def test_repair_archived_deployment_state_continued_into_bare_slug_resolves(tmp_path):
    """(z3b) 2026-07-28 defect fix — continued_into given as a path-shaped
    but extensionless reference (e.g. missing the month-nested archive
    subdir AND the `.md` suffix) is resolved via the bare-slug fallback in
    the archive/handoffs/** basename search, not just a direct-candidate
    hit."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-01_test-continued-bare.md")
    _seed_archived_handoff(repo, "2026-07-02-bare-slug-successor.md")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: bare-slug successor reference",
            "deployment_state": "continued",
            "continued_into": "archive/handoffs/2026-07-02-bare-slug-successor",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["continued_into_verified"] is True


def test_repair_archived_deployment_state_continued_into_bare_slug_not_found(tmp_path):
    """A path-shaped, extensionless continued_into reference with no real
    successor anywhere under archive/handoffs/** stays unresolved — the
    bare-slug fallback must not manufacture a false positive."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-01_test-continued-bare-absent.md")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: bare-slug successor reference absent",
            "deployment_state": "continued",
            "continued_into": "archive/handoffs/totally-absent-bare-slug",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert "continued_into" in result["error"]
    assert "override" in result["error"]


def test_repair_archived_deployment_state_continued_into_deleted_successor_override(tmp_path):
    """(z4) The genuinely-deleted-successor case: a successor that once
    existed but was removed (e.g. by a distill sweep) cannot resolve via
    filesystem search — this verb never falls back to git-history resolution
    of its own (see _resolve_continued_into's own negative-spec). The caller
    who recovered the real id from git history asserts it explicitly via
    continued_into_override=True; the write proceeds and
    continued_into_verified reports False so the record is honest about not
    having been independently verified by this call."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-01_test-continued-deleted.md")
    # Deliberately do NOT seed any file with this handoff_id — simulates a
    # successor deleted by a prior distill sweep, id recovered from git log.

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": (
                "git-history-recovered: successor deleted by distill sweep "
                "b17b69bd; id recovered from git show df2772d0"
            ),
            "deployment_state": "continued",
            "continued_into": "hnd-deleted-successor-recovered-abc999",
            "continued_into_override": True,
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["continued_into_verified"] is False
    text = hpath.read_text(encoding="utf-8")
    assert "continued_into: hnd-deleted-successor-recovered-abc999" in text


def test_repair_archived_deployment_state_continued_into_override_without_continued_rejected(tmp_path):
    """(z5) continued_into_override is meaningless (and rejected) for any
    target state other than 'continued' — same discipline as
    continued_into itself."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-01_test-override-mismatch.md")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: override on wrong state",
            "deployment_state": "shipped",
            "continued_into_override": True,
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result


def test_repair_archived_deployment_state_closed_without_closed_reason_rejected(tmp_path):
    """(r) closed without closed_reason is rejected — same cross-field discipline
    as continued/continued_into."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-01_test-closed-bad.md")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: missing closed_reason",
            "deployment_state": "closed",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert "closed_reason" in result["error"]


def test_repair_archived_deployment_state_closed_with_valid_reason(tmp_path):
    """(s) closed + a valid closed_reason succeeds."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-01_test-closed-ok.md")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: deliberate stop",
            "deployment_state": "closed",
            "closed_reason": "stale",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    text = hpath.read_text(encoding="utf-8")
    assert "deployment_state: closed" in text
    assert "closed_reason: stale" in text


def test_repair_archived_deployment_state_invalid_closed_reason_rejected(tmp_path):
    """(t) closed_reason must be one of cancelled|displaced|stale."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-01_test-closed-invalid.md")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: bogus reason",
            "deployment_state": "closed",
            "closed_reason": "bogus",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result


def test_repair_archived_deployment_state_continued_into_rejected_for_other_state(tmp_path):
    """(u) continued_into supplied but target state isn't 'continued' — rejected
    rather than silently written to a state it does not apply to."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-01_test-mismatched.md")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: mismatched param",
            "deployment_state": "shipped",
            "continued_into": "hnd-should-not-apply",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result


def test_repair_archived_deployment_state_unknown_state_rejected(tmp_path):
    """(v) unknown deployment_state value is a usage error, never silently written."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-01_test-unknown.md")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: bogus state",
            "deployment_state": "definitely_not_a_real_state",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result


def test_repair_archived_deployment_state_missing_reason_rejected(tmp_path):
    """(w) reason is required on every call — sha-repair or unset alike (the sibling
    shipped_in verb's own discipline, mirrored here)."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-01_test-noreason.md")

    result = _run(_repair_archived_deployment_state_handler(
        {"handoff_path": str(hpath), "deployment_state": "shipped"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result


def test_repair_archived_deployment_state_rejects_state_handoffs_path(tmp_path):
    """(x) This verb reaches ONLY archive/handoffs/ — a state/handoffs/ path
    (or any other) must be rejected, mirroring _repair_archived_shipped_in_handler's
    own escape guard."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-07-01_test-live.md", extra_fm="deployment_state: in_flight")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: wrong root",
            "deployment_state": "shipped",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert "archive/handoffs" in result["error"]


def test_repair_archived_deployment_state_idempotent_noop(tmp_path):
    """(y) requested state already holds, on a NON-TERMINAL current state ->
    applied=False, byte-identical no-op. (Restating an already-TERMINAL state
    is covered separately by test_repair_archived_deployment_state_refuses_
    when_current_state_is_terminal below — that case now refuses rather than
    no-opping; see the E1 terminal-precondition addition.)"""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(
        repo, "2026-07-01_test-idem.md", deployment_state="in_flight"
    )
    original = hpath.read_text(encoding="utf-8")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: already in_flight",
            "deployment_state": "in_flight",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# E1 — enforced precondition: refuse unless the record's CURRENT on-disk
# deployment_state is non-terminal (awaiting_gate/ready_to_fire/in_flight) or
# absent. Terminal (shipped/continued/closed) means the record was already
# validly archived; mutating it is out of this door's reach by design. This
# is what makes the transition self-extinguishing — see
# _TERMINAL_DEPLOYMENT_STATES's module-level docstring.
# ---------------------------------------------------------------------------


def test_repair_archived_deployment_state_refuses_when_current_state_is_terminal(tmp_path):
    """A record already sitting at a terminal deployment_state (shipped here)
    is validly archived — refuse the repair even though the requested target
    state is identical to the current one (an already-terminal record must
    not silently no-op; it must refuse)."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(
        repo, "2026-07-01_test-terminal-refuse.md", deployment_state="shipped"
    )
    original = hpath.read_text(encoding="utf-8")

    result = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: already shipped, must still refuse",
            "deployment_state": "shipped",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert "terminal" in result["error"]
    assert hpath.read_text(encoding="utf-8") == original


def test_repair_archived_deployment_state_refuses_second_call_after_repair(tmp_path):
    """Self-extinguishing property: the FIRST call on a non-terminal record
    (in_flight -> shipped) succeeds; the SECOND call on that same
    now-repaired record — regardless of what target state it names — refuses,
    because the record has left this door's reachable set for good."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(
        repo, "2026-07-01_test-self-extinguish.md", deployment_state="in_flight"
    )

    first = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: first repair, in_flight -> shipped",
            "deployment_state": "shipped",
        },
        repo_root=repo / ".git",
    ))
    assert first["exit_code"] == 0, first
    assert first["applied"] is True
    assert first["new_state"] == "shipped"

    second = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: second repair attempt must refuse",
            "deployment_state": "shipped",
        },
        repo_root=repo / ".git",
    ))
    assert second["exit_code"] == 1, second
    assert "terminal" in second["error"]

    third = _run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": str(hpath),
            "reason": "test: third repair attempt (different target) must also refuse",
            "deployment_state": "closed",
            "closed_reason": "stale",
        },
        repo_root=repo / ".git",
    ))
    assert third["exit_code"] == 1, third
    assert "terminal" in third["error"]

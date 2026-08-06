"""
coordinator_core.ops.tests.test_handoff_ship_archive

Tests for the handoff.ship_and_archive composite op (event-driven single-handoff
ship-then-archive).

Import guard: coordinator_core.ops.handoff_ship_archive MUST be imported at module
load time to fire @register_op("handoff.ship_and_archive") and populate _REGISTRY —
otherwise a registry-completeness assertion passes vacuously over an empty entry
(lesson: state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml).

Coverage:
  (a) op registered
  (b) happy path — shipped_in already present → deployment_state:shipped stamped,
      handoff git-mv'd to archive/handoffs/YYYY-MM/, source gone, exit_code:0
  (c) sha param stamps shipped_in when absent, then archives (shipped_in_stamped=True)
  (d) graceful skip — no shipped_in and no sha → deployment_state:shipped IS stamped
      (Bug-2 marking gap closed) but archival is skipped, source retained, exit_code:0
  (e) idempotent replay — second call after archive → already-archived terminal result
  (f) containment — path outside state/handoffs/ → exit_code:1, no mutation
  (g) missing handoff_path / repo_root → exit_code:1
  (h) idempotent-replay basename collision — a DIFFERENT handoff sharing the same
      basename lives under archive/handoffs/<other-month>/ with a non-matching
      shipped_in; the op must NOT falsely report archived:True (Finding 1 regression)
  (i) ship-transition mid-flow failure — _ship returns exit_code:1; composite must
      propagate exit_code:1, shipped:False, with shipped_in_stamped carried over
  (j) archival per-item failure → exit_code:2 — archive_and_commit reports a non-empty
      failed list; composite must report exit_code:2, archived:False

Spec backlink: cross-repo/archive/2026-07-12-example-retrieval-repo-em-handoff-archival-mtime-veto-and-marking-gap.md
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

# ---------------------------------------------------------------------------
# Import guard — fires @register_op("handoff.ship_and_archive") as a side-effect.
# MUST precede any test function so the registry is populated before assertions.
# ---------------------------------------------------------------------------
import coordinator_core.ops.handoff_ship_archive  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_ship_archive import _handler
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter

_OP_NAME = "handoff.ship_and_archive"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_ship_archive @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio dependency needed."""
    return asyncio.run(coro)


def _head_sha(repo) -> str:
    """Return HEAD commit SHA from the repo — guaranteed git-reachable."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo.root),
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


def _deployment_state(repo, name: str) -> str:
    """Read deployment_state from the on-disk handoff (state/handoffs/<name>)."""
    text = repo.read_text(name)
    split = split_frontmatter(text)
    assert split is not None
    return read_fm_field(split.fm_text, "deployment_state")


def _archive_glob(repo, name: str):
    """Return list of archive/handoffs/**/<name> paths that exist on disk."""
    return [p for p in (repo.root / "archive" / "handoffs").rglob("*.md") if p.name == name]


# ---------------------------------------------------------------------------
# (a) registration
# ---------------------------------------------------------------------------


def test_op_registered():
    """handoff.ship_and_archive must be in the registry after the import guard fires."""
    assert _OP_NAME in _REGISTRY, (
        f"{_OP_NAME!r} must be registered; present ops: {sorted(_REGISTRY)}"
    )


# ---------------------------------------------------------------------------
# (b) happy path — shipped_in already present
# ---------------------------------------------------------------------------


def test_ship_and_archive_happy_path(handoff_repo):
    name = "2026-07-12-ship-me.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")
    sha = _head_sha(handoff_repo)
    # Stamp a real, reachable shipped_in via a second seed rewrite would churn; instead
    # re-seed with shipped_in set to the reachable HEAD sha.
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="in_flight", shipped_in=sha,
        shipped_in_kind="ship-commit",
    )

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["shipped"] is True
    assert result["archived"] is True
    assert result["archive_skip_reason"] is None
    # Source gone from state/handoffs, present in archive/handoffs/YYYY-MM/.
    assert not (handoff_repo.root / "state" / "handoffs" / name).exists()
    assert len(_archive_glob(handoff_repo, name)) == 1


# ---------------------------------------------------------------------------
# (c) sha param stamps shipped_in when absent, then archives
# ---------------------------------------------------------------------------


def test_sha_param_stamps_then_archives(handoff_repo):
    name = "2026-07-12-stamp-then-ship.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")
    sha = _head_sha(handoff_repo)

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "sha": sha},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["shipped_in_stamped"] is True
    assert result["shipped"] is True
    assert result["archived"] is True
    assert len(_archive_glob(handoff_repo, name)) == 1

    archived = _archive_glob(handoff_repo, name)[0]
    split = split_frontmatter(archived.read_text(encoding="utf-8"))
    assert read_fm_field(split.fm_text, "shipped_in_kind") == "ship-commit"


def test_kind_param_explicit_successor_is_stamped(handoff_repo):
    """DR-096: an explicit `kind` override (e.g. the pinned-ancestor-chain
    reconciler's gate (a)) is stamped verbatim, not silently coerced to the
    ship-commit default."""
    name = "2026-07-26-kind-successor.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")
    sha = _head_sha(handoff_repo)

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "sha": sha, "kind": "successor"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    archived = _archive_glob(handoff_repo, name)[0]
    split = split_frontmatter(archived.read_text(encoding="utf-8"))
    assert read_fm_field(split.fm_text, "shipped_in_kind") == "successor"


def test_kind_param_unknown_value_rejected(handoff_repo):
    name = "2026-07-26-kind-bogus.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")
    sha = _head_sha(handoff_repo)

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "sha": sha, "kind": "bogus"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert "kind" in result["error"].lower()
    assert len(_archive_glob(handoff_repo, name)) == 0


# ---------------------------------------------------------------------------
# (d) graceful skip — deployment_state:shipped stamped, archival deferred
# ---------------------------------------------------------------------------


def test_graceful_skip_marks_terminal_without_shipped_in(handoff_repo):
    name = "2026-07-12-no-sha.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["shipped"] is True
    assert result["archived"] is False
    assert result["archive_skip_reason"] is not None
    assert "shipped_in" in result["archive_skip_reason"].lower()
    # Bug-2 marking gap closed: deployment_state:shipped IS on disk even without archival.
    assert _deployment_state(handoff_repo, name) == "shipped"
    # File retained in state/handoffs for a later archival once shipped_in lands.
    assert (handoff_repo.root / "state" / "handoffs" / name).exists()


# ---------------------------------------------------------------------------
# (e) idempotent replay
# ---------------------------------------------------------------------------


def test_idempotent_replay_after_archive(handoff_repo):
    # Review: code-reviewer F1 — the replay call must supply the same sha it shipped
    # with so the F1 identity check (matched candidate's shipped_in == supplied sha)
    # can positively identify the archived candidate as this handoff's replay.
    name = "2026-07-12-replay.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")
    sha = _head_sha(handoff_repo)
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="in_flight", shipped_in=sha,
        shipped_in_kind="ship-commit",
    )

    first = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}"}, handoff_repo.common_dir
    ))
    assert first["archived"] is True

    second = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "sha": sha}, handoff_repo.common_dir
    ))
    assert second["exit_code"] == 0, second
    assert second["shipped"] is True
    assert second["archived"] is True
    assert "idempotent" in second["message"].lower()


@pytest.mark.parametrize("sha8", ["44379324", "23814e50"])
def test_idempotent_replay_matches_numeric_looking_sha(handoff_repo, sha8):
    """Replay detection must survive a sha8 the YAML writer had to quote.

    handoff_stamp writes shipped_in with numeric_quoting=True, so an all-digit
    ('44379324') or YAML-1.1-scientific-notation ('23814e50') sha8 lands
    single-quoted on disk. Before the read_fm_field_unquoted fix, the replay
    check compared "'44379324'" against the bare sha, never matched, and
    returned a hard "handoff not found on disk" error for a legitimate
    idempotent replay — on ~13% of commits.

    Seeds the archived record directly (rather than driving a real stamp) so the
    quoted shape is pinned deterministically rather than depending on whichever
    sha8 git happens to produce.

    Regression cover for state/bug-backlog/2026-07-20-read-fm-field-does-not-
    unquote-write-read-asymmetry.yaml.
    """
    name = f"2026-07-12-numeric-replay-{sha8}.md"

    archive_dir = handoff_repo.root / "archive" / "handoffs" / "2026-07"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived = archive_dir / name
    archived.write_text(
        "---\n"
        'title: "Numeric Replay"\n'
        "created: 2026-07-12\n"
        "branch: work/test/2026-07-12\n"
        "status: shipped\n"
        'predecessor: "none"\n'
        "deployment_state: shipped\n"
        # Exactly what serialize_yaml_scalar(numeric_quoting=True) emits.
        f"shipped_in: '{sha8}'\n"
        "---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )

    # Precondition: the raw reader really does return the quotes — otherwise the
    # test would pass vacuously against a value that never needed unquoting.
    split = split_frontmatter(archived.read_text(encoding="utf-8"))
    assert read_fm_field(split.fm_text, "shipped_in") == f"'{sha8}'"

    # No live file under state/handoffs/<name> — the archived-replay path.
    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "sha": sha8},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["shipped"] is True
    assert result["archived"] is True
    assert "idempotent" in result["message"].lower()


def test_idempotent_replay_numeric_sha_still_rejects_mismatch(handoff_repo):
    """The unquoting reader must not weaken the identity check into a loose match.

    A quoted archived shipped_in that does NOT equal the supplied sha must still
    fall through to the not-found error.
    """
    name = "2026-07-12-numeric-mismatch.md"
    archive_dir = handoff_repo.root / "archive" / "handoffs" / "2026-07"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / name).write_text(
        "---\n"
        'title: "Numeric Mismatch"\n'
        "created: 2026-07-12\n"
        "branch: work/test/2026-07-12\n"
        "status: shipped\n"
        'predecessor: "none"\n'
        "deployment_state: shipped\n"
        "shipped_in: '44379324'\n"
        "---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "sha": "99999999"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["archived"] is False
    assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# (f) containment
# ---------------------------------------------------------------------------


def test_path_outside_state_handoffs_rejected(handoff_repo):
    result = _run(_handler(
        {"handoff_path": "docs/plans/not-a-handoff.md"}, handoff_repo.common_dir
    ))
    assert result["exit_code"] == 1
    assert result["shipped"] is False
    assert result["archived"] is False


# ---------------------------------------------------------------------------
# (g) missing params
# ---------------------------------------------------------------------------


def test_missing_handoff_path(handoff_repo):
    result = _run(_handler({}, handoff_repo.common_dir))
    assert result["exit_code"] == 1


def test_missing_repo_root(handoff_repo):
    result = _run(_handler({"handoff_path": "state/handoffs/x.md"}, None))
    assert result["exit_code"] == 1


# ---------------------------------------------------------------------------
# (h) idempotent-replay basename collision (Finding 1 regression test)
# ---------------------------------------------------------------------------


def test_idempotent_replay_rejects_basename_collision(handoff_repo):
    """A DIFFERENT handoff sharing the same basename under a different archive
    month, with a non-matching shipped_in, must NOT be accepted as this call's
    replay match — the op must fall through to the not-found error rather than
    falsely report archived:True for a handoff it never touched.
    """
    name = "2026-07-12-collision.md"

    # Seed an unrelated archived handoff with the SAME basename but a DIFFERENT
    # shipped_in, filed under a different (stale) month directory.
    archive_dir = handoff_repo.root / "archive" / "handoffs" / "2026-01"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stale_content = (
        "---\n"
        'title: "Stale Collision"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: shipped\n"
        'predecessor: "none"\n'
        "deployment_state: shipped\n"
        "shipped_in: 0000000000000000000000000000000000dead\n"
        "---\n\n# Handoff\n\nBody.\n"
    )
    (archive_dir / name).write_text(stale_content, encoding="utf-8")

    # No live file under state/handoffs/<name> — simulates a typo'd/deleted path.
    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "sha": "1111111111111111111111111111111111beef"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["archived"] is False
    assert result["shipped"] is False
    assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# (i) ship-transition mid-flow failure
# ---------------------------------------------------------------------------


def test_ship_transition_failure_propagates(handoff_repo, monkeypatch):
    """When _ship fails mid-flow (exit_code != 0), the composite must propagate
    exit_code:1, shipped:False, and carry over the correct shipped_in_stamped
    value from Step 1.
    """
    import coordinator_core.ops.handoff_ship_archive as hsa

    name = "2026-07-12-ship-fails.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")
    sha = _head_sha(handoff_repo)

    def _boom_ship(handoff_path, worktree, repo_root):
        return {"exit_code": 1, "error": "simulated ship failure"}

    monkeypatch.setattr(hsa, "_ship", _boom_ship)

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "sha": sha},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["shipped"] is False
    # Step 1 (stamp) ran and succeeded before the simulated Step 2 failure.
    assert result["shipped_in_stamped"] is True
    assert result["archived"] is False
    assert "ship transition failed" in result["error"].lower()


# ---------------------------------------------------------------------------
# (j) archival per-item failure -> exit_code 2
# ---------------------------------------------------------------------------


def test_archival_per_item_failure_returns_exit_code_2(handoff_repo, monkeypatch):
    """When the archival act path reports a per-item FAILURE (not a graceful
    skip), the composite must report exit_code:2, archived:False, and the
    'archival failed' message shape.
    """
    import coordinator_core.ops.fleet.archive_shipped_handoffs as archive_mod

    name = "2026-07-12-archive-fails.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")
    sha = _head_sha(handoff_repo)
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="in_flight", shipped_in=sha,
        shipped_in_kind="ship-commit",
    )

    async def _boom_archive_and_commit(worktree_root, moves, subject):
        failed = [{"id": m.candidate_id, "reason": "simulated git-mv failure"} for m in moves]
        return [], failed

    monkeypatch.setattr(archive_mod, "archive_and_commit", _boom_archive_and_commit)

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 2, result
    assert result["archived"] is False
    assert result["shipped"] is True
    assert "archival failed" in result["message"].lower()

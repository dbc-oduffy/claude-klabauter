"""
coordinator_core.ops.tests.test_handoff_correct_body

Tests for the handoff.correct_body op.

Import guard: coordinator_core.ops.handoff_correct_body MUST be imported at
module load time so @register_op("handoff.correct_body") fires and populates
_REGISTRY — mirrors coordinator_core/ops/tests/test_handoff_stamp.py's own
import-guard convention.

Coverage:
  (a) registry assertion — op name present in _REGISTRY after import
  (b) happy path — single-occurrence body correction on a claimed handoff
      whose authoring_session matches; frontmatter byte-identical after
  (c) legacy status: consumed accepted on the same path
  (d) refuses when authoring_session is absent (AC4)
  (e) refuses when authoring_session is present but not UUID-shaped (free
      text or path-shaped), or matches a sentinel (AC4)
  (f) refuses when authoring_session != calling session
  (f2) DR-247 §3a / DR-264 site-2 (chunk C8,
      docs/plans/2026-08-05-roadmap-graph-enforcement-gap.md): a
      roadmap-workflow-shaped authoring_session (state/roadmap/<id>/...) is
      authorized without the sentinel/malformed or equality checks; a
      UUID-shaped mismatch and every other malformed/sentinel shape still
      refuse exactly as before the amendment
  (g) explicitly-set COORDINATOR_SESSION_ID both passes the gate AND
      appears in the stamp, naming which var resolved it
  (h) refuses when status is open
  (i) refuses an archive/handoffs/ target
  (j) refuses when old_string occurs zero times, and when it occurs twice
  (k) refuses when old_string is empty or whitespace-only
  (l) refuses when new_string == old_string (no-op), without writing or
      stamping
  (m) refuses when the replacement spans the frontmatter delimiter
  (n) refuses when new_string adds any heading line (# through ######),
      not just "## ", or a new "---" line (AC3)
  (o) refuses when net growth exceeds the 512-byte cap (AC3)
  (p) refuses when new_string contains the correction-note marker (AC5)
  (q) stamps a visible, dated correction note on success naming the
      resolved session id and the env var that resolved it, under the
      canonical section (AC5)
  (r) a second correction on the same handoff appends to the existing
      canonical section rather than adding a new heading (AC5 accumulation)
  (s) end-to-end dispatch_message smoke through the real registered wiring,
      plus a registration-surface presence check (classification/key-scope)
  (t) writes nothing on every refusal path (TestOpWritesNothing)
  (u) refuses old_string spanning the whole body, over the 1024-char cap, or
      over half the body's length (Finding 1, code-reviewer 2026-07-31)
  (v) refuses non-string old_string/new_string rather than coercing (Finding 2)
  (w) net-growth cap is measured in UTF-8 bytes, not code points (Finding 3)

Spec backlink: coordinator_core/ops/handoff_correct_body.py
               docs/plans/2026-07-31-claimed-baton-body-correction-route.md, chunk C3
"""

from __future__ import annotations

import asyncio
import subprocess
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.handoff_correct_body  # noqa: F401 — fires @register_op
import coordinator_core.ops.handoff_discharge_criteria  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_correct_body import (
    _CORRECTION_MARKER_PREFIX,
    _CORRECTION_SECTION_HEADING,
    _DELIM_LINE_RE,
    _HEADING_LINE_RE,
    _handler,
    _is_roadmap_workflow_authoring_session,
)
from coordinator_core.ops.handoff_discharge_criteria import (
    _handler as _discharge_handler,
)

_OP_NAME = "handoff.correct_body"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_correct_body @register_op did not fire"
)

_DISCHARGE_OP_NAME = "handoff.discharge_criteria"
assert _DISCHARGE_OP_NAME in _REGISTRY, (
    f"import guard failed: {_DISCHARGE_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_discharge_criteria @register_op did not fire"
)

# HEAD-before/after and porcelain-status checks (lines ~2510-2530) confirm the
# op writes nothing on every refusal path against a REAL working tree — a
# mocked git would only prove the mock stayed uncalled, not that no bytes
# landed on disk.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_AUTHOR_SESSION = "9220b333-be7f-4707-b5f2-9ebedf3e28ce"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo and return its root.

    Mirrors test_handoff_stamp.py's own `_make_git_repo` exactly — a
    state/handoffs/ skeleton, committed. Returns repo_root (the main worktree
    root, NOT the .git dir); pass repo_root / ".git" as `repo_root` to the
    handler (P9 worktree derivation).
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
    _git("config", "user.email", "correct-body-test@claude-klabauter.test")
    _git("config", "user.name", "Correct Body Test")
    _git("config", "commit.gpgsign", "false")

    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return repo


def _seed_claimed_handoff(
    repo: Path,
    name: str,
    *,
    status: str = "claimed",
    authoring_session: "str | None" = _AUTHOR_SESSION,
    claimed_by: "str | None" = None,
    body: str = "\n# Handoff body.\n\nThe count was 29.\n",
    extra_fm: str = "",
) -> Path:
    """Write a state/handoffs/<name>.md with minimal YAML frontmatter and a
    body. `authoring_session=None` omits the field entirely.
    `claimed_by=None` omits the field; `claimed_by=""` writes an explicit
    empty value (C3 AC2 fall-through-to-author-arm coverage)."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = [
        'title: "Test Handoff"',
        f"status: {status}",
        "claimed_at: 2026-07-05T12:00:00Z",
    ]
    if authoring_session is not None:
        fm_lines.append(f"authoring_session: {authoring_session}")
    if claimed_by is not None:
        fm_lines.append(f"claimed_by: {claimed_by}")
    if extra_fm.strip():
        fm_lines.append(extra_fm.strip())
    fm_text = "\n".join(fm_lines)
    content = f"---\n{fm_text}\n---{body}"
    path.write_text(content, encoding="utf-8")
    return path


def _seed_archived_handoff(
    repo: Path,
    name: str,
    *,
    status: str = "consumed",
    authoring_session: "str | None" = _AUTHOR_SESSION,
    claimed_by: "str | None" = None,
    deployment_state: "str | None" = None,
    month: str = "2026-07",
    body: str = "\n# Archived Handoff.\n\nSome prose precedes it. Body. Some prose follows it.\n",
) -> Path:
    path = repo / "archive" / "handoffs" / month / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = [
        'title: "Archived Handoff"',
        f"status: {status}",
    ]
    if authoring_session is not None:
        fm_lines.append(f"authoring_session: {authoring_session}")
    if claimed_by is not None:
        fm_lines.append(f"claimed_by: {claimed_by}")
    if deployment_state is not None:
        fm_lines.append(f"deployment_state: {deployment_state}")
    fm_text = "\n".join(fm_lines)
    content = f"---\n{fm_text}\n---{body}"
    path.write_text(content, encoding="utf-8")
    return path


def _write_claim_dir(repo: Path, class_: str, basename: str, holder_session_id: str) -> Path:
    """Create `.git/coordinator-sessions/<class>-claims/<basename>/session_id`
    naming `holder_session_id` as the recorded claim holder — the same
    stat `pickup_assemble._claim_already_self_held` reads for
    disagreement-detection (AC2/AC4, C3)."""
    d = repo / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename
    d.mkdir(parents=True, exist_ok=True)
    (d / "session_id").write_text(holder_session_id, encoding="utf-8")
    return d


_HOLDER_SESSION = "b1111111-2222-3333-4444-555555555555"
_OTHER_SESSION = "c1111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _clear_session_env(monkeypatch):
    """Ensure a clean, known session-id env slate for every test — a test that
    needs a resolvable calling session sets COORDINATOR_SESSION_ID itself."""
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _set_calling_session(monkeypatch, value: str = _AUTHOR_SESSION):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", value)


# ---------------------------------------------------------------------------
# (a) Registry assertion
# ---------------------------------------------------------------------------


def test_op_registered():
    assert _OP_NAME in _REGISTRY


# ---------------------------------------------------------------------------
# (b) Happy path — single-occurrence correction, matching authoring_session
# ---------------------------------------------------------------------------


def test_applies_single_occurrence_correction_frontmatter_byte_identical(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-test.md")
    original = hpath.read_text(encoding="utf-8")
    fm_before = original.split("---", 2)[1]
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    after = hpath.read_text(encoding="utf-8")
    fm_after = after.split("---", 2)[1]
    assert fm_after == fm_before, "frontmatter must be byte-identical after a body correction"
    assert "The count was 25." in after
    assert "The count was 29." not in after


# ---------------------------------------------------------------------------
# (c) legacy status: consumed accepted on the same path
# ---------------------------------------------------------------------------


def test_legacy_consumed_status_accepted(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-consumed.md", status="consumed")
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert "The count was 25." in hpath.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (d) refuses when authoring_session is absent
# ---------------------------------------------------------------------------


def test_refuses_when_authoring_session_absent(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-no-author.md", authoring_session=None)
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "authoring_session" in result["error"]


# ---------------------------------------------------------------------------
# (e) refuses when authoring_session is non-UUID-shaped or a sentinel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    [
        "free text session id",
        "state/handoffs/some-file.md",
        "PLACEHOLDER",
        "em-unknown",
        "",
        "   ",
    ],
)
def test_refuses_when_authoring_session_malformed_or_sentinel(tmp_path, monkeypatch, bad_value):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-bad-author.md", authoring_session=bad_value)
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False


# ---------------------------------------------------------------------------
# (f) refuses when authoring_session != calling session
# ---------------------------------------------------------------------------


def test_refuses_when_authoring_session_mismatches_caller(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-mismatch.md")
    _set_calling_session(monkeypatch, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "authoring_session" in result["error"] or "match" in result["error"]


# ---------------------------------------------------------------------------
# (f2) DR-247 §3a / DR-264 site-2 (plan chunk C8): a roadmap-workflow-shaped
# authoring_session (`state/roadmap/<id>/...`) is authorized WITHOUT the
# sentinel/malformed check or the equality check — no live session id can
# ever equal a path. A UUID-shaped mismatch and a sentinel/malformed value
# both still refuse exactly as before this amendment.
# ---------------------------------------------------------------------------


def test_roadmap_workflow_shaped_authoring_session_is_authorized(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        "2026-08-05-roadmap-baton.md",
        authoring_session="state/roadmap/claude-klabauter-strangler-2026-07-04/",
    )
    original = hpath.read_text(encoding="utf-8")
    fm_before = original.split("---", 2)[1]
    # No calling-session env var set at all -- a roadmap-shaped
    # authoring_session must still resolve a calling session for the
    # correction-note stamp, so set one that could never equal the path.
    _set_calling_session(monkeypatch, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["session_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    after = hpath.read_text(encoding="utf-8")
    fm_after = after.split("---", 2)[1]
    assert fm_after == fm_before, "frontmatter must be byte-identical after a body correction"
    assert "The count was 25." in after
    assert "The count was 29." not in after
    # The paper-trail control DR-247 §3a substitutes for the equality gate:
    # the stamped correction note still names the resolved calling session.
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in after
    assert _CORRECTION_MARKER_PREFIX in after


def test_uuid_shaped_authoring_session_mismatch_still_refused_after_amendment(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-05-uuid-mismatch.md")
    _set_calling_session(monkeypatch, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "authoring_session" in result["error"] or "match" in result["error"]
    assert "The count was 25." not in hpath.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "bad_value",
    [
        "free text session id",
        "state/handoffs/some-file.md",
        "PLACEHOLDER",
        "em-unknown",
        "state/roadmap-not-a-real-dir",
        "state/roadmap",
    ],
)
def test_sentinel_or_malformed_authoring_session_still_refused_after_amendment(
    tmp_path, monkeypatch, bad_value
):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-05-still-refused.md", authoring_session=bad_value)
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "The count was 25." not in hpath.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "bad_value",
    [
        # Traversal in the id segment itself — matched
        # `^state/roadmap/[^/]+/` before Finding 1's fix (code-reviewer,
        # 2026-08-05).
        "state/roadmap/../../etc/passwd",
        # Whitespace-only id segment — also matched the old loose regex.
        "state/roadmap/ /x",
        # Well-formed id segment, but an embedded-traversal tail — must
        # still be refused, not just a bad `<id>` (Finding 2).
        "state/roadmap/claude-klabauter-strangler-2026-07-04/../../x",
    ],
)
def test_roadmap_shaped_traversal_authoring_session_still_refused(
    tmp_path, monkeypatch, bad_value
):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo, "2026-08-05-roadmap-traversal.md", authoring_session=bad_value
    )
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "The count was 25." not in hpath.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "bad_value",
    [
        # `$` in the id allowlist matches immediately before a trailing
        # newline even without re.MULTILINE, so `.match()` accepted a
        # newline-terminated id segment. Unreachable through
        # read_fm_field_unquoted (a single physical line by construction),
        # hence a direct predicate test rather than a seeded handoff — pinned
        # so the quirk stays closed for any future caller.
        # (security-audit-worker, 2026-08-05.)
        "state/roadmap/good\n/tail",
        "state/roadmap/good\n",
    ],
)
def test_newline_terminated_roadmap_id_is_not_workflow_shaped(bad_value):
    assert _is_roadmap_workflow_authoring_session(bad_value) is False


def test_genuine_roadmap_authoring_session_is_workflow_shaped():
    assert (
        _is_roadmap_workflow_authoring_session(
            "state/roadmap/claude-klabauter-strangler-2026-07-04/"
        )
        is True
    )


# ---------------------------------------------------------------------------
# (g) COORDINATOR_SESSION_ID passes the gate AND appears in the stamp
# ---------------------------------------------------------------------------


def test_coordinator_session_id_passes_gate_and_appears_in_stamp(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-env-precedence.md")
    # Deliberately ALSO set the lower-precedence vars to a WRONG value, so a
    # pass here proves COORDINATOR_SESSION_ID specifically won, not one of
    # the others by accident.
    monkeypatch.setenv("CLAUDE_SESSION_ID", "wrong-session-should-not-be-used")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "also-wrong-should-not-be-used")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _AUTHOR_SESSION)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["session_id"] == _AUTHOR_SESSION
    assert result["session_source"] == "COORDINATOR_SESSION_ID"

    text = hpath.read_text(encoding="utf-8")
    assert _AUTHOR_SESSION in text
    assert "COORDINATOR_SESSION_ID" in text


# ---------------------------------------------------------------------------
# (h) refuses when status is open
# ---------------------------------------------------------------------------


def test_refuses_when_status_is_open(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-open.md", status="open")
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "status" in result["error"] or "claimed" in result["error"]


# ---------------------------------------------------------------------------
# (i) AC12 archive-follow — INVERTED from the pre-ruling draft (staff-eng
# re-review Finding 0 / PM ruling 2026-08-06): a target resolvable only
# under archive/handoffs/ now SUCCEEDS under identical D2(iii)-(vii) bounds,
# not refused. This test previously asserted refusal; it is edited in place
# deliberately, per C3's own instruction, rather than left contradicting the
# new tests below.
# ---------------------------------------------------------------------------


def test_archive_follow_target_succeeds_under_identical_bounds(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "old.md")
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "Body.",
            "new_string": "Fixed body.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    text = hpath.read_text(encoding="utf-8")
    assert "Fixed body." in text
    assert _CORRECTION_MARKER_PREFIX in text


def test_archive_target_absent_from_both_roots_still_refuses(tmp_path, monkeypatch):
    """The archive-follow widening is bounded to 'follow the sweep', not
    'accept any path' — a handoff_path absent from both state/handoffs/ and
    archive/handoffs/ still refuses (AC12)."""
    repo = _make_git_repo(tmp_path)
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": "state/handoffs/2026-07-31-nowhere.md",
            "old_string": "Body.",
            "new_string": "Fixed body.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "not found" in result["error"]


def test_archive_follow_resolves_live_path_to_archived_only_twin(tmp_path, monkeypatch):
    """F1 regression (staff-eng chain-review): every pre-existing
    archive-follow test above passes the ARCHIVE path itself as
    `handoff_path`, so `p.is_file()` short-circuits into the
    caller-supplied-an-already-archived-path branch and the real follow
    arm (`if not p.is_file(): archived_candidate =
    handoff_archive_dest(...)`) never runs on a success path — proven by
    the reviewer's mutation (stubbing `handoff_archive_dest` to return a
    non-existent path left 128/128 green). This test seeds ONLY the
    archived twin (no live file at all) and passes the LIVE path, so the
    follow arm is the only way this can succeed."""
    repo = _make_git_repo(tmp_path)
    name = "2026-08-06-follow-only-archived.md"
    live_path = repo / "state" / "handoffs" / name
    archived = _seed_archived_handoff(
        repo, name, month="2026-08", body="\n# Archived-only body.\n\nThe count was 29.\n"
    )
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(live_path),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert not live_path.exists(), (
        "the archive-follow arm must land the correction on the ARCHIVED "
        "file, never create a new one under state/handoffs/"
    )
    archived_text = archived.read_text(encoding="utf-8")
    assert "The count was 25." in archived_text
    assert _CORRECTION_MARKER_PREFIX in archived_text


def test_archive_follow_resolves_live_path_to_archived_only_twin_terminal_state_refuses(
    tmp_path, monkeypatch
):
    """Same follow-arm entry (live path, archived-only twin) but AC14's
    terminal-state refusal must fire through it too, not only through the
    direct-archive-path arm."""
    repo = _make_git_repo(tmp_path)
    name = "2026-08-06-follow-only-archived-terminal.md"
    live_path = repo / "state" / "handoffs" / name
    archived = _seed_archived_handoff(
        repo, name, month="2026-08", deployment_state="shipped",
        body="\n# Archived-only body.\n\nThe count was 29.\n",
    )
    original = archived.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(live_path),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "shipped" in result["error"]
    assert not live_path.exists()
    assert archived.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# (j) refuses when old_string occurs zero times / twice
# ---------------------------------------------------------------------------


def test_refuses_old_string_zero_occurrences(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-zero.md")
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "text that does not appear anywhere",
            "new_string": "replacement",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False


def test_refuses_old_string_two_occurrences(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        "2026-07-31-twice.md",
        body="\n# Body.\n\nThe count was 29. Later, The count was 29.\n",
    )
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29",
            "new_string": "The count was 25",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False


# ---------------------------------------------------------------------------
# (k) refuses when old_string is empty or whitespace-only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_old", ["", "   ", "\n\t"])
def test_refuses_empty_or_whitespace_old_string(tmp_path, monkeypatch, bad_old):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-empty-old.md")
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": bad_old,
            "new_string": "replacement",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False


# ---------------------------------------------------------------------------
# (l) refuses new_string == old_string (no-op), without writing or stamping
# ---------------------------------------------------------------------------


def test_refuses_noop_replacement_without_writing_or_stamping(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-noop.md")
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 29.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# (m) refuses replacement spanning the frontmatter delimiter
# ---------------------------------------------------------------------------


def test_refuses_replacement_crossing_frontmatter_delimiter(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-delim.md")
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "Corrected.\n---\nfake frontmatter",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# (n) refuses new_string adding any heading line, or a new "---" line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("heading_prefix", ["# ", "## ", "### ", "#### ", "##### ", "###### "])
def test_refuses_new_string_adding_any_heading_level(tmp_path, monkeypatch, heading_prefix):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-heading.md")
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": f"The count was 25.\n{heading_prefix}New Section\n",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original


def test_refuses_new_string_adding_new_delimiter_line(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-new-delim-line.md")
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.\n---\n",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# (o) refuses when net growth exceeds the 512-byte cap
# ---------------------------------------------------------------------------


def test_refuses_net_growth_exceeding_512_byte_cap(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-oversize.md")
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch)

    oversized_new = "The count was 29." + ("x" * 513)
    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": oversized_new,
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original


def test_allows_net_growth_at_exactly_512_byte_cap(tmp_path, monkeypatch):
    """The cap is inclusive — exactly 512 bytes of net growth must succeed,
    pinning the boundary the AC3 test above probes from the other side."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-at-cap.md")
    _set_calling_session(monkeypatch)

    at_cap_new = "The count was 29." + ("x" * 512)
    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": at_cap_new,
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True


# ---------------------------------------------------------------------------
# Finding 1 (code-reviewer, 2026-07-31) — old_string size/locality bounds
# ---------------------------------------------------------------------------


def test_refuses_whole_body_old_string_same_length_new_string(tmp_path, monkeypatch):
    """The exact reproduction from the finding: old_string = the entire body,
    new_string same length — must be refused, not treated as a valid
    single-occurrence correction."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-whole-body.md")
    original = hpath.read_text(encoding="utf-8")
    body = original.split("---", 2)[2]
    _set_calling_session(monkeypatch)

    same_length_rewrite = "x" * len(body)
    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": body,
            "new_string": same_length_rewrite,
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original


def test_refuses_old_string_over_1024_char_cap(tmp_path, monkeypatch):
    long_snippet = "y" * 1025
    body = f"\n# Body.\n\n{long_snippet}\n\nThe count was 29.\n"
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-over-cap.md", body=body)
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": long_snippet,
            "new_string": "z" * 1025,
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "1024" in result["error"]
    assert hpath.read_text(encoding="utf-8") == original


def test_refuses_old_string_over_half_body_ratio_below_absolute_cap(tmp_path, monkeypatch):
    """A body short enough that the 1024-char absolute cap would NOT fire,
    but old_string still exceeds half the body — proves the ratio bound earns
    its place independent of the absolute cap."""
    body = "\nAAAA BBBB CCCC\n"  # 15 chars; old_string below is > 50% of this,
    # but not the WHOLE stripped body, so the whole-body bound doesn't fire
    # first — isolating the ratio bound.
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-ratio.md", body=body)
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch)

    old_string = "AAAA BBBB"  # 9 of 15 body chars — over 50%, well under 1024
    assert len(old_string) < 1024
    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": old_string,
            "new_string": "CCCC DDDD",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "%" in result["error"]
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Finding 2 (code-reviewer, 2026-07-31) — non-string new_string/old_string
# refused rather than coerced
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [0, False, [], {}])
def test_refuses_non_string_new_string_without_writing(tmp_path, monkeypatch, bad_value):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-bad-new-type.md")
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": bad_value,
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("bad_value", [0, False, [], {}])
def test_refuses_non_string_old_string_without_writing(tmp_path, monkeypatch, bad_value):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-bad-old-type.md")
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": bad_value,
            "new_string": "replacement",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Finding 3 (code-reviewer, 2026-07-31) — net-growth cap measured in UTF-8
# bytes, not code points
# ---------------------------------------------------------------------------


def test_net_growth_cap_measured_in_utf8_bytes_not_code_points(tmp_path, monkeypatch):
    """A multi-byte replacement near the cap boundary: each added CJK
    character is 3 UTF-8 bytes but 1 code point. 171 added characters is 171
    code points (under a naive 512 code-point cap) but 513 UTF-8 bytes (over
    the 512-byte cap) — pinning that byte measurement, not code-point
    measurement, is what applies."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-utf8-boundary.md")
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch)

    added = "中" * 171  # 171 code points, 513 UTF-8 bytes
    new_string = "The count was 29." + added
    net_growth_bytes = len(new_string.encode("utf-8")) - len("The count was 29.".encode("utf-8"))
    assert net_growth_bytes == 513
    net_growth_codepoints = len(new_string) - len("The count was 29.")
    assert net_growth_codepoints == 171  # would pass a code-point-only cap

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": new_string,
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# (p) refuses new_string containing the correction-note marker
# ---------------------------------------------------------------------------


def test_refuses_new_string_containing_correction_marker(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-forged.md")
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch)

    forged = f"The count was 25. {_CORRECTION_MARKER_PREFIX} fake -->"
    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": forged,
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# (q) stamps a visible, dated correction note on success
# ---------------------------------------------------------------------------


def test_stamps_visible_dated_correction_note_under_canonical_section(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-stamp.md")
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    text = hpath.read_text(encoding="utf-8")
    assert _CORRECTION_SECTION_HEADING in text
    assert _CORRECTION_MARKER_PREFIX in text
    assert _AUTHOR_SESSION in text
    assert "COORDINATOR_SESSION_ID" in text
    # "dated" — a note carries an ISO-ish timestamp fragment (year at least).
    assert "202" in text.split(_CORRECTION_MARKER_PREFIX, 1)[1][:60]


# ---------------------------------------------------------------------------
# (r) second correction appends to existing canonical section, no new heading
# ---------------------------------------------------------------------------


def test_second_correction_appends_to_existing_section_not_new_heading(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-second-correction.md")
    _set_calling_session(monkeypatch)

    first = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))
    assert first["exit_code"] == 0, first

    second = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 25.",
            "new_string": "The count was 25 (of 32).",
        },
        repo_root=repo / ".git",
    ))
    assert second["exit_code"] == 0, second

    text = hpath.read_text(encoding="utf-8")
    heading_count = text.count(_CORRECTION_SECTION_HEADING)
    assert heading_count == 1, (
        f"canonical section heading must appear exactly once after two "
        f"corrections; found {heading_count} in:\n{text}"
    )
    marker_count = text.count(_CORRECTION_MARKER_PREFIX)
    assert marker_count == 2, (
        f"expected two correction-note lines under the one canonical section; "
        f"found {marker_count} in:\n{text}"
    )


# ---------------------------------------------------------------------------
# (s) end-to-end dispatch through the real registered wiring, plus a
# registration-surface presence check.
# ---------------------------------------------------------------------------


def test_end_to_end_dispatch_message_smoke(tmp_path, monkeypatch):
    """Invokes the registered op name through the JSON-RPC surface (real
    registered wiring — ops/__init__.py eager import + @register_op),
    asserting the exit-code envelope. Mirrors
    test_distill_stamp_disposal.py::test_handler_end_to_end_dispatch_message_smoke's
    _origin_worktree convention."""
    import coordinator_core.ipc as ipc
    import coordinator_core.ops  # noqa: F401 — triggers eager registration

    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-e2e.md")
    _set_calling_session(monkeypatch)

    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": _OP_NAME,
        "params": {
            "handoff_path": "state/handoffs/2026-07-31-e2e.md",
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        "_origin_worktree": str(repo),
    }
    response = _run(ipc.dispatch_message(msg))

    assert "result" in response, f"dispatch must succeed; got error: {response.get('error')}"
    assert response["result"]["exit_code"] == 0
    assert response["result"]["applied"] is True
    assert "The count was 25." in hpath.read_text(encoding="utf-8")


def test_op_present_in_classification_and_key_scope_registration_surfaces():
    """Fails if the op name is absent from OP_CLASSIFICATION
    (coordinator_core/authz/classification.py) or _OP_KEY_SCOPE
    (coordinator_core/op_scopes.py) — the two registration surfaces C2
    lands alongside @register_op. Deliberately does NOT check OP_MODULE_MAP
    (coordinator_core/ops/_registry_map.py): that entry lands in this same
    slice (`ops/_registry_map.py:232`), so this test is scoped to the two
    surfaces C2 itself is responsible for."""
    from coordinator_core.authz.classification import OP_CLASSIFICATION
    from coordinator_core.op_scopes import _OP_KEY_SCOPE

    assert _OP_NAME in OP_CLASSIFICATION, (
        f"{_OP_NAME!r} missing from OP_CLASSIFICATION "
        "(coordinator_core/authz/classification.py)"
    )
    assert _OP_NAME in _OP_KEY_SCOPE, (
        f"{_OP_NAME!r} missing from _OP_KEY_SCOPE (coordinator_core/op_scopes.py)"
    )


# ---------------------------------------------------------------------------
# (t) writes nothing on every refusal path
# ---------------------------------------------------------------------------


class TestOpWritesNothing:
    """Mirrors write_guards' TestGuardWritesNothing discipline — a refusing
    op that has already written is exactly the failure this pins."""

    def _snapshot(self, handoffs_dir: Path) -> dict:
        return {p.name: p.read_bytes() for p in sorted(handoffs_dir.iterdir())}

    def test_writes_nothing_on_missing_authoring_session(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path)
        _seed_claimed_handoff(repo, "2026-07-31-w1.md", authoring_session=None)
        handoffs_dir = repo / "state" / "handoffs"
        _set_calling_session(monkeypatch)

        before = self._snapshot(handoffs_dir)
        result = _run(_handler(
            {
                "handoff_path": str(handoffs_dir / "2026-07-31-w1.md"),
                "old_string": "The count was 29.",
                "new_string": "The count was 25.",
            },
            repo_root=repo / ".git",
        ))
        after = self._snapshot(handoffs_dir)

        assert result["exit_code"] == 1
        assert after == before

    def test_writes_nothing_on_session_mismatch(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path)
        _seed_claimed_handoff(repo, "2026-07-31-w2.md")
        handoffs_dir = repo / "state" / "handoffs"
        _set_calling_session(monkeypatch, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

        before = self._snapshot(handoffs_dir)
        result = _run(_handler(
            {
                "handoff_path": str(handoffs_dir / "2026-07-31-w2.md"),
                "old_string": "The count was 29.",
                "new_string": "The count was 25.",
            },
            repo_root=repo / ".git",
        ))
        after = self._snapshot(handoffs_dir)

        assert result["exit_code"] == 1
        assert after == before

    def test_writes_nothing_on_status_open(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path)
        _seed_claimed_handoff(repo, "2026-07-31-w3.md", status="open")
        handoffs_dir = repo / "state" / "handoffs"
        _set_calling_session(monkeypatch)

        before = self._snapshot(handoffs_dir)
        result = _run(_handler(
            {
                "handoff_path": str(handoffs_dir / "2026-07-31-w3.md"),
                "old_string": "The count was 29.",
                "new_string": "The count was 25.",
            },
            repo_root=repo / ".git",
        ))
        after = self._snapshot(handoffs_dir)

        assert result["exit_code"] == 1
        assert after == before

    def test_writes_nothing_on_zero_occurrences(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path)
        _seed_claimed_handoff(repo, "2026-07-31-w4.md")
        handoffs_dir = repo / "state" / "handoffs"
        _set_calling_session(monkeypatch)

        before = self._snapshot(handoffs_dir)
        result = _run(_handler(
            {
                "handoff_path": str(handoffs_dir / "2026-07-31-w4.md"),
                "old_string": "not present anywhere",
                "new_string": "replacement",
            },
            repo_root=repo / ".git",
        ))
        after = self._snapshot(handoffs_dir)

        assert result["exit_code"] == 1
        assert after == before

    def test_writes_nothing_on_noop_replacement(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path)
        _seed_claimed_handoff(repo, "2026-07-31-w5.md")
        handoffs_dir = repo / "state" / "handoffs"
        _set_calling_session(monkeypatch)

        before = self._snapshot(handoffs_dir)
        result = _run(_handler(
            {
                "handoff_path": str(handoffs_dir / "2026-07-31-w5.md"),
                "old_string": "The count was 29.",
                "new_string": "The count was 29.",
            },
            repo_root=repo / ".git",
        ))
        after = self._snapshot(handoffs_dir)

        assert result["exit_code"] == 1
        assert after == before

    def test_writes_nothing_on_heading_injection(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path)
        _seed_claimed_handoff(repo, "2026-07-31-w6.md")
        handoffs_dir = repo / "state" / "handoffs"
        _set_calling_session(monkeypatch)

        before = self._snapshot(handoffs_dir)
        result = _run(_handler(
            {
                "handoff_path": str(handoffs_dir / "2026-07-31-w6.md"),
                "old_string": "The count was 29.",
                "new_string": "The count was 25.\n## New Heading\n",
            },
            repo_root=repo / ".git",
        ))
        after = self._snapshot(handoffs_dir)

        assert result["exit_code"] == 1
        assert after == before

    def test_writes_nothing_on_oversized_growth(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path)
        _seed_claimed_handoff(repo, "2026-07-31-w7.md")
        handoffs_dir = repo / "state" / "handoffs"
        _set_calling_session(monkeypatch)

        before = self._snapshot(handoffs_dir)
        result = _run(_handler(
            {
                "handoff_path": str(handoffs_dir / "2026-07-31-w7.md"),
                "old_string": "The count was 29.",
                "new_string": "The count was 29." + ("x" * 513),
            },
            repo_root=repo / ".git",
        ))
        after = self._snapshot(handoffs_dir)

        assert result["exit_code"] == 1
        assert after == before

    def test_writes_nothing_on_marker_forgery(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path)
        _seed_claimed_handoff(repo, "2026-07-31-w8.md")
        handoffs_dir = repo / "state" / "handoffs"
        _set_calling_session(monkeypatch)

        before = self._snapshot(handoffs_dir)
        result = _run(_handler(
            {
                "handoff_path": str(handoffs_dir / "2026-07-31-w8.md"),
                "old_string": "The count was 29.",
                "new_string": f"The count was 25. {_CORRECTION_MARKER_PREFIX} fake -->",
            },
            repo_root=repo / ".git",
        ))
        after = self._snapshot(handoffs_dir)

        assert result["exit_code"] == 1
        assert after == before

    def test_writes_nothing_on_whole_body_old_string(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path)
        hpath = _seed_claimed_handoff(repo, "2026-07-31-w10.md")
        body = hpath.read_text(encoding="utf-8").split("---", 2)[2]
        handoffs_dir = repo / "state" / "handoffs"
        _set_calling_session(monkeypatch)

        before = self._snapshot(handoffs_dir)
        result = _run(_handler(
            {
                "handoff_path": str(handoffs_dir / "2026-07-31-w10.md"),
                "old_string": body,
                "new_string": "x" * len(body),
            },
            repo_root=repo / ".git",
        ))
        after = self._snapshot(handoffs_dir)

        assert result["exit_code"] == 1
        assert after == before

    def test_writes_nothing_on_old_string_over_cap(self, tmp_path, monkeypatch):
        long_snippet = "y" * 1025
        body = f"\n# Body.\n\n{long_snippet}\n\nThe count was 29.\n"
        repo = _make_git_repo(tmp_path)
        _seed_claimed_handoff(repo, "2026-07-31-w11.md", body=body)
        handoffs_dir = repo / "state" / "handoffs"
        _set_calling_session(monkeypatch)

        before = self._snapshot(handoffs_dir)
        result = _run(_handler(
            {
                "handoff_path": str(handoffs_dir / "2026-07-31-w11.md"),
                "old_string": long_snippet,
                "new_string": "z" * 1025,
            },
            repo_root=repo / ".git",
        ))
        after = self._snapshot(handoffs_dir)

        assert result["exit_code"] == 1
        assert after == before

    def test_writes_nothing_on_non_string_new_string(self, tmp_path, monkeypatch):
        repo = _make_git_repo(tmp_path)
        _seed_claimed_handoff(repo, "2026-07-31-w12.md")
        handoffs_dir = repo / "state" / "handoffs"
        _set_calling_session(monkeypatch)

        before = self._snapshot(handoffs_dir)
        result = _run(_handler(
            {
                "handoff_path": str(handoffs_dir / "2026-07-31-w12.md"),
                "old_string": "The count was 29.",
                "new_string": 0,
            },
            repo_root=repo / ".git",
        ))
        after = self._snapshot(handoffs_dir)

        assert result["exit_code"] == 1
        assert after == before

    def test_writes_nothing_on_terminal_archived_deployment_state(self, tmp_path, monkeypatch):
        """INVERTED (AC12): archive/handoffs/ is no longer refused generally
        (see test_archive_follow_target_succeeds_under_identical_bounds) —
        the writes-nothing case for the archive arm is now the AC14
        terminal-state refusal specifically, not archive-ness itself."""
        repo = _make_git_repo(tmp_path)
        _seed_archived_handoff(repo, "w9.md", deployment_state="shipped")
        archived_dir = repo / "archive" / "handoffs" / "2026-07"
        _set_calling_session(monkeypatch)

        before = self._snapshot(archived_dir)
        result = _run(_handler(
            {
                "handoff_path": str(archived_dir / "w9.md"),
                "old_string": "Body.",
                "new_string": "Fixed body.",
            },
            repo_root=repo / ".git",
        ))
        after = self._snapshot(archived_dir)

        assert result["exit_code"] == 1
        assert after == before


# ---------------------------------------------------------------------------
# Security-audit integration (2026-07-31) — Findings 1, 2, 3
# ---------------------------------------------------------------------------


def test_refuses_zero_width_space_disguised_marker(tmp_path, monkeypatch):
    """Finding 1: a U+200B ZERO WIDTH SPACE inserted mid-marker must not
    slip past the forgery guard — it renders visually identical to a
    genuine stamp and previously defeated the bare substring test."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-zwsp-forged.md")
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch)

    prefix_with_zwsp = _CORRECTION_MARKER_PREFIX.replace(":", "​:", 1)
    forged = f"The count was 25. {prefix_with_zwsp} 2020-01-01T00:00:00Z by session deadbeef -->"

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": forged,
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original


def test_refuses_new_string_with_invisible_unicode_even_without_marker(tmp_path, monkeypatch):
    """Finding 1: an invisible-character payload is refused outright, not
    only when it happens to be hiding a forged marker."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-invisible.md")
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25​.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "invisible" in result["error"].lower()
    assert hpath.read_text(encoding="utf-8") == original


def test_defanging_is_never_written_to_disk(tmp_path, monkeypatch):
    """Finding 1: an accepted new_string must land on disk byte-for-byte as
    supplied — the defanging copy used for marker detection must never be
    the persisted text."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-07-31-cafe.md")
    _set_calling_session(monkeypatch)

    # Combining-character text (NFKC-normalizable, but legitimate prose with
    # no marker hiding in it) — accepted, and must survive un-normalized.
    accepted_new_string = "The café count was 25."  # "café" as e + combining acute

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": accepted_new_string,
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    text = hpath.read_text(encoding="utf-8")
    assert accepted_new_string in text


def test_crlf_authored_frontmatter_bytes_survive_correction_unchanged(tmp_path, monkeypatch):
    """Finding 2: a CRLF-authored handoff's frontmatter bytes (including its
    original \\r\\n line endings) must survive a body correction unchanged —
    DR-247 D2(iv) requires byte-identical frontmatter, not
    identical-after-equal-normalization."""
    repo = _make_git_repo(tmp_path)
    hpath = repo / "state" / "handoffs" / "2026-07-31-crlf.md"
    hpath.parent.mkdir(parents=True, exist_ok=True)

    fm_lines_lf = (
        'title: "CRLF Test Handoff"\n'
        "status: claimed\n"
        "claimed_at: 2026-07-05T12:00:00Z\n"
        f"authoring_session: {_AUTHOR_SESSION}\n"
    )
    body_lf = "\n# Handoff body.\n\nThe count was 29.\n"
    content_lf = f"---\n{fm_lines_lf}---{body_lf}"
    # Author the file with CRLF line endings throughout (as a real
    # Windows-authored file would arrive), by writing raw bytes.
    content_crlf = content_lf.replace("\n", "\r\n")
    hpath.write_bytes(content_crlf.encode("utf-8"))

    original_bytes = hpath.read_bytes()
    original_fm_bytes = original_bytes.split(b"---", 2)[1]

    _set_calling_session(monkeypatch)
    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    after_bytes = hpath.read_bytes()
    after_fm_bytes = after_bytes.split(b"---", 2)[1]
    assert after_fm_bytes == original_fm_bytes, (
        "CRLF-authored frontmatter bytes must survive a body correction "
        "byte-for-byte, including their original \\r\\n line endings"
    )


def test_refuses_embedded_nul_byte_handoff_path_instead_of_crashing(tmp_path, monkeypatch):
    """Finding 3: an embedded-NUL handoff_path raises ValueError out of
    Path.resolve(), which contained_path only guards with `except OSError`.
    This must return the documented _err(...) refusal, not propagate an
    uncaught exception."""
    repo = _make_git_repo(tmp_path)
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": "state/handoffs/foo\x00bar.md",
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# C3 (plan docs/plans/2026-08-06-executing-session-can-discharge-criteria.md)
# — the ownership gate replacing the authorship gate (AC2/AC3a/AC4/AC13),
# archive-follow (AC12/AC14), and the surviving DR-247 D2 bounds proven
# individually (AC5).
# ---------------------------------------------------------------------------


def test_claimed_by_mutated_between_pre_lock_read_and_lock_acquisition_aborts(tmp_path, monkeypatch):
    """Proves the frontmatter-derived ownership check rides the existing
    locked_rmw byte-identity envelope: a concurrent claimed_by mutation
    between the handler's pre-lock read and the lock's own re-read aborts
    the mutate, rather than silently applying against a moving target."""
    import coordinator_core.ops.handoff_correct_body as hcb

    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-mutate-abort.md", claimed_by=_HOLDER_SESSION)
    _set_calling_session(monkeypatch, _HOLDER_SESSION)

    real_locked_rmw = hcb.locked_rmw

    def _racing_locked_rmw(target, mutate, **kwargs):
        # Simulate a concurrent frontmatter mutation landing between the
        # handler's pre-lock read (already captured) and the lock's own
        # fresh read inside real locked_rmw.
        text = target.read_text(encoding="utf-8")
        mutated = text.replace(f"claimed_by: {_HOLDER_SESSION}", f"claimed_by: {_OTHER_SESSION}")
        assert mutated != text, "test setup: claimed_by substitution must actually change the file"
        target.write_text(mutated, encoding="utf-8")
        return real_locked_rmw(target, mutate, **kwargs)

    monkeypatch.setattr(hcb, "locked_rmw", _racing_locked_rmw)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "frontmatter changed between read and lock acquisition" in result["error"]
    assert "The count was 25." not in hpath.read_text(encoding="utf-8")


def test_holder_not_author_succeeds(tmp_path, monkeypatch):
    """The single most load-bearing assertion in this plan — the exact case
    that returns a refusal on today's HEAD (AC2)."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        "2026-08-06-holder-not-author.md",
        authoring_session=_AUTHOR_SESSION,
        claimed_by=_HOLDER_SESSION,
    )
    _set_calling_session(monkeypatch, _HOLDER_SESSION)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["ownership_basis"] == "holder"
    assert "The count was 25." in hpath.read_text(encoding="utf-8")


def test_author_not_holder_still_succeeds(tmp_path, monkeypatch):
    """No regression for the case that already worked before this amendment."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        "2026-08-06-author-not-holder.md",
        authoring_session=_AUTHOR_SESSION,
        claimed_by=_HOLDER_SESSION,
    )
    _set_calling_session(monkeypatch, _AUTHOR_SESSION)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["ownership_basis"] == "author"
    assert "The count was 25." in hpath.read_text(encoding="utf-8")


def test_neither_holder_nor_author_no_override_reason_hard_refuses(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        "2026-08-06-neither-no-override.md",
        authoring_session=_AUTHOR_SESSION,
        claimed_by=_HOLDER_SESSION,
    )
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch, _OTHER_SESSION)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original
    # Both remedies named — claim it (sanctioned path) and override_reason.
    assert "claim" in result["error"].lower()
    assert "override_reason" in result["error"]


def test_neither_holder_nor_author_empty_override_reason_refuses_distinct_error(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        "2026-08-06-neither-empty-override.md",
        authoring_session=_AUTHOR_SESSION,
        claimed_by=_HOLDER_SESSION,
    )
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch, _OTHER_SESSION)

    absent_result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    empty_result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
            "override_reason": "",
        },
        repo_root=repo / ".git",
    ))

    assert absent_result["exit_code"] == 1
    assert empty_result["exit_code"] == 1
    assert empty_result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original
    assert absent_result["error"] != empty_result["error"], (
        "absent vs. present-but-empty override_reason must produce distinct "
        "error strings (AC13)"
    )
    assert "override_reason" in empty_result["error"]


def test_neither_holder_nor_author_nonempty_override_reason_succeeds_and_stamps(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        "2026-08-06-neither-override-succeeds.md",
        authoring_session=_AUTHOR_SESSION,
        claimed_by=_HOLDER_SESSION,
    )
    _set_calling_session(monkeypatch, _OTHER_SESSION)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
            "override_reason": "holder unreachable, correcting a factual typo",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["ownership_basis"] == "neither"

    text = hpath.read_text(encoding="utf-8")
    assert "The count was 25." in text
    assert "holder unreachable, correcting a factual typo" in text
    assert "basis=neither" in text


def test_override_reason_never_touches_coordinator_override_or_allow_env_keys(tmp_path, monkeypatch):
    """Guards the specific regression test_override_unreachability_boundary.py
    exists to prevent — an agent-settable param sneaking into a prefix whose
    test-pinned semantics are human-operator-only (AC13)."""
    import coordinator_core.ops.handoff_correct_body as hcb

    source = Path(hcb.__file__).read_text(encoding="utf-8")
    # The module docstring legitimately CITES
    # `COORDINATOR_OVERRIDE_CONSUMED_HANDOFF_EDIT` in prose (the guard's own,
    # unrelated recovery override, named as contrast for why this op's gate
    # is weaker) — that citation is not a lookup. What must never appear is
    # an actual environment-variable READ of a COORDINATOR_OVERRIDE_*/
    # COORDINATOR_ALLOW_* name anywhere in this module's executable code.
    assert "os.environ.get(\"COORDINATOR_OVERRIDE" not in source
    assert "os.environ.get('COORDINATOR_OVERRIDE" not in source
    assert "os.environ[\"COORDINATOR_OVERRIDE" not in source
    assert "os.environ.get(\"COORDINATOR_ALLOW" not in source
    assert "os.environ.get('COORDINATOR_ALLOW" not in source
    assert "os.environ[\"COORDINATOR_ALLOW" not in source
    # The op's call path never IMPORTS block_consumed_handoff_edit.py (only
    # cites it in prose, above, as the guard this op's write bypasses) —
    # so no environment-variable reference in that module is reachable
    # transitively from this op.
    assert "import block_consumed_handoff_edit" not in source
    assert "from coordinator_core.write_guards" not in source

    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        "2026-08-06-env-spoof-unreachable.md",
        authoring_session=_AUTHOR_SESSION,
        claimed_by=_HOLDER_SESSION,
    )
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch, _OTHER_SESSION)
    monkeypatch.setenv("COORDINATOR_OVERRIDE_CONSUMED_HANDOFF_EDIT", "1")
    monkeypatch.setenv("COORDINATOR_ALLOW_ANYTHING", "1")

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# AC12/AC14 — archive-follow terminal-state refusal and non-terminal success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("terminal_state", ["shipped", "continued", "closed"])
def test_archive_terminal_deployment_state_refuses(tmp_path, monkeypatch, terminal_state):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(
        repo,
        f"2026-08-06-terminal-{terminal_state}.md",
        deployment_state=terminal_state,
    )
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "Body.",
            "new_string": "Fixed body.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert terminal_state in result["error"]
    assert hpath.read_text(encoding="utf-8") == original


def test_archive_nonterminal_deployment_state_succeeds(tmp_path, monkeypatch):
    """Proves the AC14 refusal is scoped to the three terminal states, not
    archive generally — a non-terminal archived deployment_state stays in
    scope, identically to a live target."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(
        repo,
        "2026-08-06-nonterminal.md",
        deployment_state="awaiting_gate",
    )
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "Body.",
            "new_string": "Fixed body.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert "Fixed body." in hpath.read_text(encoding="utf-8")


def test_distinct_error_per_terminal_state(tmp_path, monkeypatch):
    """Each of the three terminal states above produces a message naming
    that state — not one generic 'terminal' string indistinguishable across
    all three."""
    repo = _make_git_repo(tmp_path)
    _set_calling_session(monkeypatch)
    errors = {}
    for terminal_state in ("shipped", "continued", "closed"):
        hpath = _seed_archived_handoff(
            repo,
            f"2026-08-06-distinct-{terminal_state}.md",
            deployment_state=terminal_state,
        )
        result = _run(_handler(
            {
                "handoff_path": str(hpath),
                "old_string": "Body.",
                "new_string": "Fixed body.",
            },
            repo_root=repo / ".git",
        ))
        errors[terminal_state] = result["error"]
    assert len(set(errors.values())) == 3, errors


def test_direct_archive_path_containment_uses_contained_path_primitive(tmp_path, monkeypatch):
    """F1 regression (staff-eng chain-review): the direct-archive-path
    branch (caller supplies an already-archived path, so `p.is_file()` is
    True without going through the archive-follow fallback) must derive
    `is_archived` via the guarded `contained_path` primitive, not a bare
    `Path.relative_to` — only `contained_path` carries the Windows
    extended-length-prefix defence documented in
    `_path_guard.contained_path`; a bare `relative_to` can raise
    `ValueError` on a real Windows host with a long path, silently turning
    `is_archived` False and skipping the AC14 terminal-state refusal
    entirely. Pins two things: (1) `contained_path` is actually invoked for
    this branch, scoped to the archive root alone, and (2) the
    terminal-state refusal it feeds still fires."""
    import coordinator_core.ops.handoff_correct_body as hcb

    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(
        repo, "2026-08-06-f1-archive-containment.md", deployment_state="shipped",
    )
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch)

    calls = []
    real_contained_path = hcb.contained_path

    def _spy(candidate, roots):
        result = real_contained_path(candidate, roots)
        calls.append((candidate, list(roots), result))
        return result

    monkeypatch.setattr(hcb, "contained_path", _spy)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "Body.",
            "new_string": "Fixed body.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "shipped" in result["error"]
    assert hpath.read_text(encoding="utf-8") == original

    archive_only_root = [repo / "archive" / "handoffs"]
    archive_root_calls = [c for c in calls if c[1] == archive_only_root]
    assert archive_root_calls, (
        "contained_path was never called with the archive-only root set for "
        "the direct-path branch — is_archived may be using a bare "
        "Path.relative_to again instead of the guarded primitive"
    )


# ---------------------------------------------------------------------------
# AC5's D2(i) note — live+archived twin resolves to the live root
# ---------------------------------------------------------------------------


def test_live_and_archived_twin_resolves_to_live_root(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    name = "2026-08-06-twin.md"
    live = _seed_claimed_handoff(repo, name)
    archived = _seed_archived_handoff(
        repo, name, month="2026-08", body="\n# Archived twin body.\n"
    )
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(live),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    live_text = live.read_text(encoding="utf-8")
    archived_text = archived.read_text(encoding="utf-8")
    assert "The count was 25." in live_text, "correction must land on the LIVE file"
    assert "Archived twin body." in archived_text, "the archived twin must stay untouched"
    assert _CORRECTION_MARKER_PREFIX not in archived_text


# ---------------------------------------------------------------------------
# AC2 (staff-eng re-review Finding 7) — claimed_by absent/empty falls
# through to the author-arm rather than being treated as ownerless/neither.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("claimed_by_value", [None, ""])
def test_claimed_by_absent_or_empty_falls_through_to_author_arm_match_succeeds(
    tmp_path, monkeypatch, claimed_by_value
):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        f"2026-08-06-legacy-match-{claimed_by_value!r}.md",
        status="consumed",
        authoring_session=_AUTHOR_SESSION,
        claimed_by=claimed_by_value,
    )
    _set_calling_session(monkeypatch, _AUTHOR_SESSION)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["ownership_basis"] == "author"


@pytest.mark.parametrize("claimed_by_value", [None, ""])
def test_claimed_by_absent_or_empty_falls_through_to_author_arm_mismatch_refuses(
    tmp_path, monkeypatch, claimed_by_value
):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        f"2026-08-06-legacy-mismatch-{claimed_by_value!r}.md",
        status="consumed",
        authoring_session=_AUTHOR_SESSION,
        claimed_by=claimed_by_value,
    )
    original = hpath.read_text(encoding="utf-8")
    _set_calling_session(monkeypatch, _OTHER_SESSION)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# AC2/AC4 (staff-eng re-review Finding 5) — disagreement-detection stamps on
# the HOLDER arm too, both directions.
# ---------------------------------------------------------------------------


def test_holder_arm_disagreement_reaped_claim_still_allows_and_stamps(tmp_path, monkeypatch):
    """claimed_by names the caller, but no claim dir exists at all (reaped /
    expired) -> allow via frontmatter, but stamp the disagreement."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        "2026-08-06-holder-reaped.md",
        claimed_by=_HOLDER_SESSION,
    )
    _set_calling_session(monkeypatch, _HOLDER_SESSION)
    # Deliberately do NOT write a claim dir for this basename.

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["ownership_basis"] == "holder"
    text = hpath.read_text(encoding="utf-8")
    assert "claim-disagreement detected" in text


def test_holder_arm_disagreement_different_current_holder_still_allows_and_stamps(
    tmp_path, monkeypatch
):
    """The exact spoof shape: claimed_by frontmatter names the caller, but
    the claim-dir shows a DIFFERENT current holder -> allow via frontmatter
    (AC2), but stamp the disagreement."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        "2026-08-06-holder-spoof.md",
        claimed_by=_HOLDER_SESSION,
    )
    _write_claim_dir(repo, "handoff", hpath.name, _OTHER_SESSION)
    _set_calling_session(monkeypatch, _HOLDER_SESSION)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["ownership_basis"] == "holder"
    text = hpath.read_text(encoding="utf-8")
    assert "claim-disagreement detected" in text


def test_holder_arm_agreement_no_disagreement_stamped(tmp_path, monkeypatch):
    """Control case: claim dir agrees with claimed_by -> no disagreement
    text stamped, proving the marker is conditional, not unconditional."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        "2026-08-06-holder-agree.md",
        claimed_by=_HOLDER_SESSION,
    )
    _write_claim_dir(repo, "handoff", hpath.name, _HOLDER_SESSION)
    _set_calling_session(monkeypatch, _HOLDER_SESSION)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    text = hpath.read_text(encoding="utf-8")
    assert "claim-disagreement" not in text


def test_sentinel_claimed_by_does_not_authorize_holder_arm_write(tmp_path, monkeypatch):
    """F3 regression (staff-eng chain-review): a sentinel/malformed
    `claimed_by` (the literal string "none" — a live corpus shape per
    `test_coverage_empty_frontmatter_key_pad.py`) must not act as a
    skeleton key on the holder arm just because a caller sets a matching
    env literal. The holder arm applies the same
    `_is_sentinel_or_malformed_session` predicate the author arm already
    applied to `authoring_session` — a sentinel `claimed_by` falls through
    to the author arm instead, which refuses here since `authoring_session`
    does not match."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        "2026-08-06-f3-sentinel-claimed-by.md",
        claimed_by="none",
        authoring_session=_AUTHOR_SESSION,
    )
    original = hpath.read_text(encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "none")

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 30.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "neither" in result["error"]
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# F4 (staff-eng chain-review) — disagreement detection on the AUTHOR and
# NEITHER arms. `disagreement = (basis == "holder") != claim_self_held`
# was previously pinned only on the holder arm (the always-True mutation
# failed just the two holder tests, 126/128 still green). Determination
# (see run-report): this expression is CORRECT AS WRITTEN, not a false
# positive. Disagreement here means "the claimed_by-derived basis and the
# separate claim-dir mechanism disagree about who holds this baton" — on
# the author/neither arms that disagreement is real and worth surfacing:
# a caller who holds the live claim dir while claimed_by is absent/names
# someone else is exactly the frontmatter-vs-claim-dir divergence this
# stamp exists to make visible (module docstring: "the ACTUAL control...
# is the stamped, auditable correction note", never a refusal). It never
# blocks the write on any arm — only the audit trail is affected — so a
# "false positive" here costs nothing beyond an extra clause in the
# stamped note.
# ---------------------------------------------------------------------------


def test_author_arm_disagreement_self_held_claim_dir_stamps_disagreement(tmp_path, monkeypatch):
    """Author arm (claimed_by absent — legacy consumed-style baton),
    authoring_session matches the caller, AND the caller actually holds a
    live claim dir for this basename -> the frontmatter records no holder
    at all while the claim-dir mechanism shows this session as current
    holder, a real divergence -> stamped."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        "2026-08-06-f4-author-self-held.md",
        authoring_session=_AUTHOR_SESSION,
        claimed_by=None,
    )
    _write_claim_dir(repo, "handoff", hpath.name, _AUTHOR_SESSION)
    _set_calling_session(monkeypatch, _AUTHOR_SESSION)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["ownership_basis"] == "author"
    text = hpath.read_text(encoding="utf-8")
    assert "claim-disagreement detected" in text


def test_author_arm_no_claim_dir_no_disagreement_stamped(tmp_path, monkeypatch):
    """Control: author arm, no claim dir exists at all for this basename
    -> claimed_by-derived basis (not-holder) agrees with the claim-dir
    reality (not-held) -> no disagreement text stamped."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        "2026-08-06-f4-author-no-claim-dir.md",
        authoring_session=_AUTHOR_SESSION,
        claimed_by=None,
    )
    # Deliberately do NOT write a claim dir for this basename.
    _set_calling_session(monkeypatch, _AUTHOR_SESSION)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["ownership_basis"] == "author"
    text = hpath.read_text(encoding="utf-8")
    assert "claim-disagreement" not in text


def test_neither_arm_disagreement_self_held_claim_dir_stamps_disagreement(tmp_path, monkeypatch):
    """Neither arm (claimed_by and authoring_session both name someone
    else, override_reason supplied), but the caller actually holds a live
    claim dir for this basename -> real divergence between what the
    frontmatter says and what the claim-dir mechanism says -> stamped."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        "2026-08-06-f4-neither-self-held.md",
        authoring_session=_AUTHOR_SESSION,
        claimed_by=_HOLDER_SESSION,
    )
    _write_claim_dir(repo, "handoff", hpath.name, _OTHER_SESSION)
    _set_calling_session(monkeypatch, _OTHER_SESSION)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
            "override_reason": "emergency fix, PM approved",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["ownership_basis"] == "neither"
    text = hpath.read_text(encoding="utf-8")
    assert "claim-disagreement detected" in text


def test_neither_arm_no_claim_dir_no_disagreement_stamped(tmp_path, monkeypatch):
    """Control: neither arm, no claim dir at all for this basename ->
    claimed_by-derived basis (not-holder) agrees with the claim-dir
    reality (not-held) -> no disagreement text stamped."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo,
        "2026-08-06-f4-neither-no-claim-dir.md",
        authoring_session=_AUTHOR_SESSION,
        claimed_by=_HOLDER_SESSION,
    )
    # Deliberately do NOT write a claim dir for this basename.
    _set_calling_session(monkeypatch, _OTHER_SESSION)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
            "override_reason": "emergency fix, PM approved",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["ownership_basis"] == "neither"
    text = hpath.read_text(encoding="utf-8")
    assert "claim-disagreement" not in text


# ---------------------------------------------------------------------------
# AC5 — surviving DR-247 D2 bounds, one explicit test per bound.
# ---------------------------------------------------------------------------


def test_ac5_d2i_single_file_only_no_other_file_touched(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-target.md")
    other = _seed_claimed_handoff(repo, "2026-08-06-bystander.md")
    other_before = other.read_bytes()
    other_mtime_before = other.stat().st_mtime_ns
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert other.read_bytes() == other_before
    assert other.stat().st_mtime_ns == other_mtime_before


def test_ac5_d2iii_bounded_replacement_leaves_rest_of_body_unchanged(tmp_path, monkeypatch):
    body = "\n# Handoff body.\n\nLine one.\nThe count was 29.\nLine three.\n"
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-bounded.md", body=body)
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    text = hpath.read_text(encoding="utf-8")
    assert "Line one." in text
    assert "Line three." in text


def test_ac5_d2iv_frontmatter_byte_identical(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo, "2026-08-06-fm-identical.md", claimed_by=_HOLDER_SESSION
    )
    fm_before = hpath.read_text(encoding="utf-8").split("---", 2)[1]
    _set_calling_session(monkeypatch, _HOLDER_SESSION)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    fm_after = hpath.read_text(encoding="utf-8").split("---", 2)[1]
    assert fm_after == fm_before


def test_ac5_d2v_no_section_added_beyond_canonical_correction_log(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-no-section.md")
    before_heading_count = hpath.read_text(encoding="utf-8").count("\n## ") + hpath.read_text(
        encoding="utf-8"
    ).count("\n# ")
    _set_calling_session(monkeypatch)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    after_text = hpath.read_text(encoding="utf-8")
    after_heading_count = after_text.count("\n## ") + after_text.count("\n# ")
    # Exactly one new heading is expected: the canonical correction-log
    # section heading itself (added once, on the first correction).
    assert after_heading_count == before_heading_count + 1
    assert _CORRECTION_SECTION_HEADING in after_text


def test_ac5_d2vi_handler_never_issues_git_commit(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-no-commit.md")
    _set_calling_session(monkeypatch)

    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head_after == head_before, "the handler must never git-commit"

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout
    assert "2026-08-06-no-commit.md" in status, "the write must be an uncommitted, dirty change"


def test_ac5_d2vii_no_shell_out(tmp_path, monkeypatch):
    """Bound (vii) (no shell-out), asserted via a monkeypatched subprocess
    module raising on call — the cheapest available seam.

    `locked_rmw`'s shared `git_common_dir` lookup (`coordinator_core.
    lifecycle.git_common_dir`) is `lru_cache`d infra common to every op that
    uses `locked_rmw` (`handoff_stamp` included) — a pre-existing shell-out
    this op did not introduce and does not control. Warm that cache for
    this repo_root BEFORE patching `subprocess`, so the assertion below is
    scoped to whether `handoff_correct_body`'s OWN code shells out, not
    whether the shared locking seam does."""
    from coordinator_core.lifecycle import git_common_dir

    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-no-shellout.md")
    _set_calling_session(monkeypatch)
    git_common_dir(repo / ".git")  # warm the lru_cache before patching subprocess

    def _raise(*args, **kwargs):
        raise AssertionError("handoff.correct_body must never shell out")

    monkeypatch.setattr(subprocess, "run", _raise)
    monkeypatch.setattr(subprocess, "Popen", _raise)
    monkeypatch.setattr(subprocess, "call", _raise)
    monkeypatch.setattr(subprocess, "check_call", _raise)
    monkeypatch.setattr(subprocess, "check_output", _raise)

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert "The count was 25." in hpath.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC6 — the stamp names session and basis for ALL THREE arms.
# ---------------------------------------------------------------------------


def test_stamp_names_session_and_basis_for_all_three_arms(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)

    holder_hpath = _seed_claimed_handoff(
        repo, "2026-08-06-arm-holder.md",
        authoring_session=_AUTHOR_SESSION, claimed_by=_HOLDER_SESSION,
    )
    author_hpath = _seed_claimed_handoff(
        repo, "2026-08-06-arm-author.md",
        authoring_session=_AUTHOR_SESSION, claimed_by=_HOLDER_SESSION,
    )
    neither_hpath = _seed_claimed_handoff(
        repo, "2026-08-06-arm-neither.md",
        authoring_session=_AUTHOR_SESSION, claimed_by=_HOLDER_SESSION,
    )

    _set_calling_session(monkeypatch, _HOLDER_SESSION)
    holder_result = _run(_handler(
        {
            "handoff_path": str(holder_hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))
    assert holder_result["exit_code"] == 0, holder_result
    assert f"session {_HOLDER_SESSION}" in holder_hpath.read_text(encoding="utf-8")
    assert "basis=holder" in holder_hpath.read_text(encoding="utf-8")

    _set_calling_session(monkeypatch, _AUTHOR_SESSION)
    author_result = _run(_handler(
        {
            "handoff_path": str(author_hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
        },
        repo_root=repo / ".git",
    ))
    assert author_result["exit_code"] == 0, author_result
    assert f"session {_AUTHOR_SESSION}" in author_hpath.read_text(encoding="utf-8")
    assert "basis=author" in author_hpath.read_text(encoding="utf-8")

    _set_calling_session(monkeypatch, _OTHER_SESSION)
    neither_result = _run(_handler(
        {
            "handoff_path": str(neither_hpath),
            "old_string": "The count was 29.",
            "new_string": "The count was 25.",
            "override_reason": "recorded emergency correction",
        },
        repo_root=repo / ".git",
    ))
    assert neither_result["exit_code"] == 0, neither_result
    assert f"session {_OTHER_SESSION}" in neither_hpath.read_text(encoding="utf-8")
    assert "basis=neither" in neither_hpath.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Finding 8 — module docstring no longer claims archive is unreachable.
# ---------------------------------------------------------------------------


def test_module_docstring_no_longer_claims_archive_unreachable():
    import coordinator_core.ops.handoff_correct_body as hcb

    source = Path(hcb.__file__).read_text(encoding="utf-8")
    assert "never `archive/handoffs/` (DR-247 D2(ii))" not in source, (
        "the stale pre-amendment negative-spec sentence must be updated "
        "(staff-eng re-review Finding 8)"
    )
    assert "archive/handoffs/" in source
    assert "AC12" in source or "archive-follow" in source


# ---------------------------------------------------------------------------
# AC17 — duplicate checkbox line text disambiguated by surrounding context,
# not raw text match. Authored so C7's handoff.discharge_criteria extension
# (criterion identity/position resolution) can build on this shape directly.
# ---------------------------------------------------------------------------


def test_duplicate_checkbox_line_text_ties_to_correct_box_via_context(tmp_path, monkeypatch):
    body = (
        "\n## Acceptance criteria\n\n"
        "AC-1: does X\n"
        "- [ ] Done\n\n"
        "AC-2: does Y\n"
        "- [ ] Done\n"
    )
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-dup-checkbox.md", body=body)
    _set_calling_session(monkeypatch)

    # "- [ ] Done" alone occurs twice — a raw-text-only match would be
    # ambiguous. Disambiguate via enough surrounding context to occur
    # exactly once, ticking ONLY the AC-2 box.
    old_string = "AC-2: does Y\n- [ ] Done"
    new_string = "AC-2: does Y\n- [x] Done"

    result = _run(_handler(
        {
            "handoff_path": str(hpath),
            "old_string": old_string,
            "new_string": new_string,
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    text = hpath.read_text(encoding="utf-8")
    assert "AC-1: does X\n- [ ] Done" in text, "AC-1's box must stay unticked"
    assert "AC-2: does Y\n- [x] Done" in text, "AC-2's box must be ticked"


def test_duplicate_checkbox_line_text_ties_to_correct_box_via_discharge_criteria(
    tmp_path, monkeypatch
):
    """C7 (AC17) — the promoted `handoff.discharge_criteria` verb itself
    resolves the correct box by criterion identity, not raw line text,
    against the identical duplicate-checkbox-line body C3 authored."""
    body = (
        "\n## Acceptance criteria\n\n"
        "AC-1: does X\n"
        "- [ ] Done\n\n"
        "AC-2: does Y\n"
        "- [ ] Done\n"
    )
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-dup-checkbox-discharge.md", body=body)
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {"handoff_path": str(hpath), "criterion_id": "AC-2"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["resolved_criterion_id"] == "AC-2"
    assert result["discharge_op"] == "tick"
    text = hpath.read_text(encoding="utf-8")
    assert "AC-1: does X\n- [ ] Done" in text, "AC-1's box must stay unticked"
    assert "AC-2: does Y\n- [x] Done" in text, "AC-2's box must be ticked"


# ---------------------------------------------------------------------------
# C7 — handoff.discharge_criteria (AC16-AC19). A bounded wrapper over
# handoff.correct_body: never re-implements the write path (AC16); resolves
# a target checkbox by criterion identity or structural position, never raw
# text (AC17); supports a tick and a met/unmet SPLIT (AC19).
# ---------------------------------------------------------------------------

_ACC_BODY = (
    "\n## Acceptance criteria\n\n"
    "AC-1: first criterion\n"
    "- [ ] Done\n\n"
    "AC-2: second criterion\n"
    "- [ ] Done\n\n"
    "AC-3: third criterion\n"
    "- [x] Done\n"
)


def test_discharge_tick_by_criterion_id_succeeds(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-discharge-id.md", body=_ACC_BODY)
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {"handoff_path": str(hpath), "criterion_id": "AC-1"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["resolved_position"] == 1
    assert result["resolved_criterion_id"] == "AC-1"
    assert result["discharge_op"] == "tick"
    text = hpath.read_text(encoding="utf-8")
    assert "AC-1: first criterion\n- [x] Done" in text
    assert "AC-2: second criterion\n- [ ] Done" in text, "AC-2 unaffected"


def test_discharge_tick_by_position_succeeds(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-discharge-pos.md", body=_ACC_BODY)
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {"handoff_path": str(hpath), "position": 2},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["resolved_position"] == 2
    assert result["resolved_criterion_id"] == "AC-2"
    text = hpath.read_text(encoding="utf-8")
    assert "AC-2: second criterion\n- [x] Done" in text
    assert "AC-1: first criterion\n- [ ] Done" in text, "AC-1 unaffected"


def test_discharge_stamps_correction_note_with_session_and_basis(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-discharge-stamp.md", body=_ACC_BODY)
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {"handoff_path": str(hpath), "criterion_id": "AC-1"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    text = hpath.read_text(encoding="utf-8")
    assert _CORRECTION_MARKER_PREFIX in text
    assert _CORRECTION_SECTION_HEADING in text
    assert f"session {_AUTHOR_SESSION}" in text
    assert "basis=author" in text


def test_discharge_requires_exactly_one_of_criterion_id_or_position(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-discharge-both.md", body=_ACC_BODY)
    _set_calling_session(monkeypatch)

    neither = _run(_discharge_handler(
        {"handoff_path": str(hpath)}, repo_root=repo / ".git",
    ))
    assert neither["exit_code"] == 1
    assert "supply exactly one" in neither["error"]

    both = _run(_discharge_handler(
        {"handoff_path": str(hpath), "criterion_id": "AC-1", "position": 1},
        repo_root=repo / ".git",
    ))
    assert both["exit_code"] == 1
    assert "mutually exclusive" in both["error"]
    assert neither["error"] != both["error"], "distinct errors for the two malformed shapes"

    unwritten = hpath.read_text(encoding="utf-8")
    assert _CORRECTION_MARKER_PREFIX not in unwritten


def test_discharge_unknown_criterion_id_refuses(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-discharge-unknown.md", body=_ACC_BODY)
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {"handoff_path": str(hpath), "criterion_id": "AC-99"},
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "no checkbox found" in result["error"]
    assert _CORRECTION_MARKER_PREFIX not in hpath.read_text(encoding="utf-8")


def test_discharge_position_out_of_range_refuses(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-discharge-oor.md", body=_ACC_BODY)
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {"handoff_path": str(hpath), "position": 99},
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "out of range" in result["error"]


def test_discharge_already_ticked_criterion_refuses(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-discharge-already.md", body=_ACC_BODY)
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {"handoff_path": str(hpath), "criterion_id": "AC-3"},
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "already ticked" in result["error"]
    assert _CORRECTION_MARKER_PREFIX not in hpath.read_text(encoding="utf-8")


def test_discharge_no_acceptance_criteria_section_refuses(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo, "2026-08-06-discharge-nosection.md",
        body="\n# Handoff body.\n\nNo criteria section here.\n",
    )
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {"handoff_path": str(hpath), "position": 1},
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "no '## Acceptance criteria' section" in result["error"]


def test_discharge_inherits_ownership_gate_neither_refuses(tmp_path, monkeypatch):
    """AC16 — the ownership arms are inherited, never re-implemented: a
    neither-holder-nor-author caller with no override_reason hard-refuses,
    exactly as handoff.correct_body's own arm does."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo, "2026-08-06-discharge-neither.md",
        claimed_by=_HOLDER_SESSION, body=_ACC_BODY,
    )
    _set_calling_session(monkeypatch, _OTHER_SESSION)

    result = _run(_discharge_handler(
        {"handoff_path": str(hpath), "criterion_id": "AC-1"},
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "neither the claim" in result["error"]
    assert _CORRECTION_MARKER_PREFIX not in hpath.read_text(encoding="utf-8")


def test_discharge_inherits_ownership_gate_holder_succeeds(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo, "2026-08-06-discharge-holder.md",
        claimed_by=_HOLDER_SESSION, body=_ACC_BODY,
    )
    _set_calling_session(monkeypatch, _HOLDER_SESSION)

    result = _run(_discharge_handler(
        {"handoff_path": str(hpath), "criterion_id": "AC-1"},
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 0, result
    assert result["ownership_basis"] == "holder"
    assert "basis=holder" in hpath.read_text(encoding="utf-8")


def test_discharge_inherits_override_reason_neither_arm(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo, "2026-08-06-discharge-override.md",
        claimed_by=_HOLDER_SESSION, body=_ACC_BODY,
    )
    _set_calling_session(monkeypatch, _OTHER_SESSION)

    result = _run(_discharge_handler(
        {
            "handoff_path": str(hpath),
            "criterion_id": "AC-1",
            "override_reason": "recovery: holder unreachable, verified via ticket",
        },
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 0, result
    assert result["ownership_basis"] == "neither"
    text = hpath.read_text(encoding="utf-8")
    assert "basis=neither" in text
    assert "recovery: holder unreachable, verified via ticket" in text


def test_discharge_inherits_archive_terminal_state_refusal(tmp_path, monkeypatch):
    """AC16 — the terminal-state refusal on the archive-follow arm (AC14)
    is inherited, not re-implemented."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(
        repo, "2026-08-06-discharge-terminal.md",
        deployment_state="shipped", body=_ACC_BODY,
    )
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {"handoff_path": str(hpath), "criterion_id": "AC-1"},
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "terminal" in result["error"]


# --- Split (AC19) -----------------------------------------------------------


def test_discharge_split_succeeds_and_both_boxes_independently_resolvable(
    tmp_path, monkeypatch
):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-discharge-split.md", body=_ACC_BODY)
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {
            "handoff_path": str(hpath),
            "criterion_id": "AC-1",
            "met_text": "9k of the 10k run",
            "unmet_text": "final 1k (AC-1)",
        },
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 0, result
    assert result["discharge_op"] == "split"

    text = hpath.read_text(encoding="utf-8")
    assert "AC-1: first criterion\n- [x] 9k of the 10k run\n- [ ] final 1k (AC-1)" in text

    # (a) frontmatter byte-identical — reread and compare to a fresh seed.
    control = _seed_claimed_handoff(
        repo, "2026-08-06-discharge-split-control.md", body=_ACC_BODY
    )
    control_fm = control.read_text(encoding="utf-8").split("---", 2)[1]
    result_fm = text.split("---", 2)[1]
    assert control_fm == result_fm

    # (d) both resulting checkboxes are independently resolvable by
    # STRUCTURAL POSITION afterward — position 1 is now the met (already-
    # ticked) half, position 2 the unmet (still-unticked) half.
    met_via_position = _run(_discharge_handler(
        {"handoff_path": str(hpath), "position": 1},
        repo_root=repo / ".git",
    ))
    assert met_via_position["exit_code"] == 1, "position 1 now resolves to the ticked met half"
    assert "already ticked" in met_via_position["error"]

    unmet_via_position = _run(_discharge_handler(
        {"handoff_path": str(hpath), "position": 2},
        repo_root=repo / ".git",
    ))
    assert unmet_via_position["exit_code"] == 0, unmet_via_position
    text2 = hpath.read_text(encoding="utf-8")
    assert "- [x] final 1k (AC-1)" in text2
    # (F4) post-split addressability-by-criterion_id coverage lives in
    # test_handoff_discharge_criteria.py (a checkbox-own-text-carried id,
    # not this fixture's separate-annotation-line id shape, is the only
    # layout that can stay UNAMBIGUOUSLY addressable by criterion_id after
    # a one-to-two split — see that file's F4 tests).


def test_discharge_split_requires_both_met_and_unmet_text(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-discharge-split-partial.md", body=_ACC_BODY)
    _set_calling_session(monkeypatch)

    only_met = _run(_discharge_handler(
        {"handoff_path": str(hpath), "criterion_id": "AC-1", "met_text": "9k"},
        repo_root=repo / ".git",
    ))
    assert only_met["exit_code"] == 1
    assert "requires BOTH" in only_met["error"]
    assert _CORRECTION_MARKER_PREFIX not in hpath.read_text(encoding="utf-8")


def test_discharge_split_refuses_when_net_growth_cap_exceeded(tmp_path, monkeypatch):
    """(b) refuses, distinct error, no write, when the split would exceed
    _NET_GROWTH_CAP — inherited from handoff.correct_body, not re-derived."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-discharge-split-cap.md", body=_ACC_BODY)
    _set_calling_session(monkeypatch)

    huge_text = "x" * 1000
    result = _run(_discharge_handler(
        {
            "handoff_path": str(hpath),
            "criterion_id": "AC-1",
            "met_text": huge_text,
            "unmet_text": huge_text + " AC-1",
        },
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    # F3 (staff-eng chain-review): the loose `or "byte"` disjunct matches
    # most size-related refusals in this op (it is what let F2's
    # wrong-cap test pass silently) — assert the specific phrase only.
    assert "net-growth cap" in result["error"]
    assert _CORRECTION_MARKER_PREFIX not in hpath.read_text(encoding="utf-8")


def test_discharge_split_refuses_when_old_string_body_ratio_cap_exceeded(tmp_path, monkeypatch):
    """(b) refuses, distinct error, no write, when the split's old_string
    (including its identity context) would exceed
    _MAX_OLD_STRING_BODY_RATIO of a small body — WITHOUT exceeding the
    net-growth cap (F2, staff-eng chain-review): the previous version of
    this test grew the body by 803 bytes (over the 512-byte net-growth
    cap) and asserted only exit_code==1, so it was actually a duplicate of
    test_discharge_split_refuses_when_net_growth_cap_exceeded under a
    misleading name and could never detect it was measuring the wrong
    bound. Here old_string (the checkbox's own long unwrapped text) is
    ~95% of the tiny body, while met_text/unmet_text are kept to a single
    character each, so new_string is SHORTER than old_string (net growth
    is negative, nowhere near the 512-byte cap) — only the ratio bound can
    be tripping this refusal."""
    repo = _make_git_repo(tmp_path)
    tiny_body = (
        "\n## Acceptance criteria\n\n"
        "AC-1: x\n"
        "- [ ] " + ("y" * 600) + "\n"
    )
    hpath = _seed_claimed_handoff(repo, "2026-08-06-discharge-split-ratio.md", body=tiny_body)
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {
            "handoff_path": str(hpath),
            "criterion_id": "AC-1",
            "met_text": "m",
            "unmet_text": "u AC-1",
        },
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1, result
    assert "% of the body" in result["error"], (
        "expected the ratio-cap-specific refusal, not a different "
        f"precondition: {result['error']!r}"
    )
    assert "net-growth cap" not in result["error"]
    assert _CORRECTION_MARKER_PREFIX not in hpath.read_text(encoding="utf-8")


def test_discharge_split_no_new_heading_or_delimiter_line_introduced(tmp_path, monkeypatch):
    """(c) assert via _HEADING_LINE_RE / _DELIM_LINE_RE — the same
    predicates handoff.correct_body itself uses — that a split's new_string
    never introduces a heading or frontmatter-delimiter line, so this op's
    own construction stays within DR-247 D2(v) by inspection, not merely by
    correct_body's independent enforcement of it.

    F6 (staff-eng chain-review): the previous version of this test ran the
    two regexes against a LITERAL the test itself authored
    (`new_lines_region`), so the last two assertions held by inspection
    regardless of what the op actually produced — only the containment
    check constrained anything. This diffs the pre/post body and runs the
    regexes against every line the op ACTUALLY added.
    """
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-discharge-split-noheading.md", body=_ACC_BODY)
    pre_lines = set(hpath.read_text(encoding="utf-8").splitlines())
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {
            "handoff_path": str(hpath),
            "criterion_id": "AC-2",
            "met_text": "9k of the 10k run",
            "unmet_text": "final 1k (AC-2)",
        },
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 0, result

    text = hpath.read_text(encoding="utf-8")
    post_lines = text.splitlines()
    # Exclude the canonical correction-log heading/marker lines this op's
    # own AC5 stamping deliberately appends — the DR-247 D2(v) "no new
    # heading" restriction binds new_string (the split's own content),
    # never the separate, sanctioned correction-log append.
    added_lines = [
        line for line in post_lines
        if line not in pre_lines
        and line != _CORRECTION_SECTION_HEADING
        and not line.startswith(_CORRECTION_MARKER_PREFIX)
    ]
    assert added_lines, "the split produced no new lines to check"
    assert any("9k of the 10k run" in line for line in added_lines)
    assert any("final 1k" in line for line in added_lines)
    for line in added_lines:
        assert not _HEADING_LINE_RE.match(line + "\n"), line
        assert not _DELIM_LINE_RE.match(line + "\n"), line


def test_discharge_split_already_ticked_criterion_refuses(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-06-discharge-split-already.md", body=_ACC_BODY)
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {
            "handoff_path": str(hpath),
            "criterion_id": "AC-3",
            "met_text": "m",
            "unmet_text": "u",
        },
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "already ticked" in result["error"]

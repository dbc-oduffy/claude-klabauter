"""
coordinator_core.ops.test_propagate_body

Tests for the handoff.propagate op (B1, docs/plans/2026-08-01-baton-spine-
information-integrity.md).

Import guard: coordinator_core.ops.propagate_body MUST be imported at module
load time so @register_op("handoff.propagate") fires and populates _REGISTRY
— mirrors coordinator_core/ops/tests/test_handoff_correct_body.py's own
import-guard convention.

Coverage:
  (a) registry + classification/key-scope presence.
  (q) B2 (AC8) — plan.propagate: same module, registered under a second
      verb, target root docs/plans/ instead of state/handoffs/, no status
      gate (a plan body carries no claimed/consumed equivalent). One test
      per: registration/classification/key-scope presence, a successful
      delivery, an archive-target refusal, and a missing-param refusal.
  (b) delivery into a claimed handoff the CALLER DID NOT AUTHOR succeeds
      (no authorship gate) — AC7.
  (c) repeated deliveries append under a single canonical heading, never a
      new heading per delivery — AC7.
  (d) frontmatter is byte-identical after delivery.
  (e) the write lands as a git commit scoped to exactly the target file,
      carrying Nature/Delivered-By trailers and NEITHER Session-Id NOR
      Deliverable-Id (AC12 precondition 3), even when the delivering
      session's own env vars are set.
  (f)-(h) one test per AC12 precondition abort path: dirty target, an
      in-progress merge (MERGE_HEAD), detached HEAD.
  (i) legacy status: consumed accepted.
  (j) refuses an open (non-claimed/consumed) status handoff.
  (k) refuses an archive/handoffs/ target.
  (l) refuses missing/blank summary, note, slug; invalid kind.
  (m) refuses content exceeding the net-growth cap.
  (n) refuses invisible-unicode / heading-shaped / delimiter-shaped content.
  (o) writes/commits nothing on every refusal path.
  (p) subagent-context env (CLAUDE_CODE_CHILD_SESSION=1) does NOT block the
      delivery commit — pins the documented, knowing, bounded relaxation of
      block_subagent_commit's intent (module docstring point 4).

Spec backlink: coordinator_core/ops/propagate_body.py
               docs/plans/2026-08-01-baton-spine-information-integrity.md
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.propagate_body  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.propagate_body import (
    _PROPAGATED_SECTION_HEADING,
    _handler,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_OP_NAME = "handoff.propagate"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.propagate_body @register_op did not fire"
)

_PLAN_OP_NAME = "plan.propagate"
assert _PLAN_OP_NAME in _REGISTRY, (
    f"import guard failed: {_PLAN_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.propagate_body @register_op did not fire (B2)"
)

_DELIVERING_SESSION = "9220b333-be7f-4707-b5f2-9ebedf3e28ce"
_AUTHOR_SESSION = "11111111-2222-3333-4444-555555555555"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "propagate-test@claude-klabauter.test")
    _git(repo, "config", "user.name", "Propagate Test")
    _git(repo, "config", "commit.gpgsign", "false")

    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "chore: initial skeleton")
    return repo


def _seed_claimed_handoff(
    repo: Path,
    name: str,
    *,
    status: str = "claimed",
    authoring_session: str = _AUTHOR_SESSION,
    body: str = "\n# Handoff body.\n\nThe count was 29.\n",
    commit: bool = True,
) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        'title: "Test Handoff"\n'
        f"status: {status}\n"
        "claimed_at: 2026-08-01T12:00:00Z\n"
        f"authoring_session: {authoring_session}\n"
        "---" + body
    )
    # newline="" so the seed lands LF-exact, matching what `locked_rmw` writes
    # in production (`os.write(fd, new_text.encode("utf-8"))` — no newline
    # translation). Bare `write_text` translates to CRLF on Windows, which no
    # production writer ever produces; seeding CRLF made the rollback path look
    # like it corrupted the file when it had restored it byte-for-byte.
    path.write_text(content, encoding="utf-8", newline="")
    if commit:
        _git(repo, "add", "--", f"state/handoffs/{name}")
        _git(repo, "commit", "-m", f"chore: seed {name}")
    return path


def _seed_plan(
    repo: Path,
    name: str,
    *,
    status: str = "draft",
    body: str = "\n# Test Plan\n\n**Status:** In progress.\n",
    commit: bool = True,
) -> Path:
    """B2 (AC8) — seeds a live docs/plans/*.md plan body. Deliberately
    carries no `claimed`/`consumed` status concept (unlike a handoff) — a
    plan's own `status:` field is unrelated to plan.propagate's gate, which
    has none (see _TARGET_SPECS.status_gate=False for "plan")."""
    path = repo / "docs" / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        'title: "Test Plan"\n'
        f"status: {status}\n"
        "---" + body
    )
    # newline="" so the seed lands LF-exact, matching what `locked_rmw` writes
    # in production (`os.write(fd, new_text.encode("utf-8"))` — no newline
    # translation). Bare `write_text` translates to CRLF on Windows, which no
    # production writer ever produces; seeding CRLF made the rollback path look
    # like it corrupted the file when it had restored it byte-for-byte.
    path.write_text(content, encoding="utf-8", newline="")
    if commit:
        _git(repo, "add", "--", f"docs/plans/{name}")
        _git(repo, "commit", "-m", f"chore: seed {name}")
    return path


def _plan_base_params(ppath: Path, **overrides) -> dict:
    params = {
        "plan_path": str(ppath),
        "summary": "executor dispatch surfaced a stale premise in this plan body",
        "note": (
            "Verified against disk 2026-08-01: the dispatched executor found "
            "chunk B2's spec already re-derived at file:line."
        ),
        "slug": "example-market-data-repo-em",
    }
    params.update(overrides)
    return params


@pytest.fixture(autouse=True)
def _clear_session_env(monkeypatch):
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)


def _base_params(hpath: Path, **overrides) -> dict:
    params = {
        "handoff_path": str(hpath),
        "summary": "qsent-01 and its gate_dependency are both satisfied",
        "note": (
            "Verified against disk 2026-08-01: blocked_by qsent-01 resolved "
            "at claude-klabauter/state/handoffs/2026-07-30-qsent-01.md:12, and "
            "the identity carrier this baton left open was decided at "
            "docs/decisions/DR-247.md:3."
        ),
        "slug": "example-market-data-repo-em",
    }
    params.update(overrides)
    return params


# ---------------------------------------------------------------------------
# (a) registry + classification/key-scope presence
# ---------------------------------------------------------------------------


def test_op_registered():
    assert _OP_NAME in _REGISTRY


def test_op_classification_and_key_scope_present():
    from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
    from coordinator_core.op_scopes import _OP_KEY_SCOPE

    assert OP_CLASSIFICATION[_OP_NAME] == OpClass.MUTATING
    assert _OP_KEY_SCOPE[_OP_NAME] == "common_dir"


# ---------------------------------------------------------------------------
# (b) no authorship gate — delivery into a handoff the caller did not author
# ---------------------------------------------------------------------------


def test_delivery_into_handoff_not_authored_by_caller_succeeds(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-test.md", authoring_session=_AUTHOR_SESSION)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)
    assert _DELIVERING_SESSION != _AUTHOR_SESSION

    result = _run(_handler(_base_params(hpath), repo_root=repo / ".git"))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["session_id"] == _DELIVERING_SESSION
    assert result["commit_sha"]

    after = hpath.read_text(encoding="utf-8")
    assert _PROPAGATED_SECTION_HEADING in after
    assert "example-market-data-repo-em" in after


# ---------------------------------------------------------------------------
# (c) repeated deliveries append under one canonical heading
# ---------------------------------------------------------------------------


def test_repeated_deliveries_append_single_heading(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-repeat.md")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    r1 = _run(_handler(_base_params(hpath, summary="first delivery"), repo_root=repo / ".git"))
    assert r1["exit_code"] == 0, r1
    r2 = _run(_handler(_base_params(hpath, summary="second delivery"), repo_root=repo / ".git"))
    assert r2["exit_code"] == 0, r2

    after = hpath.read_text(encoding="utf-8")
    assert after.count(_PROPAGATED_SECTION_HEADING) == 1
    assert "first delivery" in after
    assert "second delivery" in after


# ---------------------------------------------------------------------------
# (d) frontmatter byte-identical after delivery
# ---------------------------------------------------------------------------


def test_frontmatter_byte_identical_after_delivery(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-fm.md")
    original = hpath.read_text(encoding="utf-8")
    fm_before = original.split("---", 2)[1]
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    result = _run(_handler(_base_params(hpath), repo_root=repo / ".git"))
    assert result["exit_code"] == 0, result

    after = hpath.read_text(encoding="utf-8")
    fm_after = after.split("---", 2)[1]
    assert fm_after == fm_before


# ---------------------------------------------------------------------------
# (e) AC12 — write lands as a git commit scoped to exactly the target file,
#     with Nature/Delivered-By trailers and NEITHER Session-Id NOR
#     Deliverable-Id, even with the delivering session's env vars set.
# ---------------------------------------------------------------------------


def test_write_lands_as_scoped_commit_without_session_or_deliverable_trailers(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-commit.md")
    rel_path = f"state/handoffs/{hpath.name}"
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _DELIVERING_SESSION)

    before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    result = _run(_handler(_base_params(hpath), repo_root=repo / ".git"))
    assert result["exit_code"] == 0, result
    after_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    assert after_head != before_head
    assert result["commit_sha"] == after_head

    # Working tree is clean — the commit captured exactly our write.
    status = _git(repo, "status", "--porcelain").stdout
    assert status.strip() == ""

    # Scoped to exactly the target path.
    stat = _git(repo, "show", "--stat", "--format=", "HEAD").stdout
    assert rel_path in stat
    changed_files = _git(repo, "diff", "--name-only", f"{before_head}..HEAD").stdout.split()
    assert changed_files == [rel_path]

    message = _git(repo, "log", "-1", "--format=%B", "HEAD").stdout
    assert "Nature: peer-delivery" in message
    assert "Delivered-By: example-market-data-repo-em" in message
    assert "Session-Id:" not in message
    assert "Deliverable-Id:" not in message


# ---------------------------------------------------------------------------
# (f)-(h) AC12 preconditions — one test per abort path
# ---------------------------------------------------------------------------


def test_refuses_when_target_file_is_dirty(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-dirty.md")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    # Holder's own uncommitted edit to the SAME target file.
    hpath.write_text(hpath.read_text(encoding="utf-8") + "\nHolder's own in-progress note.\n", encoding="utf-8")

    result = _run(_handler(_base_params(hpath), repo_root=repo / ".git"))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "uncommitted changes" in result["error"]
    assert _PROPAGATED_SECTION_HEADING not in hpath.read_text(encoding="utf-8")
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_refuses_during_in_progress_merge(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-merge.md")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    git_dir = repo / ".git"
    (git_dir / "MERGE_HEAD").write_text(
        _git(repo, "rev-parse", "HEAD").stdout.strip() + "\n", encoding="utf-8"
    )
    try:
        result = _run(_handler(_base_params(hpath), repo_root=repo / ".git"))
    finally:
        (git_dir / "MERGE_HEAD").unlink(missing_ok=True)

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "merge is in progress" in result["error"]
    assert _PROPAGATED_SECTION_HEADING not in hpath.read_text(encoding="utf-8")


def test_refuses_on_detached_head(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-detached.md")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", head_sha)

    result = _run(_handler(_base_params(hpath), repo_root=repo / ".git"))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "detached" in result["error"]
    assert _PROPAGATED_SECTION_HEADING not in hpath.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (i)/(j) status handling
# ---------------------------------------------------------------------------


def test_legacy_consumed_status_accepted(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-consumed.md", status="consumed")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    result = _run(_handler(_base_params(hpath), repo_root=repo / ".git"))
    assert result["exit_code"] == 0, result


def test_refuses_open_status(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-open.md", status="open")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    result = _run(_handler(_base_params(hpath), repo_root=repo / ".git"))
    assert result["exit_code"] == 1
    assert result["applied"] is False


# ---------------------------------------------------------------------------
# (k) archive/handoffs/ refused
# ---------------------------------------------------------------------------


def test_refuses_archive_target(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    archived = repo / "archive" / "handoffs" / "2026-07" / "old.md"
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_text(
        "---\ntitle: \"Archived\"\nstatus: consumed\nauthoring_session: "
        f"{_AUTHOR_SESSION}\n---\n\n# Body.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    result = _run(_handler(_base_params(archived), repo_root=repo / ".git"))
    assert result["exit_code"] == 1
    assert "escapes state/handoffs" in result["error"]


# ---------------------------------------------------------------------------
# (l) required-param / kind validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"summary": ""},
        {"summary": "   "},
        {"note": ""},
        {"note": "   "},
        {"slug": ""},
        {"kind": "Retracted"},
    ],
)
def test_refuses_invalid_params(tmp_path, monkeypatch, overrides):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-invalid.md")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    result = _run(_handler(_base_params(hpath, **overrides), repo_root=repo / ".git"))
    assert result["exit_code"] == 1
    assert result["applied"] is False


# ---------------------------------------------------------------------------
# (m) net-growth cap
# ---------------------------------------------------------------------------


def test_refuses_over_net_growth_cap(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-cap.md")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    result = _run(_handler(
        _base_params(hpath, note="x" * 9000),
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "net-growth cap" in result["error"]


# ---------------------------------------------------------------------------
# (n) invisible unicode / heading-shaped / delimiter-shaped content
# ---------------------------------------------------------------------------


def test_refuses_invisible_unicode(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-zw.md")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    result = _run(_handler(
        _base_params(hpath, note="looks clean​but isn't"),
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "invisible" in result["error"]


def test_refuses_heading_shaped_note(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-heading.md")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    result = _run(_handler(
        _base_params(hpath, note="## Sneaky New Section\n\nmore text"),
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "heading line" in result["error"]


def test_refuses_delimiter_shaped_note(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-delim.md")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    result = _run(_handler(
        _base_params(hpath, note="text\n---\nmore text"),
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "delimiter" in result["error"]


# ---------------------------------------------------------------------------
# (n2) `slug` gets the same content checks as summary/note (F3) — Delivered-By:
# {slug} is interpolated straight into the delivery commit message.
# ---------------------------------------------------------------------------


def test_refuses_invisible_unicode_slug(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-slug-zw.md")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    result = _run(_handler(
        _base_params(hpath, slug="looks-clean​but-isnt"),
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "invisible" in result["error"]


def test_refuses_heading_shaped_slug(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-slug-heading.md")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    result = _run(_handler(
        _base_params(hpath, slug="## Sneaky Section"),
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "heading line" in result["error"]


def test_refuses_delimiter_shaped_slug(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-slug-delim.md")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    result = _run(_handler(
        _base_params(hpath, slug="text\n---\nmore"),
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "delimiter" in result["error"]


def test_refuses_embedded_newline_slug(tmp_path, monkeypatch):
    """A slug with an embedded newline (but no delimiter/heading shape) could
    inject a spoofed trailer-shaped line into the commit message's
    `Delivered-By: {slug}` interpolation — refused independent of the other
    three checks."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-slug-newline.md")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    result = _run(_handler(
        _base_params(hpath, slug="example-market-data-repo-em\nSession-Id: spoofed-session"),
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "newline" in result["error"]


# ---------------------------------------------------------------------------
# (o) writes/commits nothing on every refusal path
# ---------------------------------------------------------------------------


def test_writes_nothing_on_refusal(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-noop.md", status="open")
    before_text = hpath.read_text(encoding="utf-8")
    before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    result = _run(_handler(_base_params(hpath), repo_root=repo / ".git"))

    assert result["exit_code"] == 1
    assert hpath.read_text(encoding="utf-8") == before_text
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before_head


# ---------------------------------------------------------------------------
# (o2) F2 — an injected `_commit_delivery` failure (AFTER `locked_rmw` has
# already rewritten the file on disk) rolls the target file back to its
# pre-mutation content and leaves HEAD untouched. AC12's "never leaves a
# dirty tree" guarantee must hold on the failure path too, not only the
# refusal-before-mutation path `test_writes_nothing_on_refusal` covers.
# ---------------------------------------------------------------------------


def test_commit_delivery_failure_restores_pre_mutation_content(tmp_path, monkeypatch):
    import coordinator_core.ops.propagate_body as propagate_body_mod

    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-rollback.md")
    before_text = hpath.read_text(encoding="utf-8")
    before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    real_run_git = propagate_body_mod._run_git

    def _failing_run_git(args, cwd, env=None):
        if args and args[0] == "write-tree":
            return subprocess.CompletedProcess(
                args=["git"] + list(args), returncode=1, stdout="", stderr="simulated disk-full write-tree failure",
            )
        return real_run_git(args, cwd, env=env)

    monkeypatch.setattr(propagate_body_mod, "_run_git", _failing_run_git)

    result = _run(_handler(_base_params(hpath), repo_root=repo / ".git"))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "rolled back" in result["error"]
    assert "write-tree" in result["error"]
    # The working tree is restored to exactly its pre-delivery content...
    assert hpath.read_text(encoding="utf-8") == before_text
    # ...and unstaged (the `git add` _commit_delivery performed before the
    # injected write-tree failure must be undone too).
    status = _git(repo, "status", "--porcelain", "--", "state/handoffs/2026-08-01-rollback.md").stdout
    assert status.strip() == "", f"expected clean status, got: {status!r}"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before_head


# ---------------------------------------------------------------------------
# (p) subagent-context env does NOT block the delivery commit (documented,
#     bounded relaxation of block_subagent_commit's intent — module
#     docstring point 4).
# ---------------------------------------------------------------------------


def test_subagent_context_env_does_not_block_delivery_commit(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-subagent.md")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)
    # Empirically-observed subagent marker (see handoff_correct_body.py's own
    # module docstring for the same finding, applied there to session-id
    # resolution rather than to a commit-authority gate).
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")

    result = _run(_handler(_base_params(hpath), repo_root=repo / ".git"))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["commit_sha"]


# ---------------------------------------------------------------------------
# (q) B2 (AC8) — plan.propagate: same module, second target root
# (docs/plans/ instead of state/handoffs/), no status gate.
# ---------------------------------------------------------------------------


def test_plan_op_registered():
    assert _PLAN_OP_NAME in _REGISTRY


def test_plan_op_classification_and_key_scope_present():
    from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
    from coordinator_core.op_scopes import _OP_KEY_SCOPE

    assert OP_CLASSIFICATION[_PLAN_OP_NAME] == OpClass.MUTATING
    assert _OP_KEY_SCOPE[_PLAN_OP_NAME] == "common_dir"


def test_plan_propagate_delivery_succeeds(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    ppath = _seed_plan(repo, "2026-08-01-test-plan.md", status="draft")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    plan_handler = _REGISTRY[_PLAN_OP_NAME]
    result = _run(plan_handler(_plan_base_params(ppath), repo_root=repo / ".git"))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["commit_sha"]

    after = ppath.read_text(encoding="utf-8")
    assert _PROPAGATED_SECTION_HEADING in after
    assert "example-market-data-repo-em" in after

    # Frontmatter untouched, and the delivery landed as its own scoped commit.
    fm_before = "title: \"Test Plan\"\nstatus: draft\n"
    assert fm_before in after
    status = _git(repo, "status", "--porcelain").stdout
    assert status.strip() == ""


def test_plan_propagate_refuses_archive_target(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    archived = repo / "archive" / "specs" / "2026-08" / "old-plan.md"
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_text(
        "---\ntitle: \"Archived Plan\"\nstatus: implemented\n---\n\n# Body.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    plan_handler = _REGISTRY[_PLAN_OP_NAME]
    result = _run(plan_handler(_plan_base_params(archived), repo_root=repo / ".git"))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "escapes docs/plans" in result["error"]


def test_plan_propagate_refuses_missing_plan_path_param(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    ppath = _seed_plan(repo, "2026-08-01-missing-param.md")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    plan_handler = _REGISTRY[_PLAN_OP_NAME]
    params = _plan_base_params(ppath)
    del params["plan_path"]

    result = _run(plan_handler(params, repo_root=repo / ".git"))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "plan_path" in result["error"]


def test_plan_propagate_repeated_deliveries_append_single_heading(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    ppath = _seed_plan(repo, "2026-08-01-plan-repeat.md")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)

    plan_handler = _REGISTRY[_PLAN_OP_NAME]
    r1 = _run(plan_handler(_plan_base_params(ppath, summary="first delivery"), repo_root=repo / ".git"))
    assert r1["exit_code"] == 0, r1
    r2 = _run(plan_handler(_plan_base_params(ppath, summary="second delivery"), repo_root=repo / ".git"))
    assert r2["exit_code"] == 0, r2

    after = ppath.read_text(encoding="utf-8")
    assert after.count(_PROPAGATED_SECTION_HEADING) == 1
    assert "first delivery" in after
    assert "second delivery" in after


# ---------------------------------------------------------------------------
# Shared-index contamination — regression lock.
#
# `git add -- <path>` scopes what the op STAGES, but `git write-tree` against
# the SHARED index serializes every path a concurrent peer already staged.
# Observed 2026-08-07 in the live tree: a delivery into one handoff landed 24
# files, 23 of them a peer's staged work, under this op's own subject line.
# The op now builds its tree in a private GIT_INDEX_FILE seeded from HEAD.
# ---------------------------------------------------------------------------


def test_peer_staged_paths_do_not_leak_into_delivery_commit(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-peer-index.md")
    rel_path = f"state/handoffs/{hpath.name}"
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _DELIVERING_SESSION)

    # A concurrent peer stages unrelated work into the shared index — an
    # addition and a modification to an already-tracked file.
    peer_new = repo / "peer_new.py"
    peer_new.write_text("# peer's in-flight work\n", encoding="utf-8")
    peer_tracked = repo / "state" / "handoffs" / ".gitkeep"
    peer_tracked.write_text("peer edit\n", encoding="utf-8")
    _git(repo, "add", "--", "peer_new.py", "state/handoffs/.gitkeep")

    staged_before = set(_git(repo, "diff", "--cached", "--name-only").stdout.split())
    assert staged_before == {"peer_new.py", "state/handoffs/.gitkeep"}

    before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    result = _run(_handler(_base_params(hpath), repo_root=repo / ".git"))
    assert result["exit_code"] == 0, result

    # The commit carries the delivery target and nothing else.
    changed_files = _git(repo, "diff", "--name-only", f"{before_head}..HEAD").stdout.split()
    assert changed_files == [rel_path], (
        f"peer-staged paths leaked into the delivery commit: {changed_files}"
    )

    # The peer's staging survives untouched — the op neither committed it nor
    # unstaged it.
    staged_after = set(_git(repo, "diff", "--cached", "--name-only").stdout.split())
    assert staged_after == staged_before


def test_rollback_does_not_unstage_holder_staged_target(tmp_path, monkeypatch):
    """A failed delivery must not reset the shared index: the op no longer
    stages there, so a `git reset -- <path>` would destroy the HOLDER's own
    staged content rather than undoing anything of the op's."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(repo, "2026-08-01-rollback-index.md")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _DELIVERING_SESSION)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _DELIVERING_SESSION)

    peer_new = repo / "peer_staged.py"
    peer_new.write_text("# holder's staged work\n", encoding="utf-8")
    _git(repo, "add", "--", "peer_staged.py")
    staged_before = set(_git(repo, "diff", "--cached", "--name-only").stdout.split())
    assert "peer_staged.py" in staged_before

    original = hpath.read_text(encoding="utf-8")

    import coordinator_core.ops.propagate_body as _pb

    def _boom(*_args, **_kwargs):
        return None, "simulated commit-tree failure"

    monkeypatch.setattr(_pb, "_commit_delivery", _boom)

    result = _run(_handler(_base_params(hpath), repo_root=repo / ".git"))
    assert result["exit_code"] == 1
    assert result["applied"] is False

    # Body restored, and the holder's staging is intact.
    assert hpath.read_text(encoding="utf-8") == original
    staged_after = set(_git(repo, "diff", "--cached", "--name-only").stdout.split())
    assert staged_after == staged_before

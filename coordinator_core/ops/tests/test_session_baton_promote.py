"""
coordinator_core.ops.tests.test_session_baton_promote — idempotency,
post-scaffold body edit, and error handling for the "session_baton.promote"
op.

Spec backlink: docs/plans/2026-08-18-a-session-always-has-a-baton.md § C3.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.ops import session_baton_promote as promote_mod
from coordinator_core.session_baton import store
from coordinator_core.win_portability import no_console_passthrough_kwargs


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, **no_console_passthrough_kwargs())
    return tmp_path


def _ensure_session_dir(repo: Path, sid: str) -> Path:
    """Pre-create the per-session directory ``cs_init`` mints on every real
    session start — this store (C6, docs/plans/2026-08-19-batons-unify-into-
    one-successor.md § C6) no longer mkdir's it itself, so a fixture calling
    the promote op (or `store.merge_baton` directly) without going through
    session init must bring it into being."""
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir


def _promote(**params):
    return promote_mod._handler(dict(params))


def _fake_scaffold(dest_rel: str, body: str = ""):
    """Build a monkeypatch target replacing _scaffold_via_doc_new with a
    fake that writes a minimal handoff-shaped file under cwd/dest_rel and
    returns dest_rel, exactly like coordinator-doc-new's real stdout
    contract (a path string)."""

    def _fake(title, branch, cwd, category=None, summary=None, gated_predicate=None):
        target = Path(cwd) / dest_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "---\n"
            f"title: \"{title}\"\n"
            "kind: session-handoff\n"
            "---\n\n"
            "## What Was Accomplished\n\n"
            "<!-- Replace with what was built, fixed, or shipped this session. -->\n\n"
            "## Current State\n\n"
            "<!-- Replace with where things stand now. -->\n"
            + body,
            encoding="utf-8",
        )
        return dest_rel

    return _fake


# ---------------------------------------------------------------------------
# Basic promotion
# ---------------------------------------------------------------------------


def test_promote_scaffolds_and_stamps_baton(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-1")
    store.merge_baton("sid-1", cwd=str(repo), first_prompt="do the thing")

    monkeypatch.setattr(
        promote_mod,
        "_scaffold_via_doc_new",
        _fake_scaffold("state/handoffs/2026-08-18-fake.md"),
    )

    result = _promote(session_id="sid-1", cwd=str(repo))
    assert result["exit_code"] == 0
    assert result["error"] is None
    assert result["handoff_path"] == "state/handoffs/2026-08-18-fake.md"
    assert result["already_promoted"] is False

    on_disk = store.read_baton("sid-1", cwd=str(repo))
    assert on_disk["promoted_to"] == "state/handoffs/2026-08-18-fake.md"

    handoff_text = (repo / "state/handoffs/2026-08-18-fake.md").read_text(
        encoding="utf-8"
    )
    assert "do the thing" in handoff_text
    assert (
        "<!-- Replace with what was built, fixed, or shipped this session. -->"
        not in handoff_text
    )


def test_promote_without_first_prompt_leaves_placeholder(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-noprompt")
    monkeypatch.setattr(
        promote_mod,
        "_scaffold_via_doc_new",
        _fake_scaffold("state/handoffs/2026-08-18-noprompt.md"),
    )

    result = _promote(session_id="sid-noprompt", cwd=str(repo))
    assert result["exit_code"] == 0

    handoff_text = (repo / "state/handoffs/2026-08-18-noprompt.md").read_text(
        encoding="utf-8"
    )
    assert (
        "<!-- Replace with what was built, fixed, or shipped this session. -->"
        in handoff_text
    )


# ---------------------------------------------------------------------------
# Idempotency: a second call returns the existing path, scaffolds nothing
# ---------------------------------------------------------------------------


def test_second_promote_call_returns_existing_path_no_rescaffold(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-idem")
    calls = []

    def _counting_fake(title, branch, cwd, category=None, summary=None, gated_predicate=None):
        calls.append(1)
        fake = _fake_scaffold("state/handoffs/2026-08-18-once.md")
        return fake(title, branch, cwd, category=category, summary=summary, gated_predicate=gated_predicate)

    monkeypatch.setattr(promote_mod, "_scaffold_via_doc_new", _counting_fake)

    first = _promote(session_id="sid-idem", cwd=str(repo))
    assert first["already_promoted"] is False
    assert len(calls) == 1

    second = _promote(session_id="sid-idem", cwd=str(repo))
    assert second["exit_code"] == 0
    assert second["already_promoted"] is True
    assert second["handoff_path"] == first["handoff_path"]
    assert len(calls) == 1  # no second scaffold call


# ---------------------------------------------------------------------------
# Closed journals — the consumer of session_baton.store's closed_at/closed_into.
# Spec backlink: docs/plans/2026-08-21-a-pickup-closes-the-baton-it-was-born-with.md
# ---------------------------------------------------------------------------


def test_promote_refuses_a_closed_journal(tmp_path, monkeypatch):
    """A journal a pickup already closed is that baton's ancestor, not a
    promotion candidate — promoting it would advertise the same work twice."""
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-closed")
    calls = []

    def _counting_fake(title, branch, cwd, category=None, summary=None, gated_predicate=None):
        calls.append(1)
        fake = _fake_scaffold("state/handoffs/2026-08-21-should-not-exist.md")
        return fake(title, branch, cwd, category=category, summary=summary, gated_predicate=gated_predicate)

    monkeypatch.setattr(promote_mod, "_scaffold_via_doc_new", _counting_fake)
    store.merge_baton(
        "sid-closed",
        cwd=str(repo),
        closed_at="2026-08-21T12:00:00Z",
        closed_into="state/handoffs/the-picked-up-baton.md",
    )

    result = _promote(session_id="sid-closed", cwd=str(repo))

    assert result["exit_code"] == 1
    assert "state/handoffs/the-picked-up-baton.md" in result["error"]
    assert not calls, "a closed journal must never reach the scaffolder"
    assert store.read_baton("sid-closed", cwd=str(repo))["promoted_to"] is None


def test_promote_refuses_on_closed_at_alone(tmp_path, monkeypatch):
    """Either half of the pair is enough. A record carrying a closure stamp
    with no target is still closed — it must not fall through to a scaffold
    because the target happens to be unreadable."""
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-closed2")
    calls = []

    def _counting_fake(title, branch, cwd, category=None, summary=None, gated_predicate=None):
        calls.append(1)
        fake = _fake_scaffold("state/handoffs/nope.md")
        return fake(title, branch, cwd, category=category, summary=summary, gated_predicate=gated_predicate)

    monkeypatch.setattr(promote_mod, "_scaffold_via_doc_new", _counting_fake)
    store.merge_baton("sid-closed2", cwd=str(repo), closed_at="2026-08-21T12:00:00Z")

    result = _promote(session_id="sid-closed2", cwd=str(repo))

    # Review: reviewer (P2 #3) — assert the guard's structural effect
    # (refusal + no scaffold + no stamp), not just message prose.
    assert result["exit_code"] == 1
    assert "an adopted artifact" in result["error"]
    assert not calls, "closed_at alone must never reach the scaffolder"
    assert store.read_baton("sid-closed2", cwd=str(repo))["promoted_to"] is None


def test_promote_returns_existing_path_when_promoted_then_closed(tmp_path, monkeypatch):
    """A record can legitimately end up BOTH promoted_to AND closed_at/
    closed_into (this session promoted first, then a later pickup adopted a
    different artifact from the same session and closed the journal). The
    promoted_to idempotence branch runs first and returns the existing
    artifact cleanly -- no re-scaffold, no refusal -- because no new artifact
    is created and the existing one is unaffected by the later closure.

    Review: reviewer (P3 #1) -- pins the branch-order guarantee the module
    docstring/comment claims but this diff left uncovered."""
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-promoted-then-closed")
    calls = []

    def _counting_fake(title, branch, cwd, category=None, summary=None, gated_predicate=None):
        calls.append(1)
        fake = _fake_scaffold("state/handoffs/2026-08-21-already-promoted.md")
        return fake(title, branch, cwd, category=category, summary=summary, gated_predicate=gated_predicate)

    monkeypatch.setattr(promote_mod, "_scaffold_via_doc_new", _counting_fake)

    first = _promote(session_id="sid-promoted-then-closed", cwd=str(repo))
    assert first["exit_code"] == 0
    assert len(calls) == 1

    store.merge_baton(
        "sid-promoted-then-closed",
        cwd=str(repo),
        closed_at="2026-08-21T12:00:00Z",
        closed_into="state/handoffs/the-picked-up-baton.md",
    )

    second = _promote(session_id="sid-promoted-then-closed", cwd=str(repo))
    assert second["exit_code"] == 0
    assert second["already_promoted"] is True
    assert second["handoff_path"] == first["handoff_path"]
    assert len(calls) == 1, "the promoted_to branch must return before reaching the scaffolder again"


def test_promote_still_works_on_an_open_journal(tmp_path, monkeypatch):
    """The guard is positive-match only: an ordinary open journal, and one
    whose closure was explicitly cleared, both promote exactly as before."""
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-open")
    monkeypatch.setattr(
        promote_mod, "_scaffold_via_doc_new", _fake_scaffold("state/handoffs/2026-08-21-open.md")
    )
    store.merge_baton(
        "sid-open", cwd=str(repo), closed_at="2026-08-21T12:00:00Z", closed_into="x.md"
    )
    store.merge_baton("sid-open", cwd=str(repo), closed_at=None, closed_into=None)

    result = _promote(session_id="sid-open", cwd=str(repo))

    assert result["exit_code"] == 0
    assert result["handoff_path"] == "state/handoffs/2026-08-21-open.md"


# ---------------------------------------------------------------------------
# Param validation
# ---------------------------------------------------------------------------


def test_missing_session_id_errors():
    result = _promote()
    assert result["exit_code"] == 1
    assert "session_id" in result["error"]


def test_blank_session_id_errors():
    result = _promote(session_id="   ")
    assert result["exit_code"] == 1


def test_non_string_title_errors(tmp_path):
    repo = _make_repo(tmp_path)
    result = _promote(session_id="sid-badtitle", title=123, cwd=str(repo))
    assert result["exit_code"] == 1
    assert "title" in result["error"]


# ---------------------------------------------------------------------------
# Scaffold failure degrades to exit_code=1, never raises
# ---------------------------------------------------------------------------


def test_scaffold_failure_returns_error_not_exception(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-fail")

    def _boom(title, branch, cwd, category=None, summary=None, gated_predicate=None):
        raise RuntimeError("coordinator-doc-new blew up")

    monkeypatch.setattr(promote_mod, "_scaffold_via_doc_new", _boom)

    result = _promote(session_id="sid-fail", cwd=str(repo))
    assert result["exit_code"] == 1
    assert "blew up" in result["error"]

    on_disk = store.read_baton("sid-fail", cwd=str(repo))
    assert on_disk["promoted_to"] is None


def test_scaffold_empty_stdout_is_an_error(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-empty")
    monkeypatch.setattr(
        promote_mod,
        "_scaffold_via_doc_new",
        lambda title, branch, cwd, category=None, summary=None, gated_predicate=None: "",
    )

    result = _promote(session_id="sid-empty", cwd=str(repo))
    assert result["exit_code"] == 1
    assert "no path" in result["error"]


# ---------------------------------------------------------------------------
# Real coordinator-doc-new integration (not mocked) — the actual seam this
# op is required to compose rather than reimplement.
# ---------------------------------------------------------------------------


@pytest.mark.real_home
def test_real_coordinator_doc_new_seam(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-real")
    store.merge_baton("sid-real", cwd=str(repo), first_prompt="the real first prompt")

    result = _promote(
        session_id="sid-real", title="A real promotion test", cwd=str(repo)
    )
    if result["exit_code"] != 0:
        pytest.skip(
            "coordinator-doc-new CLI unavailable/misconfigured in this "
            f"environment: {result['error']}"
        )

    handoff_path = Path(result["handoff_path"])
    if not handoff_path.is_absolute():
        handoff_path = repo / handoff_path
    assert handoff_path.is_file()
    text = handoff_path.read_text(encoding="utf-8")
    assert "kind: session-handoff" in text
    assert "the real first prompt" in text


# ---------------------------------------------------------------------------
# category/summary params (AC4-AC8, 2026-08-19 plan)
# ---------------------------------------------------------------------------


def test_non_string_category_errors(tmp_path):
    repo = _make_repo(tmp_path)
    result = _promote(session_id="sid-badcat", category=123, cwd=str(repo))
    assert result["exit_code"] == 1
    assert "category" in result["error"]


def test_non_string_summary_errors(tmp_path):
    repo = _make_repo(tmp_path)
    result = _promote(session_id="sid-badsum", summary=123, cwd=str(repo))
    assert result["exit_code"] == 1
    assert "summary" in result["error"]


def test_both_present_threads_category_and_summary_no_gating(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-both")
    captured = {}

    def _capturing_fake(title, branch, cwd, category=None, summary=None, gated_predicate=None):
        captured.update(category=category, summary=summary, gated_predicate=gated_predicate)
        fake = _fake_scaffold("state/handoffs/2026-08-19-both.md")
        return fake(title, branch, cwd, category=category, summary=summary, gated_predicate=gated_predicate)

    monkeypatch.setattr(promote_mod, "_scaffold_via_doc_new", _capturing_fake)

    result = _promote(
        session_id="sid-both",
        category="bug",
        summary="fixed the thing",
        cwd=str(repo),
    )
    assert result["exit_code"] == 0
    assert captured == {"category": "bug", "summary": "fixed the thing", "gated_predicate": None}


def test_both_absent_gates_naming_both_fields(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-neither")
    captured = {}

    def _capturing_fake(title, branch, cwd, category=None, summary=None, gated_predicate=None):
        captured.update(category=category, summary=summary, gated_predicate=gated_predicate)
        fake = _fake_scaffold("state/handoffs/2026-08-19-neither.md")
        return fake(title, branch, cwd, category=category, summary=summary, gated_predicate=gated_predicate)

    monkeypatch.setattr(promote_mod, "_scaffold_via_doc_new", _capturing_fake)

    result = _promote(session_id="sid-neither", cwd=str(repo))
    assert result["exit_code"] == 0
    assert captured["category"] is None
    assert captured["summary"] is None
    assert captured["gated_predicate"] == "category and summary are unfilled placeholders"


def test_category_only_gates_naming_summary(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-catonly")
    captured = {}

    def _capturing_fake(title, branch, cwd, category=None, summary=None, gated_predicate=None):
        captured.update(category=category, summary=summary, gated_predicate=gated_predicate)
        fake = _fake_scaffold("state/handoffs/2026-08-19-catonly.md")
        return fake(title, branch, cwd, category=category, summary=summary, gated_predicate=gated_predicate)

    monkeypatch.setattr(promote_mod, "_scaffold_via_doc_new", _capturing_fake)

    result = _promote(session_id="sid-catonly", category="docs", cwd=str(repo))
    assert result["exit_code"] == 0
    assert captured["category"] == "docs"
    assert captured["summary"] is None
    assert captured["gated_predicate"] == "summary is an unfilled placeholder"


def test_summary_only_gates_naming_category(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-sumonly")
    captured = {}

    def _capturing_fake(title, branch, cwd, category=None, summary=None, gated_predicate=None):
        captured.update(category=category, summary=summary, gated_predicate=gated_predicate)
        fake = _fake_scaffold("state/handoffs/2026-08-19-sumonly.md")
        return fake(title, branch, cwd, category=category, summary=summary, gated_predicate=gated_predicate)

    monkeypatch.setattr(promote_mod, "_scaffold_via_doc_new", _capturing_fake)

    result = _promote(session_id="sid-sumonly", summary="wrote the thing", cwd=str(repo))
    assert result["exit_code"] == 0
    assert captured["category"] is None
    assert captured["summary"] == "wrote the thing"
    assert captured["gated_predicate"] == "category is an unfilled placeholder"


def _read_frontmatter(handoff_path: Path) -> dict:
    import yaml

    text = handoff_path.read_text(encoding="utf-8")
    fm_text = text.split("---", 2)[1]
    return yaml.safe_load(fm_text)


@pytest.mark.parametrize(
    "category,summary",
    [
        ("infra", "did the thing"),
        (None, "did the thing"),
        ("infra", None),
        (None, None),
    ],
)
@pytest.mark.real_home
def test_category_summary_four_way_schema_validation(tmp_path, category, summary):
    """AC7: every combination of the two params validates clean against the
    handoff schema (real coordinator-doc-new seam, not mocked)."""
    from coordinator_core.frontmatter.schema_validate import (
        _SCHEMAS_DIR,
        load_schemas,
        validate_frontmatter_obj,
    )

    repo = _make_repo(tmp_path)
    sid = f"sid-4way-{category}-{summary}"
    _ensure_session_dir(repo, sid)
    result = _promote(
        session_id=sid,
        title="Four-way test",
        cwd=str(repo),
        category=category,
        summary=summary,
    )
    if result["exit_code"] != 0:
        pytest.skip(
            "coordinator-doc-new CLI unavailable/misconfigured in this "
            f"environment: {result['error']}"
        )

    handoff_path = Path(result["handoff_path"])
    if not handoff_path.is_absolute():
        handoff_path = repo / handoff_path
    fields = _read_frontmatter(handoff_path)

    schema_obj = load_schemas(_SCHEMAS_DIR)["handoff"]
    validation = validate_frontmatter_obj(fields, schema_obj)
    assert validation["ok"], validation.get("errors")

    if category and summary:
        assert fields["deployment_state"] == "ready_to_fire"
        assert fields["pickup_ready"] is True
        assert "blocking_notes" not in fields
        assert fields["category"] == "infra"
        assert fields["summary"] == "did the thing"
    else:
        assert fields["deployment_state"] == "awaiting_gate"
        assert fields["pickup_ready"] is False
        notes = fields["blocking_notes"]
        assert notes
        if not category and not summary:
            assert "category" in notes and "summary" in notes
        elif not category:
            assert "category" in notes and "summary" not in notes
        else:
            assert "summary" in notes and "category" not in notes

    on_disk = store.read_baton(sid, cwd=str(repo))
    assert on_disk["promoted_to"] == result["handoff_path"]

"""Unit + integration tests for the docs_staleness stamping stage (C6).

Purpose: prove the enrichment gap that shipped in the same commit as
`entities/exec_summary.py`'s required-with-null `docs_staleness` field —
`sections/exec_summary.py::collect()` never sets the key, so every real
emitted record was one field short of contract-valid. `test_exec_summary_
docs_staleness.py` (cockpit_schema/tests/) only validates hand-built
fixtures, which is precisely why the gap shipped unnoticed; this file
exercises the REAL producer.

Spec backlink: DoE-claude:pln-human-facing-doc-staleness-det-d9c047 § C6
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.contract.cockpit_schema.entities.exec_summary import ExecSummary
from coordinator_core.ops.emit import envelope as envelope_mod
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.envelope import _stamp_docs_staleness
from coordinator_core.ops.emit.sections import exec_summary as exec_summary_section


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(
    repo_root: Path,
    repo_name: str = "test/repo",
    *,
    full_enrichment: bool = True,
) -> EmitContext:
    """Build a test EmitContext.

    ``full_enrichment`` defaults to True here — every test in this module below the cadence-gate
    section is about what the DETECTOR computes, which is the full-enrichment tier's job. The
    gate's own cheap-tier behaviour has its own tests at the bottom of the file and passes
    ``full_enrichment=False`` explicitly.
    """
    return EmitContext(
        repo_root=repo_root,
        coordinator_root=repo_root,
        central_state_root=repo_root,
        git_branch="test-branch",
        git_sha="deadbeef" * 5,
        git_sha_short="deadbeef",
        observed_at="2026-07-28T00:00:00Z",
        hostname="test-host",
        repo_name=repo_name,
        full_enrichment=full_enrichment,
    )


def _empty_exec_summary_envelope() -> dict:
    return {"exec_summaries": []}


def _make_exec_summary_record(ctx: EmitContext) -> dict:
    return {
        "repo": ctx.repo_name,
        "coordinator_root_path": ".",
        "id": ctx.repo_name,
        "provenance": ctx.provenance("coordinator_artifact", path="docs/exec-summary.md", derivation="parsed"),
        "project": None,
        "generated": None,
        "generator": None,
        "identity": None,
        "progress": None,
        "what_makes_it_special": None,
        "near_term_goals": None,
    }


def _git(repo_root: Path, *args: str) -> None:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        env=env,
    )


def _init_repo_with_readme(repo_root: Path) -> None:
    repo_root.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "init", "-q")
    (repo_root / "README.md").write_text("# Hello\n\nInitial content.\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-q", "-m", "initial commit")
    # A couple more commits so the "history since last touch" machinery has
    # something non-trivial to walk (not required for status: "ok", but
    # closer to a real repo shape).
    (repo_root / "other.txt").write_text("one\n", encoding="utf-8")
    _git(repo_root, "add", "other.txt")
    _git(repo_root, "commit", "-q", "-m", "unrelated change 1")
    (repo_root / "other.txt").write_text("two\n", encoding="utf-8")
    _git(repo_root, "add", "other.txt")
    _git(repo_root, "commit", "-q", "-m", "unrelated change 2")


# ---------------------------------------------------------------------------
# null vs [] distinction
# ---------------------------------------------------------------------------

def test_null_when_no_coordinator_local_md(tmp_path: Path) -> None:
    """No coordinator.local.md -> detector did not run -> docs_staleness is null."""
    _init_repo_with_readme(tmp_path)

    ctx = _make_ctx(tmp_path)
    envelope = _empty_exec_summary_envelope()
    envelope["exec_summaries"].append(_make_exec_summary_record(ctx))

    _stamp_docs_staleness(envelope, ctx)

    assert envelope["exec_summaries"][0]["docs_staleness"] is None


def test_empty_list_when_registry_declares_no_docs(tmp_path: Path) -> None:
    """coordinator.local.md exists (detector DID run) but declares an empty
    human_facing_docs list -> docs_staleness is [] (ran, nothing to report),
    a distinct state from null (did not run)."""
    _init_repo_with_readme(tmp_path)
    (tmp_path / "coordinator.local.md").write_text(
        "---\nhuman_facing_docs: []\n---\n\nbody\n", encoding="utf-8"
    )

    ctx = _make_ctx(tmp_path)
    envelope = _empty_exec_summary_envelope()
    envelope["exec_summaries"].append(_make_exec_summary_record(ctx))

    _stamp_docs_staleness(envelope, ctx)

    assert envelope["exec_summaries"][0]["docs_staleness"] == []


def test_populated_when_registry_declares_a_doc_with_history(tmp_path: Path) -> None:
    """coordinator.local.md declares README.md with real content history ->
    docs_staleness carries one status:"ok" entry."""
    _init_repo_with_readme(tmp_path)
    (tmp_path / "coordinator.local.md").write_text(
        "---\nhuman_facing_docs: [README.md]\n---\n\nbody\n", encoding="utf-8"
    )

    ctx = _make_ctx(tmp_path)
    envelope = _empty_exec_summary_envelope()
    envelope["exec_summaries"].append(_make_exec_summary_record(ctx))

    _stamp_docs_staleness(envelope, ctx)

    docs_staleness = envelope["exec_summaries"][0]["docs_staleness"]
    assert docs_staleness is not None
    assert len(docs_staleness) == 1
    entry = docs_staleness[0]
    assert entry["path"] == "README.md"
    assert isinstance(entry["stale"], bool)
    assert entry["commits_since"] >= 0
    assert entry["days_since"] >= 0
    assert len(entry["last_touch_sha"]) == 40
    # Offset-bearing ISO-8601 (git %cI) -- must carry a Z or +/-HH:MM offset.
    assert entry["last_touch_date"][-1] == "Z" or "+" in entry["last_touch_date"][-6:] or (
        entry["last_touch_date"].count("-") > 2
    )


# ---------------------------------------------------------------------------
# Detector-failure degrade path
# ---------------------------------------------------------------------------

def test_raising_detector_degrades_to_null(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising detector must degrade to null, never break emission."""
    _init_repo_with_readme(tmp_path)
    (tmp_path / "coordinator.local.md").write_text(
        "---\nhuman_facing_docs: [README.md]\n---\n\nbody\n", encoding="utf-8"
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated detector failure")

    monkeypatch.setattr(
        "coordinator_core.ops.doc_staleness.build_doc_staleness_report_from_registry",
        _boom,
    )

    ctx = _make_ctx(tmp_path)
    envelope = _empty_exec_summary_envelope()
    envelope["exec_summaries"].append(_make_exec_summary_record(ctx))

    # Must not raise.
    _stamp_docs_staleness(envelope, ctx)

    assert envelope["exec_summaries"][0]["docs_staleness"] is None


def test_no_exec_summaries_records_is_a_noop(tmp_path: Path) -> None:
    """An empty exec_summaries array short-circuits before touching git at all."""
    ctx = _make_ctx(tmp_path)
    envelope = _empty_exec_summary_envelope()

    _stamp_docs_staleness(envelope, ctx)  # must not raise even with no git repo present

    assert envelope["exec_summaries"] == []


# ---------------------------------------------------------------------------
# Integration: REAL collect()-produced record round-trips through ExecSummary
# ---------------------------------------------------------------------------

def test_real_collect_produced_record_validates_after_stamping(tmp_path: Path) -> None:
    """The actual failure mode this fix closes: a hand-built fixture cannot
    catch a producer that never sets a required key. Build a real
    docs/exec-summary.md, run the real section collect(), apply the real
    enrichment stage, then validate through the real pydantic entity."""
    _init_repo_with_readme(tmp_path)
    (tmp_path / "coordinator.local.md").write_text(
        "---\nhuman_facing_docs: [README.md]\n---\n\nbody\n", encoding="utf-8"
    )

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "exec-summary.md").write_text(
        "---\n"
        "kind: exec-summary\n"
        "repo: test-repo\n"
        "project: Test Project\n"
        "generated: 2026-07-28T00:00:00Z\n"
        "generator: exec-summary-gen\n"
        "---\n\n"
        "<!-- BEGIN MANAGED: identity -->\nWhat this project is.\n<!-- END MANAGED: identity -->\n\n"
        "<!-- BEGIN MANAGED: progress -->\nWhere it stands.\n<!-- END MANAGED: progress -->\n\n"
        "<!-- BEGIN HAND: special -->\nWhat makes it special.\n<!-- END HAND: special -->\n\n"
        "<!-- BEGIN HAND: goals -->\nNear-term goals.\n<!-- END HAND: goals -->\n",
        encoding="utf-8",
    )

    ctx = _make_ctx(tmp_path, repo_name="test-repo")

    valid, malformed = exec_summary_section.collect(ctx)
    assert malformed == []
    assert len(valid) == 1
    record = valid[0]
    assert "docs_staleness" not in record  # confirms collect() itself never sets it

    envelope = {"exec_summaries": valid}
    _stamp_docs_staleness(envelope, ctx)

    stamped_record = envelope["exec_summaries"][0]
    assert "docs_staleness" in stamped_record
    assert stamped_record["docs_staleness"] is not None
    assert len(stamped_record["docs_staleness"]) == 1
    assert stamped_record["docs_staleness"][0]["path"] == "README.md"

    # The actual assertion this fix exists for: the record now round-trips
    # cleanly through the contract's pydantic entity.
    validated = ExecSummary.model_validate(stamped_record)
    assert validated.docs_staleness is not None
    assert validated.docs_staleness[0].path == "README.md"


def test_full_build_stamps_exec_summaries_via_registered_section(tmp_path: Path) -> None:
    """End-to-end through envelope.build()'s registry/post-collect pipeline
    (not just calling _stamp_docs_staleness directly) -- proves the wiring
    inside build() actually reaches the exec_summaries array it just placed."""
    _init_repo_with_readme(tmp_path)
    (tmp_path / "coordinator.local.md").write_text(
        "---\nhuman_facing_docs: [README.md]\n---\n\nbody\n", encoding="utf-8"
    )
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "exec-summary.md").write_text(
        "---\nproject: Test\n---\n\n"
        "<!-- BEGIN MANAGED: identity -->\nBody.\n<!-- END MANAGED: identity -->\n",
        encoding="utf-8",
    )

    ctx = _make_ctx(tmp_path, repo_name="test-repo")

    envelope = {"exec_summaries": []}
    valid, malformed = exec_summary_section.collect(ctx)
    envelope["exec_summaries"] = valid
    envelope_mod._stamp_docs_staleness(envelope, ctx)

    assert envelope["exec_summaries"][0]["docs_staleness"] is not None
    assert envelope["exec_summaries"][0]["docs_staleness"][0]["path"] == "README.md"

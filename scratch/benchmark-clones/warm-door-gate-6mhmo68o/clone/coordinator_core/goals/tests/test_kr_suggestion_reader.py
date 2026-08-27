"""Tests for the DR-130 KR-suggestion reader half of ``coordinator_core.goals.reassess_krs``.

Pins each invariant of the generalized optional suggestion-source contract
(``doe-claude:coordinator/schemas/kr-suggestion.schema.json``, ``DR-130``) against
this repo's reader: ``state/kr-suggestions/*.yaml`` resolution by goal-level `id:`
and `key_results[].id` (never a path/slug, never an index), unresolved-anchor
reporting, staleness detection, the append-only write path, per-file degrade-to-
warning, and dry-run's report-only contract. One test per invariant, named so a
failure names which invariant broke.

Spec backlink: pln-dr-130-kr-suggestion-source-th-32c84d
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.goals.reassess_krs import reassess


def _write_goal(
    goals_dir: Path,
    filename: str,
    *,
    goal_id: str,
    title: str = "Test goal",
    status: str = "active",
    kr_entries: list[str],
) -> Path:
    """Write a whole-document-YAML goal artifact (no '---' fence, C1 shape)."""
    lines = [
        f'id: "{goal_id}"',
        f'title: "{title}"',
        f"status: {status}",
        "key_results:",
    ]
    lines.extend(kr_entries)
    path = goals_dir / filename
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _kr(
    kr_id: str,
    text: str = "some unrelated key result text",
    status: str = "not-started",
    weekly_perceptible: str = "false",
) -> str:
    return (
        f"  - id: {kr_id}\n"
        f"    text: {text}\n"
        f"    status: {status}\n"
        f"    weekly_perceptible: {weekly_perceptible}"
    )


def _write_suggestion(
    suggestions_dir: Path,
    filename: str,
    *,
    goal_id: str,
    kr_id: str,
    proposed_status: str = "met",
    expected_current_status: str | None = None,
    rationale: str = "Standup transcript shows this KR is done.",
    producing_system: str = "test-producer",
    source_ref: str = "path/to/transcript.md",
    span: str | None = "14:22",
    recorded_at: str = "2026-08-03",
    status: str = "open",
) -> Path:
    lines = [
        "created: 2026-08-05",
        f'goal_id: "{goal_id}"',
        f"kr_id: {kr_id}",
        f"proposed_status: {proposed_status}",
    ]
    if expected_current_status is None:
        lines.append("expected_current_status: null")
    else:
        lines.append(f"expected_current_status: {expected_current_status}")
    lines.append(f'rationale: "{rationale}"')
    span_value = "null" if span is None else f'"{span}"'
    lines.append("provenance:")
    lines.append(f'  producing_system: "{producing_system}"')
    lines.append(f'  source_ref: "{source_ref}"')
    lines.append(f"  span: {span_value}")
    lines.append(f'  recorded_at: "{recorded_at}"')
    lines.append(f"status: {status}")
    suggestions_dir.mkdir(parents=True, exist_ok=True)
    path = suggestions_dir / filename
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_absent_kr_suggestions_directory_is_clean_skip(tmp_path: Path) -> None:
    """AC2: no state/kr-suggestions/ at all — no error, no warning noise."""
    repo_root = tmp_path
    goals_dir = repo_root / "state" / "goals"
    goals_dir.mkdir(parents=True)
    _write_goal(goals_dir, "g1.yaml", goal_id="goal-1", kr_entries=[_kr("kr-1")])

    result = reassess(goals_dir, dry_run=True, repo_root=repo_root)

    assert not any("kr-suggestion" in w.lower() for w in result["warnings"])
    assert "kr-suggestion" not in result["report"].lower()


def test_empty_kr_suggestions_directory_is_clean_skip(tmp_path: Path) -> None:
    """AC2: an empty state/kr-suggestions/ directory — no error, no warning noise."""
    repo_root = tmp_path
    goals_dir = repo_root / "state" / "goals"
    goals_dir.mkdir(parents=True)
    (repo_root / "state" / "kr-suggestions").mkdir(parents=True)
    _write_goal(goals_dir, "g1.yaml", goal_id="goal-1", kr_entries=[_kr("kr-1")])

    result = reassess(goals_dir, dry_run=True, repo_root=repo_root)

    assert not any("kr-suggestion" in w.lower() for w in result["warnings"])
    assert "kr-suggestion" not in result["report"].lower()


def test_goal_id_resolves_by_id_field_not_filename(tmp_path: Path) -> None:
    """AC3: a goal file whose stem deliberately differs from its `id:` still
    resolves — a slug-based shortcut would silently fail this."""
    repo_root = tmp_path
    goals_dir = repo_root / "state" / "goals"
    goals_dir.mkdir(parents=True)
    suggestions_dir = repo_root / "state" / "kr-suggestions"
    # Filename slug deliberately does NOT match the goal's real id:.
    _write_goal(
        goals_dir,
        "totally-different-filename-slug.yaml",
        goal_id="goal-real-canonical-id",
        kr_entries=[_kr("kr-1")],
    )
    _write_suggestion(
        suggestions_dir,
        "s1.yaml",
        goal_id="goal-real-canonical-id",
        kr_id="kr-1",
        proposed_status="met",
    )

    result = reassess(goals_dir, dry_run=True, repo_root=repo_root)

    assert "KR-SUGGESTION UNRESOLVED" not in result["report"]
    assert "KR-SUGGESTION [kr-1]: proposed=met" in result["report"]


def test_kr_id_resolves_by_id_not_index(tmp_path: Path) -> None:
    """AC4: KRs ordered so an index-based read would pick the wrong one — the
    suggestion's kr_id must resolve by the id field, not by position."""
    repo_root = tmp_path
    goals_dir = repo_root / "state" / "goals"
    goals_dir.mkdir(parents=True)
    suggestions_dir = repo_root / "state" / "kr-suggestions"
    # kr-target is NOT first in the list — an index-0 read would grab kr-decoy-a.
    _write_goal(
        goals_dir,
        "g1.yaml",
        goal_id="goal-1",
        kr_entries=[_kr("kr-decoy-a"), _kr("kr-decoy-b"), _kr("kr-target")],
    )
    _write_suggestion(
        suggestions_dir,
        "s1.yaml",
        goal_id="goal-1",
        kr_id="kr-target",
        proposed_status="met",
        rationale="Targets the third KR by id, not position.",
    )

    result = reassess(goals_dir, dry_run=True, repo_root=repo_root)

    assert "KR-SUGGESTION UNRESOLVED" not in result["report"]
    assert "KR-SUGGESTION [kr-target]: proposed=met" in result["report"]
    assert "KR-SUGGESTION [kr-decoy-a]" not in result["report"]
    assert "KR-SUGGESTION [kr-decoy-b]" not in result["report"]


def test_unresolvable_goal_id_is_reported(tmp_path: Path) -> None:
    """AC5: a suggestion whose goal_id matches no goal artifact is reported,
    never silently dropped."""
    repo_root = tmp_path
    goals_dir = repo_root / "state" / "goals"
    goals_dir.mkdir(parents=True)
    suggestions_dir = repo_root / "state" / "kr-suggestions"
    _write_goal(goals_dir, "g1.yaml", goal_id="goal-1", kr_entries=[_kr("kr-1")])
    _write_suggestion(
        suggestions_dir,
        "s1.yaml",
        goal_id="goal-does-not-exist",
        kr_id="kr-1",
    )

    result = reassess(goals_dir, dry_run=True, repo_root=repo_root)

    assert "KR-SUGGESTION UNRESOLVED" in result["report"]
    assert "goal-does-not-exist" in result["report"]
    assert "matches no state/goals/*.yaml id: field" in result["report"]


def test_unresolvable_kr_id_is_reported(tmp_path: Path) -> None:
    """AC5: a suggestion whose kr_id matches no KR on its (resolved) target
    goal is reported, never silently dropped."""
    repo_root = tmp_path
    goals_dir = repo_root / "state" / "goals"
    goals_dir.mkdir(parents=True)
    suggestions_dir = repo_root / "state" / "kr-suggestions"
    _write_goal(goals_dir, "g1.yaml", goal_id="goal-1", kr_entries=[_kr("kr-1")])
    _write_suggestion(
        suggestions_dir,
        "s1.yaml",
        goal_id="goal-1",
        kr_id="kr-does-not-exist",
    )

    result = reassess(goals_dir, dry_run=True, repo_root=repo_root)

    assert "KR-SUGGESTION UNRESOLVED" in result["report"]
    assert "kr-does-not-exist" in result["report"]
    assert "matches no key_results[].id on that goal" in result["report"]


def test_suggestion_resolving_onto_inactive_goal_is_reported_not_dropped(
    tmp_path: Path,
) -> None:
    """AC5 (invariant 4 read broadly): a suggestion whose goal_id/kr_id both
    resolve cleanly, but whose target goal is non-active, must still surface
    to the human rather than vanish because the per-goal loop skips inactive
    goals' report/write processing. The goal artifact itself must stay
    byte-unchanged — inactive goals never get the write-back pass."""
    repo_root = tmp_path
    goals_dir = repo_root / "state" / "goals"
    goals_dir.mkdir(parents=True)
    suggestions_dir = repo_root / "state" / "kr-suggestions"
    goal_file = _write_goal(
        goals_dir,
        "g1.yaml",
        goal_id="goal-1",
        status="closed",
        kr_entries=[_kr("kr-1", status="in-progress")],
    )
    _write_suggestion(
        suggestions_dir,
        "s1.yaml",
        goal_id="goal-1",
        kr_id="kr-1",
        proposed_status="met",
    )
    original_bytes = goal_file.read_bytes()

    result = reassess(goals_dir, dry_run=True, repo_root=repo_root)

    assert "KR-SUGGESTION NOT PRESENTED" in result["report"]
    assert "goal-1" in result["report"]
    assert "kr-1" in result["report"]
    assert "closed" in result["report"]
    assert "KR-SUGGESTION [kr-1]: proposed=met" not in result["report"]
    assert goal_file.read_bytes() == original_bytes


def test_stale_expected_current_status_is_called_out(tmp_path: Path) -> None:
    """AC6: expected_current_status diverges from the KR's live status —
    staleness must be called out, not presented as fresh."""
    repo_root = tmp_path
    goals_dir = repo_root / "state" / "goals"
    goals_dir.mkdir(parents=True)
    suggestions_dir = repo_root / "state" / "kr-suggestions"
    _write_goal(
        goals_dir,
        "g1.yaml",
        goal_id="goal-1",
        kr_entries=[_kr("kr-1", status="in-progress")],
    )
    _write_suggestion(
        suggestions_dir,
        "s1.yaml",
        goal_id="goal-1",
        kr_id="kr-1",
        proposed_status="met",
        expected_current_status="not-started",
    )

    result = reassess(goals_dir, dry_run=True, repo_root=repo_root)

    assert "STALE" in result["report"]
    assert "not-started" in result["report"]


def test_null_expected_current_status_makes_no_staleness_claim(tmp_path: Path) -> None:
    """AC6: expected_current_status: null means the producer didn't check —
    no staleness claim is made either way."""
    repo_root = tmp_path
    goals_dir = repo_root / "state" / "goals"
    goals_dir.mkdir(parents=True)
    suggestions_dir = repo_root / "state" / "kr-suggestions"
    _write_goal(
        goals_dir,
        "g1.yaml",
        goal_id="goal-1",
        kr_entries=[_kr("kr-1", status="in-progress")],
    )
    _write_suggestion(
        suggestions_dir,
        "s1.yaml",
        goal_id="goal-1",
        kr_id="kr-1",
        proposed_status="met",
        expected_current_status=None,
    )

    result = reassess(goals_dir, dry_run=True, repo_root=repo_root)

    assert "STALE" not in result["report"]
    assert "KR-SUGGESTION [kr-1]: proposed=met" in result["report"]


def test_provenance_and_rationale_render_into_report(tmp_path: Path) -> None:
    """AC7: rationale and provenance (producing_system, source_ref, span,
    recorded_at) render so a confirming human can trace the claim."""
    repo_root = tmp_path
    goals_dir = repo_root / "state" / "goals"
    goals_dir.mkdir(parents=True)
    suggestions_dir = repo_root / "state" / "kr-suggestions"
    _write_goal(goals_dir, "g1.yaml", goal_id="goal-1", kr_entries=[_kr("kr-1")])
    _write_suggestion(
        suggestions_dir,
        "s1.yaml",
        goal_id="goal-1",
        kr_id="kr-1",
        rationale="The 2026-08-03 standup transcript shows this shipped.",
        producing_system="cockpit-reconcile",
        source_ref="transcripts/2026-08-03-standup.md",
        span="14:22",
        recorded_at="2026-08-03",
    )

    result = reassess(goals_dir, dry_run=True, repo_root=repo_root)

    assert "The 2026-08-03 standup transcript shows this shipped." in result["report"]
    assert "cockpit-reconcile" in result["report"]
    assert "transcripts/2026-08-03-standup.md" in result["report"]
    assert "14:22" in result["report"]
    assert "2026-08-03" in result["report"]


def test_live_status_field_is_byte_unchanged_after_a_run(tmp_path: Path) -> None:
    """AC8: the live `status:` field is never overwritten — append-only path
    preserved with a KR-suggestion present."""
    repo_root = tmp_path
    goals_dir = repo_root / "state" / "goals"
    goals_dir.mkdir(parents=True)
    suggestions_dir = repo_root / "state" / "kr-suggestions"
    goal_file = _write_goal(
        goals_dir,
        "g1.yaml",
        goal_id="goal-1",
        kr_entries=[_kr("kr-1", status="not-started")],
    )
    _write_suggestion(suggestions_dir, "s1.yaml", goal_id="goal-1", kr_id="kr-1")

    reassess(goals_dir, dry_run=False, repo_root=repo_root)

    new_text = goal_file.read_text(encoding="utf-8")
    assert "status: not-started" in new_text
    assert 'id: "goal-1"' in new_text


def test_malformed_suggestion_file_warns_and_does_not_block_sibling(tmp_path: Path) -> None:
    """AC9: a malformed/unparseable suggestion file degrades to a warning
    naming that file; it never aborts the run or blocks sibling suggestions."""
    repo_root = tmp_path
    goals_dir = repo_root / "state" / "goals"
    goals_dir.mkdir(parents=True)
    suggestions_dir = repo_root / "state" / "kr-suggestions"
    _write_goal(goals_dir, "g1.yaml", goal_id="goal-1", kr_entries=[_kr("kr-1")])
    _write_suggestion(
        suggestions_dir,
        "z-valid.yaml",
        goal_id="goal-1",
        kr_id="kr-1",
        proposed_status="met",
    )
    suggestions_dir.mkdir(parents=True, exist_ok=True)
    (suggestions_dir / "a-bad.yaml").write_text(
        "goal_id: [unterminated flow seq\n", encoding="utf-8"
    )

    result = reassess(goals_dir, dry_run=True, repo_root=repo_root)

    assert any("a-bad.yaml" in w for w in result["warnings"])
    assert "KR-SUGGESTION [kr-1]: proposed=met" in result["report"]


def test_non_mapping_suggestion_file_warns_and_does_not_block_sibling(tmp_path: Path) -> None:
    """AC9 (variant): a suggestion file that parses as valid YAML but not to
    a mapping (e.g. a bare list) is skipped with a warning, sibling unaffected."""
    repo_root = tmp_path
    goals_dir = repo_root / "state" / "goals"
    goals_dir.mkdir(parents=True)
    suggestions_dir = repo_root / "state" / "kr-suggestions"
    _write_goal(goals_dir, "g1.yaml", goal_id="goal-1", kr_entries=[_kr("kr-1")])
    _write_suggestion(
        suggestions_dir,
        "z-valid.yaml",
        goal_id="goal-1",
        kr_id="kr-1",
        proposed_status="met",
    )
    (suggestions_dir / "a-list.yaml").write_text("- one\n- two\n", encoding="utf-8")

    result = reassess(goals_dir, dry_run=True, repo_root=repo_root)

    assert any("a-list.yaml" in w for w in result["warnings"])
    assert "KR-SUGGESTION [kr-1]: proposed=met" in result["report"]


def test_dry_run_writes_nothing_with_suggestion_present(tmp_path: Path) -> None:
    """AC10: --dry-run remains report-only — suggestions appear in the report,
    zero file writes."""
    repo_root = tmp_path
    goals_dir = repo_root / "state" / "goals"
    goals_dir.mkdir(parents=True)
    suggestions_dir = repo_root / "state" / "kr-suggestions"
    goal_file = _write_goal(
        goals_dir,
        "g1.yaml",
        goal_id="goal-1",
        kr_entries=[_kr("kr-1", status="not-started")],
    )
    _write_suggestion(suggestions_dir, "s1.yaml", goal_id="goal-1", kr_id="kr-1")
    original_bytes = goal_file.read_bytes()

    result = reassess(goals_dir, dry_run=True, repo_root=repo_root)

    assert result["written"] == []
    assert goal_file.read_bytes() == original_bytes
    assert "KR-SUGGESTION [kr-1]" in result["report"]


def test_suggestion_presented_alongside_computed_proposal_never_instead_of(
    tmp_path: Path,
) -> None:
    """The suggestion must not suppress or overwrite the computed proposal for
    the same KR — both lines must be present so a human can compare them."""
    repo_root = tmp_path
    goals_dir = repo_root / "state" / "goals"
    goals_dir.mkdir(parents=True)
    suggestions_dir = repo_root / "state" / "kr-suggestions"
    _write_goal(
        goals_dir,
        "g1.yaml",
        goal_id="goal-1",
        kr_entries=[_kr("kr-1", status="not-started")],
    )
    _write_suggestion(
        suggestions_dir,
        "s1.yaml",
        goal_id="goal-1",
        kr_id="kr-1",
        proposed_status="met",
    )

    result = reassess(goals_dir, dry_run=True, repo_root=repo_root)

    assert "KR [kr-1]: current=not-started" in result["report"]
    assert "KR-SUGGESTION [kr-1]: proposed=met" in result["report"]


def test_duplicate_goal_id_is_ambiguous_and_written_to_neither_file(
    tmp_path: Path,
) -> None:
    """Two goal files sharing an `id:`, targeted by a suggestion whose kr_id
    is present on both — resolution is genuinely undecidable, so the
    suggestion must be reported AMBIGUOUS (not UNRESOLVED, not silently
    resolved against whichever file was read last) and, critically, its text
    must never land in either goal artifact's bytes after a non-dry-run
    pass — this is what actually catches the cross-goal write a naive
    last-file-wins index would produce."""
    repo_root = tmp_path
    goals_dir = repo_root / "state" / "goals"
    goals_dir.mkdir(parents=True)
    suggestions_dir = repo_root / "state" / "kr-suggestions"
    goal_file_a = _write_goal(
        goals_dir,
        "g1.yaml",
        goal_id="dup-id",
        kr_entries=[_kr("kr-1", status="not-started")],
    )
    goal_file_b = _write_goal(
        goals_dir,
        "g2.yaml",
        goal_id="dup-id",
        kr_entries=[_kr("kr-1", status="not-started")],
    )
    _write_suggestion(
        suggestions_dir,
        "s1.yaml",
        goal_id="dup-id",
        kr_id="kr-1",
        rationale="Should never resolve — the goal_id is ambiguous.",
    )

    result = reassess(goals_dir, dry_run=False, repo_root=repo_root)

    assert "KR-SUGGESTION AMBIGUOUS" in result["report"]
    assert "dup-id" in result["report"]
    assert "KR-SUGGESTION UNRESOLVED" not in result["report"]
    assert "KR-SUGGESTION [kr-1]: proposed=" not in result["report"]

    text_a = goal_file_a.read_text(encoding="utf-8")
    text_b = goal_file_b.read_text(encoding="utf-8")
    assert "Should never resolve" not in text_a
    assert "Should never resolve" not in text_b


def test_no_repo_root_is_clean_skip_no_warning(tmp_path: Path) -> None:
    """No repo_root supplied — the suggestion source cannot be located, and
    degrades exactly like an absent directory: no warning noise."""
    goals_dir = tmp_path / "state" / "goals"
    goals_dir.mkdir(parents=True)
    _write_goal(goals_dir, "g1.yaml", goal_id="goal-1", kr_entries=[_kr("kr-1")])

    result = reassess(goals_dir, dry_run=True, repo_root=None)

    assert "kr-suggestion" not in result["report"].lower()

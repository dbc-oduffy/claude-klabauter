"""Characterization tests for coordinator_core.ops.handoff_gate_aging.

Port of: handoff-gate-aging.sh (DoE 67202df6, 2026-07-16) — the bash
predicate had ZERO existing test coverage, so these are the FIRST tests it
has ever had. Built directly from the predicate spec (14d/7d, >= both
boundaries) rather than ported from an existing bash test suite (none
existed).

Spec backlink: DoE scratch/subagent-sandbox/bash-to-python-engine-migration/
    recipe-small-bin-clis-gate-aging-scope-soak-scope-warning.md § 1
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import coordinator_core.ops.handoff_transition as _handoff_transition_mod
from coordinator_core.ops.handoff_gate_aging import (
    _read_gate_evidence_resolved,
    check_one,
    classify_gate,
    main,
    scan,
    scan_triage,
)

_TODAY = date(2026, 7, 16)

#: claude-klabauter repo root, resolved without a subprocess call:
#: tests/test_handoff_gate_aging.py -> tests -> coordinator_core -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(tmp_path: Path, name: str, deployment_state: str | None, created: str | None,
           last_recheck: str | None = None) -> Path:
    p = tmp_path / name
    body = "---\n"
    if deployment_state is not None:
        body += f"deployment_state: {deployment_state}\n"
    if created is not None:
        body += f"created: {created}\n"
    if last_recheck is not None:
        body += f"last_gate_recheck: {last_recheck}\n"
    body += "---\nbody\n"
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# check_one — predicate boundary cases
# ---------------------------------------------------------------------------


def test_stale_at_exact_14d_threshold_no_recheck(tmp_path):
    p = _write(tmp_path, "h.md", "awaiting_gate", "2026-07-02")  # exactly 14d
    line, rc = check_one(p, _TODAY)
    assert rc == 0
    assert line == f"STALE: {p} (created 14d ago, last_gate_recheck absent)"


def test_not_stale_at_13d_below_threshold(tmp_path):
    p = _write(tmp_path, "h.md", "awaiting_gate", "2026-07-03")  # 13d
    line, rc = check_one(p, _TODAY)
    assert (line, rc) == ("", 0)


def test_stale_when_recheck_exactly_7d_cooldown(tmp_path):
    p = _write(tmp_path, "h.md", "awaiting_gate", "2026-06-01", "2026-07-09")  # recheck=7d
    line, rc = check_one(p, _TODAY)
    assert rc == 0
    assert line == f"STALE: {p} (created 45d ago, last_gate_recheck 7d ago)"


def test_not_stale_when_recheck_6d_within_cooldown(tmp_path):
    p = _write(tmp_path, "h.md", "awaiting_gate", "2026-06-01", "2026-07-10")  # recheck=6d
    line, rc = check_one(p, _TODAY)
    assert (line, rc) == ("", 0)


def test_not_stale_when_not_awaiting_gate(tmp_path):
    p = _write(tmp_path, "h.md", "shipped", "2026-01-01")
    line, rc = check_one(p, _TODAY)
    assert (line, rc) == ("", 0)


def test_parse_error_missing_created(tmp_path):
    p = _write(tmp_path, "h.md", "awaiting_gate", None)
    line, rc = check_one(p, _TODAY)
    assert (line, rc) == ("", 2)


def test_parse_error_unparseable_created_date(tmp_path):
    p = _write(tmp_path, "h.md", "awaiting_gate", "not-a-date")
    line, rc = check_one(p, _TODAY)
    assert (line, rc) == ("", 2)


def test_parse_error_unparseable_recheck_date(tmp_path):
    p = _write(tmp_path, "h.md", "awaiting_gate", "2026-06-01", "not-a-date")
    line, rc = check_one(p, _TODAY)
    assert (line, rc) == ("", 2)


# ---------------------------------------------------------------------------
# scan — directory collection + rc arbitration
# ---------------------------------------------------------------------------


def test_scan_single_file(tmp_path):
    p = _write(tmp_path, "h.md", "awaiting_gate", "2026-07-02")
    lines, rc = scan(str(p), _TODAY)
    assert rc == 1
    assert len(lines) == 1


def test_scan_directory_one_level_only(tmp_path):
    _write(tmp_path, "stale.md", "awaiting_gate", "2026-07-02")
    _write(tmp_path, "fresh.md", "awaiting_gate", "2026-07-15")
    sub = tmp_path / "nested"
    sub.mkdir()
    _write(sub, "should-not-be-seen.md", "awaiting_gate", "2026-01-01")

    lines, rc = scan(str(tmp_path), _TODAY)
    assert rc == 1
    assert len(lines) == 1
    assert "stale.md" in lines[0]


def test_scan_stale_wins_over_parse_error(tmp_path):
    _write(tmp_path, "stale.md", "awaiting_gate", "2026-07-02")
    _write(tmp_path, "broken.md", "awaiting_gate", None)  # parse error
    lines, rc = scan(str(tmp_path), _TODAY)
    assert rc == 1  # STALE wins over PARSE_ERROR when stale_count > 0
    assert len(lines) == 1


def test_scan_parse_error_only_when_zero_stale(tmp_path):
    _write(tmp_path, "broken.md", "awaiting_gate", None)
    lines, rc = scan(str(tmp_path), _TODAY)
    assert (lines, rc) == ([], 2)


def test_scan_no_matches_no_errors(tmp_path):
    _write(tmp_path, "fresh.md", "awaiting_gate", "2026-07-15")
    lines, rc = scan(str(tmp_path), _TODAY)
    assert (lines, rc) == ([], 0)


# ---------------------------------------------------------------------------
# main — CLI arg validation (parity-critical exit codes 0/1/2)
# ---------------------------------------------------------------------------


def test_main_missing_arg_exits_2(capsys):
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 2
    assert "handoff-gate-aging.sh: missing argument" in captured.err


def test_main_path_not_found_exits_2(tmp_path, capsys):
    missing = str(tmp_path / "does-not-exist.md")
    rc = main([missing])
    captured = capsys.readouterr()
    assert rc == 2
    assert "handoff-gate-aging.sh: path not found" in captured.err


def test_main_stale_exits_1_and_prints_line(tmp_path, capsys):
    # main() uses the REAL date.today() (no today-injection at the CLI layer) —
    # use a relative offset so this test doesn't rot as wall-clock time passes.
    from datetime import timedelta

    old_enough = (date.today() - timedelta(days=20)).isoformat()
    p = _write(tmp_path, "h.md", "awaiting_gate", old_enough)
    rc = main([str(p)])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out.startswith("STALE:")


def test_main_not_stale_exits_0(tmp_path, capsys):
    from datetime import timedelta

    too_recent = (date.today() - timedelta(days=1)).isoformat()
    p = _write(tmp_path, "h.md", "awaiting_gate", too_recent)
    rc = main([str(p)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""


# ---------------------------------------------------------------------------
# classify_gate — C6/AC4: evidence-resolved / review-due / merely-aged are
# THREE distinguishable signals, all imported from gate_eval's C3 predicate,
# never re-implemented here.
# ---------------------------------------------------------------------------


def _write_handoff(dir_path: Path, name: str, fields: dict) -> Path:
    """Write a handoff .md with an arbitrary set of frontmatter fields.

    `blocked_by` (or any list-valued field) is rendered as a YAML block list;
    everything else is rendered as a scalar `key: value` line. Sufficient for
    the shapes `evaluate_gate_triage`/`_read_meta` actually need — this is a
    test fixture writer, not a general YAML emitter.
    """
    dir_path.mkdir(parents=True, exist_ok=True)
    p = dir_path / name
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, list):
            if value:
                lines.append(f"{key}:")
                lines.extend(f"  - {item}" for item in value)
            else:
                lines.append(f"{key}: []")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append("body")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_classify_evidence_resolved_when_structured_blocked_by_all_shipped(tmp_path):
    handoffs_dir = tmp_path / "state" / "handoffs"
    gated = _write_handoff(
        handoffs_dir,
        "gated.md",
        {
            "id": "gated-01",
            "deployment_state": "awaiting_gate",
            "created": "2026-01-01",
            "blocked_by": ["dep-01"],
        },
    )
    _write_handoff(
        handoffs_dir,
        "dep.md",
        {
            "id": "dep-01",
            "deployment_state": "shipped",
            "shipped_in": "abc1234",
        },
    )

    result = classify_gate(gated, date(2026, 7, 27), worktree_root=tmp_path)
    assert result["triage_status"] == "freed"
    assert result["signal"] == "evidence-resolved"


def test_classify_merely_aged_when_prose_gate_dominates_and_old(tmp_path):
    handoffs_dir = tmp_path / "state" / "handoffs"
    gated = _write_handoff(
        handoffs_dir,
        "aged.md",
        {
            "id": "aged-01",
            "deployment_state": "awaiting_gate",
            "created": "2026-01-01",
            "gate_dependency": '"human sign-off needed"',
        },
    )

    result = classify_gate(gated, date(2026, 7, 27), worktree_root=tmp_path)
    assert result["triage_status"] == "indeterminate"
    assert result["signal"] == "merely-aged"


def test_classify_not_stale_when_prose_gate_dominates_and_recent(tmp_path):
    handoffs_dir = tmp_path / "state" / "handoffs"
    gated = _write_handoff(
        handoffs_dir,
        "fresh.md",
        {
            "id": "fresh-01",
            "deployment_state": "awaiting_gate",
            "created": "2026-07-26",
            "gate_dependency": '"human sign-off needed"',
        },
    )

    result = classify_gate(gated, date(2026, 7, 27), worktree_root=tmp_path)
    assert result["triage_status"] == "indeterminate"
    assert result["signal"] == "not-stale"


def test_classify_gate_propagates_scan_errors_from_shared_walker(tmp_path, monkeypatch):
    """Review: code-reviewer — Finding 3 (P2) regression test. An unreadable
    archive subtree reported by the shared walker
    (`_collect_all_handoffs_for_gate_index`) must surface through
    `classify_gate`'s own `scan_errors` return, not be silently discarded —
    this caller previously threw the walker's `scan_errors` away entirely."""
    handoffs_dir = tmp_path / "state" / "handoffs"
    gated = _write_handoff(
        handoffs_dir,
        "gated.md",
        {
            "id": "gated-01",
            "deployment_state": "awaiting_gate",
            "created": "2026-01-01",
            "gate_dependency": '"human sign-off needed"',
        },
    )
    (tmp_path / "archive" / "handoffs").mkdir(parents=True, exist_ok=True)

    real_walk = os.walk

    def _boom_walk(top, *args, **kwargs):
        onerror = kwargs.get("onerror")
        if onerror is not None and str(top).endswith(str(Path("archive") / "handoffs")):
            onerror(OSError(13, "Permission denied", str(top)))
            return iter(())
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(os, "walk", _boom_walk)

    result = classify_gate(gated, date(2026, 7, 27), worktree_root=tmp_path)
    assert result["scan_errors"] != []


def test_classify_not_gated_when_not_awaiting_gate(tmp_path):
    handoffs_dir = tmp_path / "state" / "handoffs"
    shipped = _write_handoff(
        handoffs_dir,
        "shipped.md",
        {"id": "shipped-01", "deployment_state": "shipped", "created": "2026-01-01"},
    )

    result = classify_gate(shipped, date(2026, 7, 27), worktree_root=tmp_path)
    assert result["signal"] == "not-gated"
    assert result["triage_status"] is None


def test_classify_review_due_via_caller_supplied_gate_evidence_never_promotes_to_actionable(tmp_path):
    """D3a — a `deadline` leg's elapsed date is a PROMPT, never a satisfier.

    Models the real `signed-commit` corpus shape (prose: "... deferred to July
    2026") but supplies `gate_evidence` explicitly as a caller-resolved
    argument, never read off the fixture's own frontmatter (module docstring
    "C6 SCOPE BOUNDARY" — no live handoff carries a `gate_evidence:` block
    yet, so this demonstrates the mechanism is wired and correct, not that it
    fires from a live directory scan today).
    """
    handoffs_dir = tmp_path / "state" / "handoffs"
    gated = _write_handoff(
        handoffs_dir,
        "signed-commit-like.md",
        {
            "id": "signed-commit-like",
            "deployment_state": "awaiting_gate",
            "created": "2026-06-30",
            "gate_dependency": '"PM revisits the enforcement-mechanism choice — deferred to July 2026"',
        },
    )
    gate_evidence = {
        "covers_prose": True,
        "legs": [{"leg_id": "deadline-1", "kind": "deadline", "elapsed": True}],
    }

    result = classify_gate(
        gated, date(2026, 7, 27), worktree_root=tmp_path, gate_evidence=gate_evidence
    )
    assert result["triage_status"] == "review-due"
    assert result["signal"] == "review-due"
    assert result["signal"] != "evidence-resolved"


def test_classify_real_corpus_signed_commit_never_evidence_resolved():
    """Corpus verification (dispatch brief, plan § Corpus anomalies :479):
    `signed-commit` (state/handoffs/2026-06-30_021546_12e715f3.md, real disk,
    not a fixture) has an elapsed deadline in its prose but carries NO
    `gate_evidence:` block on disk (none is authored onto it by this chunk —
    the schema is DoE-gated). Against today's real corpus this must resolve
    `indeterminate` (prose dominance, no evidence to demote it) — NEVER
    `freed`/"evidence-resolved". A classifier that reports this record as
    ACTIONABLE is a bug, not a pass (plan's own explicit corpus check).
    """
    path = _REPO_ROOT / "state" / "handoffs" / "2026-06-30_021546_12e715f3.md"
    if not path.is_file():
        import pytest

        pytest.skip(f"corpus fixture record moved/archived: {path}")

    result = classify_gate(path, date(2026, 8, 15), worktree_root=_REPO_ROOT)
    assert result["triage_status"] != "freed"
    assert result["signal"] != "evidence-resolved"


# ---------------------------------------------------------------------------
# AC4/C6 — scan_triage actually reads gate_evidence off frontmatter (never
# hardcodes None), live-resolves every leg via handoff_transition's own
# helper, and a multi-leg block survives the read intact (not truncated by
# dag._parse_yaml_list_block).
# ---------------------------------------------------------------------------


def _stub_resolve_leg_always_true(leg):
    """Stand in for `sibling_fact.resolve_leg` — no real repo registry/sibling
    I/O in this test process, so every I/O-kind leg reports satisfied. Proves
    the read+live-resolve WIRING, not `sibling_fact`'s own I/O (out of scope,
    already covered by its own test suite)."""
    return {"leg_id": leg.get("leg_id"), "read_ok": True, "observed": True, "error": None}


def test_scan_triage_reads_gate_evidence_off_disk_multi_leg_survives(tmp_path, monkeypatch):
    """A `gate_evidence:` block with MULTIPLE legs must round-trip intact off
    disk — dag._parse_yaml_list_block would truncate every leg after the
    first indented continuation line to an opaque scalar; the safe fm_dict
    route this module now uses must not."""
    monkeypatch.setattr(_handoff_transition_mod, "resolve_leg", _stub_resolve_leg_always_true)

    handoffs_dir = tmp_path / "state" / "handoffs"
    gated = _write_handoff(
        handoffs_dir,
        "gated.md",
        {
            "id": "gated-02",
            "deployment_state": "awaiting_gate",
            "created": "2026-01-01",
        },
    )
    gate_evidence_block = (
        "gate_evidence:\n"
        "  covers_prose: false\n"
        "  legs:\n"
        "    - leg_id: leg-one\n"
        "      kind: file-exists\n"
        "      repo: some-repo\n"
        "      ref: some/path/one\n"
        "      expected: true\n"
        "    - leg_id: leg-two\n"
        "      kind: file-exists\n"
        "      repo: some-repo\n"
        "      ref: some/path/two\n"
        "      expected: true\n"
    )
    text = gated.read_text(encoding="utf-8")
    gated.write_text(text.replace("---\n\nbody\n", f"{gate_evidence_block}---\n\nbody\n"), encoding="utf-8")

    resolved = _read_gate_evidence_resolved(gated, date(2026, 7, 27))
    assert resolved is not None
    assert [leg["leg_id"] for leg in resolved["legs"]] == ["leg-one", "leg-two"]
    for leg in resolved["legs"]:
        assert leg["read_ok"] is True
        assert leg["observed"] is True


def test_scan_triage_evidence_resolved_not_merely_aged_when_legs_resolve(tmp_path, monkeypatch):
    """Old (STALE by the 14d/7d predicate) AND every gate_evidence leg
    resolves -> `evidence-resolved`, NEVER demoted to `merely-aged` — the
    triage signal must win over the calendar-only fallback."""
    monkeypatch.setattr(_handoff_transition_mod, "resolve_leg", _stub_resolve_leg_always_true)

    handoffs_dir = tmp_path / "state" / "handoffs"
    gated = _write_handoff(
        handoffs_dir,
        "gated.md",
        {
            "id": "gated-03",
            "deployment_state": "awaiting_gate",
            "created": "2026-01-01",  # far past the 14d threshold
        },
    )
    gate_evidence_block = (
        "gate_evidence:\n"
        "  covers_prose: false\n"
        "  legs:\n"
        "    - leg_id: leg-one\n"
        "      kind: file-exists\n"
        "      repo: some-repo\n"
        "      ref: some/path\n"
        "      expected: true\n"
    )
    text = gated.read_text(encoding="utf-8")
    gated.write_text(text.replace("---\n\nbody\n", f"{gate_evidence_block}---\n\nbody\n"), encoding="utf-8")

    results = scan_triage(str(handoffs_dir), date(2026, 7, 27))
    assert len(results) == 1
    result = results[0]
    assert result["triage_status"] == "freed"
    assert result["signal"] == "evidence-resolved"
    assert result["signal"] != "merely-aged"


def test_scan_triage_no_gate_evidence_block_passes_none(tmp_path):
    """A handoff with no `gate_evidence:` block resolves `None`, identical to
    pre-AC4 behaviour — this module never authors/infers/backfills one.

    Carries a prose `gate_dependency` (mirrors
    `test_classify_merely_aged_when_prose_gate_dominates_and_old`) so the
    triage verdict is `indeterimate`/`merely-aged` rather than the unrelated
    vacuous-empty-`blocked_by` `freed` path — isolating the assertion to
    "no gate_evidence was read", not gate_eval's own empty-set semantics."""
    handoffs_dir = tmp_path / "state" / "handoffs"
    gated = _write_handoff(
        handoffs_dir,
        "aged.md",
        {
            "id": "aged-02",
            "deployment_state": "awaiting_gate",
            "created": "2026-01-01",
            "gate_dependency": '"human sign-off needed"',
        },
    )

    resolved = _read_gate_evidence_resolved(gated, date(2026, 7, 27))
    assert resolved is None

    results = scan_triage(str(handoffs_dir), date(2026, 7, 27))
    assert len(results) == 1
    assert results[0]["signal"] == "merely-aged"


def test_read_gate_evidence_resolved_returns_none_on_malformed_yaml(tmp_path):
    """Review: code-reviewer Finding 1 -- a `gate_evidence:` block whose
    frontmatter contains invalid YAML (unparseable, not merely absent) must
    return `None`, per the reader's own 'never a caller-visible exception'
    contract, rather than letting `yaml.YAMLError` propagate and abort the
    caller's unguarded per-handoff sweep loop."""
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    gated = handoffs_dir / "malformed.md"
    gated.write_text(
        "---\n"
        "deployment_state: awaiting_gate\n"
        "created: 2026-01-01\n"
        "gate_evidence: [unterminated flow mapping\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )

    resolved = _read_gate_evidence_resolved(gated, date(2026, 7, 27))
    assert resolved is None


def test_read_gate_evidence_resolved_returns_none_on_unreadable_encoding(tmp_path):
    """Review: code-reviewer Finding 1 -- a non-UTF-8-encoded file raises
    `UnicodeDecodeError` from `path.read_text(encoding='utf-8')`, NOT
    `OSError` — the pre-fix reader only caught `OSError` and would have let
    this propagate uncaught."""
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    gated = handoffs_dir / "bad-encoding.md"
    gated.write_bytes(b"---\ndeployment_state: awaiting_gate\n\xff\xfe---\n\nbody\n")

    resolved = _read_gate_evidence_resolved(gated, date(2026, 7, 27))
    assert resolved is None


# ---------------------------------------------------------------------------
# main --triage — opt-in flag, additive only; default scan/check_one output
# stays byte-identical without it.
# ---------------------------------------------------------------------------


def test_main_default_output_unchanged_without_triage_flag(tmp_path, capsys):
    """Negative-spec guard: default `main` output/rc shape is pre-existing
    and unchanged (module docstring) — passing no flag must reproduce
    exactly what `test_main_stale_exits_1_and_prints_line` already asserts."""
    from datetime import timedelta

    old_enough = (date.today() - timedelta(days=20)).isoformat()
    p = _write(tmp_path, "h.md", "awaiting_gate", old_enough)
    rc = main([str(p)])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out.startswith("STALE:")
    assert "signal" not in captured.out


def test_main_triage_flag_surfaces_evidence_resolved_signal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(_handoff_transition_mod, "resolve_leg", _stub_resolve_leg_always_true)

    from datetime import timedelta

    old_enough = (date.today() - timedelta(days=20)).isoformat()
    gated = _write_handoff(
        tmp_path,
        "gated.md",
        {
            "id": "gated-04",
            "deployment_state": "awaiting_gate",
            "created": old_enough,
        },
    )
    gate_evidence_block = (
        "gate_evidence:\n"
        "  covers_prose: false\n"
        "  legs:\n"
        "    - leg_id: leg-one\n"
        "      kind: file-exists\n"
        "      repo: some-repo\n"
        "      ref: some/path\n"
        "      expected: true\n"
    )
    text = gated.read_text(encoding="utf-8")
    gated.write_text(text.replace("---\n\nbody\n", f"{gate_evidence_block}---\n\nbody\n"), encoding="utf-8")

    rc = main([str(gated), "--triage"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "evidence-resolved" in captured.out
    assert "gated-04" in captured.out


def test_main_missing_arg_after_stripping_triage_flag_exits_2(capsys):
    rc = main(["--triage"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "missing argument" in captured.err

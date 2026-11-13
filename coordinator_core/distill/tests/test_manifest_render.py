"""
coordinator_core.distill.tests.test_manifest_render

Unit tests for coordinator_core.distill.manifest_render — the § 1b markdown
render surface for the C9 disposal-manifest shape (2026-07-23 architecture
review § 1b).

Coverage:
  (a) eligible + retained rows both render, with the retained row's blocking
      guard + evidence pulled from its guards_run "block" receipt.
  (b) a retained row with a MULTI-LINE evidence string does not break the
      markdown table (embedded newline becomes <br>, not a literal linebreak).
  (c) mass_throttle banner present when the flag is set, absent otherwise.
  (d) counts line reflects scan_stats.
  (e) a synthetic blocker (no matching guards_run "block" receipt, e.g.
      artifact-class-unresolved) falls back to retention_reason rather than
      rendering a blank guard/evidence pair.
  (f) empty rows list renders a counts-only document, no crash.

Spec backlink: pln-claude-klabauter-driven-ceremony-redesig-c7fe9a § C9/C12/C13
Governing review: state/review-trail/findings/2026-07-23-the Staff Engineer-arch-ceremony-redesign-post-execution.md § 1b
"""

from __future__ import annotations

from coordinator_core.distill import manifest_schema as _schema
from coordinator_core.distill.manifest_render import render_disposal_manifest


def _manifest(rows, mass_throttle=False, run_id="2026-07-23-01h00"):
    total = len(rows)
    eligible = sum(1 for r in rows if r["eligible"])
    return _schema.make_disposal_manifest(
        run_id=run_id,
        rows=rows,
        scan_stats=_schema.make_scan_stats(total, eligible, total - eligible),
        mass_throttle=mass_throttle,
    )


def test_render_eligible_and_retained_rows():
    eligible_row = _schema.make_disposal_row(
        path="archive/handoffs/a.md",
        artifact_class="handoff",
        guards_run=[_schema.make_guard_receipt("shipped_in", "pass", "sha=abc123")],
        eligible=True,
        log_row="- archive/handoffs/a.md -> EPHEMERAL (disposed)",
    )
    retained_row = _schema.make_disposal_row(
        path="archive/handoffs/b.md",
        artifact_class="handoff",
        guards_run=[
            _schema.make_guard_receipt("shipped_in", "block", "shipped_in absent or empty"),
            _schema.make_guard_receipt("realized_by", "pass", "inline"),
        ],
        eligible=False,
        retention_reason="blocked by: shipped_in",
        log_row="",
    )
    manifest = _manifest([eligible_row, retained_row])
    rendered = render_disposal_manifest(manifest)

    assert "archive/handoffs/a.md" in rendered
    assert "ELIGIBLE" in rendered
    assert "archive/handoffs/b.md" in rendered
    assert "RETAINED" in rendered
    assert "shipped_in" in rendered
    assert "shipped_in absent or empty" in rendered
    assert "realized_by" not in rendered.split("archive/handoffs/b.md", 1)[1].split("\n")[0]


def test_render_multiline_evidence_does_not_break_table():
    row = _schema.make_disposal_row(
        path="archive/handoffs/c.md",
        artifact_class="handoff",
        guards_run=[
            _schema.make_guard_receipt(
                "commitment-closure",
                "block",
                "commitment-closure: open commitment ledger-1.yaml references candidate\n"
                "(second detail line)",
            )
        ],
        eligible=False,
        retention_reason="blocked by: commitment-closure",
        log_row="",
    )
    manifest = _manifest([row])
    rendered = render_disposal_manifest(manifest)

    table_lines = [
        line for line in rendered.splitlines() if line.startswith("| archive/handoffs/c.md")
    ]
    assert len(table_lines) == 1
    assert "<br>" in table_lines[0]
    assert "(second detail line)" in table_lines[0]


def test_render_mass_throttle_banner_present_when_set():
    row = _schema.make_disposal_row(
        path="archive/handoffs/a.md",
        artifact_class="handoff",
        guards_run=[_schema.make_guard_receipt("shipped_in", "pass", "sha=abc123")],
        eligible=True,
        log_row="- archive/handoffs/a.md -> EPHEMERAL (disposed)",
    )
    throttled = render_disposal_manifest(_manifest([row], mass_throttle=True))
    quiet = render_disposal_manifest(_manifest([row], mass_throttle=False))

    assert "MASS THROTTLE ENGAGED" in throttled
    assert "MASS THROTTLE ENGAGED" not in quiet


def test_render_counts_line_reflects_scan_stats():
    eligible_row = _schema.make_disposal_row(
        path="archive/handoffs/a.md",
        artifact_class="handoff",
        guards_run=[_schema.make_guard_receipt("shipped_in", "pass", "sha=abc123")],
        eligible=True,
        log_row="- archive/handoffs/a.md -> EPHEMERAL (disposed)",
    )
    retained_row = _schema.make_disposal_row(
        path="archive/handoffs/b.md",
        artifact_class="handoff",
        guards_run=[_schema.make_guard_receipt("shipped_in", "block", "absent")],
        eligible=False,
        retention_reason="blocked by: shipped_in",
        log_row="",
    )
    rendered = render_disposal_manifest(_manifest([eligible_row, retained_row]))
    assert "**Scanned:** 2" in rendered
    assert "**Eligible:** 1" in rendered
    assert "**Retained:** 1" in rendered


def test_render_synthetic_blocker_falls_back_to_retention_reason():
    row = _schema.make_disposal_row(
        path="archive/specs/mystery.md",
        artifact_class="unresolved",
        guards_run=[
            _schema.make_guard_receipt("active-reference", "pass", "no active references found"),
        ],
        eligible=False,
        retention_reason="blocked by: artifact-class-unresolved",
        log_row="",
    )
    rendered = render_disposal_manifest(_manifest([row]))
    assert "RETAINED" in rendered
    assert "artifact-class-unresolved" in rendered


def test_render_empty_rows_no_crash():
    manifest = _manifest([])
    rendered = render_disposal_manifest(manifest)
    assert "No candidates scanned" in rendered
    assert "**Scanned:** 0" in rendered


def test_render_row_symbols_carry_sym_tag_only_when_resolved():
    row = _schema.make_disposal_row(
        path="archive/handoffs/a.md", artifact_class="handoff", guards_run=[], eligible=True, log_row="x"
    )
    row["symbols"] = [
        {"qualified_name": "pkg.mod.fn", "sym": "scip-python python p 0.1 `pkg.mod`/fn()."},
        {"qualified_name": "pkg.mod.gone"},
    ]
    rendered = render_disposal_manifest(_manifest([row]))
    assert "pkg.mod.fn [sym: scip-python python p 0.1 `pkg.mod`/fn().]" in rendered
    assert "pkg.mod.gone |" in rendered and "pkg.mod.gone [sym" not in rendered


def test_anchor_manifest_attaches_resolved_symbols_to_py_rows_only(tmp_path):
    from coordinator_core.distill.manifest_render import anchor_manifest
    from coordinator_core.symbol_knowledge.tests.test_anchors import _store

    _store(tmp_path, [("pkg/m.py", "fn", "function", "", "scip fn")])
    py = _schema.make_disposal_row(path="pkg/m.py", artifact_class="code", guards_run=[], eligible=True, log_row="x")
    md = _schema.make_disposal_row(path="docs/a.md", artifact_class="doc", guards_run=[], eligible=True, log_row="x")
    manifest = _manifest([py, md])
    anchored = anchor_manifest(tmp_path, manifest)
    assert anchored["rows"][0]["symbols"] == [{"path": "pkg/m.py", "qualified_name": "fn", "sym": "scip fn"}]
    assert "symbols" not in anchored["rows"][1] and "symbols" not in manifest["rows"][0]
    assert "fn [sym: scip fn]" in render_disposal_manifest(anchored)
    assert "[sym" not in render_disposal_manifest(manifest)

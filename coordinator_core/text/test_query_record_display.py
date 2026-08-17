"""Byte-parity tests for coordinator_core.text.query_record_display.

Expected strings are transcribed by hand from `bin/query-records.js`'s
TYPE_DISPLAY table (bin/query-records.js:307-340) and its formatRecords()
link-rewriting (bin/query-records.js:1601-1638) — never derived by spawning
node against the real oracle (this suite runs with no `node` on PATH).

Two layers:
  - Unit-level: each `_display_*` renderer called directly against a plain
    frontmatter dict, one assertion per field-fallback branch the oracle's
    JS source exercises (``||`` fallback vs bare interpolation).
  - Integration-level: a representative on-disk fixture per supported type,
    read through the REAL pipeline (`ceremony.records_query.query_records`
    -> `format_records`) so frontmatter parsing + the renderer compose
    exactly as `refresh_queries.process_file` uses them in production.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.text.query_record_display import (
    TYPE_DISPLAY,
    _default_display,
    format_records,
)


# ---------------------------------------------------------------------------
# Unit-level — one renderer call per type, transcribed from TYPE_DISPLAY.
# ---------------------------------------------------------------------------


def test_display_handoff_prefers_deployment_state_over_status():
    """bin/query-records.js:308 — `fm.deployment_state || fm.status || 'unknown'`."""
    fn = TYPE_DISPLAY["handoff"]
    assert fn("p.md", {"title": "T", "deployment_state": "shipped", "status": "open"}) == (
        "- [T](p.md) — shipped"
    )
    assert fn("p.md", {"status": "open"}) == "- [p.md](p.md) — open"
    assert fn("p.md", {}) == "- [p.md](p.md) — unknown"


def test_display_handoff_archived_shipped_in_suffix():
    """bin/query-records.js:309."""
    fn = TYPE_DISPLAY["handoff-archived"]
    assert fn("p.md", {"title": "T", "status": "claimed", "shipped_in": "abc123"}) == (
        "- [T](p.md) — claimed (shipped: abc123)"
    )
    assert fn("p.md", {"status": "claimed"}) == "- [p.md](p.md) — claimed"


def test_display_plan():
    """bin/query-records.js:311."""
    fn = TYPE_DISPLAY["plan"]
    assert fn("p.md", {"title": "Plan T", "status": "draft"}) == "- [Plan T](p.md) — draft"
    assert fn("p.md", {}) == "- [p.md](p.md) — unknown"


def test_display_cross_repo_memo():
    """bin/query-records.js:316."""
    fn = TYPE_DISPLAY["cross-repo-memo"]
    assert fn("p.md", {"title": "M", "status": "open", "from": "rag"}) == (
        "- [M](p.md) — open (from rag)"
    )
    assert fn("p.md", {}) == "- [p.md](p.md) — unknown (from ?)"


def test_display_debt():
    """bin/query-records.js:317."""
    fn = TYPE_DISPLAY["debt"]
    assert fn("p.md", {"title": "D", "severity": "P1", "status": "open", "source": "audit"}) == (
        "- [D](p.md) — P1 open (source: audit)"
    )
    assert fn("p.md", {}) == "- [p.md](p.md) — P? unknown (source: ?)"


def test_display_bug():
    """bin/query-records.js:320 (Slice-C F4 — surface, not system)."""
    fn = TYPE_DISPLAY["bug"]
    assert fn("p.md", {"title": "B", "severity": "P0", "status": "open", "surface": "cli"}) == (
        "- [B](p.md) — P0 open (surface: cli)"
    )
    assert fn("p.md", {}) == "- [p.md](p.md) — P? unknown (surface: ?)"


def test_display_improvement():
    """bin/query-records.js:321 (Slice-C F4 — proposed_action, not proposed_target)."""
    fn = TYPE_DISPLAY["improvement"]
    assert fn("p.md", {"title": "I", "status": "open", "proposed_action": "refactor X"}) == (
        "- [I](p.md) — open (action: refactor X)"
    )
    assert fn("p.md", {}) == "- [p.md](p.md) — unknown (action: ?)"


def test_display_decision_guide_falsy_count_omits_suffix():
    """bin/query-records.js:333 — `fm.decision_count ? ... : ''`; 0 is falsy in JS too."""
    fn = TYPE_DISPLAY["decision-guide"]
    assert fn("p.md", {"title": "G", "status": "active", "decision_count": 5}) == (
        "- [G](p.md) — active (5 DRs)"
    )
    assert fn("p.md", {"status": "active", "decision_count": 0}) == "- [p.md](p.md) — active"
    assert fn("p.md", {}) == "- [p.md](p.md) — unknown"


def test_display_completion_renders_literal_undefined_for_missing_title_or_nature():
    """bin/query-records.js:314 — faithful oracle quirk (module docstring):
    `${fm.title}`/`${fm.nature}` have NO `||` fallback, so a missing field
    prints the literal string "undefined", not an empty title."""
    fn = TYPE_DISPLAY["completion"]
    assert fn("p.md", {"title": "Shipped X", "nature": "feature", "chain": "c1",
                        "commits": ["abc", "def"]}) == (
        "- **Shipped X** [feature] (chain: c1) — abc, def"
    )
    assert fn("p.md", {"nature": "feature"}) == "- **undefined** [feature] (chain: none) — no-commit"
    assert fn("p.md", {"title": "T"}) == "- **T** [undefined] (chain: none) — no-commit"
    # Empty commits list is JS-falsy too ('[].join(",")' === '' -> '|| no-commit').
    assert fn("p.md", {"title": "T", "nature": "n", "commits": []}) == (
        "- **T** [n] (chain: none) — no-commit"
    )


def test_display_decision():
    """bin/query-records.js:310."""
    fn = TYPE_DISPLAY["decision"]
    assert fn("p.md", {"title": "D", "status": "accepted"}) == "- [D](p.md) — accepted"
    assert fn("p.md", {}) == "- [p.md](p.md) — unknown"


def test_display_review_findings_count_nullish_not_falsy():
    """bin/query-records.js:312 — `fm.findings_count ?? '?'`, NOT `||`: a
    findings_count of 0 must render as 0, not fall back to '?'."""
    fn = TYPE_DISPLAY["review"]
    assert fn("p.md", {"title": "R", "reviewer": "the Staff Engineer", "findings_count": 5}) == (
        "- [R](p.md) — reviewer: the Staff Engineer, findings: 5"
    )
    assert fn("p.md", {"title": "R", "reviewer": "the Staff Engineer", "findings_count": 0}) == (
        "- [R](p.md) — reviewer: the Staff Engineer, findings: 0"
    )
    assert fn("p.md", {}) == "- [p.md](p.md) — reviewer: ?, findings: ?"


def test_display_lesson_uses_raw_path_not_basename():
    """bin/query-records.js:313 — `fm.title || p` (bare `p`, not `path.basename(p)`
    like every other renderer's fallback)."""
    fn = TYPE_DISPLAY["lesson"]
    assert fn("state/lessons/x.yaml", {"title": "L", "tier": "universal"}) == "- **L** [universal]"
    assert fn("state/lessons/x.yaml", {}) == "- **state/lessons/x.yaml** [untagged]"


def test_display_handoff_ledger_dispatch_counts_nullish_not_falsy():
    """bin/query-records.js:315 — `agent_dispatches`/`opus_dispatches` use `??`;
    a dispatch count of 0 must render as 0, not '?'."""
    fn = TYPE_DISPLAY["handoff-ledger"]
    assert fn(
        "state/handoffs/h.md#ledger-0",
        {
            "tshirt": "L", "agent_dispatches": 26, "opus_dispatches": 4,
            "session_id": "sid-1", "created": "2026-05-19",
        },
    ) == (
        "- [state/handoffs/h.md#ledger-0] tshirt=L agents=26 opus=4 "
        "session=sid-1 created=2026-05-19"
    )
    assert fn(
        "state/handoffs/h.md#ledger-0",
        {"tshirt": "L", "agent_dispatches": 0, "opus_dispatches": 0},
    ) == (
        "- [state/handoffs/h.md#ledger-0] tshirt=L agents=0 opus=0 "
        "session=? created=?"
    )
    assert fn("state/handoffs/h.md#ledger-0", {}) == (
        "- [state/handoffs/h.md#ledger-0] tshirt=? agents=? opus=? "
        "session=? created=?"
    )


def test_display_research_claim():
    """bin/query-records.js:337."""
    fn = TYPE_DISPLAY["research-claim"]
    assert fn(
        "docs/research/x.claims.json#claim-0",
        {"claim_text": "A claim", "confidence": "high", "type": "empirical"},
    ) == "- A claim [high] (empirical)"
    assert fn("docs/research/x.claims.json#claim-0", {}) == (
        "- docs/research/x.claims.json#claim-0 [?] (?)"
    )


def test_display_research_synthesis_nullish_score_not_falsy():
    """bin/query-records.js:336 — `fm.coverage_score ?? '?'`, NOT `||`: a
    coverage_score of 0 must render as 0, not fall back to '?'."""
    fn = TYPE_DISPLAY["research-synthesis"]
    assert fn("docs/research/x.md", {
        "title": "Widget Research", "pipeline": "web", "coverage_score": 0.82,
    }) == "- [Widget Research](docs/research/x.md) — pipeline: web, score: 0.82"
    assert fn("docs/research/x.md", {"title": "Widget Research", "coverage_score": 0}) == (
        "- [Widget Research](docs/research/x.md) — pipeline: ?, score: 0"
    )
    assert fn("docs/research/x.md", {}) == (
        "- [docs/research/x.md](docs/research/x.md) — pipeline: ?, score: ?"
    )


def test_display_coverage_audit_links_basename_nullish_counts():
    """bin/query-records.js:338 — links on `path.basename(p)`, not `fm.title`
    (the oracle has no title field for this type); `??` on both counts."""
    fn = TYPE_DISPLAY["coverage-audit"]
    assert fn("docs/research/x-coverage-audit.md", {
        "present_count": 5, "absent_count": 2,
    }) == "- [x-coverage-audit.md](docs/research/x-coverage-audit.md) — present: 5, absent: 2"
    assert fn("docs/research/x-coverage-audit.md", {"present_count": 0, "absent_count": 0}) == (
        "- [x-coverage-audit.md](docs/research/x-coverage-audit.md) — present: 0, absent: 0"
    )
    assert fn("docs/research/x-coverage-audit.md", {}) == (
        "- [x-coverage-audit.md](docs/research/x-coverage-audit.md) — present: ?, absent: ?"
    )


def test_display_gap_report_undefined_deepening_faithful_oracle_quirk():
    """bin/query-records.js:339 — links on basename; `??` on gap_count/
    coverage_score, but `deepening_recommended` has NO fallback at all in the
    oracle, so a missing value renders the literal string "undefined" (same
    faithful quirk as `_display_completion`)."""
    fn = TYPE_DISPLAY["gap-report"]
    assert fn("docs/research/x-gap-report.md", {
        "gap_count": 3, "coverage_score": 0.6, "deepening_recommended": True,
    }) == "- [x-gap-report.md](docs/research/x-gap-report.md) — gaps: 3, score: 0.6, deepening: true"
    assert fn("docs/research/x-gap-report.md", {"gap_count": 0, "coverage_score": 0}) == (
        "- [x-gap-report.md](docs/research/x-gap-report.md) — gaps: 0, score: 0, deepening: undefined"
    )


def test_display_sizing_prefers_name_falls_back_to_link_path_never_intent():
    """coordinator_core/text/query_record_display.py::_display_sizing — not in
    the oracle's TYPE_DISPLAY table (sizing-object postdates the JS port).
    `name` is a short label; falling back to `intent` (multi-sentence prose)
    would blow out a markdown list, exactly the failure `name` was added to
    fix — the highest-value assertion here is that `intent` never surfaces."""
    fn = TYPE_DISPLAY["sizing-object"]
    assert fn("state/sizings/x.yaml", {"name": "Widget Sizing", "intent": "A very long prose."}) == (
        "- [Widget Sizing](state/sizings/x.yaml)"
    )
    fallback = fn("state/sizings/x.yaml", {"intent": "A very long multi-sentence prose blurb."})
    assert fallback == "- [state/sizings/x.yaml](state/sizings/x.yaml)"
    assert "A very long multi-sentence prose blurb." not in fallback


def test_default_display_fallback_uses_link_path_not_basename():
    """bin/query-records.js:1616 — the generic fallback's `fm.title || p` falls
    back to the (already-relativized) link path itself, NOT `path.basename(p)`
    like every dedicated TYPE_DISPLAY renderer above does."""
    assert _default_display("a/b/p.md", {}) == "- [a/b/p.md](a/b/p.md)"
    assert _default_display("a/b/p.md", {"title": "T"}) == "- [T](a/b/p.md)"


# ---------------------------------------------------------------------------
# Integration-level — real on-disk fixtures through query_records + format_records.
# ---------------------------------------------------------------------------


def _write_yaml(p: Path, body: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _write_md(p: Path, frontmatter: str, body: str = "Body.\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return p


def test_handoff_fixture_round_trip(tmp_path):
    _write_md(
        tmp_path / "state" / "handoffs" / "h1.md",
        "title: Handoff One\nstatus: open\nroadmap_id: rm-x",
    )
    records = query_records("handoff", tmp_path, where="roadmap_id=rm-x", limit=0)
    expansion = format_records(records, {"type": "handoff", "format": "markdown-list"},
                                root=tmp_path, from_dir=tmp_path)
    assert expansion == "- [Handoff One](state/handoffs/h1.md) — open"


def test_bug_fixture_round_trip_yaml_whole_file(tmp_path):
    _write_yaml(
        tmp_path / "state" / "bug-backlog" / "b1.yaml",
        "title: A Bug\nseverity: P1\nstatus: open\nsurface: cli\n",
    )
    records = query_records("bug", tmp_path, limit=0)
    expansion = format_records(records, {"type": "bug", "format": "markdown-list"},
                                root=tmp_path, from_dir=tmp_path)
    assert expansion == "- [A Bug](state/bug-backlog/b1.yaml) — P1 open (surface: cli)"


def test_completion_fixture_round_trip(tmp_path):
    _write_md(
        tmp_path / "archive" / "completed" / "2026-07" / "c1.md",
        "title: Shipped Thing\nnature: feature\nchain: chain-1\ncommits:\n  - abc123\n  - def456",
    )
    records = query_records("completion", tmp_path, limit=0)
    expansion = format_records(records, {"type": "completion", "format": "markdown-list"},
                                root=tmp_path, from_dir=tmp_path)
    assert expansion == (
        "- **Shipped Thing** [feature] (chain: chain-1) — abc123, def456"
    )


def test_decision_guide_fixture_round_trip(tmp_path):
    _write_md(
        tmp_path / "docs" / "guides" / "topic-decisions.md",
        "title: Topic Decisions\nstatus: active\ndecision_count: 3",
    )
    records = query_records("decision-guide", tmp_path, limit=0)
    expansion = format_records(records, {"type": "decision-guide", "format": "markdown-list"},
                                root=tmp_path, from_dir=tmp_path)
    assert expansion == "- [Topic Decisions](docs/guides/topic-decisions.md) — active (3 DRs)"


def test_decision_fixture_round_trip(tmp_path):
    _write_md(
        tmp_path / "docs" / "decisions" / "2026-07-01-example.md",
        "title: Example Decision\nstatus: accepted",
    )
    records = query_records("decision", tmp_path, limit=0)
    expansion = format_records(records, {"type": "decision", "format": "markdown-list"},
                                root=tmp_path, from_dir=tmp_path)
    assert expansion == "- [Example Decision](docs/decisions/2026-07-01-example.md) — accepted"


def test_review_fixture_round_trip(tmp_path):
    _write_md(
        tmp_path / "state" / "reviews" / "2026-07-01-example.md",
        "title: Example Review\nreviewer: the Staff Engineer\nfindings_count: 2",
    )
    records = query_records("review", tmp_path, limit=0)
    expansion = format_records(records, {"type": "review", "format": "markdown-list"},
                                root=tmp_path, from_dir=tmp_path)
    assert expansion == (
        "- [Example Review](state/reviews/2026-07-01-example.md) — reviewer: the Staff Engineer, findings: 2"
    )


def test_lesson_fixture_round_trip_yaml_whole_file(tmp_path):
    _write_yaml(
        tmp_path / "state" / "lessons" / "2026-07-01-example.yaml",
        "title: Example Lesson\ntier: universal\n",
    )
    records = query_records("lesson", tmp_path, limit=0)
    expansion = format_records(records, {"type": "lesson", "format": "markdown-list"},
                                root=tmp_path, from_dir=tmp_path)
    assert expansion == "- **Example Lesson** [universal]"


def test_handoff_ledger_fixture_round_trip_fragment_path_relativized(tmp_path):
    """Synthetic type: one Session Ledger block -> one #ledger-N record; the
    fragment must survive `_relativize_link`'s path resolution untouched."""
    _write_md(
        tmp_path / "state" / "handoffs" / "h1.md",
        "title: A Handoff",
        body=(
            "## Session Ledger\n\n"
            "| Field | Value |\n"
            "|---|---|\n"
            "| tshirt | L |\n"
            "| session_id | sid-1 |\n"
        ),
    )
    records = query_records("handoff-ledger", tmp_path, limit=0)
    assert records[0]["path"] == "state/handoffs/h1.md#ledger-0"
    sub_dir = tmp_path / "docs"
    sub_dir.mkdir()
    expansion = format_records(records, {"type": "handoff-ledger", "format": "markdown-list"},
                                root=tmp_path, from_dir=sub_dir)
    assert expansion == (
        "- [../state/handoffs/h1.md#ledger-0] tshirt=L agents=? opus=? "
        "session=sid-1 created=?"
    )


def test_research_claim_fixture_round_trip_fragment_path_relativized(tmp_path):
    """Synthetic type: one claims.json array element -> one #claim-N record."""
    import json

    claims_path = tmp_path / "docs" / "research" / "2026-07-01-example.claims.json"
    claims_path.parent.mkdir(parents=True)
    claims_path.write_text(
        json.dumps([{"claim_text": "A claim", "confidence": "high", "type": "empirical"}]),
        encoding="utf-8",
    )
    records = query_records("research-claim", tmp_path, limit=0)
    assert records[0]["path"] == "docs/research/2026-07-01-example.claims.json#claim-0"
    expansion = format_records(records, {"type": "research-claim", "format": "markdown-list"},
                                root=tmp_path, from_dir=tmp_path)
    assert expansion == "- A claim [high] (empirical)"


def test_research_synthesis_fixture_round_trip_sibling_exclusion(tmp_path):
    """The regression this whole change hinges on, exercised through the REAL
    ceremony pipeline: a `research-synthesis` query over a docs/research/ tree
    containing all three sibling shapes must expand to ONLY the plain
    synthesis file's markdown line."""
    _write_md(
        tmp_path / "docs" / "research" / "2026-07-22-widget-research.md",
        "title: Widget Research\npipeline: web\ncoverage_score: 0.82",
    )
    _write_md(
        tmp_path / "docs" / "research" / "2026-07-22-widget-gap-report.md",
        "gap_count: 3\ncoverage_score: 0.6\ndeepening_recommended: true",
    )
    _write_md(
        tmp_path / "docs" / "research" / "2026-07-22-widget-coverage-audit.md",
        "present_count: 5\nabsent_count: 2",
    )
    records = query_records("research-synthesis", tmp_path, limit=0)
    assert [r["path"] for r in records] == ["docs/research/2026-07-22-widget-research.md"]
    expansion = format_records(records, {"type": "research-synthesis", "format": "markdown-list"},
                                root=tmp_path, from_dir=tmp_path)
    assert expansion == (
        "- [Widget Research](docs/research/2026-07-22-widget-research.md) "
        "— pipeline: web, score: 0.82"
    )


def test_gap_report_fixture_round_trip(tmp_path):
    _write_md(
        tmp_path / "docs" / "research" / "2026-07-22-widget-gap-report.md",
        "gap_count: 3\ncoverage_score: 0.6\ndeepening_recommended: true",
    )
    records = query_records("gap-report", tmp_path, limit=0)
    expansion = format_records(records, {"type": "gap-report", "format": "markdown-list"},
                                root=tmp_path, from_dir=tmp_path)
    assert expansion == (
        "- [2026-07-22-widget-gap-report.md](docs/research/2026-07-22-widget-gap-report.md) "
        "— gaps: 3, score: 0.6, deepening: true"
    )


def test_coverage_audit_fixture_round_trip(tmp_path):
    _write_md(
        tmp_path / "docs" / "research" / "2026-07-22-widget-coverage-audit.md",
        "present_count: 5\nabsent_count: 2",
    )
    records = query_records("coverage-audit", tmp_path, limit=0)
    expansion = format_records(records, {"type": "coverage-audit", "format": "markdown-list"},
                                root=tmp_path, from_dir=tmp_path)
    assert expansion == (
        "- [2026-07-22-widget-coverage-audit.md](docs/research/2026-07-22-widget-coverage-audit.md) "
        "— present: 5, absent: 2"
    )


def test_sizing_fixture_round_trip_yaml_whole_file(tmp_path):
    _write_yaml(
        tmp_path / "state" / "sizings" / "s1.yaml",
        "name: Widget Sizing\nintent: A very long multi-sentence prose blurb.\n",
    )
    records = query_records("sizing-object", tmp_path, limit=0)
    expansion = format_records(records, {"type": "sizing-object", "format": "markdown-list"},
                                root=tmp_path, from_dir=tmp_path)
    assert expansion == "- [Widget Sizing](state/sizings/s1.yaml)"


def test_tracker_and_roadmap_fall_back_to_default_display(tmp_path):
    """tracker/roadmap have NO TYPE_DISPLAY entry in the oracle either
    (bin/query-records.js:307-340 never lists them) — they render via the
    same generic fallback any other absent-type would."""
    _write_md(tmp_path / "docs" / "project-tracker.md", "title: My Tracker\nstatus: active")
    records = query_records("tracker", tmp_path, limit=0)
    expansion = format_records(records, {"type": "tracker", "format": "markdown-list"},
                                root=tmp_path, from_dir=tmp_path)
    assert expansion == "- [My Tracker](docs/project-tracker.md)"


# ---------------------------------------------------------------------------
# format — json / paths (bin/query-records.js:1602-1606); no link-rewrite
# applies to either (markdown-list is the only format linkCtx affects).
# ---------------------------------------------------------------------------


def test_format_paths_returns_bare_repo_relative_paths(tmp_path):
    _write_md(tmp_path / "state" / "handoffs" / "h1.md", "title: H\nstatus: open")
    records = query_records("handoff", tmp_path, limit=0)
    out = format_records(records, {"type": "handoff", "format": "paths"},
                          root=tmp_path, from_dir=tmp_path / "sub")
    assert out == "state/handoffs/h1.md"


def test_format_json_round_trips_records():
    import json

    records = [{"path": "state/handoffs/h1.md", "frontmatter": {"title": "H", "status": "open"}}]
    out = format_records(records, {"type": "handoff", "format": "json"})
    assert json.loads(out) == records

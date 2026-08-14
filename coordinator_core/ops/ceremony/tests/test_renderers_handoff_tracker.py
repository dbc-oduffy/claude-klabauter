"""
coordinator_core.ops.ceremony.tests.test_renderers_handoff_tracker — golden-fixture
parity test for C8b's ``render_repo_section`` port of ``render-handoff-tracker.js``.

Golden-fixture provenance (load-bearing for AC12's "full-fidelity" claim): the
checked-in fixture tree at ``tests/fixtures/render_handoff_tracker/`` and the golden
output at ``tests/fixtures/render_handoff_tracker_golden.md`` were captured by running
the REAL DoE ``bin/render-handoff-tracker.js`` (``renderRepoSection`` export) against
the fixture tree, via a small node harness — BEFORE the node source is removed, per
the plan's "reconstruct from DoE:85006468^" discipline. The only normalization applied
to the captured output is replacing the live ``<!-- generated: ... -->`` timestamp with
the ``{{GENERATED}}`` placeholder this test also substitutes in the Python output before
comparing (the timestamp line is intentionally excluded from ``render_repo_section``'s
return value — see the module's own negative-spec — so no live-clock substitution is
needed on the Python side; the placeholder in the golden file documents where the node
source's timestamp line would have been, for reviewer legibility).

The fixture tree exercises: active-filter (terminal-by-status, terminal-by-deployment-
state, both excluded), workstream-seq denominator spanning active+archived, standalone
(no-workstream) rows, spinoff vs spinoff-roadmap grouping + extra columns, cross-repo-
memo open/in_progress inclusion + actioned exclusion, a parse-error handoff (no
frontmatter block) surfaced as a fail-loud row, and (C9-parity) three
``handoff_phase: execution`` handoffs — one each for the Fireable
(``deployment_state: ready_to_fire``), Gated (``awaiting_gate``), and Other
(an unrecognized state, ``in_flight``) sub-tables — golden-captured against the
real node ``renderRepoSection`` AFTER this addition, so the golden file's
``## Execution Handoffs`` section is itself parity-verified, not hand-authored.

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C8b
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.ceremony.renderers import (
    build_workstream_index,
    is_active,
    render_repo_section,
    workstream_seq,
)

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_FIXTURE_ROOT = _FIXTURES_DIR / "render_handoff_tracker"
_GOLDEN_PATH = _FIXTURES_DIR / "render_handoff_tracker_golden.md"


def test_render_repo_section_matches_node_golden_output():
    golden = _GOLDEN_PATH.read_text(encoding="utf-8")
    # Strip the node CLI's leading generated-timestamp comment line + its blank
    # line — render_repo_section returns only the ## Handoffs onward section
    # (the timestamp/header assembly is a CLI/dispatch concern, see module
    # negative-spec).
    golden_body = golden.split("\n\n", 1)[1].rstrip("\n")

    actual = render_repo_section(_FIXTURE_ROOT)

    assert actual == golden_body


def test_parse_error_handoff_surfaced_not_dropped():
    actual = render_repo_section(_FIXTURE_ROOT)
    assert "⚠ PARSE-ERROR" in actual
    assert "handoff-parse-error.md" in actual


def test_terminal_handoffs_excluded_from_active_table():
    actual = render_repo_section(_FIXTURE_ROOT)
    assert "handoff-terminal-status.md" not in actual
    assert "handoff-terminal-deployment.md" not in actual


def test_execution_handoffs_partitioned_into_fireable_gated_other():
    actual = render_repo_section(_FIXTURE_ROOT)
    sections = actual.split("## Execution Handoffs", 1)[1]
    fireable, rest = sections.split("### Gated", 1)
    gated, other = rest.split("### Other", 1)
    assert "handoff-execution-fireable.md" in fireable
    assert "handoff-execution-gated.md" not in fireable
    assert "handoff-execution-other.md" not in fireable
    assert "handoff-execution-gated.md" in gated
    assert "handoff-execution-fireable.md" not in gated
    assert "handoff-execution-other.md" not in gated
    assert "handoff-execution-other.md" in other
    assert "handoff-execution-fireable.md" not in other
    assert "handoff-execution-gated.md" not in other


def test_execution_handoffs_excluded_from_plain_handoffs_table():
    actual = render_repo_section(_FIXTURE_ROOT)
    handoffs_table = actual.split("## Handoffs", 1)[1].split("## Execution Handoffs", 1)[0]
    assert "handoff-execution-fireable.md" not in handoffs_table
    assert "handoff-execution-gated.md" not in handoffs_table
    assert "handoff-execution-other.md" not in handoffs_table


def test_execution_handoffs_each_appear_exactly_once(tmp_path):
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    (handoffs_dir / "exec-fireable.md").write_text(
        "---\ncreated: 2026-01-10\ndeployment_state: ready_to_fire\n"
        "handoff_phase: execution\n---\nBody.\n",
        encoding="utf-8",
    )
    (handoffs_dir / "exec-gated.md").write_text(
        "---\ncreated: 2026-01-11\ndeployment_state: awaiting_gate\n"
        "handoff_phase: execution\n---\nBody.\n",
        encoding="utf-8",
    )
    (handoffs_dir / "exec-other.md").write_text(
        "---\ncreated: 2026-01-12\ndeployment_state: in_flight\n"
        "handoff_phase: execution\n---\nBody.\n",
        encoding="utf-8",
    )
    (handoffs_dir / "exec-absent-state.md").write_text(
        "---\ncreated: 2026-01-13\nhandoff_phase: execution\n---\nBody.\n",
        encoding="utf-8",
    )
    actual = render_repo_section(tmp_path)
    lines = actual.splitlines()
    for name in (
        "exec-fireable.md",
        "exec-gated.md",
        "exec-other.md",
        "exec-absent-state.md",
    ):
        matching_rows = [line for line in lines if name in line]
        assert len(matching_rows) == 1, (
            f"{name} should appear on exactly one row, got {len(matching_rows)}: {matching_rows}"
        )


def test_no_execution_handoffs_renders_none_in_all_three_subtables(tmp_path):
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    (handoffs_dir / "plain.md").write_text(
        "---\ncreated: 2026-01-01\ndeployment_state: ready_to_fire\n---\nBody.\n",
        encoding="utf-8",
    )
    actual = render_repo_section(tmp_path)
    sections = actual.split("## Execution Handoffs", 1)[1].split("## Spinoffs", 1)[0]
    assert sections.count("_none_") == 3


def test_spinoff_roadmap_extra_columns_present_only_when_roadmap_row_exists():
    actual = render_repo_section(_FIXTURE_ROOT)
    assert "stub_id" in actual
    assert "blocked_by" in actual
    assert "rmap-01" in actual
    assert "rmap-00" in actual


def test_actioned_memo_excluded_open_and_in_progress_included():
    actual = render_repo_section(_FIXTURE_ROOT)
    assert "memo-open.md" in actual
    assert "memo-in-progress.md" in actual
    assert "memo-actioned.md" not in actual


def test_is_active_terminal_deployment_state():
    assert is_active({"deployment_state": "shipped"}) is False
    assert is_active({"deployment_state": "abandoned"}) is False
    assert is_active({"deployment_state": "ready_to_fire"}) is True


def test_is_active_terminal_status():
    assert is_active({"status": "consumed"}) is False
    assert is_active({"status": "superseded"}) is False
    assert is_active({"status": "active"}) is True


def test_is_active_none_frontmatter_is_not_active():
    assert is_active(None) is False


def test_build_workstream_index_spans_active_and_archived():
    ws_map = build_workstream_index(_FIXTURE_ROOT)
    assert "alpha" in ws_map
    # 5 handoffs sharing workstream=alpha: 2 active, 1 terminal-status,
    # 1 terminal-deployment, 1 archived. The denominator counts all of them.
    assert len(ws_map["alpha"]) == 5


def test_workstream_seq_rank_and_denominator():
    ws_map = build_workstream_index(_FIXTURE_ROOT)
    seq = workstream_seq(ws_map, "alpha", "2026-01-01")
    assert seq == "2 of 5"


def test_workstream_seq_absent_workstream_returns_none():
    ws_map = build_workstream_index(_FIXTURE_ROOT)
    assert workstream_seq(ws_map, None, "2026-01-01") is None
    assert workstream_seq(ws_map, "nonexistent-ws", "2026-01-01") is None

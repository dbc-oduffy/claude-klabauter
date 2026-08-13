"""Tests for coordinator_core.ops.prune_resolved_queue_entries.

Port of: prune-resolved-queue-entries.sh (coordinator-claude b5a4192c, 2026-07-20),
regression-net-ported from prune-resolved-queue-entries.test.sh (coordinator-claude 3a561713,
2026-07-22, 447 LoC, 8-rule fixture-diff suite) — every named fixture below carries the
same case name/comment as its bash counterpart for cross-referencing.
"""
from __future__ import annotations

import os
import stat

import pytest

from coordinator_core.ops.prune_resolved_queue_entries import main


def _run(tmp_path, basename, content):
    p = tmp_path / basename
    p.write_text(content)
    rc = main([str(p)])
    return rc, p.read_text()


# ---------------------------------------------------------------------------
# Rule 2 — ## Resolved section strip (all allowlisted files)
# ---------------------------------------------------------------------------


def test_rule2_resolved_section_dropped_to_next_h2(tmp_path):
    rc, out = _run(
        tmp_path,
        "bug-backlog.md",
        "# Header\n\n## Open\n- alpha\n- beta\n\n"
        "## Resolved this run (bug-blitz 2026-05-18)\n"
        "- gamma — fixed via X\n- delta — fixed via Y\n\n"
        "## Next steps\n- epsilon\n",
    )
    assert rc == 0
    assert out == (
        "# Header\n\n## Open\n- alpha\n- beta\n\n## Next steps\n- epsilon\n"
    )


def test_rule2_history_closed_archive_all_stripped(tmp_path):
    rc, out = _run(
        tmp_path,
        "bug-backlog.md",
        "## Open\n- alpha\n\n## History\n- old1\n\n## Closed\n- old2\n\n"
        "## Archive\n- old3\n\n## Active\n- beta\n",
    )
    assert rc == 0
    assert out == "## Open\n- alpha\n\n## Active\n- beta\n"


# ---------------------------------------------------------------------------
# Rule 5 — H3 status-closure block
# ---------------------------------------------------------------------------


def test_rule5_bracketed_dated_h3_block_dropped(tmp_path):
    rc, out = _run(
        tmp_path,
        "bug-backlog.md",
        "## Open\n\n"
        "### BS-2026-05-18-OPEN [P2] still broken\n"
        "**Status:** OPEN — needs investigation.\n"
        "Lots of forensic narrative here.\n\n"
        "### [FIXED 2026-05-16] BS-2026-05-16-CLOSED [P2] this is done\n"
        "**Status:** FIXED — shipped commit abc1234.\n"
        "30 lines of forensic narrative here.\nMore narrative.\nEven more.\n\n"
        "### BS-2026-05-18-ALSO-OPEN [P3] still open\nBody of open entry.\n",
    )
    assert rc == 0
    assert out == (
        "## Open\n\n"
        "### BS-2026-05-18-OPEN [P2] still broken\n"
        "**Status:** OPEN — needs investigation.\n"
        "Lots of forensic narrative here.\n\n"
        "### BS-2026-05-18-ALSO-OPEN [P3] still open\nBody of open entry.\n"
    )


def test_rule5_mid_line_bracketed_status(tmp_path):
    rc, out = _run(
        tmp_path,
        "bug-backlog.md",
        "### BS-2026-04-28-WIDGET [P2] [FIXED — STALE ENTRY] something\n"
        "Body suppressed.\nMore body.\n\n"
        "### BS-2026-05-18-OPEN [P3] still open\nOpen body.\n",
    )
    assert rc == 0
    assert out == "### BS-2026-05-18-OPEN [P3] still open\nOpen body.\n"


def test_rule5_em_dash_suffix_closure(tmp_path):
    rc, out = _run(
        tmp_path,
        "bug-backlog.md",
        "### BS-2026-04-17-sweep [P0] 12 more FindObject pre-create guards — FIXED\n"
        "Long body about the fix.\n\n"
        "### BS-2026-05-18-OPEN open entry\nOpen body.\n",
    )
    assert rc == 0
    assert out == "### BS-2026-05-18-OPEN open entry\nOpen body.\n"


def test_rule5_h3_closure_transitions_into_next_h3_closure(tmp_path):
    rc, out = _run(
        tmp_path,
        "bug-backlog.md",
        "### [FIXED 2026-05-15] BS-A closed\nBody A.\n\n"
        "### [RESOLVED 2026-05-16] BS-B also closed\nBody B.\n\n"
        "### BS-C-OPEN still open\nBody C.\n",
    )
    assert rc == 0
    assert out == "### BS-C-OPEN still open\nBody C.\n"


def test_rule5_non_closure_h3_entries_survive(tmp_path):
    content = (
        "### BS-PARTIAL [P2] partial fix in flight\nBody partial.\n\n"
        "### BS-WILL-BE-DONE [P3] should be done in v2\nBody forward-looking.\n"
    )
    rc, out = _run(tmp_path, "bug-backlog.md", content)
    assert rc == 0
    assert out == content


# ---------------------------------------------------------------------------
# Rule 6 — Strikethrough-closure line strip
# ---------------------------------------------------------------------------


def test_rule6_example_sim_repo_shape_strikethrough_row_dropped(tmp_path):
    rc, out = _run(
        tmp_path,
        "bug-backlog.md",
        "| ID | Area | Pri | Description | Status | Date |\n"
        "|----|------|-----|-------------|--------|------|\n"
        "| ~~BS-2026-04-09-1~~ | FDM | ~~P2~~ CLOSED | ~~text~~ — "
        "**FIXED (e36d8a3):** wired CVars. | N/A — fixed | 2026-04-09 |\n"
        "| BS-2026-05-18-OPEN | FDM | P2 | open thing | OPEN | 2026-05-18 |\n",
    )
    assert rc == 0
    assert out == (
        "| ID | Area | Pri | Description | Status | Date |\n"
        "|----|------|-----|-------------|--------|------|\n"
        "| BS-2026-05-18-OPEN | FDM | P2 | open thing | OPEN | 2026-05-18 |\n"
    )


def test_rule6_non_closure_strikethrough_survives(tmp_path):
    content = (
        "Narrative line: ~~old approach~~ — we now use the new approach.\n"
        "Another line: ~~deprecated module~~ is the predecessor.\n"
    )
    rc, out = _run(tmp_path, "bug-backlog.md", content)
    assert rc == 0
    assert out == content


# ---------------------------------------------------------------------------
# Rule 7 — Table-row resolution strip
# ---------------------------------------------------------------------------


def test_rule7_example_repo_row_dropped(tmp_path):
    rc, out = _run(
        tmp_path,
        "bug-backlog.md",
        "| BS-2026-03-19-1 | FIXED (run 2) — webhook lock failure now deletes "
        "lock, allowing Stripe retry              |\n"
        "| BS-2026-03-19-2 | FIXED (run 2) — session-orchestrator terminates "
        "AWS session on finalization failure       |\n"
        "| BS-2026-05-18-OPEN | OPEN — needs work |\n",
    )
    assert rc == 0
    assert out == "| BS-2026-05-18-OPEN | OPEN — needs work |\n"


def test_rule7_narrative_fixed_in_later_column_survives(tmp_path):
    content = "| BS-2026-05-18-OPEN | OPEN | should be FIXED in v2 | not yet |\n"
    rc, out = _run(tmp_path, "bug-backlog.md", content)
    assert rc == 0
    assert out == content


# ---------------------------------------------------------------------------
# Rule 1 — Entry-shape strip (queue files only)
# ---------------------------------------------------------------------------


def test_rule1_resolution_resolved_drops_entry(tmp_path):
    rc, out = _run(
        tmp_path,
        "improvement-queue.md",
        "# Queue\n\n"
        "- 2026-05-15 | self | foo.md:10 | done thing | proposed target: wiki\n"
        "  resolution: resolved 2026-05-16 abc1234\n"
        "- 2026-05-15 | self | bar.md:20 | open thing | proposed target: wiki\n",
    )
    assert rc == 0
    assert out == (
        "# Queue\n\n"
        "- 2026-05-15 | self | bar.md:20 | open thing | proposed target: wiki\n"
    )


def test_rule1_closeout_subline_drops_entry(tmp_path):
    rc, out = _run(
        tmp_path,
        "improvement-queue.md",
        "- 2026-05-15 | self | foo.md:10 | done thing | proposed target: wiki\n"
        "  **Closeout:** landed in commit abc1234.\n"
        "- 2026-05-15 | self | bar.md:20 | open thing | proposed target: wiki\n",
    )
    assert rc == 0
    assert out == (
        "- 2026-05-15 | self | bar.md:20 | open thing | proposed target: wiki\n"
    )


# ---------------------------------------------------------------------------
# Rule 3 — Trivial ceremony lines stripped (queue files only)
# ---------------------------------------------------------------------------


def test_rule3_ceremony_lines_stripped_entry_kept(tmp_path):
    rc, out = _run(
        tmp_path,
        "improvement-queue.md",
        "- 2026-05-15 | self | foo.md:10 | thing | proposed target: wiki\n"
        "  recurring: 0\n  resolution: pending\n",
    )
    assert rc == 0
    assert out == (
        "- 2026-05-15 | self | foo.md:10 | thing | proposed target: wiki\n"
    )


# ---------------------------------------------------------------------------
# Rule 1 does NOT apply to bug-backlog
# ---------------------------------------------------------------------------


def test_rule1_inactive_on_bug_backlog(tmp_path):
    content = (
        "- 2026-05-15 | self | foo.md:10 | thing | proposed target: wiki\n"
        "  resolution: resolved 2026-05-16\n"
    )
    rc, out = _run(tmp_path, "bug-backlog.md", content)
    assert rc == 0
    assert out == content


# ---------------------------------------------------------------------------
# Reviewer findings — bug-fix confirmation tests (F1-F9), ported verbatim
# ---------------------------------------------------------------------------


def test_f1_rule7_keyword_in_col3_survives_col2_drops(tmp_path):
    rc, out = _run(
        tmp_path,
        "bug-backlog.md",
        "| BS-2026-05-18-A | OPEN | FIXED — keyword in third column | needs work |\n"
        "| BS-2026-05-18-B | FIXED — col 2, this one drops | shipped |\n",
    )
    assert rc == 0
    assert out == (
        "| BS-2026-05-18-A | OPEN | FIXED — keyword in third column | needs work |\n"
    )


def test_f2_rule5_markdown_link_closed_survives(tmp_path):
    rc, out = _run(
        tmp_path,
        "bug-backlog.md",
        "### BS-2026-05-18-X refers to [CLOSED issue](https://tracker/123) for context\n"
        "Body of the still-open entry.\n\n"
        "### [FIXED 2026-05-18] BS-Y closed\nBody suppressed.\n",
    )
    assert rc == 0
    assert out == (
        "### BS-2026-05-18-X refers to [CLOSED issue](https://tracker/123) for context\n"
        "Body of the still-open entry.\n\n"
    )


def test_f3_rule8_closure_mid_entry_drops_whole_entry_and_orphans(tmp_path):
    rc, out = _run(
        tmp_path,
        "improvement-queue.md",
        "- 2026-05-15 | self | foo.md:10 | thing | proposed target: wiki\n"
        "| ~~BS-Y~~ | ~~P2~~ CLOSED | ~~text~~ — **FIXED:** stuff |\n"
        "  resolution: pending\n  some: subline\n"
        "- 2026-05-15 | self | bar.md:20 | other open | proposed target: wiki\n",
    )
    assert rc == 0
    assert out == (
        "- 2026-05-15 | self | bar.md:20 | other open | proposed target: wiki\n"
    )


def test_f4_rule6_case_sensitive_lowercase_survives(tmp_path):
    rc, out = _run(
        tmp_path,
        "bug-backlog.md",
        "| ~~BS-A~~ | fixed (lowercase narrative) |\n"
        "| ~~BS-B~~ | Fixed (mixed case narrative) |\n"
        "| ~~BS-C~~ | FIXED uppercase closure |\n",
    )
    assert rc == 0
    assert out == (
        "| ~~BS-A~~ | fixed (lowercase narrative) |\n"
        "| ~~BS-B~~ | Fixed (mixed case narrative) |\n"
    )


def test_f5_rule5_bare_keyword_at_eol_drops(tmp_path):
    rc, out = _run(
        tmp_path,
        "bug-backlog.md",
        "### BS-2026-05-18-X [P2] DONE\nBody suppressed under bare-EOL match.\n\n"
        "### BS-2026-05-18-Y [P3] still open\nOpen body.\n",
    )
    assert rc == 0
    assert out == "### BS-2026-05-18-Y [P3] still open\nOpen body.\n"


def test_f6_rule7_tight_table_format_matches(tmp_path):
    rc, out = _run(
        tmp_path,
        "bug-backlog.md",
        "| BS-2026-05-18-A| FIXED — no space before pipe |\n"
        "| BS-2026-05-18-B | OPEN — still working |\n",
    )
    assert rc == 0
    assert out == "| BS-2026-05-18-B | OPEN — still working |\n"


def test_f8_rule7_narrative_fixed_col3_survives(tmp_path):
    content = "| BS-2026-05-18-OPEN | OPEN | should be FIXED in v2 | not yet |\n"
    rc, out = _run(tmp_path, "bug-backlog.md", content)
    assert rc == 0
    assert out == content


def test_f9_h2_resolved_with_inner_h3_fixed_both_suppressed(tmp_path):
    rc, out = _run(
        tmp_path,
        "bug-backlog.md",
        "## Open\n- alpha\n\n## Resolved\n### [FIXED 2026-05-01] BS-X\n"
        "body of inner H3\n\n## Active\n- beta\n",
    )
    assert rc == 0
    assert out == "## Open\n- alpha\n\n## Active\n- beta\n"


# ---------------------------------------------------------------------------
# Rule 4 — Idempotency (per-rule + combined ruleset)
# ---------------------------------------------------------------------------


def _idempotent(tmp_path, basename, content):
    p = tmp_path / basename
    p.write_text(content)
    rc1 = main([str(p)])
    after_first = p.read_text()
    rc2 = main([str(p)])
    after_second = p.read_text()
    return rc1, rc2, after_first, after_second


def test_f7_rule5_idempotent_on_clean_h3_entries(tmp_path):
    content = (
        "### BS-OPEN-1 [P2] still working\nBody 1.\n\n"
        "### BS-OPEN-2 [P3] also working\nBody 2.\n"
    )
    rc1, rc2, first, second = _idempotent(tmp_path, "bug-backlog.md", content)
    assert (rc1, rc2) == (0, 0)
    assert first == second == content


def test_f7_rule6_idempotent_on_clean_strikethrough(tmp_path):
    content = "Narrative: ~~old approach~~ — we now use the new way.\n"
    rc1, rc2, first, second = _idempotent(tmp_path, "bug-backlog.md", content)
    assert (rc1, rc2) == (0, 0)
    assert first == second == content


def test_f7_rule7_idempotent_on_clean_table_rows(tmp_path):
    content = (
        "| BS-2026-05-18-OPEN-1 | OPEN | needs work |\n"
        "| BS-2026-05-18-OPEN-2 | P2 | also open |\n"
    )
    rc1, rc2, first, second = _idempotent(tmp_path, "bug-backlog.md", content)
    assert (rc1, rc2) == (0, 0)
    assert first == second == content


def test_rule4_idempotent_across_combined_ruleset(tmp_path):
    content = (
        "# Header\n\n## Open\n- alpha\n\n## Resolved this run\n- old1\n\n"
        "### [FIXED 2026-05-16] BS-X closed\nBody suppressed.\n\n"
        "| ~~BS-Y~~ | ~~P2~~ CLOSED | ~~text~~ — **FIXED:** stuff |\n"
        "| BS-Z | FIXED — table-row closure |\n\n"
        "### BS-OPEN still open\nOpen body.\n"
    )
    rc1, rc2, first, second = _idempotent(tmp_path, "bug-backlog.md", content)
    assert (rc1, rc2) == (0, 0)
    assert first == second


# ---------------------------------------------------------------------------
# Allowlist guard
# ---------------------------------------------------------------------------


def test_guard_rejects_non_allowlisted_basename(tmp_path, capsys):
    p = tmp_path / "random-file.md"
    p.write_text("content\n")
    rc = main([str(p)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "only operates on improvement-queue.md or bug-backlog.md" in captured.err


def test_guard_missing_file(tmp_path, capsys):
    missing = tmp_path / "bug-backlog.md"
    rc = main([str(missing)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "file not found" in captured.err


def test_usage_error_wrong_arg_count(capsys):
    rc = main([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "Usage: prune-resolved-queue-entries.sh <queue-file>" in captured.err

    rc = main(["a", "b"])
    assert rc == 1


# ---------------------------------------------------------------------------
# Windows/permission edge — atomic replace must not require TMPDIR and must
# not crash when the target directory has no world-writable temp fallback
# (regression guard for the addendum's Windows-portability rule: this module
# must resolve its tempfile from the target's own directory, never
# os.environ.get("TMPDIR", "/tmp")).
# ---------------------------------------------------------------------------


def test_atomic_replace_does_not_use_tmpdir_env(tmp_path, monkeypatch):
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)
    rc, out = _run(tmp_path, "bug-backlog.md", "## Open\n- alpha\n")
    assert rc == 0
    assert out == "## Open\n- alpha\n"


def test_no_orphan_tempfiles_left_behind(tmp_path):
    _run(tmp_path, "bug-backlog.md", "## Open\n- alpha\n")
    leftovers = [
        f
        for f in os.listdir(tmp_path)
        if f != "bug-backlog.md"
    ]
    assert leftovers == []

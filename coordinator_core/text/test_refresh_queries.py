"""Characterization tests for coordinator_core.text.refresh_queries.

Unit 1 (parsing/traversal, below) is Golden-oracle-derived: fixtures were
hand-run against the real node oracle (coordinator/bin/refresh-queries.js,
Coordinator-claude) on 2026-07-17 BEFORE this port was authored (per the porter
brief's characterization-first requirement — no pre-existing
characterization test covered this script). Expected outputs/exit-codes/
stderr text asserted there are transcribed directly from that oracle run,
not re-derived from this module's own implementation — independent
re-derivation, not a self-check. These Unit-1 tests need no node and are
unaffected by the de-node conversion below.

Unit 2 (below) exercises `process_file`/`main` end-to-end against the NATIVE
queryRecords()+formatRecords() path (2026-07-22 de-node port — see
refresh_queries.py's "query-records.js dependency" module-docstring note).
The prior de-node Gate A frozen-golden bridge-replay machinery
(`_BridgeReplay`, `_goldens/refresh_queries/*.json`) is retired along with
the node bridge it stood in for: there is no longer a subprocess boundary to
freeze a golden answer against, so these tests use real on-disk record
fixtures (`state/handoffs/*.md`, a deterministically-empty `--where`) and
assert against the file's own on-disk content directly — no node, no
recorded golden, no mock.

TYPE_DISPLAY renderer byte-parity is exercised separately, in
`test_query_record_display.py` (one fixture per ported type, expected
strings transcribed from `bin/query-records.js`'s TYPE_DISPLAY table with
line-number citations — never derived by spawning node).

Oracle commands used to derive the ORIGINAL Unit-1 fixtures (for reproduction):
    node coordinator/bin/refresh-queries.js --root <workdir>
    node coordinator/bin/refresh-queries.js --root <workdir> --check
    node coordinator/bin/refresh-queries.js --root <workdir> --files <a>,<b>,<c>
    node coordinator/bin/refresh-queries.js --root <workdir> --bogus-flag

Port source: coordinator/bin/refresh-queries.js (coordinator-claude)
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import coordinator_core.text.refresh_queries as _rq_mod
from coordinator_core.text.refresh_queries import (
    ArgParseError,
    QueryRecordsBusinessError,
    QueryRecordsTransportError,
    _run_query_records_native,
    build_code_block_line_set,
    line_of_offset,
    main,
    parse_args,
    parse_query_spec,
    process_file,
    resolve_files_opt,
    walk_md,
)


# ---------------------------------------------------------------------------
# Unit 1 — parsing / traversal helpers (no node required)
# ---------------------------------------------------------------------------


def test_parse_args_defaults():
    opts = parse_args([])
    assert opts == {"root": None, "check": False, "files": None}


def test_parse_args_root_check_files():
    opts = parse_args(["--root", "/tmp/x", "--check", "--files", "a.md,b.md"])
    assert opts == {"root": "/tmp/x", "check": True, "files": "a.md,b.md"}


def test_parse_args_unknown_argument_raises_matching_oracle_message():
    # Oracle stderr (node, --bogus-flag): "Unknown argument: --bogus-flag" exit 1.
    # This port raises instead of exiting inline; main() maps it to exit 2
    # (dedicated CLI-usage code — see module docstring's exit-code contract).
    with pytest.raises(ArgParseError, match=r"^Unknown argument: --bogus-flag$"):
        parse_args(["--bogus-flag"])


def test_detect_root_explicit_path_is_absolutized(tmp_path):
    from coordinator_core.text.refresh_queries import detect_root

    result = detect_root(str(tmp_path))
    assert result == os.path.abspath(str(tmp_path))


def test_parse_query_spec_type_only():
    opts = parse_query_spec("<!-- BEGIN query: plans -->")
    assert opts == {
        "type": "plans",
        "where": None,
        "sort": None,
        "limit": 50,
        "since": None,
        "format": "markdown-list",
    }


def test_parse_query_spec_all_fields():
    opts = parse_query_spec(
        "<!-- BEGIN query: lesson where=scope=universal sort=-created limit=10 "
        "since=14d format=json -->"
    )
    assert opts == {
        "type": "lesson",
        "where": "scope=universal",
        "sort": "-created",
        "limit": 10,
        "since": "14d",
        "format": "json",
    }


def test_parse_query_spec_empty_raises():
    with pytest.raises(ValueError):
        parse_query_spec("<!-- BEGIN query:  -->")


def test_walk_md_excludes_node_modules_git_archive_recurses_others(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / "archive").mkdir()
    (tmp_path / "sub").mkdir()
    (tmp_path / "node_modules" / "x.md").write_text("x", encoding="utf-8")
    (tmp_path / ".git" / "x.md").write_text("x", encoding="utf-8")
    (tmp_path / "archive" / "x.md").write_text("x", encoding="utf-8")
    (tmp_path / "sub" / "nested.md").write_text("x", encoding="utf-8")
    (tmp_path / "top.md").write_text("x", encoding="utf-8")
    (tmp_path / "not-md.txt").write_text("x", encoding="utf-8")

    found = {os.path.relpath(p, tmp_path) for p in walk_md(str(tmp_path))}
    assert found == {os.path.join("sub", "nested.md"), "top.md"}


def test_build_code_block_line_set_fenced_block():
    content = "line0\n```\nline2-in-fence\nline3-in-fence\n```\nline5"
    result = build_code_block_line_set(content)
    # Lines 2,3 are the fence interior; line 4 (closing ```) is also "inside"
    # per the oracle's own inclusive walk (the closing-fence line itself is
    # added to inCode before the `inside=false` flip — oracle-matched).
    assert result == {2, 3, 4}


def test_build_code_block_line_set_no_fence():
    assert build_code_block_line_set("just\nplain\ntext") == set()


def test_line_of_offset():
    content = "a\nbb\nccc"
    assert line_of_offset(content, 0) == 0
    assert line_of_offset(content, 2) == 1  # first char of "bb"
    assert line_of_offset(content, 5) == 2  # first char of "ccc"


def test_resolve_files_opt_skips_missing_and_non_md(tmp_path):
    keep = tmp_path / "keep.md"
    keep.write_text("x", encoding="utf-8")
    (tmp_path / "notmd.txt").write_text("x", encoding="utf-8")

    resolved = resolve_files_opt(
        f"{keep},{tmp_path / 'missing.md'},{tmp_path / 'notmd.txt'}", str(tmp_path)
    )
    assert resolved == [str(keep)]


def test_resolve_files_opt_note_on_stderr(tmp_path, capsys):
    resolve_files_opt(str(tmp_path / "missing.md"), str(tmp_path))
    err = capsys.readouterr().err
    assert "Note: 1 of 1 --files entries skipped (not found or not .md)" in err


# ---------------------------------------------------------------------------
# Unit 2 — refresh/check logic + orchestration, against the NATIVE
# queryRecords()+formatRecords() path (see module docstring). Real on-disk
# record fixtures, no node, no golden replay.
# ---------------------------------------------------------------------------


def _write(p: Path, content: str) -> Path:
    p.write_text(content, encoding="utf-8")
    return p


def _write_handoff(root: Path, name: str, **frontmatter_extra) -> Path:
    """Write a minimal `state/handoffs/<name>.md` fixture."""
    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True, exist_ok=True)
    lines = ["---", "title: Test Handoff", "status: open"]
    for key, value in frontmatter_extra.items():
        lines.append(f"{key}: {value}")
    lines += ["---", "", "Body.", ""]
    return _write(handoffs / name, "\n".join(lines))


def _base_query_opts(**overrides) -> dict:
    opts = {"type": "handoff", "where": None, "sort": None, "limit": 50, "since": None,
            "format": "markdown-list"}
    opts.update(overrides)
    return opts


def _write_archived_handoff(root: Path, subdir: str, name: str, **frontmatter_extra) -> Path:
    """Write a minimal `archive/handoffs/<subdir>/<name>.md` fixture — mirrors
    `_write_handoff` but under the archived-baton subtree exercised by the
    roadmap-callout archive-follow fix (`_run_query_records_native`)."""
    archived = root / "archive" / "handoffs" / subdir
    archived.mkdir(parents=True, exist_ok=True)
    lines = ["---", "title: Test Handoff", "status: consumed"]
    for key, value in frontmatter_extra.items():
        lines.append(f"{key}: {value}")
    lines += ["---", "", "Body.", ""]
    return _write(archived / name, "\n".join(lines))


def test_process_file_simple_callout_replaced_and_idempotent(tmp_path):
    doc = _write(
        tmp_path / "doc1.md",
        "# Doc1\n\n"
        "<!-- BEGIN query: handoff where=roadmap_id=nonexistent-marker-xyz -->\n"
        "placeholder old content\n"
        "<!-- END query -->\n\n"
        "Some text.\n",
    )
    result = process_file(str(doc), str(tmp_path), check_mode=False)
    assert result == {"changed": True, "changedCount": 1, "errorCount": 0}
    # No state/handoffs dir at all -> empty result set -> empty expansion;
    # markers collapse to two adjacent lines with nothing between them.
    assert doc.read_text(encoding="utf-8") == (
        "# Doc1\n\n"
        "<!-- BEGIN query: handoff where=roadmap_id=nonexistent-marker-xyz -->\n"
        "<!-- END query -->\n\n"
        "Some text.\n"
    )

    # Rerun is idempotent (no further change).
    result2 = process_file(str(doc), str(tmp_path), check_mode=False)
    assert result2 == {"changed": False, "changedCount": 0, "errorCount": 0}


def test_process_file_renders_matching_records_with_relative_link(tmp_path):
    _write_handoff(tmp_path, "a-handoff.md", roadmap_id="rm-1")
    sub = tmp_path / "state" / "roadmap" / "rm-1"
    sub.mkdir(parents=True)
    doc = _write(
        sub / "STUB-INDEX.md",
        "<!-- BEGIN query: handoff where=roadmap_id=rm-1 -->\nold\n<!-- END query -->\n",
    )
    result = process_file(str(doc), str(tmp_path), check_mode=False)
    assert result == {"changed": True, "changedCount": 1, "errorCount": 0}
    updated = doc.read_text(encoding="utf-8")
    # Link is relative to the callout FILE's own directory
    # (state/roadmap/rm-1/), not repo-root-relative — bin/query-records.js:1627-1633.
    assert "[Test Handoff](../../handoffs/a-handoff.md) — open" in updated


def test_process_file_applies_sort_and_limit(tmp_path):
    _write_handoff(tmp_path, "b.md", roadmap_id="rm-9", created="2026-01-02")
    _write_handoff(tmp_path, "a.md", roadmap_id="rm-9", created="2026-01-05")
    doc = _write(
        tmp_path / "STUB-INDEX.md",
        "<!-- BEGIN query: handoff where=roadmap_id=rm-9 sort=-created limit=1 -->\n"
        "old\n<!-- END query -->\n",
    )
    result = process_file(str(doc), str(tmp_path), check_mode=False)
    assert result == {"changed": True, "changedCount": 1, "errorCount": 0}
    updated = doc.read_text(encoding="utf-8")
    # sort=-created keeps the newer record first; limit=1 drops the rest —
    # queryRecords applies sort BEFORE limit (bin/query-records.js:1496-1511),
    # so the OLDER record (b.md) must never survive here.
    assert "a.md" in updated
    assert "b.md" not in updated


def test_process_file_skips_marker_inside_fenced_code_block(tmp_path):
    original = (
        "# Doc2\n\n"
        "```\n"
        "<!-- BEGIN query: handoff -->\n"
        "old\n"
        "<!-- END query -->\n"
        "```\n\n"
        "Normal text after.\n"
    )
    doc = _write(tmp_path / "doc2.md", original)
    result = process_file(str(doc), str(tmp_path), check_mode=False)
    assert result == {"changed": False, "changedCount": 0, "errorCount": 0}
    assert doc.read_text(encoding="utf-8") == original


def test_process_file_skips_marker_inside_inline_backticks(tmp_path):
    original = "Inline: `<!-- BEGIN query: handoff -->` some text\n<!-- END query -->\n"
    doc = _write(tmp_path / "doc3.md", original)
    result = process_file(str(doc), str(tmp_path), check_mode=False)
    assert result == {"changed": False, "changedCount": 0, "errorCount": 0}
    assert doc.read_text(encoding="utf-8") == original


def test_process_file_bogus_type_is_business_error_not_transport(tmp_path):
    original = "# Doc4\n\n<!-- BEGIN query: totally-bogus-type -->\nold\n<!-- END query -->\n"
    doc = _write(tmp_path / "doc4.md", original)
    result = process_file(str(doc), str(tmp_path), check_mode=False)
    # queryRecords: unknown type -> per-callout warning, errorCount=1, file
    # left UNCHANGED (business error, not a transport crash).
    assert result == {"changed": False, "changedCount": 0, "errorCount": 1}
    assert doc.read_text(encoding="utf-8") == original


def test_process_file_duplicate_identical_markers_only_first_refreshed(tmp_path):
    """Faithful oracle-bug repro (negative-spec, module docstring): two
    IDENTICAL begin-marker callouts in one file — only the first is
    refreshed; the second is silently left stale. Confirmed live against
    the node oracle 2026-07-17 (see module docstring)."""
    doc = _write(
        tmp_path / "doc5.md",
        "# Doc5\n\n"
        "<!-- BEGIN query: handoff where=roadmap_id=nonexistent-marker-xyz -->\n"
        "old1\n"
        "<!-- END query -->\n\n"
        "<!-- BEGIN query: handoff where=roadmap_id=nonexistent-marker-xyz -->\n"
        "old2\n"
        "<!-- END query -->\n",
    )
    result = process_file(str(doc), str(tmp_path), check_mode=False)
    assert result == {"changed": True, "changedCount": 1, "errorCount": 0}
    updated = doc.read_text(encoding="utf-8")
    assert "old1" not in updated  # first callout refreshed
    assert "old2" in updated  # second callout left stale (the bug)


def test_main_full_walk_write_mode_matches_oracle(tmp_path, capsys):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "archive").mkdir()
    (tmp_path / "sub").mkdir()

    _write(
        tmp_path / "doc1.md",
        "# Doc1\n\n<!-- BEGIN query: handoff where=roadmap_id=nonexistent-marker-xyz -->\n"
        "placeholder old content\n<!-- END query -->\n\nSome text.\n",
    )
    _write(
        tmp_path / "doc4.md",
        "# Doc4\n\n<!-- BEGIN query: totally-bogus-type -->\nold\n<!-- END query -->\n",
    )
    _write(
        tmp_path / "sub" / "nested.md",
        "# Nested\n\n<!-- BEGIN query: handoff where=roadmap_id=nonexistent-marker-xyz -->\n"
        "placeholder old content\n<!-- END query -->\n",
    )
    _write(
        tmp_path / "node_modules" / "should-be-ignored.md",
        "<!-- BEGIN query: totally-bogus-type -->\nold\n<!-- END query -->\n",
    )
    _write(
        tmp_path / "archive" / "should-be-ignored.md",
        "<!-- BEGIN query: totally-bogus-type -->\nold\n<!-- END query -->\n",
    )

    rc = main(["--root", str(tmp_path)])
    out = capsys.readouterr()

    # Oracle: exit 1 (errorCount>0 from doc4), doc1+sub/nested.md updated,
    # node_modules/archive never walked (never contribute the bogus-type error).
    assert rc == 1
    assert "[updated] doc1.md (1 callout(s))" in out.out
    assert "[updated] sub/nested.md (1 callout(s))" in out.out or "[updated] sub" + os.sep + "nested.md (1 callout(s))" in out.out
    assert "should-be-ignored" not in out.out
    assert "should-be-ignored" not in out.err


def test_main_check_mode_reports_and_exits_1_without_writing(tmp_path, capsys):
    doc = _write(
        tmp_path / "doc1.md",
        "# Doc1\n\n<!-- BEGIN query: handoff where=roadmap_id=nonexistent-marker-xyz -->\n"
        "placeholder old content\n<!-- END query -->\n",
    )
    original = doc.read_text(encoding="utf-8")

    rc = main(["--root", str(tmp_path), "--check"])
    out = capsys.readouterr()

    assert rc == 1
    assert "[would change] doc1.md (1 callout(s))" in out.out
    assert "file(s) have out-of-sync query callouts" in out.err
    # --check must not write.
    assert doc.read_text(encoding="utf-8") == original


def test_main_all_up_to_date_message(tmp_path, capsys):
    _write(tmp_path / "plain.md", "# No callouts here.\n")
    rc = main(["--root", str(tmp_path)])
    out = capsys.readouterr()
    assert rc == 0
    assert "All query callouts are up to date." in out.out


def test_main_unknown_argument_exits_2_not_1(tmp_path):
    # DIVERGENT-FROM-ORACLE, documented (module docstring exit-code contract):
    # the oracle exits 1 for an unrecognized flag; this port uses a dedicated
    # CLI-usage exit code (2) so a caller cannot mistake a typo'd flag for a
    # "docs are stale" business failure. This is the ONLY deliberate exit-code
    # deviation from the oracle in this port.
    rc = main(["--bogus-flag"])
    assert rc == 2


def test_main_native_crash_is_isolated_as_transport_failure(tmp_path, monkeypatch):
    doc = _write(
        tmp_path / "doc1.md",
        "# Doc1\n\n<!-- BEGIN query: handoff -->\nold\n<!-- END query -->\n",
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated native crash")

    # No subprocess boundary left to crash "for free" (see module docstring's
    # per-callout crash-isolation negative-spec) — force one directly on the
    # native call this module now makes in-process.
    monkeypatch.setattr(_rq_mod, "query_records", _boom)
    rc = main(["--root", str(tmp_path)])
    assert rc == 3
    assert doc.read_text(encoding="utf-8") == (
        "# Doc1\n\n<!-- BEGIN query: handoff -->\nold\n<!-- END query -->\n"
    )


def test_run_query_records_native_raises_business_error_for_unknown_type(tmp_path):
    with pytest.raises(QueryRecordsBusinessError):
        _run_query_records_native(_base_query_opts(type="totally-bogus-type"), str(tmp_path))


def test_run_query_records_native_raises_business_error_for_bad_where(tmp_path):
    # "not a valid clause shape" -> _parse_where's SystemExit(1), folded into
    # QueryRecordsBusinessError (business, not transport) — see the module
    # docstring's exit-code contract update for the 2026-07-22 de-node port.
    with pytest.raises(QueryRecordsBusinessError):
        _run_query_records_native(
            _base_query_opts(where="this is not a valid clause"), str(tmp_path)
        )


def test_run_query_records_native_raises_transport_error_on_unexpected_crash(tmp_path, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(_rq_mod, "query_records", _boom)
    with pytest.raises(QueryRecordsTransportError):
        _run_query_records_native(_base_query_opts(), str(tmp_path))


def test_run_query_records_native_renders_matching_record(tmp_path):
    _write_handoff(tmp_path, "a-handoff.md", roadmap_id="rm-2")
    expansion = _run_query_records_native(
        _base_query_opts(where="roadmap_id=rm-2"), str(tmp_path), str(tmp_path)
    )
    assert expansion == "- [Test Handoff](state/handoffs/a-handoff.md) — open"


# ---------------------------------------------------------------------------
# Roadmap-callout archive-follow (cross-repo/inbox/2026-08-04-market-
# intelligence-em-baton-terminal-state-not-cleared-programmatically.md
# defect 2) — a `handoff where=roadmap_id=...` callout must render an
# archived baton's TRUE terminal state and a live archive/ link, never a
# stale `in_flight` against a dead `state/handoffs/` path.
# ---------------------------------------------------------------------------


def test_archived_baton_renders_terminal_state_with_live_archive_link(tmp_path):
    _write_archived_handoff(
        tmp_path, "2026-07", "shipped-baton.md",
        roadmap_id="rm-shipped", deployment_state="shipped", sprint=1,
    )
    expansion = _run_query_records_native(
        _base_query_opts(where="roadmap_id=rm-shipped", sort="sprint"), str(tmp_path), str(tmp_path)
    )
    assert expansion == "- [Test Handoff](archive/handoffs/2026-07/shipped-baton.md) — shipped"
    assert "state/handoffs" not in expansion  # no dead live-tree link


def test_live_and_archived_batons_for_same_roadmap_both_render_correctly(tmp_path):
    _write_handoff(tmp_path, "live-baton.md", roadmap_id="rm-mixed", deployment_state="in_flight", sprint=2)
    _write_archived_handoff(
        tmp_path, "2026-07", "shipped-baton.md",
        roadmap_id="rm-mixed", deployment_state="shipped", sprint=1,
    )
    expansion = _run_query_records_native(
        _base_query_opts(where="roadmap_id=rm-mixed", sort="sprint"), str(tmp_path), str(tmp_path)
    )
    lines = expansion.split("\n")
    assert lines == [
        "- [Test Handoff](archive/handoffs/2026-07/shipped-baton.md) — shipped",
        "- [Test Handoff](state/handoffs/live-baton.md) — in_flight",
    ]


def test_dangling_stub_id_in_neither_live_nor_archive_degrades_to_omission(tmp_path):
    # A predecessor pointer to a stub_id that exists in NEITHER live nor
    # archived handoffs (the memo's sibling 80-dangling-pointers finding) —
    # must not crash and must not fabricate a dead link.
    expansion = _run_query_records_native(
        _base_query_opts(where="roadmap_id=rm-nothing-here", sort="sprint"), str(tmp_path), str(tmp_path)
    )
    assert expansion == ""


def test_handoff_query_without_roadmap_id_stays_live_only(tmp_path):
    # No `roadmap_id=` in `where` -> no archive union; this is the
    # non-roadmap-callout shape other callers (session_hierarchy_derive.py,
    # ceremony/renderers.py, roadmap/number_stubs.py) rely on staying live-only.
    _write_handoff(tmp_path, "live-baton.md", status="open")
    _write_archived_handoff(tmp_path, "2026-07", "shipped-baton.md", deployment_state="shipped")
    expansion = _run_query_records_native(_base_query_opts(), str(tmp_path), str(tmp_path))
    assert expansion == "- [Test Handoff](state/handoffs/live-baton.md) — open"


# ---------------------------------------------------------------------------
# Session self-claim (coordinator_core.session.claims.self_claim) — see
# refresh_queries.py's module-docstring "Session self-claim" note.
# ---------------------------------------------------------------------------


def test_process_file_write_self_claims_the_written_path(tmp_path, monkeypatch):
    doc = _write(
        tmp_path / "doc1.md",
        "<!-- BEGIN query: handoff -->\nold\n<!-- END query -->\n",
    )
    claimed: list[str] = []
    monkeypatch.setattr(_rq_mod.claims, "self_claim", lambda path, **_kw: claimed.append(path) or True)

    result = process_file(str(doc), str(tmp_path), check_mode=False)

    assert result["changed"] is True
    assert claimed == [str(doc)]


def test_process_file_check_mode_never_self_claims(tmp_path, monkeypatch):
    doc = _write(
        tmp_path / "doc1.md",
        "<!-- BEGIN query: handoff -->\nold\n<!-- END query -->\n",
    )
    claimed: list[str] = []
    monkeypatch.setattr(_rq_mod.claims, "self_claim", lambda path, **_kw: claimed.append(path) or True)

    result = process_file(str(doc), str(tmp_path), check_mode=True)

    assert result["changed"] is True  # --check still reports out-of-sync...
    assert claimed == []  # ...but writes nothing, so nothing is claimed.
    # Confirm --check really wrote nothing to disk.
    assert doc.read_text(encoding="utf-8") == (
        "<!-- BEGIN query: handoff -->\nold\n<!-- END query -->\n"
    )


def test_process_file_self_claim_failure_does_not_break_write_or_result(tmp_path, monkeypatch):
    doc = _write(
        tmp_path / "doc1.md",
        "<!-- BEGIN query: handoff -->\nold\n<!-- END query -->\n",
    )

    def _boom(path, **_kw):
        raise OSError("simulated self-claim failure")

    monkeypatch.setattr(_rq_mod.claims, "self_claim", _boom)

    result = process_file(str(doc), str(tmp_path), check_mode=False)

    # The write and the reported result are unaffected by the self-claim
    # blowing up (AC-2) — a failed self-claim must never be fatal.
    assert result == {"changed": True, "changedCount": 1, "errorCount": 0}
    assert doc.read_text(encoding="utf-8") == (
        "<!-- BEGIN query: handoff -->\n<!-- END query -->\n"
    )


def test_main_self_claim_failure_leaves_exit_status_unchanged(tmp_path, monkeypatch):
    _write(
        tmp_path / "doc1.md",
        "<!-- BEGIN query: handoff -->\nold\n<!-- END query -->\n",
    )

    def _boom(path, **_kw):
        raise OSError("simulated self-claim failure")

    monkeypatch.setattr(_rq_mod.claims, "self_claim", _boom)

    rc = main(["--root", str(tmp_path)])

    assert rc == 0  # write-mode success is unaffected by an advisory-only failure

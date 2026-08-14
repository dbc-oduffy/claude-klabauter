"""Unit tests for coordinator_core.snippet_sync.verify — the consolidated
verify/--fix/--list engine replacing 7 retired
`coordinator/bin/verify-<name>-sync.sh` scripts.

Golden-diff parity against the 7 retired bash scripts (run against the live
DoE-side registry.toml + snippets/) is verified separately at build time
(see recipe-t3a-g3.md § 6 build notes) — these tests isolate each of the 4
header-extraction dialects, the allow_insert DR-6 placement logic, and the
--list canonical-universe (Q15) semantics against synthetic fixtures so they
don't depend on the sibling DoE checkout's exact file layout.

Spec backlink: DoE scratch/subagent-sandbox/bash-to-python-engine-migration/recipe-t3a-g3.md § 6
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from coordinator_core.snippet_sync.verify import (
    SnippetCommentResidueError,
    _assert_no_residual_comment_fragments,
    _extract_snippet_body,
    _strip_leading_html_comments,
    run,
)


def _make_plugin_root(tmp_path: Path) -> Path:
    plugin_root = tmp_path / "plugin"
    (plugin_root / "snippets").mkdir(parents=True)
    (plugin_root / "agents").mkdir(parents=True)
    return plugin_root


def _write_registry(plugin_root: Path, body: str) -> None:
    (plugin_root / "snippets" / "registry.toml").write_text(
        textwrap.dedent(body), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# header_style dialects
# ---------------------------------------------------------------------------


def test_header_style_comment_block(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
        sentinel_end   = "<!-- END foo -->"
        header_style   = "comment-block"
        consumers      = ["agents/consumer.md"]
        """,
    )
    (plugin_root / "snippets" / "foo.md").write_text(
        "<!-- canonical -->\n<!-- consumers -->\n\nBody line one.\nBody line two.\n",
        encoding="utf-8",
    )
    (plugin_root / "agents" / "consumer.md").write_text(
        "# Consumer\n\n"
        "<!-- BEGIN foo (synced from snippets/foo.md) -->\n"
        "Body line one.\nBody line two.\n"
        "<!-- END foo -->\n",
        encoding="utf-8",
    )
    outcome = run("foo", "verify", plugin_root=plugin_root)
    assert outcome.exit_code == 0
    assert outcome.lines == [f"OK           {plugin_root / 'agents/consumer.md'}"]


def test_header_style_fixed_2_line(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin  = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
        sentinel_end    = "<!-- END foo -->"
        header_style    = "fixed-2-line"
        consumer_source = "scan"
        search_scope    = "plugin-root"
        consumers       = []
        """,
    )
    (plugin_root / "snippets" / "foo.md").write_text(
        "<!-- comment header -->\n\nBody content.\n", encoding="utf-8"
    )
    (plugin_root / "agents" / "consumer.md").write_text(
        "<!-- BEGIN foo (synced from snippets/foo.md) -->\n"
        "Body content.\n"
        "<!-- END foo -->\n",
        encoding="utf-8",
    )
    outcome = run("foo", "verify", plugin_root=plugin_root)
    assert outcome.exit_code == 0
    assert outcome.lines[0].startswith("OK")


def test_header_style_fixed_2_line_strip_end_sentinel(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
        sentinel_end   = "<!-- END foo -->"
        header_style   = "fixed-2-line-strip-end-sentinel"
        allow_insert   = true
        consumers      = ["agents/consumer.md"]
        """,
    )
    # Snippet file structure: comment, blank, body..., then a (defensive)
    # trailing END-sentinel-shaped line that must be stripped from the body.
    (plugin_root / "snippets" / "foo.md").write_text(
        "<!-- comment header -->\n\n## Heading\nBody content.\n<!-- END foo -->\n",
        encoding="utf-8",
    )
    (plugin_root / "agents" / "consumer.md").write_text(
        "<!-- BEGIN foo (synced from snippets/foo.md) -->\n"
        "## Heading\nBody content.\n"
        "<!-- END foo -->\n",
        encoding="utf-8",
    )
    outcome = run("foo", "verify", plugin_root=plugin_root)
    assert outcome.exit_code == 0
    assert outcome.lines[0].startswith("OK")


def test_header_style_sentinel_embedded(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
        sentinel_end   = "<!-- END foo -->"
        header_style   = "sentinel-embedded"
        consumers      = ["agents/consumer.md"]
        """,
    )
    (plugin_root / "snippets" / "foo.md").write_text(
        "<!-- comment header -->\n\n"
        "<!-- BEGIN foo (synced from snippets/foo.md) -->\n"
        "## Heading\nBody content.\n"
        "<!-- END foo -->\n",
        encoding="utf-8",
    )
    (plugin_root / "agents" / "consumer.md").write_text(
        "<!-- BEGIN foo (synced from snippets/foo.md) -->\n"
        "## Heading\nBody content.\n"
        "<!-- END foo -->\n",
        encoding="utf-8",
    )
    outcome = run("foo", "verify", plugin_root=plugin_root)
    assert outcome.exit_code == 0
    assert outcome.lines[0].startswith("OK")


def test_empty_snippet_body_fails_loud(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
        sentinel_end   = "<!-- END foo -->"
        header_style   = "sentinel-embedded"
        consumers      = ["agents/consumer.md"]
        """,
    )
    # No BEGIN/END wrapper present in the snippet file itself -> empty body.
    (plugin_root / "snippets" / "foo.md").write_text("no wrapper here\n", encoding="utf-8")
    (plugin_root / "agents" / "consumer.md").write_text(
        "<!-- BEGIN foo (synced from snippets/foo.md) -->\nstuff\n<!-- END foo -->\n",
        encoding="utf-8",
    )
    outcome = run("foo", "verify", plugin_root=plugin_root)
    assert outcome.exit_code == 2
    assert any("empty" in line for line in outcome.stderr_lines)


# ---------------------------------------------------------------------------
# --fix / MISMATCH / MISSING_END
# ---------------------------------------------------------------------------


def test_fix_rewrites_mismatched_block(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
        sentinel_end   = "<!-- END foo -->"
        header_style   = "fixed-2-line"
        consumers      = ["agents/consumer.md"]
        """,
    )
    (plugin_root / "snippets" / "foo.md").write_text(
        "<!-- comment -->\n\nCorrect body.\n", encoding="utf-8"
    )
    consumer = plugin_root / "agents" / "consumer.md"
    consumer.write_text(
        "<!-- BEGIN foo (synced from snippets/foo.md) -->\n"
        "WRONG body.\n"
        "<!-- END foo -->\n",
        encoding="utf-8",
    )
    verify_outcome = run("foo", "verify", plugin_root=plugin_root)
    assert verify_outcome.exit_code == 1
    assert verify_outcome.lines[0].startswith("MISMATCH")

    fix_outcome = run("foo", "--fix", plugin_root=plugin_root)
    assert fix_outcome.exit_code == 0
    assert fix_outcome.lines[0].startswith("FIXED")
    assert "Correct body." in consumer.read_text(encoding="utf-8")

    reverify = run("foo", "--check", plugin_root=plugin_root)
    assert reverify.exit_code == 0
    assert reverify.lines[0].startswith("OK")


def test_missing_end_sentinel_flagged(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
        sentinel_end   = "<!-- END foo -->"
        header_style   = "fixed-2-line"
        consumers      = ["agents/consumer.md"]
        """,
    )
    (plugin_root / "snippets" / "foo.md").write_text(
        "<!-- comment -->\n\nBody.\n", encoding="utf-8"
    )
    (plugin_root / "agents" / "consumer.md").write_text(
        "<!-- BEGIN foo (synced from snippets/foo.md) -->\nBody, no end.\n",
        encoding="utf-8",
    )
    outcome = run("foo", "verify", plugin_root=plugin_root)
    assert outcome.exit_code == 1
    assert outcome.lines[0].startswith("MISSING_END")


# ---------------------------------------------------------------------------
# allow_insert (quota-self-detect-preamble's differentiator, DR-6 placement)
# ---------------------------------------------------------------------------


def test_allow_insert_false_silently_excludes_sentinelless_consumer(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
        sentinel_end   = "<!-- END foo -->"
        header_style   = "fixed-2-line"
        consumers      = ["agents/consumer.md"]
        """,
    )
    (plugin_root / "snippets" / "foo.md").write_text(
        "<!-- comment -->\n\nBody.\n", encoding="utf-8"
    )
    (plugin_root / "agents" / "consumer.md").write_text(
        "# No sentinel here at all.\n", encoding="utf-8"
    )
    outcome = run("foo", "verify", plugin_root=plugin_root)
    # allow_insert=False (default): exists-but-no-sentinel is NOT a consumer —
    # silently excluded, no MISSING flag, exit 0.
    assert outcome.exit_code == 0
    assert outcome.lines == [
        "no consumers found — nothing to verify (run --fix on the consumer "
        "files first to insert sentinel blocks)"
    ]


def test_allow_insert_true_flags_missing_and_inserts_on_fix(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
        sentinel_end   = "<!-- END foo -->"
        header_style   = "fixed-2-line-strip-end-sentinel"
        allow_insert   = true
        consumers      = ["agents/consumer.md"]
        """,
    )
    (plugin_root / "snippets" / "foo.md").write_text(
        "<!-- comment -->\n\n## Heading\nBody.\n", encoding="utf-8"
    )
    consumer = plugin_root / "agents" / "consumer.md"
    consumer.write_text(
        "# Mock Agent\n\n## Role\n\nSome text.\n", encoding="utf-8"
    )

    verify_outcome = run("foo", "verify", plugin_root=plugin_root)
    assert verify_outcome.exit_code == 1
    assert verify_outcome.lines[0].startswith("MISSING ")

    fix_outcome = run("foo", "--fix", plugin_root=plugin_root)
    assert fix_outcome.exit_code == 0
    assert fix_outcome.lines[0].startswith("INSERTED")
    text = consumer.read_text(encoding="utf-8")
    assert "<!-- BEGIN foo (synced from snippets/foo.md) -->" in text
    assert "<!-- END foo -->" in text
    assert "Body." in text

    reverify = run("foo", "--check", plugin_root=plugin_root)
    assert reverify.exit_code == 0
    assert reverify.lines[0].startswith("OK")


# ---------------------------------------------------------------------------
# --list canonical-universe (Q15)
# ---------------------------------------------------------------------------


def test_list_registry_driven_is_canonical_universe_unfiltered(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
        sentinel_end   = "<!-- END foo -->"
        header_style   = "fixed-2-line"
        consumers      = ["agents/present.md", "agents/absent.md"]
        """,
    )
    (plugin_root / "snippets" / "foo.md").write_text(
        "<!-- comment -->\n\nBody.\n", encoding="utf-8"
    )
    # present.md exists but has no sentinel; absent.md doesn't exist at all.
    (plugin_root / "agents" / "present.md").write_text("no sentinel\n", encoding="utf-8")

    outcome = run("foo", "--list", plugin_root=plugin_root)
    assert outcome.exit_code == 0
    # Both appear — canonical universe regardless of on-disk state (Q15).
    assert str(plugin_root / "agents/present.md") in outcome.lines
    assert str(plugin_root / "agents/absent.md") in outcome.lines


def test_list_scan_driven_is_discovered_set_only(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin  = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
        sentinel_end    = "<!-- END foo -->"
        header_style    = "fixed-2-line"
        consumer_source = "scan"
        search_scope    = "plugin-root"
        consumers       = []
        """,
    )
    (plugin_root / "snippets" / "foo.md").write_text(
        "<!-- comment -->\n\nBody.\n", encoding="utf-8"
    )
    (plugin_root / "agents" / "consumer.md").write_text(
        "<!-- BEGIN foo (synced from snippets/foo.md) -->\nBody.\n<!-- END foo -->\n",
        encoding="utf-8",
    )
    outcome = run("foo", "--list", plugin_root=plugin_root)
    assert outcome.exit_code == 0
    assert outcome.lines == [str(plugin_root / "agents/consumer.md")]


def test_fence_aware_skips_fenced_occurrence(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin  = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
        sentinel_end    = "<!-- END foo -->"
        header_style    = "fixed-2-line"
        consumer_source = "scan"
        search_scope    = "plugin-root"
        fence_aware     = true
        consumers       = []
        """,
    )
    (plugin_root / "snippets" / "foo.md").write_text(
        "<!-- comment -->\n\nBody.\n", encoding="utf-8"
    )
    # Sentinel appears ONLY inside a fenced code block — a documentation
    # example, not a live consumer block.
    (plugin_root / "agents" / "doc-only.md").write_text(
        "Example usage:\n\n```\n<!-- BEGIN foo (synced from snippets/foo.md) -->\n"
        "Body.\n<!-- END foo -->\n```\n",
        encoding="utf-8",
    )
    outcome = run("foo", "--list", plugin_root=plugin_root)
    assert outcome.exit_code == 0
    assert outcome.lines == []


# ---------------------------------------------------------------------------
# Mode dispatch
# ---------------------------------------------------------------------------


def test_check_is_alias_for_verify(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
        sentinel_end   = "<!-- END foo -->"
        header_style   = "fixed-2-line"
        consumers      = []
        """,
    )
    (plugin_root / "snippets" / "foo.md").write_text(
        "<!-- comment -->\n\nBody.\n", encoding="utf-8"
    )
    outcome = run("foo", "--check", plugin_root=plugin_root)
    assert outcome.exit_code == 0


def test_unknown_mode_exits_2(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end   = "e"
        consumers      = []
        """,
    )
    outcome = run("foo", "--bogus-mode", plugin_root=plugin_root)
    assert outcome.exit_code == 2


def test_unknown_snippet_name_exits_2(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end   = "e"
        consumers      = []
        """,
    )
    outcome = run("not-foo", "verify", plugin_root=plugin_root)
    assert outcome.exit_code == 2
    assert "unknown snippet" in outcome.stderr_lines[0]


def test_missing_snippet_file_exits_1(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end   = "e"
        consumers      = []
        """,
    )
    # No snippets/foo.md written.
    outcome = run("foo", "verify", plugin_root=plugin_root)
    assert outcome.exit_code == 1
    assert "canonical snippet not found" in outcome.stderr_lines[0]


# ---------------------------------------------------------------------------
# Defect A — comment-block leading-header strip must be comment-aware, not
# line-aware (multi-line comments, multiple leading blocks, same-line
# trailing content, unterminated comments, mid-body `<!--`).
# ---------------------------------------------------------------------------


def test_strip_leading_comment_single_line_unchanged():
    lines = "<!-- x -->\n\nBody line one.\nBody line two.".split("\n")
    out = _strip_leading_html_comments(lines)
    assert out == ["Body line one.", "Body line two."]


def test_strip_leading_comment_multiline_spans_all_lines():
    text = (
        "<!-- some header\n"
        "     continuation prose\n"
        "     more prose -->\n"
        "\n"
        "body starts here\n"
        "second body line"
    )
    out = _strip_leading_html_comments(text.split("\n"))
    # The whole 3-line comment (and the blank after it) is consumed — no
    # continuation prose, no dangling '-->', leaks into the body.
    assert out == ["body starts here", "second body line"]
    assert not any("-->" in line for line in out)
    assert not any("continuation prose" in line for line in out)


def test_strip_leading_comment_multiple_consecutive_blocks():
    text = (
        "<!-- canonical source -->\n"
        "<!-- consumers: fixed list\n"
        "     spanning two lines -->\n"
        "\n"
        "<!-- a third, separately-blank-separated block -->\n"
        "\n"
        "Real body content."
    )
    out = _strip_leading_html_comments(text.split("\n"))
    assert out == ["Real body content."]


def test_strip_leading_comment_trailing_content_same_line_as_closer():
    # Decision (documented in the helper's docstring): content after the
    # closing '-->' on the SAME line is body, not comment — it is kept, and
    # leading-comment consumption stops there (body has started).
    text = "<!-- header --> trailing body starts here\nsecond line"
    out = _strip_leading_html_comments(text.split("\n"))
    assert out == [" trailing body starts here", "second line"]


def test_strip_leading_comment_unterminated_fails_loud():
    # Decision (documented in the helper's docstring): an unterminated
    # leading '<!--' raises rather than silently swallowing the whole body.
    text = "<!-- unterminated header\nmore text that never closes\nand more"
    with pytest.raises(ValueError, match="unterminated leading HTML comment"):
        _strip_leading_html_comments(text.split("\n"), snippet_name="my-snippet")


def test_strip_leading_comment_mid_body_comment_not_stripped():
    text = "<!-- header -->\n\nBody line.\n<!-- this is body content, not header -->\nMore body."
    out = _strip_leading_html_comments(text.split("\n"))
    assert out == [
        "Body line.",
        "<!-- this is body content, not header -->",
        "More body.",
    ]


def test_extract_snippet_body_comment_block_unterminated_reports_error(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
        sentinel_end   = "<!-- END foo -->"
        header_style   = "comment-block"
        consumers      = ["agents/consumer.md"]
        """,
    )
    (plugin_root / "snippets" / "foo.md").write_text(
        "<!-- unterminated\nnever closes\nBody content.\n", encoding="utf-8"
    )
    (plugin_root / "agents" / "consumer.md").write_text(
        "<!-- BEGIN foo (synced from snippets/foo.md) -->\nBody content.\n<!-- END foo -->\n",
        encoding="utf-8",
    )
    outcome = run("foo", "verify", plugin_root=plugin_root)
    assert outcome.exit_code == 2
    assert any("unterminated" in line for line in outcome.stderr_lines)


# ---------------------------------------------------------------------------
# Defect C — residual-HTML-comment-fragment assertion (companion to the
# verifier's byte-equality contract; catches defects that live IN the
# canonical source itself).
# ---------------------------------------------------------------------------


def test_residual_comment_fragment_not_firing_on_clean_body():
    body = "Body line one.\nBody line two.\n\n<!-- a well-formed inline example -->\nMore."
    _assert_no_residual_comment_fragments(body, "foo")  # must not raise


def test_residual_comment_fragment_fires_on_bare_closer():
    body = "orphaned continuation prose\nmore prose -->\n\nbody starts here"
    with pytest.raises(SnippetCommentResidueError, match="bare '-->'"):
        _assert_no_residual_comment_fragments(body, "foo")


def test_residual_comment_fragment_fires_on_unclosed_opener():
    body = "Body content.\n<!-- an opener that never closes\nmore text"
    with pytest.raises(SnippetCommentResidueError, match="unclosed"):
        _assert_no_residual_comment_fragments(body, "foo")


def test_residual_comment_fragment_would_have_caught_doe_incident():
    """Demonstrates the DoE 2026-07-25 incident shape directly: reconstruct
    what the PRE-FIX single-line-only strip would have produced from a
    multi-line leading header (dropping only the opening line, leaking the
    continuation prose and the dangling '-->' into the body), and show the
    Defect C assertion fires on it — this is the check that would have
    caught the live incident before it ever reached a consumer file.
    """
    lines = (
        "<!-- some header\n"
        "     continuation prose\n"
        "     more prose -->\n"
        "\n"
        "body starts here"
    ).split("\n")
    # Pre-fix behaviour: only lines starting with '<!--' or blank (while
    # still skipping) were dropped — here that's just the first line.
    pre_fix_skip = True
    pre_fix_out = []
    for line in lines:
        if pre_fix_skip and line.startswith("<!--"):
            continue
        if pre_fix_skip and line.strip() == "":
            continue
        pre_fix_skip = False
        pre_fix_out.append(line)
    pre_fix_body = "\n".join(pre_fix_out)
    assert "continuation prose" in pre_fix_body
    assert "-->" in pre_fix_body

    with pytest.raises(SnippetCommentResidueError, match="bare '-->'"):
        _assert_no_residual_comment_fragments(pre_fix_body, "reviewer-calibration")

    # The FIXED extractor produces a clean body that passes the same assertion.
    fixed_out = _strip_leading_html_comments(lines)
    fixed_body = "\n".join(fixed_out)
    assert "continuation prose" not in fixed_body
    assert "-->" not in fixed_body
    _assert_no_residual_comment_fragments(fixed_body, "reviewer-calibration")  # no raise


def test_residual_comment_fragment_wired_into_run_for_non_comment_block_style(tmp_path):
    # Defect C says the assertion must cover EVERY header_style, not just
    # comment-block. fixed-2-line strips only the first 2 lines
    # unconditionally, so a stray unclosed '<!--' surviving into the body
    # (e.g. a 3rd header line the header_style doesn't know to strip) is
    # exactly the class this assertion exists to catch.
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin  = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
        sentinel_end    = "<!-- END foo -->"
        header_style    = "fixed-2-line"
        consumer_source = "scan"
        search_scope    = "plugin-root"
        consumers       = []
        """,
    )
    (plugin_root / "snippets" / "foo.md").write_text(
        "<!-- comment header -->\n\n<!-- an opener the 2-line strip doesn't know about\nBody content.\n",
        encoding="utf-8",
    )
    (plugin_root / "agents" / "consumer.md").write_text(
        "<!-- BEGIN foo (synced from snippets/foo.md) -->\nBody content.\n<!-- END foo -->\n",
        encoding="utf-8",
    )
    outcome = run("foo", "verify", plugin_root=plugin_root)
    assert outcome.exit_code == 2
    assert any("residual HTML-comment fragment" in line for line in outcome.stderr_lines)


def test_extract_snippet_body_still_matches_existing_dialects(tmp_path):
    # Regression guard: _extract_snippet_body's public contract (signature +
    # per-dialect behaviour) is unchanged for callers passing snippet_name.
    body = _extract_snippet_body(
        "<!-- x -->\n\nBody.\n",
        "comment-block",
        "<!-- BEGIN foo -->",
        "<!-- END foo -->",
        snippet_name="foo",
    )
    assert body == "Body.\n"


# ---------------------------------------------------------------------------
# ORPHAN-SENTINEL DETECTION (2026-07-25 DoE incident — see the
# "ORPHAN-SENTINEL DETECTION" block in verify.py for the four constraints).
#
# Fixtures reconstruct the real incident: `parallel-review-synthesizer.md`
# carried `reviewer-calibration` sentinels with a DRIFTED begin marker (no
# `(synced from ...)` clause, which is why DoE's exact-match grep missed it),
# while the snippet's own `consumers` list was EMPTY.
# ---------------------------------------------------------------------------

_ORPHAN_REGISTRY = """
    schema_version = 2

    [snippet.calib]
    sentinel_begin = "<!-- BEGIN calib (synced from snippets/calib.md) -->"
    sentinel_end   = "<!-- END calib -->"
    header_style = "sentinel-embedded"
    consumers = []
"""

_ORPHAN_SNIPPET = (
    "<!-- BEGIN calib (synced from snippets/calib.md) -->\n"
    "Findings below the `severity floor` are noise.\n"
    "<!-- END calib -->\n"
)


def _orphan_fixture(tmp_path, *, begin_line, extra_prose="", registry=_ORPHAN_REGISTRY):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root, registry)
    (plugin_root / "snippets" / "calib.md").write_text(_ORPHAN_SNIPPET, encoding="utf-8")
    (plugin_root / "agents" / "synth.md").write_text(
        "# synth\n\nNever suppress findings.\n\n"
        f"{begin_line}\n"
        "Findings below the `severity floor` are noise.\n"
        "<!-- END calib -->\n"
        f"{extra_prose}",
        encoding="utf-8",
    )
    return plugin_root


def test_orphan_detected_when_consumers_empty(tmp_path):
    # The ordering fix: `consumers = []` means the zero-consumers early return
    # fires, so a scan placed after it would never run on the one snippet that
    # actually had an orphan.
    plugin_root = _orphan_fixture(
        tmp_path, begin_line="<!-- BEGIN calib (synced from snippets/calib.md) -->"
    )
    result = run("calib", "verify", plugin_root=plugin_root)
    assert result.exit_code == 1
    assert any("ORPHAN" in line for line in result.lines)
    assert any("agents/synth.md" in line.replace("\\", "/") for line in result.lines)


def test_orphan_detected_with_drifted_sentinel_prefix_match(tmp_path):
    # Constraint 1: the incident's orphan lacked the `(synced from ...)` clause.
    # An exact-match scan misses it; the prefix match must not.
    plugin_root = _orphan_fixture(tmp_path, begin_line="<!-- BEGIN calib -->")
    result = run("calib", "verify", plugin_root=plugin_root)
    assert result.exit_code == 1
    assert any("DRIFTED sentinel" in line for line in result.lines)


def test_orphan_prefix_match_does_not_swallow_sibling_snippet_names(tmp_path):
    # `<!-- BEGIN calib-extended -->` must NOT register as a `calib` orphan —
    # the boundary after the name is load-bearing, a bare startswith is not.
    plugin_root = _orphan_fixture(tmp_path, begin_line="<!-- BEGIN calib-extended -->")
    result = run("calib", "verify", plugin_root=plugin_root)
    assert not any("ORPHAN" in line for line in result.lines)


def test_orphan_fix_fails_loud_and_deletes_nothing(tmp_path):
    # Constraint 2: --fix names both exits and modifies no bytes.
    plugin_root = _orphan_fixture(tmp_path, begin_line="<!-- BEGIN calib -->")
    victim = plugin_root / "agents" / "synth.md"
    before = victim.read_text(encoding="utf-8")
    result = run("calib", "--fix", plugin_root=plugin_root)
    assert result.exit_code == 1
    assert victim.read_text(encoding="utf-8") == before
    joined = "\n".join(result.lines)
    assert "(A)" in joined and "(B)" in joined
    assert "deliberately does not delete" in joined


def test_orphan_flags_adjacent_dependent_prose(tmp_path):
    # Constraint 3: a hand-authored line OUTSIDE the sentinels referencing
    # terminology introduced INSIDE them must be surfaced, or exit (B) leaves
    # it dangling and recreates the defect in a new costume.
    plugin_root = _orphan_fixture(
        tmp_path,
        begin_line="<!-- BEGIN calib -->",
        extra_prose="\nWhen applying the `severity floor`, drop minor findings.\n",
    )
    result = run("calib", "verify", plugin_root=plugin_root)
    joined = "\n".join(result.lines)
    assert "dependent prose OUTSIDE the block" in joined
    assert "severity floor" in joined


def test_orphan_scan_exempt_for_consumer_source_scan(tmp_path):
    # Constraint 4: sentinel-present IS enrolment under consumer_source="scan",
    # so an orphan cannot exist by construction and must never be reported.
    registry = """
        schema_version = 2

        [snippet.calib]
        sentinel_begin = "<!-- BEGIN calib (synced from snippets/calib.md) -->"
        sentinel_end   = "<!-- END calib -->"
        header_style = "sentinel-embedded"
        consumer_source = "scan"
        consumers = []
    """
    plugin_root = _orphan_fixture(
        tmp_path, begin_line="<!-- BEGIN calib -->", registry=registry
    )
    result = run("calib", "verify", plugin_root=plugin_root)
    assert not any("ORPHAN" in line for line in result.lines)


def test_registered_consumer_is_not_an_orphan(tmp_path):
    # The canonical no-false-positive case: an enrolled, in-sync consumer.
    registry = """
        schema_version = 2

        [snippet.calib]
        sentinel_begin = "<!-- BEGIN calib (synced from snippets/calib.md) -->"
        sentinel_end   = "<!-- END calib -->"
        header_style = "sentinel-embedded"
        consumers = ["agents/synth.md"]
    """
    plugin_root = _orphan_fixture(
        tmp_path,
        begin_line="<!-- BEGIN calib (synced from snippets/calib.md) -->",
        registry=registry,
    )
    result = run("calib", "verify", plugin_root=plugin_root)
    assert not any("ORPHAN" in line for line in result.lines)
    assert result.exit_code == 0


def test_canonical_snippet_file_is_never_its_own_orphan(tmp_path):
    # snippets/calib.md carries the sentinel by definition.
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root, _ORPHAN_REGISTRY)
    (plugin_root / "snippets" / "calib.md").write_text(_ORPHAN_SNIPPET, encoding="utf-8")
    result = run("calib", "verify", plugin_root=plugin_root)
    assert not any("ORPHAN" in line for line in result.lines)
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Review integration (2026-07-28 code-reviewer, findings 1/2/6) — the orphan
# scan's file-type scope, its per-file occurrence count, and the lexical
# tightness of the dependent-prose match.
# ---------------------------------------------------------------------------


def test_orphan_reports_every_occurrence_in_a_file_not_just_the_first(tmp_path):
    # Finding 2: the original shape `break`-ed after the first hit per file, so a
    # doc that accumulated several stale pasted copies reported one with no signal
    # the others existed. Silent caps are forbidden.
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root, _ORPHAN_REGISTRY)
    (plugin_root / "snippets" / "calib.md").write_text(_ORPHAN_SNIPPET, encoding="utf-8")
    block = "<!-- BEGIN calib -->\nbody\n<!-- END calib -->\n"
    (plugin_root / "agents" / "synth.md").write_text(
        "# synth\n\n" + block + "\nprose\n\n" + block + "\nmore\n\n" + block,
        encoding="utf-8",
    )
    result = run("calib", "verify", plugin_root=plugin_root)
    assert result.exit_code == 1
    header = next(line for line in result.lines if "ORPHAN" in line)
    assert "3 block(s) across 1 file(s)" in header


def test_orphan_detected_in_non_markdown_file_via_shell_dialect(tmp_path):
    # Finding 1: the `*.md`-only glob made a mis-pasted sentinel in a .sh/.py file
    # invisible — precisely the failure orphan-detection exists to catch. The
    # shell-comment dialect is a real sentinel form, not only an in-fence one.
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root, _ORPHAN_REGISTRY)
    (plugin_root / "snippets" / "calib.md").write_text(_ORPHAN_SNIPPET, encoding="utf-8")
    (plugin_root / "agents" / "helper.sh").write_text(
        "#!/bin/sh\n# BEGIN calib\n# body\n# END calib\n", encoding="utf-8"
    )
    result = run("calib", "verify", plugin_root=plugin_root)
    assert result.exit_code == 1
    assert any("helper.sh" in line for line in result.lines)


def test_orphan_scan_skips_undecodable_binary_files(tmp_path):
    # The widened glob relies on the UnicodeDecodeError guard rather than an
    # extension allowlist — a binary must not crash or match.
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root, _ORPHAN_REGISTRY)
    (plugin_root / "snippets" / "calib.md").write_text(_ORPHAN_SNIPPET, encoding="utf-8")
    (plugin_root / "agents" / "blob.bin").write_bytes(b"\x00\xff BEGIN calib \xfe")
    result = run("calib", "verify", plugin_root=plugin_root)
    assert result.exit_code == 0
    assert not any("blob.bin" in line for line in result.lines)


def test_dependent_prose_match_is_word_boundary_anchored(tmp_path):
    # Finding 6: raw substring matching made a 3-char term like `add` fire on
    # `address`. Independent of the definition-vs-reference problem the negative
    # spec covers — this axis is purely lexical.
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root, _ORPHAN_REGISTRY)
    (plugin_root / "snippets" / "calib.md").write_text(
        "<!-- BEGIN calib (synced from snippets/calib.md) -->\n"
        "Use the `add` verb.\n<!-- END calib -->\n",
        encoding="utf-8",
    )
    (plugin_root / "agents" / "synth.md").write_text(
        "# synth\n\n<!-- BEGIN calib -->\nUse the `add` verb.\n<!-- END calib -->\n"
        "\nThe address book is unrelated.\n",
        encoding="utf-8",
    )
    result = run("calib", "verify", plugin_root=plugin_root)
    joined = "\n".join(result.lines)
    assert "ORPHAN" in joined
    assert "address book" not in joined


# ---------------------------------------------------------------------------
# delivery = "inject" enforcement (registry schema_version 3, DoE 7ff1cb75e;
# answer memo cross-repo/inbox/2026-07-28-doe-claude-em-delivery-field-answer.md).
#
# On an inject row the block reaches consumers via contract_blocks: assembly and
# is pasted by NOTHING, so a pasted sentinel is an orphan BY CONSTRUCTION — even
# in a file named in `consumers`, which is a documentation set there. The one
# carve-out is `conditional_consumer`, which stays a genuine paste target.
# ---------------------------------------------------------------------------

_INJECT_REGISTRY = """
    schema_version = 3

    [snippet.calib]
    sentinel_begin = "<!-- BEGIN calib (synced from snippets/calib.md) -->"
    sentinel_end   = "<!-- END calib -->"
    header_style = "sentinel-embedded"
    delivery = "inject"
    consumers = ["agents/synth.md"]
"""


def test_inject_row_flags_sentinel_in_a_declared_plain_consumer(tmp_path):
    # The live case: 4 registry rows are inject WITH a non-empty consumers list.
    # Under `paste` this file is a registered consumer and NOT an orphan; under
    # `inject` the same file with the same sentinel IS one. If this test ever
    # passes for the wrong reason, the whole inject leg is dead.
    plugin_root = _orphan_fixture(
        tmp_path,
        begin_line="<!-- BEGIN calib (synced from snippets/calib.md) -->",
        registry=_INJECT_REGISTRY,
    )
    result = run("calib", "verify", plugin_root=plugin_root)
    assert result.exit_code == 1
    joined = "\n".join(result.lines)
    assert "ORPHAN" in joined
    assert "agents/synth.md" in joined.replace("\\", "/")
    assert 'delivery="inject"' in joined
    # Finding 1 (code-reviewer-a29f969d): the same file must NOT also be
    # reported as an active, in-sync (or fixed) consumer — that dual report
    # is exactly the bug: "this is correctly in sync" and "this is an orphan,
    # delete it by hand" about the same file in one run. Assert absence, not
    # just presence of ORPHAN, or a regression here is invisible to the suite.
    for status in ("OK ", "FIXED ", "MISMATCH "):
        assert not any(
            line.startswith(status) and "synth.md" in line.replace("\\", "/")
            for line in result.lines
        ), f"unexpected {status.strip()} line for synth.md alongside ORPHAN: {result.lines!r}"


def test_same_fixture_under_paste_delivery_is_not_an_orphan(tmp_path):
    # Control for the test above — identical tree, `delivery` flipped to paste.
    # Without this pair, the inject test could pass because of some unrelated
    # mismatch rather than because of the delivery axis.
    plugin_root = _orphan_fixture(
        tmp_path,
        begin_line="<!-- BEGIN calib (synced from snippets/calib.md) -->",
        registry=_INJECT_REGISTRY.replace('delivery = "inject"', 'delivery = "paste"'),
    )
    result = run("calib", "verify", plugin_root=plugin_root)
    assert not any("ORPHAN" in line for line in result.lines)


def test_inject_row_exempts_declared_conditional_consumer(tmp_path):
    # The carve-out. Four inject rows carry a genuinely-pasted example-game-repo
    # live-install conditional; treating inject as "nothing is ever pasted"
    # hard-errors on those legitimate pastes.
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 3

        [snippet.calib]
        sentinel_begin = "<!-- BEGIN calib (synced from snippets/calib.md) -->"
        sentinel_end   = "<!-- END calib -->"
        header_style = "sentinel-embedded"
        delivery = "inject"
        consumers = []

        [[snippet.calib.conditional_consumer]]
        path = "agents/conditional.md"
        condition_type = "file-exists"
        """,
    )
    (plugin_root / "snippets" / "calib.md").write_text(_ORPHAN_SNIPPET, encoding="utf-8")
    (plugin_root / "agents" / "conditional.md").write_text(
        "# conditional\n\n<!-- BEGIN calib (synced from snippets/calib.md) -->\n"
        "Findings below the `severity floor` are noise.\n<!-- END calib -->\n",
        encoding="utf-8",
    )
    result = run("calib", "verify", plugin_root=plugin_root)
    assert not any("ORPHAN" in line for line in result.lines)


def test_inject_row_exempts_declared_machine_local_key_conditional_consumer(tmp_path):
    # Finding 3 (code-reviewer-a29f969d): the carve-out test above only covers
    # condition_type = "file-exists" — this pins the machine-local-key leg,
    # which is also the exact path Finding 2's duplicate-resolution/duplicate-
    # NOTE bug lived on. With machine_local_bin left unset (default None),
    # `_ml_get` short-circuits to "" (key unset) without needing a real
    # resolver binary — see registry._ml_get.
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 3

        [snippet.calib]
        sentinel_begin = "<!-- BEGIN calib (synced from snippets/calib.md) -->"
        sentinel_end   = "<!-- END calib -->"
        header_style = "sentinel-embedded"
        delivery = "inject"
        consumers = []

        [[snippet.calib.conditional_consumer]]
        path = "agents/conditional.md"
        condition_type = "machine-local-key"
        condition_key = "SOME_KEY"
        """,
    )
    (plugin_root / "snippets" / "calib.md").write_text(_ORPHAN_SNIPPET, encoding="utf-8")
    result = run("calib", "verify", plugin_root=plugin_root)
    # No file on disk resolves the conditional (key is unset), so there is
    # nothing to sync and no orphan — the assertion of interest is the NOTE
    # count below, which pins Finding 2's fix (exactly one resolution call).
    assert not any("ORPHAN" in line for line in result.lines)
    note_count = sum(1 for line in result.stderr_lines if "key unset" in line)
    assert note_count == 1, (
        f"expected exactly one 'key unset' NOTE (single resolve_conditional_consumers "
        f"call), got {note_count}: {result.stderr_lines!r}"
    )


def test_inject_orphan_report_does_not_advise_adding_to_consumers(tmp_path):
    # Exit (A) is not merely reworded on an inject row — "add it to consumers"
    # is the WRONG repair there (nothing pastes from that list), so following it
    # would leave the block orphaned with the warning silenced.
    plugin_root = _orphan_fixture(
        tmp_path, begin_line="<!-- BEGIN calib -->", registry=_INJECT_REGISTRY
    )
    joined = "\n".join(run("calib", "verify", plugin_root=plugin_root).lines)
    assert "contract_blocks:" in joined
    assert "is NOT the fix" in joined


def test_zero_consumer_message_does_not_advise_fix_on_an_inject_row(tmp_path):
    # DR-240 Decision 4 applies to EVERY advice-bearing line, not just the orphan
    # report. "run --fix to insert sentinel blocks" on an inject row would have a
    # reader paste blocks that the next verify run reports as orphans.
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root, _INJECT_REGISTRY.replace('consumers = ["agents/synth.md"]', "consumers = []"))
    (plugin_root / "snippets" / "calib.md").write_text(_ORPHAN_SNIPPET, encoding="utf-8")
    joined = "\n".join(run("calib", "verify", plugin_root=plugin_root).lines)
    assert "no paste targets" in joined
    assert "Do NOT" in joined
    assert "run --fix on the consumer files first" not in joined


def test_zero_consumer_message_still_advises_fix_on_a_paste_row(tmp_path):
    # Control: the paste-row advice is correct and must survive.
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root, _INJECT_REGISTRY.replace('delivery = "inject"', 'delivery = "paste"').replace('consumers = ["agents/synth.md"]', "consumers = []"))
    (plugin_root / "snippets" / "calib.md").write_text(_ORPHAN_SNIPPET, encoding="utf-8")
    joined = "\n".join(run("calib", "verify", plugin_root=plugin_root).lines)
    assert "run --fix on the consumer files first" in joined
    assert "no paste targets" not in joined


# ---------------------------------------------------------------------------
# eligible_glob completeness (registry schema_version 4, DoE 355255cc3)
#
# The verifier leg: `registry.eligible_glob_gaps` computes the gap set (unit-
# tested in test_registry.py); these cover that `run()` actually REPORTS it,
# exits non-zero on it, mutates nothing on --fix, and reads the same content
# root every other tree-walking surface here does.
# ---------------------------------------------------------------------------

_V4_GLOB_REGISTRY = """
    schema_version = 4

    [snippet.foo]
    sentinel_begin = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
    sentinel_end   = "<!-- END foo -->"
    header_style   = "fixed-2-line"
    delivery       = "paste"
    eligible_glob  = "agents/*.md"
    consumers      = ["agents/enrolled.md"]

    [[snippet.foo.excluded_consumer]]
    path   = "agents/bespoke.md"
    reason = "sanctioned per-persona narrowing — carries a deliberate variant"
    """

_FOO_BEGIN = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
_FOO_END = "<!-- END foo -->"


def _v4_glob_fixture(tmp_path: Path, *, registry: str = _V4_GLOB_REGISTRY) -> Path:
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root, registry)
    (plugin_root / "snippets" / "foo.md").write_text(
        "<!-- canonical -->\n<!-- consumers -->\nCanonical body.\n", encoding="utf-8"
    )
    (plugin_root / "agents" / "enrolled.md").write_text(
        f"# Enrolled\n\n{_FOO_BEGIN}\nCanonical body.\n{_FOO_END}\n", encoding="utf-8"
    )
    (plugin_root / "agents" / "bespoke.md").write_text("# Bespoke variant\n", encoding="utf-8")
    return plugin_root


def test_eligible_glob_gap_is_reported_and_exits_non_zero(tmp_path):
    plugin_root = _v4_glob_fixture(tmp_path)
    (plugin_root / "agents" / "newcomer.md").write_text("# Newcomer\n", encoding="utf-8")

    result = run("foo", "verify", plugin_root=plugin_root)
    joined = "\n".join(result.lines)
    assert result.exit_code == 1
    assert "UNDECLARED" in joined
    assert "agents/newcomer.md" in joined
    # The declared members must not be reported — a check that flags everything
    # says nothing.
    assert "agents/enrolled.md" not in joined.split("UNDECLARED", 1)[1]
    assert "agents/bespoke.md" not in joined.split("UNDECLARED", 1)[1]
    # The in-sync consumer is still verified in the same run.
    assert any(line.startswith("OK ") for line in result.lines)


def test_fully_declared_universe_is_clean(tmp_path):
    """Control for the test above: same tree, no undeclared member. Without it,
    the gap report could be firing for an unrelated reason."""
    plugin_root = _v4_glob_fixture(tmp_path)
    result = run("foo", "verify", plugin_root=plugin_root)
    assert result.exit_code == 0
    assert not any("UNDECLARED" in line for line in result.lines)


def test_eligible_glob_gap_reported_even_with_no_active_consumers(tmp_path):
    """The gap scan must run BEFORE the zero-consumers early return — a row whose
    every glob member is undeclared is the extreme case of the defect, and it is
    exactly the row that resolves an empty active set."""
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        _V4_GLOB_REGISTRY.replace('consumers      = ["agents/enrolled.md"]', "consumers      = []"),
    )
    (plugin_root / "snippets" / "foo.md").write_text(
        "<!-- canonical -->\n<!-- consumers -->\nCanonical body.\n", encoding="utf-8"
    )
    (plugin_root / "agents" / "orphan-universe.md").write_text("# Nobody decided\n", encoding="utf-8")

    result = run("foo", "verify", plugin_root=plugin_root)
    joined = "\n".join(result.lines)
    assert result.exit_code == 1
    assert "UNDECLARED" in joined
    assert "agents/orphan-universe.md" in joined


def test_eligible_glob_gap_is_never_auto_repaired_on_fix(tmp_path):
    """Same NEGATIVE SPEC as the orphan scan: --fix reports the gap and writes
    nothing, because which declaration a gap wants is a human call."""
    plugin_root = _v4_glob_fixture(tmp_path)
    newcomer = plugin_root / "agents" / "newcomer.md"
    newcomer.write_text("# Newcomer\n", encoding="utf-8")
    registry_before = (plugin_root / "snippets" / "registry.toml").read_text(encoding="utf-8")

    result = run("foo", "--fix", plugin_root=plugin_root)
    assert result.exit_code == 1
    assert "UNDECLARED" in "\n".join(result.lines)
    assert newcomer.read_text(encoding="utf-8") == "# Newcomer\n"
    assert (plugin_root / "snippets" / "registry.toml").read_text(encoding="utf-8") == registry_before


def test_eligible_glob_gap_scan_honours_content_root(tmp_path):
    """A completeness check that globs the true plugin root while the consumer
    set resolves into a COORDINATOR_CONTENT_ROOT redirect compares two different
    trees — the split `effective_content_root` exists to close."""
    plugin_root = _v4_glob_fixture(tmp_path)
    (plugin_root / "agents" / "real-tree-only.md").write_text("# Real tree\n", encoding="utf-8")

    content_root = tmp_path / "redirect"
    (content_root / "agents").mkdir(parents=True)
    (content_root / "agents" / "enrolled.md").write_text(
        f"# Enrolled\n\n{_FOO_BEGIN}\nCanonical body.\n{_FOO_END}\n", encoding="utf-8"
    )
    (content_root / "agents" / "bespoke.md").write_text("# Bespoke variant\n", encoding="utf-8")

    redirected = run("foo", "verify", plugin_root=plugin_root, content_root=content_root)
    assert redirected.exit_code == 0, redirected.lines
    assert not any("UNDECLARED" in line for line in redirected.lines)

    # Without the redirect the very same registry DOES report the real tree's gap.
    assert any("UNDECLARED" in line for line in run("foo", "verify", plugin_root=plugin_root).lines)


def test_eligible_glob_gap_advice_branches_on_delivery(tmp_path):
    """DR-240 § Decision 4: advice-bearing output is delivery-specific. On an
    inject row "add it to consumers" is the wrong repair — that list is a
    documentation set nothing pastes from."""
    plugin_root = _v4_glob_fixture(
        tmp_path, registry=_V4_GLOB_REGISTRY.replace('delivery       = "paste"', 'delivery       = "inject"')
    )
    (plugin_root / "agents" / "newcomer.md").write_text("# Newcomer\n", encoding="utf-8")
    joined = "\n".join(run("foo", "verify", plugin_root=plugin_root).lines)
    assert "UNDECLARED" in joined
    assert "contract_blocks:" in joined


def test_v3_shaped_row_at_v4_verifies_unchanged(tmp_path):
    """Additive-optional at the ENGINE level, not just the reader's: a row with
    neither new field behaves at schema_version 4 exactly as it did at 3."""
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(
        plugin_root,
        """
        schema_version = 4

        [snippet.foo]
        sentinel_begin = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
        sentinel_end   = "<!-- END foo -->"
        header_style   = "fixed-2-line"
        delivery       = "paste"
        consumers      = ["agents/enrolled.md"]
        """,
    )
    (plugin_root / "snippets" / "foo.md").write_text(
        "<!-- canonical -->\n<!-- consumers -->\nCanonical body.\n", encoding="utf-8"
    )
    (plugin_root / "agents" / "enrolled.md").write_text(
        f"# Enrolled\n\n{_FOO_BEGIN}\nCanonical body.\n{_FOO_END}\n", encoding="utf-8"
    )
    result = run("foo", "verify", plugin_root=plugin_root)
    assert result.exit_code == 0
    assert result.lines == [f"OK           {plugin_root / 'agents/enrolled.md'}"]

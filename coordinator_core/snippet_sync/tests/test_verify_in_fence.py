"""Unit tests for the in_fence_consumers dialect of coordinator_core.snippet_sync.verify.

resolve-claude-klabauter-bin's consumer copies live INSIDE bash code fences, carrying
the mechanically-derived shell-comment sentinel dialect (`# BEGIN X` /
`# END X`) rather than the registry's HTML form. These tests cover: dual-
dialect discovery, exclusion of quoted/prose mentions and the canonical
snippet file, the multi-block-per-file verify/--fix round-trip (real
consumers hold up to ~27 copies in one file), fence preservation across a
--fix, and the backward-compat path (flag unset → byte-identical to the
single-dialect engine, verified by the pre-existing suite plus the explicit
non-discovery test here).
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from coordinator_core.snippet_sync.registry import get_snippet_meta, load_registry
from coordinator_core.snippet_sync.verify import run

_BEGIN_HTML = "<!-- BEGIN foo (synced from snippets/foo.md) -->"
_END_HTML = "<!-- END foo -->"
_BEGIN_SHELL = "# BEGIN foo (synced from snippets/foo.md)"
_END_SHELL = "# END foo"

_FENCE_INTERIOR = '_foo_root="resolved"\necho "line two"'

_CANONICAL = (
    "<!-- canonical source comment -->\n"
    "<!-- consumers comment -->\n"
    "\n"
    "Prose explaining the snippet.\n"
    "\n"
    "```bash\n" + _FENCE_INTERIOR + "\n```\n"
    "\n"
    "> Portability note.\n"
)


def _make_plugin_root(tmp_path: Path) -> Path:
    plugin_root = tmp_path / "plugin"
    (plugin_root / "snippets").mkdir(parents=True)
    (plugin_root / "skills").mkdir(parents=True)
    return plugin_root


def _write_registry(plugin_root: Path, *, in_fence: bool = True) -> None:
    flag = "in_fence_consumers = true\n" if in_fence else ""
    (plugin_root / "snippets" / "registry.toml").write_text(
        textwrap.dedent(
            f"""
            schema_version = 2
            [snippet.foo]
            sentinel_begin  = "{_BEGIN_HTML}"
            sentinel_end    = "{_END_HTML}"
            header_style    = "fixed-2-line"
            consumer_source = "scan"
            search_scope    = "plugin-root"
            fence_aware     = true
            {flag}consumers       = []
            """
        ),
        encoding="utf-8",
    )


def _write_canonical(plugin_root: Path, content: str = _CANONICAL) -> None:
    (plugin_root / "snippets" / "foo.md").write_text(content, encoding="utf-8")


def _in_fence_block(interior: str) -> str:
    return _BEGIN_SHELL + "\n" + interior + "\n" + _END_SHELL


def _consumer_text(*interiors: str) -> str:
    fences = "\n".join(
        "```bash\nset -e\n" + _in_fence_block(interior) + "\necho after\n```\n"
        for interior in interiors
    )
    return "# Skill\n\nSome prose.\n\n" + fences


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_in_fence_shell_consumer_discovered_and_verifies_ok(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root)
    _write_canonical(plugin_root)
    consumer = plugin_root / "skills" / "consumer.md"
    consumer.write_text(_consumer_text(_FENCE_INTERIOR), encoding="utf-8")

    listed = run("foo", "--list", plugin_root=plugin_root)
    assert listed.exit_code == 0
    assert listed.lines == [str(consumer)]

    outcome = run("foo", "verify", plugin_root=plugin_root)
    assert outcome.exit_code == 0
    assert outcome.lines == [f"OK           {consumer}"]


def test_shell_sentinel_outside_fence_is_not_a_consumer(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root)
    _write_canonical(plugin_root)
    # Shell-comment sentinel pair as prose, no surrounding fence.
    (plugin_root / "skills" / "prose-only.md").write_text(
        "The block is delimited by:\n\n"
        + _BEGIN_SHELL + "\n...\n" + _END_SHELL + "\n",
        encoding="utf-8",
    )
    outcome = run("foo", "--list", plugin_root=plugin_root)
    assert outcome.exit_code == 0
    assert outcome.lines == []


def test_canonical_file_quoting_its_own_shell_sentinel_is_not_a_consumer(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root)
    # Canonical file whose fence QUOTES the shell dialect verbatim — the exact
    # shape that would read as a live in-fence block anywhere else.
    _write_canonical(
        plugin_root,
        _CANONICAL + "\nExample of a consumer copy:\n\n```bash\n"
        + _in_fence_block("stale example body") + "\n```\n",
    )
    outcome = run("foo", "--list", plugin_root=plugin_root)
    assert outcome.exit_code == 0
    assert outcome.lines == []

    verify = run("foo", "verify", plugin_root=plugin_root)
    assert verify.exit_code == 0
    assert verify.lines == ["no consumers detected — nothing to verify"]


def test_html_sentinel_inside_fence_still_excluded_when_flag_set(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root)
    _write_canonical(plugin_root)
    # fence_aware quoted-mention exclusion for the HTML dialect must survive
    # the flag: an HTML sentinel inside a fence stays a documentation example.
    (plugin_root / "skills" / "doc-only.md").write_text(
        "Example:\n\n```\n" + _BEGIN_HTML + "\nBody.\n" + _END_HTML + "\n```\n",
        encoding="utf-8",
    )
    outcome = run("foo", "--list", plugin_root=plugin_root)
    assert outcome.exit_code == 0
    assert outcome.lines == []


# ---------------------------------------------------------------------------
# Verify / --fix round-trip
# ---------------------------------------------------------------------------


def test_fix_round_trip_inside_fence_preserves_fence(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root)
    _write_canonical(plugin_root)
    consumer = plugin_root / "skills" / "consumer.md"
    consumer.write_text(_consumer_text("STALE drifted body"), encoding="utf-8")

    verify_outcome = run("foo", "verify", plugin_root=plugin_root)
    assert verify_outcome.exit_code == 1
    assert verify_outcome.lines == [f"MISMATCH     {consumer}"]

    fix_outcome = run("foo", "--fix", plugin_root=plugin_root)
    assert fix_outcome.exit_code == 0
    assert fix_outcome.lines == [f"FIXED        {consumer}"]

    text = consumer.read_text(encoding="utf-8")
    # Fence + surrounding lines preserved; body replaced with the canonical
    # fence interior; sentinels intact.
    assert (
        "```bash\nset -e\n" + _in_fence_block(_FENCE_INTERIOR) + "\necho after\n```"
    ) in text
    assert "STALE drifted body" not in text

    reverify = run("foo", "--check", plugin_root=plugin_root)
    assert reverify.exit_code == 0
    assert reverify.lines == [f"OK           {consumer}"]


def test_multiple_in_fence_blocks_per_file_all_synced(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root)
    _write_canonical(plugin_root)
    consumer = plugin_root / "skills" / "consumer.md"
    # Real consumers hold up to ~27 copies per file — one OK, two drifted.
    consumer.write_text(
        _consumer_text(_FENCE_INTERIOR, "drift one", "drift two"), encoding="utf-8"
    )

    verify_outcome = run("foo", "verify", plugin_root=plugin_root)
    assert verify_outcome.exit_code == 1
    assert verify_outcome.lines == [f"MISMATCH     {consumer}"]

    fix_outcome = run("foo", "--fix", plugin_root=plugin_root)
    assert fix_outcome.exit_code == 0
    assert fix_outcome.lines == [f"FIXED        {consumer}"]

    text = consumer.read_text(encoding="utf-8")
    assert text.count(_in_fence_block(_FENCE_INTERIOR)) == 3
    assert "drift" not in text

    reverify = run("foo", "verify", plugin_root=plugin_root)
    assert reverify.exit_code == 0


def test_indented_in_fence_block_verifies_ok_and_fix_preserves_indent(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root)
    _write_canonical(plugin_root)
    consumer = plugin_root / "skills" / "consumer.md"

    def indented(interior: str) -> str:
        block = _in_fence_block(interior)
        return (
            "1. Step one:\n\n   ```bash\n"
            + "\n".join("   " + bl for bl in block.split("\n"))
            + "\n   ```\n"
        )

    # Uniformly indented copy of the canonical interior — consumer state on
    # disk (nested under a list), not drift.
    consumer.write_text("# Skill\n\n" + indented(_FENCE_INTERIOR), encoding="utf-8")
    outcome = run("foo", "verify", plugin_root=plugin_root)
    assert outcome.exit_code == 0
    assert outcome.lines == [f"OK           {consumer}"]

    # A drifted indented copy gets fixed WITH its indentation preserved.
    consumer.write_text("# Skill\n\n" + indented("drifted"), encoding="utf-8")
    fix_outcome = run("foo", "--fix", plugin_root=plugin_root)
    assert fix_outcome.exit_code == 0
    assert fix_outcome.lines == [f"FIXED        {consumer}"]
    assert indented(_FENCE_INTERIOR) in consumer.read_text(encoding="utf-8")

    reverify = run("foo", "verify", plugin_root=plugin_root)
    assert reverify.exit_code == 0


def test_in_fence_block_missing_end_before_fence_close_flagged(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root)
    _write_canonical(plugin_root)
    consumer = plugin_root / "skills" / "consumer.md"
    # Fence closes before the shell END sentinel — a later fence's END must
    # not be mistaken for this block's closer.
    consumer.write_text(
        "# Skill\n\n```bash\n" + _BEGIN_SHELL + "\nbody, no end\n```\n\n"
        "```bash\n" + _in_fence_block(_FENCE_INTERIOR) + "\n```\n",
        encoding="utf-8",
    )
    outcome = run("foo", "verify", plugin_root=plugin_root)
    assert outcome.exit_code == 1
    assert outcome.lines == [f"MISSING_END  {consumer}"]


def test_canonical_without_fence_fails_loud_when_shell_consumers_exist(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root)
    _write_canonical(plugin_root, "<!-- c1 -->\n<!-- c2 -->\n\nProse only, no fence.\n")
    (plugin_root / "skills" / "consumer.md").write_text(
        _consumer_text(_FENCE_INTERIOR), encoding="utf-8"
    )
    outcome = run("foo", "verify", plugin_root=plugin_root)
    assert outcome.exit_code == 2
    assert any("no non-empty ``` fenced code block" in line for line in outcome.stderr_lines)


# ---------------------------------------------------------------------------
# Backward compat + registry meta
# ---------------------------------------------------------------------------


def test_flag_unset_ignores_in_fence_shell_blocks(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root, in_fence=False)
    _write_canonical(plugin_root)
    (plugin_root / "skills" / "consumer.md").write_text(
        _consumer_text("drifted body"), encoding="utf-8"
    )
    listed = run("foo", "--list", plugin_root=plugin_root)
    assert listed.exit_code == 0
    assert listed.lines == []

    outcome = run("foo", "verify", plugin_root=plugin_root)
    assert outcome.exit_code == 0
    assert outcome.lines == ["no consumers detected — nothing to verify"]


def test_meta_in_fence_consumers_defaults_false_and_parses_true(tmp_path):
    plugin_root = _make_plugin_root(tmp_path)
    _write_registry(plugin_root, in_fence=True)
    data = load_registry(plugin_root / "snippets" / "registry.toml")
    assert get_snippet_meta(data, "foo")["in_fence_consumers"] is True

    _write_registry(plugin_root, in_fence=False)
    data = load_registry(plugin_root / "snippets" / "registry.toml")
    assert get_snippet_meta(data, "foo")["in_fence_consumers"] is False

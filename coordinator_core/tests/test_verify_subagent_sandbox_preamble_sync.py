"""Characterization tests for coordinator_core.ops.verify_subagent_sandbox_preamble_sync.

Built against the example-doctrine-repo-side bash original — see the port's own module
docstring for the byte-parity/negative-spec ledger.

Port of: verify-subagent-sandbox-preamble-sync.sh (example-doctrine-repo b5a4192c, 2026-07-20)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.verify_subagent_sandbox_preamble_sync import (
    BEGIN_SENTINEL,
    END_SENTINEL,
    extract_block,
    insert_block,
    main,
    rewrite_block,
    run,
)

_SNIPPET_BODY_TEXT = "This is the canonical offer body.\nSecond line."


@pytest.fixture
def env(tmp_path: Path):
    plugin_root = tmp_path / "plugin_root"
    (plugin_root / "snippets").mkdir(parents=True)
    (plugin_root / "agents").mkdir(parents=True)
    # Per-role adaptation (C5, 2026-07-24): the canonical snippet carries one
    # <!-- VARIANT:<type> --> ... <!-- END VARIANT --> block per template type
    # this cohort uses (review-findings, assessment) — see VARIANT_TYPES in the
    # module under test. Both variants share `_SNIPPET_BODY_TEXT` here so
    # existing content assertions (keyed on the default "agents/code-reviewer.md"
    # consumer, which maps to the review-findings variant) still hold without
    # per-test rewrites.
    (plugin_root / "snippets" / "subagent-sandbox-preamble.md").write_text(
        "<!-- canonical source comment -->\n"
        "<!-- consumers comment -->\n"
        "\n"
        "<!-- VARIANT:review-findings -->\n"
        f"{_SNIPPET_BODY_TEXT}\n"
        "<!-- END VARIANT -->\n"
        "\n"
        "<!-- VARIANT:assessment -->\n"
        f"{_SNIPPET_BODY_TEXT}\n"
        "<!-- END VARIANT -->\n"
    )

    # sentinel-blocks-cli.js dependency retired 2026-07-22 (extract_block now
    # calls coordinator_core.text.sentinel_blocks in-process) — script_dir no
    # longer needs a lib/ fixture, but stays a positional run()/main() arg for
    # CLI-argv/trampoline-contract compatibility (see module docstring).
    script_dir = plugin_root / "bin"
    script_dir.mkdir(parents=True)

    return plugin_root, script_dir


def _consumer_path(plugin_root: Path, name: str = "agents/code-reviewer.md") -> Path:
    return plugin_root / name


def test_list_mode_prints_all_consumers_exit_0(env):
    plugin_root, script_dir = env
    rc, out, err = run(str(plugin_root), str(script_dir), "--list")
    assert rc == 0
    assert err == []
    assert len(out) == 16
    assert out[0] == str(plugin_root / "agents" / "code-reviewer.md")
    assert out[-1] == str(plugin_root / "agents" / "notebooklm-research-scout.md")


def test_missing_snippet_file_exits_1(tmp_path):
    plugin_root = tmp_path / "empty_root"
    (plugin_root / "bin" / "lib").mkdir(parents=True)
    rc, out, err = run(str(plugin_root), str(plugin_root / "bin"), "--check")
    assert rc == 1
    assert any("canonical snippet not found" in line for line in err)


def test_unknown_mode_exits_2(env):
    plugin_root, script_dir = env
    rc, out, err = run(str(plugin_root), str(script_dir), "--bogus")
    assert rc == 2
    assert any("unknown argument" in line for line in err)


def test_missing_consumer_file_reported_on_stderr_exit_1_regardless_of_mode(env):
    plugin_root, script_dir = env
    # None of the 16 consumer files exist in this fixture -> every one is MISSING_FILE.
    rc, out, err = run(str(plugin_root), str(script_dir), "--check")
    assert rc == 1
    assert len(err) == 16
    assert all(line.startswith("MISSING_FILE ") for line in err)

    rc_fix, _out_fix, err_fix = run(str(plugin_root), str(script_dir), "--fix")
    assert rc_fix == 1
    assert len(err_fix) == 16


def test_check_mode_reports_missing_when_no_begin_sentinel(env):
    plugin_root, script_dir = env
    consumer = _consumer_path(plugin_root)
    consumer.write_text("# Some agent prompt\n\nNo sentinel here.\n")
    rc, out, err = run(str(plugin_root), str(script_dir), "--check")
    assert rc == 1
    assert f"MISSING      {consumer}" in out


def test_fix_mode_inserts_block_at_end_of_file_when_no_anchor(env):
    plugin_root, script_dir = env
    consumer = _consumer_path(plugin_root)
    consumer.write_text("# Some agent prompt\n\nBody text.\n")
    # Fixture only materializes this one of the 16 CONSUMERS entries, so the other
    # 15 remain MISSING_FILE and keep the aggregate exit code nonzero regardless of
    # mode — assert on this consumer's own row and content, not the aggregate rc.
    _rc, out, _err = run(str(plugin_root), str(script_dir), "--fix")
    assert f"INSERTED     {consumer}" in out

    text = consumer.read_text()
    assert BEGIN_SENTINEL in text
    assert END_SENTINEL in text
    assert _SNIPPET_BODY_TEXT in text

    # Re-running --check afterward reports OK for this consumer (round-trip through
    # the in-process extract_block) even though the aggregate rc still reflects the
    # other 15 fixture-absent consumers.
    _rc2, out2, _err2 = run(str(plugin_root), str(script_dir), "--check")
    assert f"OK           {consumer}" in out2


def test_fix_mode_anchors_after_quota_self_detect_preamble_over_other_sentinels(env):
    plugin_root, script_dir = env
    consumer = _consumer_path(plugin_root)
    consumer.write_text(
        "# Agent\n\n"
        "<!-- BEGIN text-only-recovery-preamble -->\nold stuff\n<!-- END text-only-recovery-preamble -->\n\n"
        "<!-- BEGIN quota-self-detect-preamble -->\nquota body\n<!-- END quota-self-detect-preamble -->\n\n"
        "## Examples\nsome examples\n"
    )
    run(str(plugin_root), str(script_dir), "--fix")
    lines = consumer.read_text().splitlines()
    quota_end_idx = next(i for i, l in enumerate(lines) if l.strip() == "<!-- END quota-self-detect-preamble -->")
    begin_idx = next(i for i, l in enumerate(lines) if l.strip() == BEGIN_SENTINEL)
    examples_idx = next(i for i, l in enumerate(lines) if l.strip() == "## Examples")
    # New block lands right after quota-self-detect-preamble's END, before "## Examples".
    assert quota_end_idx < begin_idx < examples_idx


def test_check_mode_reports_mismatch_and_fix_mode_repairs(env):
    plugin_root, script_dir = env
    consumer = _consumer_path(plugin_root)
    consumer.write_text(
        f"# Agent\n\n{BEGIN_SENTINEL}\nstale body\n{END_SENTINEL}\n"
    )
    rc, out, err = run(str(plugin_root), str(script_dir), "--check")
    assert rc == 1
    assert f"MISMATCH     {consumer}" in out

    # Aggregate rc stays nonzero (the other 15 CONSUMERS entries are fixture-absent
    # MISSING_FILE rows, unaffected by --fix); assert this consumer's own row + content.
    _rc_fix, out_fix, _err_fix = run(str(plugin_root), str(script_dir), "--fix")
    assert f"FIXED        {consumer}" in out_fix
    assert _SNIPPET_BODY_TEXT in consumer.read_text()
    assert "stale body" not in consumer.read_text()


def test_missing_end_sentinel_reported(env):
    plugin_root, script_dir = env
    consumer = _consumer_path(plugin_root)
    consumer.write_text(f"# Agent\n\n{BEGIN_SENTINEL}\nno end here\n")
    rc, out, err = run(str(plugin_root), str(script_dir), "--check")
    assert rc == 1
    assert f"MISSING_END  {consumer}" in out


def test_extract_block_parity_with_sentinel_blocks_js_oracle(tmp_path):
    """Parity check against the example-doctrine-repo oracle's exact contract (`coordinator/bin/
    lib/sentinel-blocks-cli.js` `extract`, example-doctrine-repo repo, lines 32-56): the
    block is the text strictly between the marker LINES (marker lines
    themselves excluded), byte-identical to slicing the fixture by hand from
    the newline immediately after BEGIN up to the start of the END marker
    line — this is what the node CLI's `extractBlock` (`sentinel-blocks.js`
    lines 80-89) would also produce for a marker-on-its-own-line fixture."""
    fixture = tmp_path / "consumer.md"
    fixture.write_text(
        "# Some agent prompt\n\n"
        f"{BEGIN_SENTINEL}\n"
        "line one of the body\n"
        "line two of the body\n"
        f"{END_SENTINEL}\n"
        "trailing content\n"
    )
    expected_block = "line one of the body\nline two of the body\n"

    rc, block, err = extract_block(str(fixture))
    assert rc == 0
    assert block == expected_block
    assert err == ""


def test_extract_block_missing_markers_returns_1(tmp_path):
    fixture = tmp_path / "consumer.md"
    fixture.write_text("# Some agent prompt\n\nno sentinels here\n")
    rc, block, err = extract_block(str(fixture))
    assert rc == 1
    assert block == ""
    assert "markers not found" in err


def test_extract_block_missing_file_returns_1(tmp_path):
    rc, block, err = extract_block(str(tmp_path / "does-not-exist.md"))
    assert rc == 1
    assert block == ""
    assert "cannot read file" in err


def test_main_forwards_argv_and_defaults_mode_to_check(env, capsys):
    plugin_root, script_dir = env
    rc = main([str(plugin_root), str(script_dir)])
    err = capsys.readouterr().err
    # Default mode (argv[2] absent) behaves as --check (verify-only): nonzero on
    # drift; none of the 16 fixture consumers exist, so every one is MISSING_FILE.
    assert rc == 1
    assert err.count("MISSING_FILE") == 16


def test_main_missing_required_args_returns_2(capsys):
    rc = main([])
    err = capsys.readouterr().err
    assert rc == 2
    assert "missing required" in err

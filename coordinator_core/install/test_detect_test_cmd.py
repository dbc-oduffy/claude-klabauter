"""
Tests for coordinator_core.install.detect_test_cmd.

Independently re-derives expected behaviour from the module's own documented
contract (docstring exit-code table) rather than shelling out to the retired
bash oracle — the oracle (coordinator/lib/setup-detect-test-cmd.sh, example-doctrine-repo
repo) was rewritten in-place to a polyglot trampoline as part of this port, so
a parity test that shells out to it would compare the port against itself.

Golden values below were captured by running the ORIGINAL bash oracle against
each fixture shape BEFORE the in-place rewrite (manual verification pass, not
re-derived from this module's own output) — see BIG_PORT Wave C porter report
for setup-detect-test-cmd.
"""

from __future__ import annotations

import os
import stat
import sys

import pytest

from coordinator_core.install.detect_test_cmd import (
    collect_candidates,
    detect_and_write_test_cmds,
    detect_node,
    detect_python,
    detect_rust,
    key_present,
    main,
    upsert_frontmatter_key,
)


def _write(path, content):
    path.write_text(content, encoding="utf-8")


def _local_md(tmp_path, body="---\nproject_type: x\n---\n"):
    md = tmp_path / "coordinator.local.md"
    _write(md, body)
    return md


# ---------------------------------------------------------------------------
# Unit 1 — detectors
# ---------------------------------------------------------------------------


def test_detect_node_absent(tmp_path):
    assert detect_node(tmp_path) is None


def test_detect_node_test_and_unit_unambiguous(tmp_path):
    _write(
        tmp_path / "package.json",
        '{"scripts": {"test": "jest", "test:unit": "jest --testPathPattern=unit"}}\n',
    )
    result = detect_node(tmp_path)
    assert result.stack == "node"
    assert result.fast == "npm run test:unit"
    assert result.full == "npm run test"


def test_detect_node_test_only(tmp_path):
    _write(tmp_path / "package.json", '{"scripts": {"test": "jest"}}\n')
    result = detect_node(tmp_path)
    assert result.fast == "npm run test"
    assert result.full is None


def test_detect_node_pnpm_lockfile_preferred(tmp_path):
    _write(tmp_path / "package.json", '{"scripts": {"test": "vitest"}}\n')
    _write(tmp_path / "pnpm-lock.yaml", "lockfileVersion: 6\n")
    result = detect_node(tmp_path)
    assert result.fast == "pnpm run test"


def test_detect_node_lint_fallback_when_no_test_scripts(tmp_path):
    _write(tmp_path / "package.json", '{"scripts": {"lint": "eslint ."}}\n')
    result = detect_node(tmp_path)
    assert result.fast == "npm run lint"
    assert result.full is None


def test_detect_node_no_matching_scripts_returns_none(tmp_path):
    _write(tmp_path / "package.json", '{"scripts": {"build": "webpack"}}\n')
    assert detect_node(tmp_path) is None


def test_detect_python_absent(tmp_path):
    assert detect_python(tmp_path) is None


def test_detect_python_markers_tightens_fast(tmp_path):
    _write(
        tmp_path / "pyproject.toml",
        '[tool.pytest.ini_options]\nmarkers = ["slow", "integration"]\n',
    )
    result = detect_python(tmp_path)
    assert result.fast == 'pytest -m "not slow and not integration"'
    assert result.full == "pytest"


def test_detect_python_no_markers_fast_equals_full_so_full_none(tmp_path):
    _write(tmp_path / "pyproject.toml", "[tool.pytest.ini_options]\n")
    result = detect_python(tmp_path)
    assert result.fast == "pytest"
    assert result.full is None


def test_detect_python_build_system_heuristic(tmp_path):
    _write(tmp_path / "pyproject.toml", "[build-system]\nrequires = []\n")
    result = detect_python(tmp_path)
    assert result is not None
    assert result.fast == "pytest"


def test_detect_python_pyproject_without_markers_or_pytest_or_buildsystem_is_none(tmp_path):
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'x'\n")
    assert detect_python(tmp_path) is None


def test_detect_rust_absent(tmp_path):
    assert detect_rust(tmp_path) is None


def test_detect_rust_present(tmp_path):
    _write(tmp_path / "Cargo.toml", "[package]\nname = 'foo'\n")
    result = detect_rust(tmp_path)
    assert result.stack == "rust"
    assert result.fast == "cargo test --lib"
    assert result.full == "cargo test"


# ---------------------------------------------------------------------------
# Unit 2 — candidate aggregation + frontmatter mutation
# ---------------------------------------------------------------------------


def test_collect_candidates_multi_stack_order(tmp_path):
    _write(tmp_path / "package.json", '{"scripts": {"test": "jest"}}\n')
    _write(tmp_path / "Cargo.toml", "[package]\nname='x'\n")
    stacks, fast, full = collect_candidates(tmp_path)
    assert stacks == ["node", "rust"]
    assert fast == ["npm run test", "cargo test --lib"]
    assert full == ["cargo test"]


def test_upsert_creates_file_when_absent(tmp_path):
    md = tmp_path / "coordinator.local.md"
    upsert_frontmatter_key(md, "fast_test_cmd", "pytest")
    assert md.read_text() == "---\nfast_test_cmd: pytest\n---\n"


def test_upsert_prepends_frontmatter_when_none_exists(tmp_path):
    md = tmp_path / "coordinator.local.md"
    _write(md, "# just a heading\nno frontmatter here\n")
    upsert_frontmatter_key(md, "fast_test_cmd", "pytest")
    text = md.read_text()
    assert text.startswith("---\nfast_test_cmd: pytest\n---\n")
    assert "# just a heading" in text


def test_upsert_replaces_existing_key_in_place(tmp_path):
    md = _local_md(tmp_path, "---\nproject_type: x\nfast_test_cmd: old\n---\n")
    upsert_frontmatter_key(md, "fast_test_cmd", "new")
    text = md.read_text()
    assert "fast_test_cmd: new" in text
    assert "fast_test_cmd: old" not in text
    assert "project_type: x" in text


def test_upsert_inserts_before_closing_dashes_when_key_absent(tmp_path):
    md = _local_md(tmp_path, "---\nproject_type: x\n---\nbody content\n")
    upsert_frontmatter_key(md, "fast_test_cmd", "pytest")
    text = md.read_text()
    assert text == "---\nproject_type: x\nfast_test_cmd: pytest\n---\nbody content\n"


def test_upsert_preserves_permission_bits(tmp_path):
    # Windows has no POSIX mode bits; os.stat().st_mode & 0o777 is always
    # 0o666 there regardless of the os.chmod() argument. Assert the real
    # invariant under test — mode preserved across the upsert — by
    # comparing against whatever mode chmod actually produced on this
    # platform, rather than asserting the literal POSIX octal.
    md = _local_md(tmp_path)
    os.chmod(md, 0o755)
    mode_before = stat.S_IMODE(os.stat(md).st_mode)
    upsert_frontmatter_key(md, "fast_test_cmd", "pytest")
    mode_after = stat.S_IMODE(os.stat(md).st_mode)
    assert mode_after == mode_before


def test_key_present_true_and_false(tmp_path):
    md = _local_md(tmp_path, "---\nfast_test_cmd: pytest\n---\n")
    assert key_present(md, "fast_test_cmd") is True
    assert key_present(md, "full_test_cmd") is False


def test_key_present_empty_value_is_false(tmp_path):
    md = _local_md(tmp_path, "---\nfast_test_cmd:\n---\n")
    assert key_present(md, "fast_test_cmd") is False


def test_key_present_missing_file_is_false(tmp_path):
    assert key_present(tmp_path / "nope.md", "fast_test_cmd") is False


# ---------------------------------------------------------------------------
# Unit 3 — orchestrator (golden fixtures captured from the bash oracle)
# ---------------------------------------------------------------------------


def test_node_only_writes_expected_commands(tmp_path):
    _write(
        tmp_path / "package.json",
        '{"scripts": {"test": "jest", "test:unit": "jest --testPathPattern=unit"}}\n',
    )
    md = _local_md(tmp_path, "---\nproject_type: node\n---\n")
    rc = detect_and_write_test_cmds(str(tmp_path), non_interactive=True)
    assert rc == 0
    assert md.read_text() == (
        "---\nproject_type: node\nfast_test_cmd: npm run test:unit\n"
        "full_test_cmd: npm run test\n---\n"
    )


def test_python_markers_writes_expected_commands(tmp_path):
    _write(
        tmp_path / "pyproject.toml",
        '[tool.pytest.ini_options]\nmarkers = ["slow", "integration"]\n',
    )
    md = _local_md(tmp_path, "---\nproject_type: python\n---\n")
    rc = detect_and_write_test_cmds(str(tmp_path), non_interactive=True)
    assert rc == 0
    text = md.read_text()
    assert 'fast_test_cmd: pytest -m "not slow and not integration"' in text
    assert "full_test_cmd: pytest" in text


def test_rust_writes_expected_commands(tmp_path):
    _write(tmp_path / "Cargo.toml", "[package]\nname = 'foo'\n")
    md = _local_md(tmp_path, "---\nproject_type: rust\n---\n")
    rc = detect_and_write_test_cmds(str(tmp_path), non_interactive=True)
    assert rc == 0
    text = md.read_text()
    assert "fast_test_cmd: cargo test --lib" in text
    assert "full_test_cmd: cargo test" in text


def test_ambiguous_multi_stack_returns_1_and_writes_nothing(tmp_path):
    _write(tmp_path / "package.json", '{"scripts": {"test": "jest"}}\n')
    _write(tmp_path / "Cargo.toml", "[package]\nname = 'foo'\n")
    md = _local_md(tmp_path, "---\nproject_type: mixed\n---\n")
    before = md.read_text()
    rc = detect_and_write_test_cmds(str(tmp_path), non_interactive=True)
    assert rc == 1
    assert md.read_text() == before  # unchanged — no silent pick


def test_no_stack_detected_returns_2(tmp_path):
    _local_md(tmp_path, "---\nproject_type: unknown\n---\n")
    rc = detect_and_write_test_cmds(str(tmp_path), non_interactive=True)
    assert rc == 2


def test_idempotent_noop_returns_2(tmp_path):
    _write(tmp_path / "package.json", '{"scripts": {"test": "jest"}}\n')
    _local_md(
        tmp_path,
        "---\nproject_type: node\nfast_test_cmd: npm run test\n"
        "full_test_cmd: npm run test\n---\n",
    )
    rc = detect_and_write_test_cmds(str(tmp_path), non_interactive=True)
    assert rc == 2


def test_missing_local_md_returns_3(tmp_path):
    rc = detect_and_write_test_cmds(str(tmp_path), non_interactive=True)
    assert rc == 3


def test_malformed_frontmatter_returns_3(tmp_path):
    _local_md(tmp_path, "project_type: broken\n")
    rc = detect_and_write_test_cmds(str(tmp_path), non_interactive=True)
    assert rc == 3


def test_preset_fast_path_skips_detection(tmp_path):
    md = _local_md(tmp_path, "---\nproject_type: x\n---\n")
    rc = detect_and_write_test_cmds(
        str(tmp_path),
        non_interactive=True,
        preset_fast="make fast",
        preset_full="make full",
    )
    assert rc == 0
    assert md.read_text() == (
        "---\nproject_type: x\nfast_test_cmd: make fast\n"
        "full_test_cmd: make full\n---\n"
    )


def test_preset_fast_only_reuses_for_full(tmp_path):
    md = _local_md(tmp_path, "---\nproject_type: x\n---\n")
    rc = detect_and_write_test_cmds(
        str(tmp_path), non_interactive=True, preset_fast="make fast"
    )
    assert rc == 0
    text = md.read_text()
    assert "fast_test_cmd: make fast" in text
    assert "full_test_cmd: make fast" in text


def test_force_overwrites_existing_keys(tmp_path):
    _write(tmp_path / "package.json", '{"scripts": {"test": "jest"}}\n')
    md = _local_md(
        tmp_path,
        "---\nproject_type: node\nfast_test_cmd: old\nfull_test_cmd: old\n---\n",
    )
    rc = detect_and_write_test_cmds(str(tmp_path), non_interactive=True, force=True)
    assert rc == 0
    text = md.read_text()
    assert "fast_test_cmd: npm run test" in text
    assert "old" not in text


def test_repo_root_missing_returns_1(tmp_path):
    rc = detect_and_write_test_cmds(str(tmp_path / "does-not-exist"), non_interactive=True)
    assert rc == 1


def test_interactive_decline_returns_1_and_writes_nothing(tmp_path):
    _write(tmp_path / "Cargo.toml", "[package]\nname='x'\n")
    md = _local_md(tmp_path, "---\nproject_type: rust\n---\n")
    before = md.read_text()
    rc = detect_and_write_test_cmds(
        str(tmp_path), non_interactive=False, confirm=lambda: False
    )
    assert rc == 1
    assert md.read_text() == before


def test_interactive_accept_writes(tmp_path):
    _write(tmp_path / "Cargo.toml", "[package]\nname='x'\n")
    md = _local_md(tmp_path, "---\nproject_type: rust\n---\n")
    rc = detect_and_write_test_cmds(
        str(tmp_path), non_interactive=False, confirm=lambda: True
    )
    assert rc == 0
    assert "fast_test_cmd: cargo test --lib" in md.read_text()


# ---------------------------------------------------------------------------
# CLI (main) tests
# ---------------------------------------------------------------------------


def test_main_root_flag(tmp_path):
    _write(tmp_path / "Cargo.toml", "[package]\nname='x'\n")
    _local_md(tmp_path, "---\nproject_type: rust\n---\n")
    rc = main(["--root", str(tmp_path), "--non-interactive"])
    assert rc == 0


def test_main_positional_root(tmp_path):
    _write(tmp_path / "Cargo.toml", "[package]\nname='x'\n")
    _local_md(tmp_path, "---\nproject_type: rust\n---\n")
    rc = main([str(tmp_path), "--non-interactive"])
    assert rc == 0


def test_main_non_interactive_with_presets(tmp_path):
    md = _local_md(tmp_path, "---\nproject_type: x\n---\n")
    rc = main(["--root", str(tmp_path), "--non-interactive", "make fast", "make full"])
    assert rc == 0
    text = md.read_text()
    assert "fast_test_cmd: make fast" in text
    assert "full_test_cmd: make full" in text


def test_main_env_var_non_interactive(tmp_path, monkeypatch):
    _write(tmp_path / "Cargo.toml", "[package]\nname='x'\n")
    _local_md(tmp_path, "---\nproject_type: rust\n---\n")
    monkeypatch.setenv("SETUP_DETECT_NONINTERACTIVE", "1")
    rc = main(["--root", str(tmp_path)])
    assert rc == 0


def test_main_help_exits_0(capsys):
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "setup-detect-test-cmd" in out


def test_main_unknown_flag_returns_1():
    rc = main(["--bogus"])
    assert rc == 1


def test_main_root_flag_missing_value_returns_1():
    rc = main(["--root"])
    assert rc == 1


def test_main_defaults_to_cwd_when_root_omitted(tmp_path, monkeypatch):
    _write(tmp_path / "Cargo.toml", "[package]\nname='x'\n")
    _local_md(tmp_path, "---\nproject_type: rust\n---\n")
    monkeypatch.chdir(tmp_path)
    rc = main(["--non-interactive"])
    assert rc == 0


# ---------------------------------------------------------------------------
# Family-I fresh-install-shape smoke test
# ---------------------------------------------------------------------------


def test_fresh_install_shape_smoke(tmp_path, monkeypatch):
    """Simulates a first-run repo-setup invocation on a cold checkout: no
    CLAUDE_KLABAUTER_ROOT env var needed (this test exercises the module directly, not
    the trampoline), a freshly-created coordinator.local.md with only
    project_type set, and a Node-only stack — the common onboarding shape."""
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    _write(tmp_path / "package.json", '{"scripts": {"test": "npm test"}}\n')
    _local_md(tmp_path, "---\nproject_type: node\n---\n")
    rc = main(["--root", str(tmp_path), "--non-interactive"])
    assert rc == 0
    text = (tmp_path / "coordinator.local.md").read_text()
    assert "fast_test_cmd: npm run test" in text
    # Idempotent re-run (as repo-setup would do on a re-invoke) is a clean no-op.
    rc2 = main(["--root", str(tmp_path), "--non-interactive"])
    assert rc2 == 2


# ---------------------------------------------------------------------------
# resolution-journal wiring (C7 of docs/research/2026-08-06-install-receipt-
# persistence-design.md) — clause 0, the sole SHAPED clause
# (`_LOCAL_MD_CLAUSE_INDEX`, the fast_test_cmd/full_test_cmd frontmatter
# upsert). `coordinator.local.md` is operator-owned and never auto-created
# by this module (see module docstring's negative-spec) — a MISSING file is
# genuinely "never got there" for this clause (no journal row at all), while
# a present file that this run deliberately writes nothing into (idempotent
# no-op, no stack detected, declined) is "resolved to nothing" (a present,
# empty row) — the two must stay distinguishable per read_journal's own
# invariant.
# ---------------------------------------------------------------------------


@pytest.fixture
def _journal_env(tmp_path, monkeypatch):
    from coordinator_core.install import resolution_journal as journal_mod

    journal_path = tmp_path / "journal" / "resolution-journal.jsonl"
    monkeypatch.setenv(journal_mod.RESOLUTION_JOURNAL_ENV_VAR, str(journal_path))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    return journal_mod


def _resolved(journal_mod):
    from coordinator_core.install.detect_test_cmd import _LOCAL_MD_CLAUSE_INDEX

    journal = journal_mod.read_journal()
    return journal.get("detect-test-cmd", {}).get(_LOCAL_MD_CLAUSE_INDEX)


def test_journal_records_written_keys(tmp_path, _journal_env):
    _write(tmp_path / "package.json", '{"scripts": {"test": "jest", "test:unit": "jest --unit"}}\n')
    md = _local_md(tmp_path, "---\nproject_type: node\n---\n")

    rc = detect_and_write_test_cmds(str(tmp_path), non_interactive=True)

    assert rc == 0
    resolution = _resolved(_journal_env)
    assert resolution is not None
    keys = {e.key for e in resolution.entries}
    assert keys == {"fast_test_cmd", "full_test_cmd"}
    for entry in resolution.entries:
        assert entry.kind == "structured-file-key"
        assert entry.path == str(md)


def test_journal_records_preset_keys(tmp_path, _journal_env):
    md = _local_md(tmp_path, "---\nproject_type: x\n---\n")

    rc = detect_and_write_test_cmds(
        str(tmp_path), non_interactive=True, preset_fast="make fast", preset_full="make full",
    )

    assert rc == 0
    resolution = _resolved(_journal_env)
    assert resolution is not None
    keys = {e.key for e in resolution.entries}
    assert keys == {"fast_test_cmd", "full_test_cmd"}
    assert all(e.path == str(md) for e in resolution.entries)


def test_journal_empty_entries_on_idempotent_noop(tmp_path, _journal_env):
    _write(tmp_path / "package.json", '{"scripts": {"test": "jest"}}\n')
    _local_md(
        tmp_path,
        "---\nproject_type: node\nfast_test_cmd: npm run test\n"
        "full_test_cmd: npm run test\n---\n",
    )

    rc = detect_and_write_test_cmds(str(tmp_path), non_interactive=True)

    assert rc == 2
    resolution = _resolved(_journal_env)
    assert resolution is not None
    assert resolution.entries == ()


def test_journal_empty_entries_on_no_stack_detected(tmp_path, _journal_env):
    _local_md(tmp_path, "---\nproject_type: unknown\n---\n")

    rc = detect_and_write_test_cmds(str(tmp_path), non_interactive=True)

    assert rc == 2
    resolution = _resolved(_journal_env)
    assert resolution is not None
    assert resolution.entries == ()


def test_journal_empty_entries_on_interactive_decline(tmp_path, _journal_env):
    _write(tmp_path / "Cargo.toml", "[package]\nname='x'\n")
    _local_md(tmp_path, "---\nproject_type: rust\n---\n")

    rc = detect_and_write_test_cmds(str(tmp_path), non_interactive=False, confirm=lambda: False)

    assert rc == 1
    resolution = _resolved(_journal_env)
    assert resolution is not None
    assert resolution.entries == ()


def test_journal_unreported_on_missing_local_md(tmp_path, _journal_env):
    """The sharp case: coordinator.local.md is operator-owned and never
    auto-created (module docstring negative-spec) — a missing file means
    `detect_and_write_test_cmds` returns 3 before reaching any point that
    could call `_record_resolution` at all. This is "never got there", not
    "resolved to nothing" — no journal row for this writer/clause, full
    stop, distinct from an empty-tuple row."""
    rc = detect_and_write_test_cmds(str(tmp_path), non_interactive=True)

    assert rc == 3
    assert not (tmp_path / "coordinator.local.md").exists()
    assert _resolved(_journal_env) is None


def test_journal_unreported_on_malformed_frontmatter(tmp_path, _journal_env):
    """Same 'never got there' reasoning as the missing-file case: a present
    but frontmatter-malformed coordinator.local.md also returns 3 before any
    `_record_resolution` call site is reached."""
    _local_md(tmp_path, "project_type: broken\n")

    rc = detect_and_write_test_cmds(str(tmp_path), non_interactive=True)

    assert rc == 3
    assert _resolved(_journal_env) is None


def test_journal_records_partial_write_on_write_failure(tmp_path, _journal_env, monkeypatch):
    """A write failure after fast_test_cmd landed but before full_test_cmd
    does must journal the key that actually made it to disk, not the
    unattempted one alongside it — see `detect_and_write_test_cmds`'s own
    comment at that except-block."""
    import coordinator_core.install.detect_test_cmd as dtc_module

    md = _local_md(tmp_path, "---\nproject_type: x\n---\n")

    calls = {"n": 0}
    real_upsert = dtc_module.upsert_frontmatter_key

    def _flaky_upsert(file, key, value):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_upsert(file, key, value)
        raise OSError("simulated disk failure")

    monkeypatch.setattr(dtc_module, "upsert_frontmatter_key", _flaky_upsert)

    rc = detect_and_write_test_cmds(
        str(tmp_path), non_interactive=True, preset_fast="make fast", preset_full="make full",
    )

    assert rc == 1
    resolution = _resolved(_journal_env)
    assert resolution is not None
    keys = {e.key for e in resolution.entries}
    assert keys == {"fast_test_cmd"}


def test_journal_omits_entry_when_mutation_disabled(tmp_path, _journal_env, monkeypatch):
    """`detect_test_cmd.py` does not itself gate its frontmatter write on
    `COORDINATOR_DISABLE_MACHINE_MUTATION` — only the journal append does,
    via `resolution_journal.record_resolution`'s own guard. The write still
    lands on disk; only this clause's journal row is refused."""
    md = _local_md(tmp_path, "---\nproject_type: x\n---\n")
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")

    rc = detect_and_write_test_cmds(
        str(tmp_path), non_interactive=True, preset_fast="make fast", preset_full="make full",
    )

    assert rc == 0
    assert "fast_test_cmd: make fast" in md.read_text()
    assert _resolved(_journal_env) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

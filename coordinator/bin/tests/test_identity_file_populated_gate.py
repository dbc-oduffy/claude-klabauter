"""test_identity_file_populated_gate — closes the gap `check_identity_file_present`
left open: it only checks `.percolate-identity` EXISTS, not that
`PERSONAL_REVIEW_PATTERNS` inside it is actually populated. A present-but-empty
file previously satisfied the FATAL gate, then fell through to
`warn_machine_slug_net` (a per-target WARN, never fatal, and scoped only to
`coordinator-claude*`/`deep-research-claude*` target names — every
`claude-klabauter*` row got neither a warning nor a failure), leaving
PERSONAL_REVIEW_PATTERNS empty and the Phase 4 personal-codename audit INERT.

Drives `publish.main()` end-to-end with every OTHER precondition stubbed
inert (mirrors `_wire_main_preconditions` in
`test_percolate_identity_check_gate.py`), but leaves `check_identity_file_present`,
`check_identity_file_safe`, and `parse_percolate_identity` REAL so the new
populated-patterns gate actually runs against a real file on disk.

Run: python -m pytest coordinator/bin/tests/test_identity_file_populated_gate.py -q
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_identity_populated_gate_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


class _StubClaudeKlabauter:
    def resolve_target(self, store, name):
        return {"hooks": [], "file_surface": {}, "guards": [], "inject": []}

    def run_percolate(self, store_path, target, target_root, phase, **kwargs):
        return {"phase": phase, "guard_results": [], "rename_manifest": None, "restored_native": []}

    def iter_surface_files(self, root, **kwargs):
        return iter(())

    def run_identity_check(self, dest):
        return {"ran": False, "skipped": True, "exit_code": None, "findings": ""}


def _fake_process_target_succeeds(target, setup_dir, totals, **kwargs):
    # `main()`'s row loop (§ the row-honesty fix, `test_publish_skipped_row_
    # not_counted_succeeded.py`) treats "`process_target` did not raise AND
    # `totals.processed` did not advance" as a FAILED row — a `None`-
    # returning no-op fake (this fixture's original shape) therefore marks
    # every row FAILED (and `main()` returns before even reaching the
    # gate under test's "proceeds" branch). Advance `totals.processed` to
    # model the row genuinely landing, matching every other `main()`-
    # driving publish test fixture in this package.
    totals.processed += 1


def _wire_main_preconditions_except_identity(monkeypatch, *, setup_dir: Path, rows: list) -> None:
    """Same shape as `_wire_main_preconditions` in
    `test_percolate_identity_check_gate.py`, deliberately WITHOUT stubbing
    `check_identity_file_present` / `check_identity_file_safe` /
    `parse_percolate_identity` — those three are the gate under test here."""
    percolate_root = setup_dir.parent
    monkeypatch.setattr(
        publish, "_resolve_percolate_root_and_rung", lambda **kwargs: (percolate_root, "test-rung")
    )
    monkeypatch.setattr(
        publish, "load_targets", lambda setup_dir, target_filter="", **kwargs: rows
    )
    monkeypatch.setattr(publish, "locate_percolate_store", lambda setup_dir: setup_dir / "store.yaml")
    monkeypatch.setattr(publish, "_import_claude_klabauter_percolate", lambda: _StubClaudeKlabauter())
    monkeypatch.setattr(publish, "assert_percolate_store_ready", lambda claude_klabauter_root, store_path: {"targets": {}})
    monkeypatch.setattr(publish, "_import_publish_sync", lambda setup_dir: object())
    monkeypatch.setattr(publish, "check_publish_sync_contract", lambda *a, **k: None)
    monkeypatch.setattr(publish, "process_target", _fake_process_target_succeeds)
    # This file's gate under test is the PRE-loop populated-patterns check —
    # the four POST-loop end-of-run legs are out of scope here (each has its
    # own dedicated test file) and, unlike `_StubClaudeKlabauter` in those sibling
    # files, this fixture's stub carries no `run_parse_sweep`/
    # `enumerate_gate_entrypoints`, so leaving the function/entrypoint gates
    # un-stubbed would fail them on an AttributeError rather than exercising
    # anything this file cares about. Stub all four inert so a "proceeds"
    # test's rc depends only on the identity-populated gate + row success.
    monkeypatch.setattr(publish, "dispatch_end_of_run_identity_check", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_install_doc_payload_check", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_unscanned_published_check", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_function_gate", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_entrypoint_gate", lambda *a, **k: True)


def _row(name: str, repo_root: Path) -> str:
    return f"{name}|mirror|{repo_root / 'src'}|{repo_root}"


def _write_identity(setup_dir: Path, *, review_patterns: list[str] | None) -> Path:
    """`review_patterns=None` writes an EMPTY array literal (present, parses,
    but empty); an empty list writes the array key with only whitespace/
    comment lines (still empty after parsing); a populated list writes real
    tokens."""
    identity_path = setup_dir / ".percolate-identity"
    if review_patterns is None:
        body = ""
    else:
        body = "\n".join(f'  "{pat}"' for pat in review_patterns)
    identity_path.write_text(
        "PERSONAL_EXPECTED_PATTERNS=(\n)\n"
        f"PERSONAL_REVIEW_PATTERNS=(\n{body}\n)\n"
        "PERSONAL_ALLOW_TOKENS=(\n)\n",
        encoding="utf-8",
    )
    identity_path.chmod(0o600)
    return identity_path


class TestEmptyIdentityFileAbortsRun:
    """`.percolate-identity` exists (satisfies `check_identity_file_present`)
    but is literally empty — the array literals themselves never parse to
    any content, so `PERSONAL_REVIEW_PATTERNS` is empty. This is the case
    `check_identity_file_present` alone cannot catch."""

    def test_run_aborts_nonzero_with_remediation_named(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        (setup_dir / ".percolate-identity").write_text("", encoding="utf-8")
        (setup_dir / ".percolate-identity").chmod(0o600)
        (setup_dir / ".percolate-identity.example").write_text("# example\n", encoding="utf-8")

        repo_root = tmp_path / "dest-repo"
        (repo_root / ".git").mkdir(parents=True)
        _wire_main_preconditions_except_identity(
            monkeypatch, setup_dir=setup_dir, rows=[_row("claude-klabauter", repo_root)]
        )

        rc = publish.main([])

        assert rc != 0
        captured = capsys.readouterr()
        assert "FATAL" in captured.err
        assert "PERSONAL_REVIEW_PATTERNS" in captured.err
        assert ".percolate-identity.example" in captured.err
        # process_target must never have been reached — this aborts the WHOLE
        # run before any target dispatch, not a per-target skip.
        assert "Done." not in captured.out or "0 target" not in captured.out


class TestParsedButEmptyReviewPatternsAbortsRun:
    """`.percolate-identity` exists and parses (the array literal is present
    and well-formed) but `PERSONAL_REVIEW_PATTERNS` itself is empty/
    whitespace-only — distinct from the totally-empty-file case above,
    proving the gate checks CONTENT, not just file presence or parseability."""

    def test_empty_array_body_aborts_run(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        _write_identity(setup_dir, review_patterns=None)
        (setup_dir / ".percolate-identity.example").write_text("# example\n", encoding="utf-8")

        repo_root = tmp_path / "dest-repo"
        (repo_root / ".git").mkdir(parents=True)
        _wire_main_preconditions_except_identity(
            monkeypatch, setup_dir=setup_dir, rows=[_row("claude-klabauter", repo_root)]
        )

        rc = publish.main([])

        assert rc != 0
        captured = capsys.readouterr()
        assert "FATAL" in captured.err
        assert "PERSONAL_REVIEW_PATTERNS" in captured.err
        assert ".percolate-identity.example" in captured.err

    def test_whitespace_only_comment_body_aborts_run(self, tmp_path, monkeypatch, capsys):
        """Array body contains only a comment line (no real token) —
        `shlex.split` on a comment-stripped body yields an empty list, same
        as the fully-empty case; must still abort."""
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        identity_path = setup_dir / ".percolate-identity"
        identity_path.write_text(
            "PERSONAL_EXPECTED_PATTERNS=(\n)\n"
            "PERSONAL_REVIEW_PATTERNS=(\n  # no real patterns yet\n)\n"
            "PERSONAL_ALLOW_TOKENS=(\n)\n",
            encoding="utf-8",
        )
        identity_path.chmod(0o600)
        (setup_dir / ".percolate-identity.example").write_text("# example\n", encoding="utf-8")

        repo_root = tmp_path / "dest-repo"
        (repo_root / ".git").mkdir(parents=True)
        _wire_main_preconditions_except_identity(
            monkeypatch, setup_dir=setup_dir, rows=[_row("claude-klabauter", repo_root)]
        )

        rc = publish.main([])

        assert rc != 0
        captured = capsys.readouterr()
        assert "FATAL" in captured.err
        assert "PERSONAL_REVIEW_PATTERNS" in captured.err


class TestPopulatedIdentityFileProceeds:
    """Regression guard: a genuinely populated `PERSONAL_REVIEW_PATTERNS`
    must not be caught by the new gate — the run proceeds past it."""

    def test_populated_patterns_run_proceeds(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        _write_identity(setup_dir, review_patterns=["some-machine-slug"])

        repo_root = tmp_path / "dest-repo"
        (repo_root / ".git").mkdir(parents=True)
        _wire_main_preconditions_except_identity(
            monkeypatch, setup_dir=setup_dir, rows=[_row("claude-klabauter", repo_root)]
        )

        rc = publish.main([])

        assert rc == 0
        captured = capsys.readouterr()
        assert "PERSONAL_REVIEW_PATTERNS" not in captured.err


class TestMachineLocalIdentityFallback:
    """`resolve_percolate_identity_path` — the machine-local fallback rung.

    All settings-home paths here are `tmp_path`-scoped via
    `COORDINATOR_SETTINGS_HOME` monkeypatch — never the real
    `~/.coordinator-claude-settings/.percolate-identity`."""

    def test_per_repo_present_wins_over_machine_local(self, tmp_path, monkeypatch):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        settings_home = tmp_path / "settings-home"
        settings_home.mkdir()
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        per_repo = _write_identity(setup_dir, review_patterns=["per-repo-slug"])
        machine_local = settings_home / ".percolate-identity"
        machine_local.write_text("PERSONAL_REVIEW_PATTERNS=(\n  \"machine-local-slug\"\n)\n", encoding="utf-8")
        machine_local.chmod(0o600)

        resolved = publish.resolve_percolate_identity_path(setup_dir)

        assert resolved == per_repo

    def test_bad_per_repo_never_falls_through_to_valid_machine_local(self, tmp_path, monkeypatch):
        """A present-but-degenerate per-repo file must FATAL on its OWN degeneracy, never be
        silently rescued by a valid machine-local rung.

        This is the adversarial precedence case: every other test in this class varies ONE rung
        at a time, so a `resolve_percolate_identity_path` that checked validity (rather than mere
        presence) before returning would pass all of them while silently downgrading a repo that
        deliberately carries its own patterns to some other repo's. The resolver returns on
        `is_file()` alone, and this pins that -- the empty-PERSONAL_REVIEW_PATTERNS abort must
        name the PER-REPO path, proving the machine-local file was never consulted.
        """
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        settings_home = tmp_path / "settings-home"
        settings_home.mkdir()
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        per_repo = _write_identity(setup_dir, review_patterns=[])
        machine_local = settings_home / ".percolate-identity"
        machine_local.write_text(
            "PERSONAL_EXPECTED_PATTERNS=(\n)\n"
            "PERSONAL_REVIEW_PATTERNS=(\n  \"machine-local-slug\"\n)\n"
            "PERSONAL_ALLOW_TOKENS=(\n)\n",
            encoding="utf-8",
        )
        machine_local.chmod(0o600)

        assert publish.resolve_percolate_identity_path(setup_dir) == per_repo

    def test_per_repo_absent_machine_local_valid_resolves_and_proceeds(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        settings_home = tmp_path / "settings-home"
        settings_home.mkdir()
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        machine_local = settings_home / ".percolate-identity"
        machine_local.write_text(
            "PERSONAL_EXPECTED_PATTERNS=(\n)\n"
            "PERSONAL_REVIEW_PATTERNS=(\n  \"machine-local-slug\"\n)\n"
            "PERSONAL_ALLOW_TOKENS=(\n)\n",
            encoding="utf-8",
        )
        machine_local.chmod(0o600)

        repo_root = tmp_path / "dest-repo"
        (repo_root / ".git").mkdir(parents=True)
        _wire_main_preconditions_except_identity(
            monkeypatch, setup_dir=setup_dir, rows=[_row("claude-klabauter", repo_root)]
        )

        rc = publish.main([])

        assert rc == 0
        captured = capsys.readouterr()
        assert "PERSONAL_REVIEW_PATTERNS" not in captured.err

    def test_machine_local_present_but_review_patterns_empty_aborts(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        (setup_dir / ".percolate-identity.example").write_text("# example\n", encoding="utf-8")
        settings_home = tmp_path / "settings-home"
        settings_home.mkdir()
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        machine_local = settings_home / ".percolate-identity"
        machine_local.write_text(
            "PERSONAL_EXPECTED_PATTERNS=(\n)\nPERSONAL_REVIEW_PATTERNS=(\n)\nPERSONAL_ALLOW_TOKENS=(\n)\n",
            encoding="utf-8",
        )
        machine_local.chmod(0o600)

        repo_root = tmp_path / "dest-repo"
        (repo_root / ".git").mkdir(parents=True)
        _wire_main_preconditions_except_identity(
            monkeypatch, setup_dir=setup_dir, rows=[_row("claude-klabauter", repo_root)]
        )

        rc = publish.main([])

        assert rc != 0
        captured = capsys.readouterr()
        assert "FATAL" in captured.err
        assert "PERSONAL_REVIEW_PATTERNS" in captured.err

    @pytest.mark.skipif(
        os.name != "posix",
        reason=(
            "group/world-writable is a POSIX mode bit; publish.py's own "
            "ownership/mode security check is a deliberate no-op on Windows "
            "(it prints a WARNING and proceeds unverified — see "
            "check_identity_file_safe) rather than a gap in this test."
        ),
    )
    def test_machine_local_present_but_group_world_writable_aborts(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        settings_home = tmp_path / "settings-home"
        settings_home.mkdir()
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        machine_local = settings_home / ".percolate-identity"
        machine_local.write_text(
            "PERSONAL_EXPECTED_PATTERNS=(\n)\n"
            "PERSONAL_REVIEW_PATTERNS=(\n  \"machine-local-slug\"\n)\n"
            "PERSONAL_ALLOW_TOKENS=(\n)\n",
            encoding="utf-8",
        )
        machine_local.chmod(0o666)

        repo_root = tmp_path / "dest-repo"
        (repo_root / ".git").mkdir(parents=True)
        _wire_main_preconditions_except_identity(
            monkeypatch, setup_dir=setup_dir, rows=[_row("claude-klabauter", repo_root)]
        )

        rc = publish.main([])

        assert rc != 0
        captured = capsys.readouterr()
        assert "SECURITY" in captured.err

    def test_both_absent_names_both_candidate_paths(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        settings_home = tmp_path / "settings-home"
        settings_home.mkdir()
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        repo_root = tmp_path / "dest-repo"
        (repo_root / ".git").mkdir(parents=True)
        _wire_main_preconditions_except_identity(
            monkeypatch, setup_dir=setup_dir, rows=[_row("claude-klabauter", repo_root)]
        )

        rc = publish.main([])

        assert rc != 0
        captured = capsys.readouterr()
        assert "FATAL" in captured.err
        assert str(setup_dir / ".percolate-identity") in captured.err
        assert str(settings_home / ".percolate-identity") in captured.err

    def test_env_override_honoured_for_machine_local_rung(self, tmp_path, monkeypatch):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        custom_home = tmp_path / "custom-settings-home"
        custom_home.mkdir()
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(custom_home))

        machine_local = custom_home / ".percolate-identity"
        machine_local.write_text("PERSONAL_REVIEW_PATTERNS=(\n  \"slug\"\n)\n", encoding="utf-8")
        machine_local.chmod(0o600)

        resolved = publish.resolve_percolate_identity_path(setup_dir)

        assert resolved == machine_local

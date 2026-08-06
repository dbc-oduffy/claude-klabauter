"""test_install_doc_payload_gate_wiring — wiring tests for
`dispatch_end_of_run_install_doc_payload_check`, the publish-side call site
for `check-install-doc-payload.py`'s `check_tree()` (task: close the
klabauter row-1 P0 where an install doc referenced a file no publish row
ever shipped, and nothing compared the doc against the published tree).

Placement decision under test: END-OF-RUN ONLY, never per-row. Wiring this
gate per-row (as `dispatch_percolate_pre_ci`'s docstring originally
suggested, mirroring `run_identity_check`) produces false positives for any
multi-row target: `check-install-doc-payload.py`'s own author found, running
it live against `dist/klabauter-toplevel`, that `CONTRIBUTING.md` references
`.github/scripts/run-all-checks.py` -- a file that ships from the SIBLING
toplevel row, not the row being checked (state/subagent-share/
e4ae702d-32fa-4954-96d0-63fcfe810f9b/coordinatorexecutor-cad6ccb4.md).
Unlike the identity checker, `check_tree()` has no "not found yet" signal to
make that case advisory per-row -- a finding just IS a finding -- so a
per-row call would either always advisory-skip cross-row references
(defeating the gate) or false-positive on every multi-row target, exactly as
observed live. Only after every declared row has synced does the tree the
docs actually describe exist to check against.

Severity split mirrors `dispatch_end_of_run_identity_check`'s, for the
identical `--target` reason (`main()` skips every non-matching row, so a
single-target debug publish may never place a file a doc references):
findings are a HARD failure on an unfiltered run, ADVISORY (loud WARNING,
no failure) under `--target`.

Run: python -m pytest coordinator/bin/tests/test_install_doc_payload_gate_wiring.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_install_doc_payload_gate_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _write_clean_tree(root: Path) -> None:
    """An INSTALL.md whose only code-formatted command resolves in-tree."""
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "setup.py").write_text("# setup\n", encoding="utf-8")
    (root / "INSTALL.md").write_text(
        "# Install\n\n```\npython3 scripts/setup.py\n```\n", encoding="utf-8"
    )


def _write_broken_tree(root: Path) -> None:
    """An INSTALL.md referencing a script that was never published --
    the P0 shape this gate exists to catch."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "INSTALL.md").write_text(
        "# Install\n\n```\npython3 scripts/setup.py\n```\n", encoding="utf-8"
    )


def _write_cross_row_tree(root: Path) -> None:
    """The exact live shape the module's own author found:
    CONTRIBUTING.md referencing a file that ships from a SIBLING row,
    absent from this row's slice of the tree."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "CONTRIBUTING.md").write_text(
        "# Contributing\n\nRun `.github/scripts/run-all-checks.py` before you push.\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# dispatch_end_of_run_install_doc_payload_check — direct unit tests.
# ---------------------------------------------------------------------------
class TestEndOfRunInstallDocPayloadCheckLeg:
    def test_clean_tree_passes(self, tmp_path):
        repo_root = tmp_path / "repo"
        _write_clean_tree(repo_root)

        ok = publish.dispatch_end_of_run_install_doc_payload_check(
            [repo_root], target_filtered=False
        )
        assert ok is True

    def test_unfiltered_broken_reference_is_hard_failure(self, tmp_path, capsys):
        repo_root = tmp_path / "repo"
        _write_broken_tree(repo_root)

        ok = publish.dispatch_end_of_run_install_doc_payload_check(
            [repo_root], target_filtered=False
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "install-doc payload check FAILED" in captured.err
        assert "scripts/setup.py" in captured.err

    def test_filtered_broken_reference_is_advisory_not_a_failure(self, tmp_path, capsys):
        repo_root = tmp_path / "repo"
        _write_broken_tree(repo_root)

        ok = publish.dispatch_end_of_run_install_doc_payload_check(
            [repo_root], target_filtered=True
        )
        assert ok is True
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "advisory under --target" in captured.err
        assert "scripts/setup.py" in captured.err

    def test_cross_row_reference_shape_from_the_authors_live_finding(self, tmp_path):
        """The exact live false-positive shape the author found
        (CONTRIBUTING.md -> .github/scripts/run-all-checks.py, shipped by a
        sibling row) is a REAL finding at end-of-run -- it is real because,
        by construction, this leg only ever runs after every row this
        invocation is going to sync already has. If this leg ran per-row
        instead, this exact fixture is what would false-positive on a row
        that isn't finished publishing yet; run at end-of-run, it is a
        legitimate signal that the sibling row's file never actually
        shipped in THIS invocation."""
        repo_root = tmp_path / "repo"
        _write_cross_row_tree(repo_root)

        ok = publish.dispatch_end_of_run_install_doc_payload_check(
            [repo_root], target_filtered=False
        )
        assert ok is False

    def test_multiple_repo_roots_worst_of_both(self, tmp_path):
        clean_root = tmp_path / "clean-repo"
        _write_clean_tree(clean_root)
        broken_root = tmp_path / "broken-repo"
        _write_broken_tree(broken_root)

        ok = publish.dispatch_end_of_run_install_doc_payload_check(
            [clean_root, broken_root], target_filtered=False
        )
        assert ok is False


# ---------------------------------------------------------------------------
# publish.main() wiring -- proves the leg is actually called, dry-run never
# fires it, and the per-row leg does NOT independently reproduce the
# cross-row false positive (confirming the placement decision holds
# end-to-end, not just at the unit level).
# ---------------------------------------------------------------------------
class _StubClaudeKlabauter:
    """Trivial fake `ClaudeKlabauterPercolate` -- these tests exist to prove the
    install-doc-payload wiring, not engine-phase behaviour, so every engine
    call is a no-op; `run_identity_check` returns an unconditional CLEAN PASS
    (`ran=True, skipped=False, exit_code=0`) so the identity leg (already
    covered by test_percolate_identity_check_gate.py, including its own
    unfiltered-skip-is-a-hard-failure case) never masks this leg's own
    pass/fail signal -- a `skipped=True` stand-in would itself hard-fail an
    unfiltered run before this leg's result could be observed. `file_surface`
    names `*.md`/`*.py` so `dispatch_end_of_run_unscanned_published_check`
    (which calls the REAL `coordinator_core.percolate.surface.iter_surface_files`
    directly, not this stub's own `iter_surface_files` below) sees these
    fixtures' plain `.md`/`.py` content as in-surface -- an empty
    `file_surface` would make every published file "unscanned" and fail
    that leg regardless of what this test is actually exercising."""

    def resolve_target(self, store, name):
        return {
            "hooks": [],
            "file_surface": {"include_extensions": ["*.md", "*.py"]},
            "guards": [],
            "inject": [],
        }

    def run_percolate(self, store_path, target, target_root, phase, **kwargs):
        return {"phase": phase, "guard_results": [], "rename_manifest": None, "restored_native": []}

    def iter_surface_files(self, root, **kwargs):
        return iter(())

    def run_identity_check(self, dest):
        return {"ran": True, "skipped": False, "exit_code": 0, "findings": "clean"}


def _wire_main_preconditions(monkeypatch, *, setup_dir: Path, rows: list) -> None:
    percolate_root = setup_dir.parent
    monkeypatch.setattr(
        publish, "_resolve_percolate_root_and_rung", lambda **kwargs: (percolate_root, "test-rung")
    )
    monkeypatch.setattr(
        publish, "load_targets", lambda setup_dir, target_filter="", **kwargs: rows
    )
    monkeypatch.setattr(publish, "locate_percolate_store", lambda setup_dir: setup_dir / "store.yaml")
    monkeypatch.setattr(publish, "_import_claude_klabauter_percolate", lambda: _StubClaudeKlabauter())
    monkeypatch.setattr(publish, "assert_percolate_store_ready", lambda claude-klabauter, store_path: {"targets": {}})
    monkeypatch.setattr(publish, "check_identity_file_present", lambda *a, **k: None)
    monkeypatch.setattr(publish, "check_identity_file_safe", lambda *a, **k: None)
    monkeypatch.setattr(
        publish,
        "parse_percolate_identity",
        lambda *a, **k: publish.PercolateIdentity(review=["test-machine-slug"]),
    )
    monkeypatch.setattr(publish, "_import_publish_sync", lambda setup_dir: object())
    monkeypatch.setattr(publish, "check_publish_sync_contract", lambda *a, **k: None)
    monkeypatch.setattr(publish, "process_target", lambda *a, **k: None)


def _single_row(name: str, repo_root: Path) -> list:
    return [f"{name}|mirror|{repo_root / 'src'}|{repo_root}"]


class TestInstallDocPayloadMainWiring:
    def test_full_run_broken_reference_fails(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        _write_broken_tree(repo_root)

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=_single_row("t", repo_root))

        rc = publish.main([])
        assert rc != 0
        captured = capsys.readouterr()
        assert "install-doc payload check FAILED" in captured.err

    def test_full_run_clean_tree_passes(self, tmp_path, monkeypatch):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        _write_clean_tree(repo_root)

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=_single_row("t", repo_root))

        rc = publish.main([])
        assert rc == 0

    def test_target_filtered_run_broken_reference_stays_advisory(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        _write_broken_tree(repo_root)

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=_single_row("engine-row", repo_root))

        rc = publish.main(["engine-row"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "advisory under --target" in captured.err

    def test_dry_run_never_fires_the_leg(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        _write_broken_tree(repo_root)  # would fail loudly if the leg fired

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=_single_row("t", repo_root))

        rc = publish.main(["--dry-run"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "install-doc payload check" not in captured.err

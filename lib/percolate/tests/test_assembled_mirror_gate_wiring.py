"""test_assembled_mirror_gate_wiring -- wiring tests for
`coordinator/bin/publish.py::dispatch_end_of_run_assembled_mirror_gate` and
its exemption-ledger loader `_load_assembled_mirror_gate_exemptions`.

Chunk C3 (docs/plans/2026-08-28-a-dropped-module-must-not-leave-its-test-
behind.md) gives C2's `run_assembled_mirror_gate` a production call site.
These tests prove the WIRING fires through the driver-level function (real
`run_assembled_mirror_gate`, a genuine `pytest --collect-only` subprocess
against scratch trees) and that the declared-exemption ledger behaves as
specified: an undeclared refusal is FATAL, a declared one is a WARNING.

Run: python -m pytest coordinator/lib/percolate/tests/test_assembled_mirror_gate_wiring.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_BIN_DIR = _REPO_ROOT / "coordinator" / "bin"


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_assembled_mirror_gate_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


class _ResolvedTargetStub:
    """Minimal stand-in for `publish.ResolvedTarget` -- `.name` (read by
    `dispatch_end_of_run_assembled_mirror_gate` for exemption lookup) and
    `.source_dir` (read by the same function to build the source-root list
    it now passes to `find_modules_missing_tests` for the C6 coverage
    WARN). `source_dir` defaults to a throwaway path -- these tests assert
    on the pass/refuse verdict, never on coverage-WARN content, so any
    walkable-or-not path is sufficient; `find_modules_missing_tests`
    tolerates a nonexistent root (`Path.rglob` yields nothing)."""

    def __init__(self, name, source_dir=None):
        self.name = name
        self.source_dir = source_dir if source_dir is not None else Path("does-not-exist")


def _write_collectable_tree(root: Path) -> None:
    """A tree whose own fast-tier command collects cleanly: one passing
    test, a `pytest.ini` declaring the marker vocabulary the gate's own
    `MARKER_EXPRESSION` selects against, and no `cadence`/`pending_fix`/
    `designed_red` marker registration needed since none is used.

    Also carries an empty `coordinator_core/` directory at its root --
    `_verify_isolation_precondition` (assembled_mirror_gate.py) refuses to
    spawn the collection subprocess against a tree missing this directory,
    since cwd-shadowing of the ambient editable install is what the gate's
    own isolation guarantee rests on (see that function's docstring). A
    synthetic scratch tree without it is REFUSED before any subprocess
    runs, not collected cleanly -- this fixture is standing in for a real
    assembled mirror, which always has this directory.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "coordinator_core").mkdir(parents=True, exist_ok=True)
    (root / "pytest.ini").write_text(
        "[pytest]\ntestpaths = .\n", encoding="utf-8", newline="\n"
    )
    (root / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8", newline="\n"
    )


def _write_colliding_tree(root: Path) -> None:
    """A tree whose collection genuinely errors -- module-scope code that
    raises on import, the exact klabauter#3 shape this whole plan exists
    to catch."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pytest.ini").write_text(
        "[pytest]\ntestpaths = .\n", encoding="utf-8", newline="\n"
    )
    (root / "test_broken.py").write_text(
        "import this_module_does_not_exist_anywhere_c3_wiring\n",
        encoding="utf-8",
        newline="\n",
    )


class TestEndOfRunAssembledMirrorGateLeg:
    def test_clean_tree_passes(self, tmp_path):
        repo_root = tmp_path / "repo"
        _write_collectable_tree(repo_root)

        ok = publish.dispatch_end_of_run_assembled_mirror_gate(
            [repo_root], rows_by_repo_root={}, target_filtered=False
        )
        assert ok is True

    def test_clean_tree_with_no_source_rows_skips_coverage_leg(self, tmp_path):
        """P2 regression (code review, 2026-08-30): `rows_by_repo_root.get
        (repo_root, [])` empty (no row keyed to this repo root) used to fall
        back to `source_roots or [repo_root]` -- comparing the assembled
        mirror against ITSELF, silently reinstating the exact source-vs-
        mirror conflation this gate exists to kill. The coverage leg must
        now abstain out loud instead of emitting a WARN it cannot stand
        behind."""
        import io

        repo_root = tmp_path / "repo"
        _write_collectable_tree(repo_root)
        buf = io.StringIO()

        ok = publish.dispatch_end_of_run_assembled_mirror_gate(
            [repo_root], rows_by_repo_root={}, target_filtered=False, out=buf
        )

        assert ok is True
        out = buf.getvalue()
        assert "coverage leg: SKIPPED" in out
        assert str(repo_root) in out
        assert "assembled-mirror-gate: WARN" not in out

    def test_colliding_tree_with_no_exemption_is_fatal(self, tmp_path, monkeypatch, capsys):
        repo_root = tmp_path / "repo"
        _write_colliding_tree(repo_root)
        monkeypatch.setattr(publish, "_load_assembled_mirror_gate_exemptions", lambda: {})

        ok = publish.dispatch_end_of_run_assembled_mirror_gate(
            [repo_root],
            rows_by_repo_root={repo_root: [_ResolvedTargetStub("claude-klabauter")]},
            target_filtered=False,
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "assembled-mirror gate FAILED" in captured.err
        assert "claude-klabauter" in captured.err

    def test_target_filtered_collision_still_hard_fails(self, tmp_path, monkeypatch, capsys):
        """Same judgement call as the sibling end-of-run legs (§
        `dispatch_end_of_run_argv_parity_gate`'s own docstring): --target
        filtering must not soften a genuine collection failure to advisory."""
        repo_root = tmp_path / "repo"
        _write_colliding_tree(repo_root)
        monkeypatch.setattr(publish, "_load_assembled_mirror_gate_exemptions", lambda: {})

        ok = publish.dispatch_end_of_run_assembled_mirror_gate(
            [repo_root],
            rows_by_repo_root={repo_root: [_ResolvedTargetStub("claude-klabauter")]},
            target_filtered=True,
        )
        assert ok is False

    def test_declared_exemption_downgrades_to_warning(self, tmp_path, monkeypatch):
        import io

        repo_root = tmp_path / "repo"
        _write_colliding_tree(repo_root)
        monkeypatch.setattr(
            publish,
            "_load_assembled_mirror_gate_exemptions",
            lambda: {"claude-klabauter": "known debt, tracked separately"},
        )

        out_buffer = io.StringIO()
        ok = publish.dispatch_end_of_run_assembled_mirror_gate(
            [repo_root],
            rows_by_repo_root={repo_root: [_ResolvedTargetStub("claude-klabauter")]},
            target_filtered=False,
            out=out_buffer,
        )
        assert ok is True
        assert "known debt, tracked separately" in out_buffer.getvalue()

    def test_exemption_on_a_different_row_does_not_cover_this_root(self, tmp_path, monkeypatch, capsys):
        """A declared exemption is keyed by row name -- it must not blanket-
        cover every repo root, only the one(s) whose contributing rows it
        actually names."""
        repo_root = tmp_path / "repo"
        _write_colliding_tree(repo_root)
        monkeypatch.setattr(
            publish,
            "_load_assembled_mirror_gate_exemptions",
            lambda: {"some-other-row": "unrelated debt"},
        )

        ok = publish.dispatch_end_of_run_assembled_mirror_gate(
            [repo_root],
            rows_by_repo_root={repo_root: [_ResolvedTargetStub("claude-klabauter")]},
            target_filtered=False,
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "assembled-mirror gate FAILED" in captured.err

    def test_missing_repo_root_is_a_hard_failure(self, tmp_path, capsys):
        repo_root = tmp_path / "does-not-exist"

        ok = publish.dispatch_end_of_run_assembled_mirror_gate(
            [repo_root], rows_by_repo_root={}, target_filtered=False
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "not a directory" in captured.err


class TestLoadAssembledMirrorGateExemptions:
    def test_missing_file_returns_empty(self, tmp_path):
        exemptions = publish._load_assembled_mirror_gate_exemptions(tmp_path / "absent.yaml")
        assert exemptions == {}

    def test_reads_declared_entries(self, tmp_path):
        path = tmp_path / "declarations.yaml"
        path.write_text(
            "assembled_mirror_gate_exemptions:\n"
            "  - name: claude-klabauter\n"
            "    reason: known debt\n",
            encoding="utf-8",
        )
        exemptions = publish._load_assembled_mirror_gate_exemptions(path)
        assert exemptions == {"claude-klabauter": "known debt"}

    def test_malformed_entry_is_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "declarations.yaml"
        path.write_text(
            "assembled_mirror_gate_exemptions:\n"
            "  - name: claude-klabauter\n"  # missing reason -- malformed
            "  - name: valid-row\n"
            "    reason: a real reason\n",
            encoding="utf-8",
        )
        exemptions = publish._load_assembled_mirror_gate_exemptions(path)
        assert exemptions == {"valid-row": "a real reason"}

    def test_absent_key_returns_empty(self, tmp_path):
        path = tmp_path / "declarations.yaml"
        path.write_text("rows:\n  some-row: {}\n", encoding="utf-8")
        exemptions = publish._load_assembled_mirror_gate_exemptions(path)
        assert exemptions == {}

    def test_real_declarations_file_loads_without_raising(self):
        """The actual `setup/publish-allowlist-declarations.yaml` must parse
        and expose its `assembled_mirror_gate_exemptions` list -- proves the
        top-level key does not collide with the existing `rows:` schema.

        Asserted as shape, not as a fixed membership: the loader degrades a
        malformed entry to silence (see its own docstring), so an empty dict
        here is indistinguishable from an unreadable ledger. Every declared
        entry carrying a non-empty reason is the property worth pinning --
        the ledger's own comment requires a stated reason per row, and that
        is what a silent degradation would drop."""
        real_path = _REPO_ROOT / "setup" / "publish-allowlist-declarations.yaml"
        exemptions = publish._load_assembled_mirror_gate_exemptions(real_path)
        assert isinstance(exemptions, dict)
        assert all(name and reason.strip() for name, reason in exemptions.items())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

"""test_publish_argv_parity_gate -- wiring and behaviour tests for
`dispatch_end_of_run_argv_parity_gate`, the publish-side call site for C1's
`directive_cli_arity.argv_parity_report`.

Spec backlink: C4 of
`docs/plans/2026-08-15-bind-the-klabauter-publish-rows-into-a-parity-group.md`.

This gate runs C1's static AST oracle against each distinct ASSEMBLED
DESTINATION repo root (`<root>/coordinator_core` paired with
`<root>/coordinator/bin`), once per end-of-run, the exact tree the
klabauter mirror's consumers execute. It closes the 2026-08-15 live
incident's blind spot: nothing previously checked whether a published
mirror's argv emitter and its argv-parsing entrypoint agree.

Covers: direct wiring (called from `main()`, verdict reaches `gates_ok`),
the exit-2 path on a synthetic skewed destination tree for BOTH
`unaccepted` and `undeclared_required`, non-degradation under `--target`,
and that `--dry-run` never reaches it.

Run: python -m pytest coordinator/bin/tests/test_publish_argv_parity_gate.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_argv_parity_gate_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


_KNOWN_CLI_SOURCE = '''
import argparse


def build_parser():
    parser = argparse.ArgumentParser(prog="known-cli.py")
    parser.add_argument("--sid", default=None)
    parser.add_argument("--subject", required=True)
    return parser


if __name__ == "__main__":
    build_parser().parse_args()
'''


def _write(root: Path, rel: str, source: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator" / "bin").mkdir(parents=True)
    return root


def _write_clean_tree(root: Path) -> None:
    """Emitter emits exactly what the parser declares as required -- a
    fully resolvable, clean pairing."""
    _write(root, "coordinator/bin/known-cli.py", _KNOWN_CLI_SOURCE)
    _write(
        root,
        "coordinator_core/clean_assembler.py",
        '''
def build_directive(sid, subject):
    args = ["--sid", sid, "--subject", subject]
    return {"id": "d1", "cli": "known-cli", "args": args, "depends_on": None}
''',
    )


def _write_unaccepted_tree(root: Path) -> None:
    """Emitter emits a flag the target parser does not declare -- the
    2026-08-15 incident's own shape (wsc-tail.py rejecting a flag
    directives_commit_tail.py had already started emitting)."""
    _write(root, "coordinator/bin/known-cli.py", _KNOWN_CLI_SOURCE)
    _write(
        root,
        "coordinator_core/unaccepted_assembler.py",
        '''
def build_directive(sid, subject):
    args = ["--sid", sid, "--subject", subject, "--not-declared-flag"]
    return {"id": "d1", "cli": "known-cli", "args": args, "depends_on": None}
''',
    )


def _write_undeclared_required_tree(root: Path) -> None:
    """Emitter never emits `--subject`, a `required=True` flag the parser
    declares -- the direction C3's reorder can introduce for non-additive
    parser changes (the Staff Engineer C1/F4, C4/F4)."""
    _write(root, "coordinator/bin/known-cli.py", _KNOWN_CLI_SOURCE)
    _write(
        root,
        "coordinator_core/missing_required_assembler.py",
        '''
def build_directive(sid):
    args = ["--sid", sid]
    return {"id": "d1", "cli": "known-cli", "args": args, "depends_on": None}
''',
    )


# ---------------------------------------------------------------------------
# dispatch_end_of_run_argv_parity_gate -- direct unit tests.
# ---------------------------------------------------------------------------
class TestEndOfRunArgvParityGateLeg:
    def test_clean_tree_passes(self, tmp_path):
        repo_root = _make_repo(tmp_path)
        _write_clean_tree(repo_root)

        ok = publish.dispatch_end_of_run_argv_parity_gate([repo_root], target_filtered=False)
        assert ok is True

    def test_unaccepted_flag_is_a_failure(self, tmp_path, capsys):
        repo_root = _make_repo(tmp_path)
        _write_unaccepted_tree(repo_root)

        ok = publish.dispatch_end_of_run_argv_parity_gate([repo_root], target_filtered=False)
        assert ok is False
        captured = capsys.readouterr()
        assert "argv-parity gate FAILED" in captured.err
        assert "--not-declared-flag" in captured.err

    def test_undeclared_required_flag_is_a_failure(self, tmp_path, capsys):
        repo_root = _make_repo(tmp_path)
        _write_undeclared_required_tree(repo_root)

        ok = publish.dispatch_end_of_run_argv_parity_gate([repo_root], target_filtered=False)
        assert ok is False
        captured = capsys.readouterr()
        assert "argv-parity gate FAILED" in captured.err
        assert "--subject" in captured.err

    def test_non_degradation_under_target_filtered(self, tmp_path, capsys):
        """FAIL-HARD UNCONDITIONALLY (AC4) -- unlike the identity/install-doc
        legs, this gate must fail identically whether or not `--target`
        narrowed this invocation's row set. A `--target` subset publish is
        precisely how a partial/skewed mirror gets constructed."""
        repo_root = _make_repo(tmp_path)
        _write_unaccepted_tree(repo_root)

        ok = publish.dispatch_end_of_run_argv_parity_gate([repo_root], target_filtered=True)
        assert ok is False
        captured = capsys.readouterr()
        assert "argv-parity gate FAILED" in captured.err
        assert "advisory" not in captured.err

    def test_unresolved_pairing_is_neither_pass_nor_fail_content_but_does_not_fail(self, tmp_path):
        """An unresolvable pairing (unknown `cli`) must not itself flip the
        gate to failure -- `unresolved` is never treated as `clean`, but a
        gate over a tree with only unresolved pairings and no real
        unaccepted/undeclared_required finding has nothing provable to fail
        on."""
        repo_root = _make_repo(tmp_path)
        _write(
            repo_root,
            "coordinator_core/unresolvable_assembler.py",
            '''
def build_directive():
    return {"id": "d1", "cli": "no-such-cli", "args": ["--x"], "depends_on": None}
''',
        )

        ok = publish.dispatch_end_of_run_argv_parity_gate([repo_root], target_filtered=False)
        assert ok is True

    def test_multiple_repo_roots_worst_of_both(self, tmp_path):
        clean_root = _make_repo(tmp_path, "clean-repo")
        _write_clean_tree(clean_root)
        broken_root = _make_repo(tmp_path, "broken-repo")
        _write_unaccepted_tree(broken_root)

        ok = publish.dispatch_end_of_run_argv_parity_gate(
            [clean_root, broken_root], target_filtered=False
        )
        assert ok is False

    def test_origin_lookup_batched_once_across_all_failing_roots(self, tmp_path, monkeypatch):
        """docs/plans/2026-08-19-burn-down-the-amplification-hitlist.md C5-2:
        the origin-lookup spawn helper must be called EXACTLY ONCE per gate
        invocation, passed EVERY failing root's pairings together -- not
        once per `repo_root` from inside the loop over `repo_roots` (the
        per-item-call-inside-a-qualifying-loop shape
        `test_no_unbatched_per_item_git_spawn.py`'s amplification collector
        flags). TWO roots, BOTH broken, is required to distinguish this
        from a single-root call, which looks identical either way -- a
        single-root regression test would pass unchanged against the
        pre-fix shape that called the batch helper once per root from
        inside the loop."""
        root_a = _make_repo(tmp_path, "broken-a")
        _write_unaccepted_tree(root_a)
        root_b = _make_repo(tmp_path, "broken-b")
        _write_undeclared_required_tree(root_b)

        calls: list = []
        original = publish._argv_parity_pairing_origin_batch_by_root

        def _spy(rel_modules_by_root):
            calls.append(dict(rel_modules_by_root))
            return original(rel_modules_by_root)

        monkeypatch.setattr(publish, "_argv_parity_pairing_origin_batch_by_root", _spy)

        ok = publish.dispatch_end_of_run_argv_parity_gate(
            [root_a, root_b], target_filtered=False
        )
        assert ok is False
        assert len(calls) == 1, f"expected exactly one batched origin-lookup call, got {len(calls)}"
        assert set(calls[0]) == {root_a, root_b}

    def test_missing_source_baseline_fails_hard(self, tmp_path, monkeypatch, capsys):
        """A gate that cannot find its baseline must FAIL, not degrade to
        permissive -- even over an otherwise-clean destination tree."""
        repo_root = _make_repo(tmp_path)
        _write_clean_tree(repo_root)
        monkeypatch.setattr(
            publish, "_SOURCE_ARGV_PARITY_BASELINE_PATH", tmp_path / "no-such-baseline.json"
        )

        ok = publish.dispatch_end_of_run_argv_parity_gate([repo_root], target_filtered=False)
        assert ok is False
        captured = capsys.readouterr()
        assert "could not load its source-repo baseline" in captured.err

    def test_baseline_relative_skew_matching_baseline_passes(self, tmp_path, monkeypatch):
        """A pairing whose exact module/directive_id/cli/token is already in
        the committed baseline is a KNOWN pre-existing defect, not a new
        one -- the gate must not fail on it, mirroring C2's subset
        semantics."""
        repo_root = _make_repo(tmp_path)
        _write_unaccepted_tree(repo_root)
        baseline_path = tmp_path / "baseline.json"
        baseline_path.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "module": "coordinator_core/unaccepted_assembler.py",
                            "directive_id": "d1",
                            "cli": "known-cli",
                            "unresolved": False,
                            "unaccepted": ["--not-declared-flag"],
                            "undeclared_required": [],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(publish, "_SOURCE_ARGV_PARITY_BASELINE_PATH", baseline_path)

        ok = publish.dispatch_end_of_run_argv_parity_gate([repo_root], target_filtered=False)
        assert ok is True

    def test_partial_baseline_match_still_fails_on_the_new_token(self, tmp_path, monkeypatch, capsys):
        """A pairing that already has ONE baselined token must still fail
        when it picks up a SECOND, NEW token -- the baseline relaxation is
        per-TOKEN, not per-pairing. Guards against a regression that widens
        the subset check to "pairing already has a baseline entry" instead
        of "this exact token is in the baseline"."""
        repo_root = _make_repo(tmp_path)
        _write(repo_root, "coordinator/bin/known-cli.py", _KNOWN_CLI_SOURCE)
        _write(
            repo_root,
            "coordinator_core/two_flag_assembler.py",
            '''
def build_directive(sid, subject):
    args = [
        "--sid", sid, "--subject", subject,
        "--not-declared-flag", "--also-new-flag",
    ]
    return {"id": "d1", "cli": "known-cli", "args": args, "depends_on": None}
''',
        )
        baseline_path = tmp_path / "baseline.json"
        baseline_path.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "module": "coordinator_core/two_flag_assembler.py",
                            "directive_id": "d1",
                            "cli": "known-cli",
                            "unresolved": False,
                            "unaccepted": ["--not-declared-flag"],
                            "undeclared_required": [],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(publish, "_SOURCE_ARGV_PARITY_BASELINE_PATH", baseline_path)

        ok = publish.dispatch_end_of_run_argv_parity_gate([repo_root], target_filtered=False)
        assert ok is False
        captured = capsys.readouterr()
        assert "argv-parity gate FAILED" in captured.err
        assert "--also-new-flag" in captured.err
        assert "--not-declared-flag" not in captured.err

    def test_origin_tag_reports_destination_only_for_non_git_tree(self, tmp_path, capsys):
        """A synthetic fixture tree is never a git work tree, so
        `_argv_parity_pairing_origin` cannot resolve it and reports
        `unknown-origin` -- proves the tag is present in the failure output
        without depending on a real git repo (this test must never touch
        the real klabauter mirror or any other git tree)."""
        repo_root = _make_repo(tmp_path)
        _write_unaccepted_tree(repo_root)

        ok = publish.dispatch_end_of_run_argv_parity_gate([repo_root], target_filtered=False)
        assert ok is False
        captured = capsys.readouterr()
        assert "origin" in captured.err or "unknown-origin" in captured.err


# ---------------------------------------------------------------------------
# publish.main() wiring -- proves the leg is actually called, its verdict
# reaches gates_ok, and --dry-run never fires it.
# ---------------------------------------------------------------------------
class _StubClaudeKlabauter:
    """Trivial fake `ClaudeKlabauterPercolate` -- these tests exist to prove the
    argv-parity wiring, not engine-phase behaviour, so every OTHER engine
    call is a no-op/clean-pass stand-in, matching the shape
    `test_install_doc_payload_gate_wiring.py::_StubClaudeKlabauter` uses."""

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

    def run_parse_sweep(self, repo_root):
        return type("ParseResult", (), {"ok": True, "failures": [], "scanned": 0})()

    def enumerate_gate_entrypoints(self, repo_root):
        return ()


def _fake_process_target_succeeds(target, setup_dir, totals, **kwargs):
    totals.processed += 1


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
    monkeypatch.setattr(publish, "assert_percolate_store_ready", lambda claude_klabauter_root, store_path: {"targets": {}})
    monkeypatch.setattr(publish, "check_identity_file_present", lambda *a, **k: None)
    monkeypatch.setattr(publish, "check_identity_file_safe", lambda *a, **k: None)
    monkeypatch.setattr(
        publish,
        "parse_percolate_identity",
        lambda *a, **k: publish.PercolateIdentity(review=["test-machine-slug"]),
    )
    monkeypatch.setattr(publish, "_import_publish_sync", lambda setup_dir: object())
    monkeypatch.setattr(publish, "check_publish_sync_contract", lambda *a, **k: None)
    monkeypatch.setattr(publish, "process_target", _fake_process_target_succeeds)


def _single_row(name: str, repo_root: Path) -> list:
    return [f"{name}|mirror|{repo_root / 'src'}|{repo_root}"]


class TestArgvParityGateMainWiring:
    def test_full_run_unaccepted_flag_exits_2(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        _write_unaccepted_tree(repo_root)

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=_single_row("t", repo_root))

        rc = publish.main([])
        # Exit 2, not 1: this row's bytes DID land (`process_target`
        # advanced `totals.processed`) -- only the POST-publish argv-parity
        # gate failed, matching every other end-of-run leg's exit-code
        # contract (§ `main()`'s own docstring).
        assert rc == 2
        captured = capsys.readouterr()
        assert "argv-parity gate FAILED" in captured.err

    def test_full_run_clean_tree_passes(self, tmp_path, monkeypatch):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        _write_clean_tree(repo_root)

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=_single_row("t", repo_root))

        rc = publish.main([])
        assert rc == 0

    def test_target_filtered_run_still_fails_hard(self, tmp_path, monkeypatch, capsys):
        """AC4 non-degradation, exercised end-to-end through `main()`: a
        `--target`-scoped run with a skewed destination must still exit 2,
        never fall back to an advisory warning the way the identity/
        install-doc legs do."""
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        _write_unaccepted_tree(repo_root)

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=_single_row("engine-row", repo_root))

        rc = publish.main(["engine-row"])
        assert rc == 2
        captured = capsys.readouterr()
        assert "argv-parity gate FAILED" in captured.err

    def test_dry_run_never_fires_the_leg(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        _write_unaccepted_tree(repo_root)  # would fail loudly if the leg fired

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=_single_row("t", repo_root))

        rc = publish.main(["--dry-run"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "argv-parity gate" not in captured.err

"""
Tests for coordinator_core.ops.fleet.record_history -- "fleet.record_history".

Spec backlink: docs/plans/2026-08-20-a-counted-fleet-answer-for-record-history.md,
chunk C1.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import record_history as frh
from coordinator_core.ops.record_history import UnsupportedRecordTypeError


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        capture_output=True,
        check=check,
    )


def _make_real_git_repo(tmp_path: Path, name: str = "git-repo") -> Path:
    """A REAL, `git init`'d worktree — `_is_git_worktree` spawns
    `git rev-parse --is-inside-work-tree`, so a directory-with-a-`.git`-
    folder fixture (as `fleet.work_state`'s tests use) is not sufficient
    here; this predicate needs a repo git itself will accept."""
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@claude-klabauter.test")
    _git(root, "config", "user.name", "ClaudeKlabauterTest")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-m", "init")
    return root


def _make_real_non_git_dir(tmp_path: Path, name: str = "non-git-dir") -> Path:
    """Exists, is a directory, is NOT a git worktree at all."""
    root = tmp_path / name
    root.mkdir()
    return root


def _make_record(root: Path, record_type: str, name: str, status: str) -> Path:
    subdir = root / "state" / "sizings"
    subdir.mkdir(parents=True, exist_ok=True)
    path = subdir / name
    path.write_text(f"---\nstatus: {status}\n---\nBody.\n", encoding="utf-8")
    return path


class TestUnsupportedRecordTypeValidatedUpFront:
    def test_unsupported_type_raises_before_root_resolution(self, monkeypatch) -> None:
        """Validated explicitly, not merely delegated to `derive_across_roots`'s
        per-walked-root call — an empty/all-skipped root set would otherwise
        let an unsupported type through with no roots ever walked."""
        def _forbidden():
            raise AssertionError("root resolution must not run before type validation")

        monkeypatch.setattr(frh, "_resolve_active_sibling_paths", _forbidden)

        with pytest.raises(UnsupportedRecordTypeError):
            frh.build_fleet_record_history("handoff-ledger")

    def test_unsupported_type_message_lists_supported_types(self) -> None:
        with pytest.raises(UnsupportedRecordTypeError) as exc_info:
            frh.build_fleet_record_history("not-a-real-type")
        assert "not-a-real-type" in str(exc_info.value)


class TestPassThroughShapeUnchanged:
    def test_derive_across_roots_result_returned_verbatim(self, monkeypatch) -> None:
        sentinel = {
            "record_type": "sizing-object",
            "queried_root_count": 2,
            "roots_walked": ["a", "b"],
            "roots_skipped": [],
            "repos": {"a": [], "b": []},
        }
        captured = {}

        def _fake_derive_across_roots(roots, record_type):
            captured["roots"] = roots
            captured["record_type"] = record_type
            return sentinel

        fake_roots = [Path("a"), Path("b")]
        monkeypatch.setattr(frh, "_resolve_active_sibling_paths", lambda: fake_roots)
        monkeypatch.setattr(frh, "derive_across_roots", _fake_derive_across_roots)

        result = frh.build_fleet_record_history("sizing-object")

        assert result is sentinel
        assert captured["roots"] == fake_roots
        assert captured["record_type"] == "sizing-object"


class TestSkippedRootLegExercisedForReal:
    """AC3: one real git worktree, one real-but-non-git directory —
    `_is_git_worktree` (the predicate under test) is exercised for real,
    not assumed via a `.git`-folder-only fixture."""

    def test_git_worktree_walked_non_git_dir_skipped(self, tmp_path, monkeypatch) -> None:
        git_root = _make_real_git_repo(tmp_path, "git-repo")
        non_git_root = _make_real_non_git_dir(tmp_path, "non-git-dir")
        _make_record(git_root, "sizing-object", "s-000001.md", "sized")

        monkeypatch.setattr(
            frh, "_resolve_active_sibling_paths", lambda: [git_root, non_git_root]
        )

        result = frh.build_fleet_record_history("sizing-object")

        assert result["queried_root_count"] == 1
        assert git_root.as_posix() in result["roots_walked"]
        skipped_roots = {entry["root"] for entry in result["roots_skipped"]}
        assert non_git_root.as_posix() in skipped_roots
        assert git_root.as_posix() in result["repos"]
        assert non_git_root.as_posix() not in result["repos"]


class TestOpHandlerWiring:
    def test_registered_op_calls_build_fleet_record_history(self, monkeypatch) -> None:
        sentinel = {"record_type": "sizing-object", "queried_root_count": 0,
                     "roots_walked": [], "roots_skipped": [], "repos": {}}
        captured = {}

        def _fake_build(record_type):
            captured["record_type"] = record_type
            return sentinel

        monkeypatch.setattr(frh, "build_fleet_record_history", _fake_build)

        result = frh._fleet_record_history({"record_type": "sizing-object"}, repo_root=None)

        assert result is sentinel
        assert captured["record_type"] == "sizing-object"

    def test_missing_record_type_param_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            frh._fleet_record_history({}, repo_root=None)


class TestSpawnBudgetStaticProof:
    """AC5: a stub/AST assertion rather than a real-spawn test — the spawn
    ratchet requires `spawns_process` + `cadence` markers on anything that
    genuinely spawns git, which would push this coverage off the fast tier.
    Proves the handler and `build_fleet_record_history` issue no
    `subprocess` call of their own — every git spawn is delegated entirely
    into `coordinator_core.ops.record_history`, never duplicated here."""

    def test_module_source_has_no_direct_subprocess_call(self) -> None:
        source = Path(frh.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in (
                "run", "Popen", "check_output", "check_call",
            ):
                value = node.value
                if isinstance(value, ast.Name) and value.id == "subprocess":
                    pytest.fail(
                        f"direct subprocess.{node.attr} call found in "
                        f"{frh.__file__} at line {node.lineno}"
                    )
        assert "import subprocess" not in source

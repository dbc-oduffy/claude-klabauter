"""
Tests for coordinator_core.ops.fleet.record_history -- "fleet.record_history".

Spec backlink: docs/plans/2026-08-20-a-counted-fleet-answer-for-record-history.md,
chunk C1.

AC3's real-git-worktree coverage lives in the sibling
`test_record_history_real_git.py`, which is `spawns_process`/`cadence`-tiered
because it spawns real git. Everything here stays spawn-free and fast-tier.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import record_history as frh
from coordinator_core.ops.record_history import UnsupportedRecordTypeError


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


class TestSpawnBudgetIsPerWalkedRoot:
    """AC5's counting half. `TestSpawnBudgetStaticProof` proves this module
    adds no spawn of its own; this proves the inherited budget is one git pass
    per WALKED root and zero for a skipped one, without spawning anything --
    `derive_across_roots` calls `derive_type_history` exactly once per walked
    root and `continue`s past a skipped one, so counting calls to that seam
    counts the git passes."""

    def test_one_git_pass_per_walked_root_and_none_for_skipped(self, monkeypatch, tmp_path) -> None:
        import coordinator_core.ops.record_history as rh

        walked_a = tmp_path / "walked-a"
        walked_b = tmp_path / "walked-b"
        skipped = tmp_path / "skipped"
        for d in (walked_a, walked_b, skipped):
            d.mkdir()

        calls: list[Path] = []

        def _fake_derive_type_history(root, record_type):
            calls.append(Path(root))
            return []

        monkeypatch.setattr(
            rh, "_is_git_worktree", lambda root: Path(root) != skipped
        )
        monkeypatch.setattr(rh, "derive_type_history", _fake_derive_type_history)
        monkeypatch.setattr(
            frh, "_resolve_active_sibling_paths", lambda: [walked_a, walked_b, skipped]
        )

        result = frh.build_fleet_record_history("sizing-object")

        assert len(calls) == 2, f"expected one pass per walked root, got {calls}"
        assert set(calls) == {walked_a, walked_b}
        assert skipped not in calls
        assert result["queried_root_count"] == 2

    def test_budget_does_not_grow_with_skipped_root_count(self, monkeypatch, tmp_path) -> None:
        """Independence from ROOT count, not just from record count: adding
        skipped roots must not add passes."""
        import coordinator_core.ops.record_history as rh

        walked = tmp_path / "walked"
        walked.mkdir()
        skipped_roots = []
        for i in range(5):
            d = tmp_path / f"skipped-{i}"
            d.mkdir()
            skipped_roots.append(d)

        calls: list[Path] = []
        monkeypatch.setattr(rh, "_is_git_worktree", lambda root: Path(root) == walked)
        monkeypatch.setattr(
            rh, "derive_type_history", lambda root, rt: calls.append(Path(root)) or []
        )
        monkeypatch.setattr(
            frh, "_resolve_active_sibling_paths", lambda: [walked, *skipped_roots]
        )

        result = frh.build_fleet_record_history("sizing-object")

        assert len(calls) == 1
        assert result["queried_root_count"] == 1
        assert len(result["roots_skipped"]) == 5

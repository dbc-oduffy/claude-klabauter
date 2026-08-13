"""test_distill_delete_guard_cli_batches_realized_by — pytest tests for
coordinator/bin/distill-delete-guard.py's consumption of C29's batched
`_git_objects_exist` primitive (coordinator_core.distill.delete_guard) via the
C31 `existence_map` optional-parameter seam on `resolve_realized_by` /
`check_realized_by` / `evaluate_candidate_detailed` / `evaluate_candidate`.

Spec backlink: pln-kill-the-n-1-git-spawn-class-a-88897a § C31

Coverage:
  shape pre-scan (CLI):
    test_sha_shaped_realized_by_values_collects_full_and_short_sha_only
    test_sha_shaped_realized_by_values_skips_inline_and_path_shaped
  batching behavior (CLI, in-process argv, monkeypatched `_git_objects_exist` only):
    test_main_calls_git_objects_exist_once_not_per_candidate
    test_main_no_sha_shaped_candidates_still_calls_batch_with_empty_list
  existence_map contract on the library seam itself (delete_guard.py):
    test_resolve_realized_by_absent_map_unchanged_behavior
    test_resolve_realized_by_map_hit_used_without_scalar_spawn
    test_resolve_realized_by_map_miss_falls_through_to_scalar_not_treated_as_absent
"""
from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from coordinator_core.distill import delete_guard as _delete_guard  # noqa: E402


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "distill_delete_guard_cli",
        _BIN_DIR / "distill-delete-guard.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _make_candidate(tmp_path: Path, name: str, realized_by: str) -> Path:
    path = tmp_path / name
    path.write_text(
        "---\n"
        "from: someone\n"
        "to: someone-else\n"
        f"realized_by: {realized_by}\n"
        "status: actioned\n"
        "distill_fate: ephemeral\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# shape pre-scan
# ---------------------------------------------------------------------------


def test_sha_shaped_realized_by_values_collects_full_and_short_sha_only(tmp_path: Path) -> None:
    full = "a" * 40
    short = "b812d89"
    p1 = _make_candidate(tmp_path, "c1.md", full)
    p2 = _make_candidate(tmp_path, "c2.md", short)
    shas = _mod._sha_shaped_realized_by_values([p1, p2])
    assert shas == {full, short}


def test_sha_shaped_realized_by_values_skips_inline_and_path_shaped(tmp_path: Path) -> None:
    p1 = _make_candidate(tmp_path, "c1.md", "inline")
    p2 = _make_candidate(tmp_path, "c2.md", "some/repo/relative/path.md")
    shas = _mod._sha_shaped_realized_by_values([p1, p2])
    assert shas == set()


# ---------------------------------------------------------------------------
# batching behavior (CLI)
# ---------------------------------------------------------------------------


def test_main_calls_git_objects_exist_once_not_per_candidate(tmp_path: Path, monkeypatch) -> None:
    full = "c" * 40
    short = "d812d89"
    p1 = _make_candidate(tmp_path, "c1.md", full)
    p2 = _make_candidate(tmp_path, "c2.md", short)

    batch_calls: list[list[str]] = []
    scalar_calls: list[str] = []

    def fake_batch(shas, repo_root):
        batch_calls.append(list(shas))
        return {sha: True for sha in shas}

    def fake_scalar(sha, repo_root):
        scalar_calls.append(sha)
        return True

    monkeypatch.setattr(_delete_guard, "_git_objects_exist", fake_batch)
    monkeypatch.setattr(_delete_guard, "_git_object_exists", fake_scalar)
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    rc = _mod.main([str(p1), str(p2), "--repo-root", str(tmp_path)])

    assert rc == 0
    assert len(batch_calls) == 1
    assert set(batch_calls[0]) == {full, short}
    # The batched primitive supplied both existence answers via the
    # existence_map parameter — the scalar per-candidate spawn must never
    # fire for a fully-covered SHA set.
    assert scalar_calls == []


def test_main_no_sha_shaped_candidates_still_calls_batch_with_empty_list(tmp_path: Path, monkeypatch) -> None:
    p1 = _make_candidate(tmp_path, "c1.md", "inline")

    batch_calls: list[list[str]] = []

    def fake_batch(shas, repo_root):
        batch_calls.append(list(shas))
        return {}

    monkeypatch.setattr(_delete_guard, "_git_objects_exist", fake_batch)
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    rc = _mod.main([str(p1), "--repo-root", str(tmp_path)])

    assert rc == 0
    assert batch_calls == [[]]


# ---------------------------------------------------------------------------
# existence_map contract on the library seam itself
# ---------------------------------------------------------------------------


def test_resolve_realized_by_absent_map_unchanged_behavior(tmp_path: Path, monkeypatch) -> None:
    sha = "e" * 40
    calls: list[str] = []

    def fake_scalar(s, repo_root):
        calls.append(s)
        return True

    monkeypatch.setattr(_delete_guard, "_git_object_exists", fake_scalar)
    result = _delete_guard.resolve_realized_by(sha, tmp_path)
    assert result is True
    assert calls == [sha]


def test_resolve_realized_by_map_hit_used_without_scalar_spawn(tmp_path: Path, monkeypatch) -> None:
    sha = "f" * 40
    calls: list[str] = []

    def fake_scalar(s, repo_root):
        calls.append(s)
        return False  # would be wrong if used — proves the map was preferred

    monkeypatch.setattr(_delete_guard, "_git_object_exists", fake_scalar)
    result = _delete_guard.resolve_realized_by(sha, tmp_path, existence_map={sha: True})
    assert result is True
    assert calls == []


def test_resolve_realized_by_map_miss_falls_through_to_scalar_not_treated_as_absent(
    tmp_path: Path, monkeypatch
) -> None:
    sha_in_map = "1" * 40
    sha_missing_from_map = "2" * 40
    calls: list[str] = []

    def fake_scalar(s, repo_root):
        calls.append(s)
        return True

    monkeypatch.setattr(_delete_guard, "_git_object_exists", fake_scalar)
    # A map that only covers a DIFFERENT sha — sha_missing_from_map is absent
    # from it and must fall through to the scalar (never treated as False).
    result = _delete_guard.resolve_realized_by(
        sha_missing_from_map, tmp_path, existence_map={sha_in_map: True}
    )
    assert result is True
    assert calls == [sha_missing_from_map]

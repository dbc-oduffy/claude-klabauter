"""A fleet heal of prepare-commit-msg survives the next orient check-only read."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import git_hook_install as ghi  # noqa: E402
from test_git_hook_install import _make_worktree  # noqa: E402


def test_fleet_heal_persists_across_orient_check_only_read(tmp_path, monkeypatch, capsys):
    wt_root = _make_worktree(tmp_path)
    (wt_root / "CLAUDE.md").write_text("x", encoding="utf-8")
    coord_bin = tmp_path / "bin"
    coord_bin.mkdir()
    (coord_bin / "coordinator-prepare-commit-msg").write_text("x", encoding="utf-8")
    monkeypatch.setattr(ghi, "_resolve_coord_bin", lambda bin_dir, name, **_: str(coord_bin))

    registry = {"repos.wt": str(wt_root)}
    monkeypatch.setattr(ghi, "_merged_flat_registry", lambda: registry)

    hook = tmp_path / "main" / ".git" / "hooks" / "prepare-commit-msg"
    assert not hook.exists()

    ghi.ensure_hooks_fleet(str(coord_bin))
    capsys.readouterr()
    assert hook.is_file(), "heal did not write the hook"
    healed_bytes = hook.read_bytes()

    ghi.ensure_hooks_fleet(str(coord_bin), check_only=True, registry=registry)
    orient = capsys.readouterr().err.strip()
    assert orient == "", f"orient check-only read names the healed repo: {orient}"

    ghi.ensure_hooks_fleet(str(coord_bin))
    second = capsys.readouterr().err
    assert "repos.wt" not in second, f"second heal reported the repo: {second}"
    assert hook.read_bytes() == healed_bytes

from __future__ import annotations

import filecmp
from pathlib import Path

from coordinator_core.install.substrate import (
    _CH_FAMILY_FILES,
    _RM_FAMILY_FILES,
    _install_bin_resolvers,
    _load_bin_templates_manifest,
    _resolve_bin_templates_manifest_root,
    _static_bin_family_names,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_every_static_family_member_is_byte_identical_to_its_source(monkeypatch, tmp_path):
    ml_bin = tmp_path / "ml_bin"
    ch_bin = tmp_path / "ch_bin"
    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()

    bin_manifest = _load_bin_templates_manifest(_resolve_bin_templates_manifest_root())
    src_by_name: "dict[str, Path]" = {}
    for entry in bin_manifest.install_bin_resolvers_entries():
        f = entry.name
        src = ml_bin / f
        _write(src, f"ml-source-content::{f}\n")
        src_by_name[f] = src
    for f, _exec_bit in _CH_FAMILY_FILES:
        src = ch_bin / f
        _write(src, f"ch-source-content::{f}\n")
        src_by_name[f] = src
    # fixture — resolved via CLAUDE_KLABAUTER_ROOT below, exactly like a real install.
    for f in _RM_FAMILY_FILES:
        src_by_name[f] = _REPO_ROOT / "coordinator" / "lib" / "resolve-claude-klabauter" / f

    # coordinator_claude_klabauter_root() Rung 1 short-circuits on COORDINATOR_ENGINE_ROOT
    # CLAUDE_KLABAUTER_ROOT name from Rung 1; it is deleted rather than left alone so an
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(_REPO_ROOT))

    _install_bin_resolvers(
        ml_bin, ch_bin, bin_dst,
        check_only=False,
        python3_cmd_resolved_bin="/usr/bin/python3",
    )

    family_names = _static_bin_family_names()
    assert family_names, "the static family set must be non-empty for this test to assert anything"

    for name in sorted(family_names):
        dst = bin_dst / name
        src = src_by_name[name]
        assert dst.is_file(), f"{name}: expected in bin_dst post-install, missing"
        assert filecmp.cmp(src, dst, shallow=False), (
            f"{name}: bin_dst copy is not byte-identical to its OWN source {src} "
            "(never compare a .cmd/.ps1 twin against its extensionless sibling)"
        )

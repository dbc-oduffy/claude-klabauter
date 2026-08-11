"""Install-integration check: after a real `_install_bin_resolvers` pass,
every STATIC code-family artifact in `bin_dst` is byte-identical to its
source — a whole-family invariant, not a per-file census.

Spec backlink: docs/plans/2026-07-25-install-surface-freshness-classification.md
§ C10 / AC15. `_static_bin_family_names()` (substrate.py:1369) is the set
already consumed by `_sweep_orphaned_agent_helpers`'s completeness check;
this test iterates that SAME set post-install so coverage cannot silently
diverge from what a real install actually writes — the reviewer found the
`.ps1` staleness this closes by running this diff by hand.

Deliberately NOT covered here: the dynamically-derived agent-helper
forwarders (`coordinator/bin/*` bareword names) — their membership varies
with the live `coordinator/bin/` listing and is a separate concern from the
STATIC families this test asserts (see `_static_bin_family_names`'s own
docstring on the distinction). Also not covered: unit-level branch matrices
over `_install_one` itself (that's `test_install_one_overwrite_policy.py`)
and the classification-policy matrix (`_install_bin_resolvers`'s own C6
suite) — this file is install-integration (real source tree, real install
run, real destination), not a unit test, per the plan's explicit placement
rationale.
"""
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

    # Synthetic sources for the ml_bin-rooted families. Each .cmd/.ps1 twin
    # is given content that DIFFERS from its extensionless sibling's, so a
    # regression that cross-compared a twin against the wrong source (rather
    # than its own) would be caught, not accidentally passed.
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
    # _resolve_claude_klabauter.py's source is fixed by _install_bin_resolvers to the
    # REAL repo tree (coordinator/lib/resolve-claude-klabauter/), not a synthetic
    # fixture — resolved via CLAUDE_KLABAUTER_ROOT below, exactly like a real install.
    for f in _RM_FAMILY_FILES:
        src_by_name[f] = _REPO_ROOT / "coordinator" / "lib" / "resolve-claude-klabauter" / f

    # coordinator_claude_klabauter_root() Rung 1 short-circuits on CLAUDE_KLABAUTER_ROOT being
    # set — this repo checkout IS the claude-klabauter root, so this is a same-repo
    # self-reference, not a cross-repo dependency.
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(_REPO_ROOT))

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

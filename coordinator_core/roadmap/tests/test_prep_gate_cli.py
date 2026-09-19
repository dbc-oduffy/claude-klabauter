"""
coordinator_core/roadmap/tests/test_prep_gate_cli.py — the door-served CLI half
of the mise-prep bar.

Subject: `coordinator_core.roadmap.prep_gate_cli._targets`'s sidecar exclusion
(2026-09-18-doe-holds-no-scripts, leg 2), restated to the letter from DoE-claude
`coordinator/bin/mise-prep-gate.py :: _is_plan_sidecar`. A directory expansion
must skip a review/coverage sidecar (compound stem) the same way DoE's own
script does, or a corpus walk reports NOT-PREPPED for hundreds of files that
were never plans.

Zero spawns; every case builds its own `tmp_path` tree.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.roadmap import prep_gate_cli as cli


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("placeholder\n", encoding="utf-8")
    return path


def test_is_plan_sidecar_flags_compound_stems():
    assert cli._is_plan_sidecar(Path("2026-06-24-baz.prior-art-check.md"))
    assert cli._is_plan_sidecar(Path("2026-06-27-foo.md.plan-coverage-check.md"))
    assert cli._is_plan_sidecar(Path("2026-07-01-bar.code-review-A-store.md"))
    assert not cli._is_plan_sidecar(Path("2026-06-27-foo.md"))


def test_directory_expansion_excludes_sidecars(tmp_path):
    plans = tmp_path / "docs" / "plans"
    _touch(plans / "2026-06-27-foo.md")
    _touch(plans / "2026-06-27-foo.md.prior-art-check.md")
    _touch(plans / "2026-06-27-foo.md.plan-coverage-check.md")

    targets = cli._targets(["docs/plans"], tmp_path)

    assert [p.name for p in targets] == ["2026-06-27-foo.md"]


def test_a_sidecar_named_explicitly_is_still_gated(tmp_path):
    """Only a DIRECTORY expansion prunes sidecars — a caller who types a
    compound-stem path on the command line still gets it gated, the same
    asymmetry DoE-claude's own script keeps."""
    plans = tmp_path / "docs" / "plans"
    sidecar = _touch(plans / "2026-06-27-foo.md.prior-art-check.md")

    targets = cli._targets(["docs/plans/2026-06-27-foo.md.prior-art-check.md"], tmp_path)

    assert targets == [sidecar]

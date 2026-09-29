"""
coordinator_core/roadmap/tests/test_prep_gate_cli.py — the door-served CLI half
of the mise-prep bar.

Subject: `coordinator_core.roadmap.prep_gate_cli`, restated to the letter from
DoE-claude `coordinator/bin/mise-prep-gate.py` where a shape has an engine
equivalent to be restated against: directory expansion that skips a
review/coverage sidecar (compound stem, `_is_plan_sidecar`), `--tally`,
`--json`, the `EXIT_*` route codes including `EXIT_ENGINE_ERROR`, and batch
resilience (one crashing target must not lose a sibling target's verdict in
the same invocation).

Zero spawns; every case builds its own `tmp_path` tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.roadmap import prep_gate_cli as cli
from coordinator_core.roadmap.tests.test_prep_gate import (
    _CLEAN_FM,
    _CLEAN_SPINE,
    _write_plan,
)


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


# ---------------------------------------------------------------------------
# --json / --tally / EXIT_* / batch resilience
# ---------------------------------------------------------------------------


def _prepped(root: Path, slug: str) -> Path:
    (root / "coordinator_core").mkdir(parents=True, exist_ok=True)
    return _write_plan(root, slug=slug, frontmatter=_CLEAN_FM, spine=_CLEAN_SPINE)


def _not_prepped(root: Path, slug: str) -> Path:
    return _write_plan(root, slug=slug, frontmatter="census: []\n")


def test_exit_status_distinguishes_prepped_from_not_prepped(tmp_path, capsys):
    _prepped(tmp_path, "2026-09-28-a.md")
    code = cli.main(["docs/plans", "--repo-root", str(tmp_path)])
    assert code == cli.EXIT_PREPPED

    _not_prepped(tmp_path, "2026-09-28-b.md")
    code = cli.main(["docs/plans", "--repo-root", str(tmp_path)])
    assert code == cli.EXIT_NOT_PREPPED
    capsys.readouterr()


def test_json_emits_reports_and_tally(tmp_path, capsys):
    _prepped(tmp_path, "2026-09-28-a.md")
    code = cli.main(["docs/plans", "--repo-root", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    payload = __import__("json").loads(out)
    assert code == cli.EXIT_PREPPED
    assert payload["reports"][0]["verdict"] == "PREPPED"
    assert payload["tally"]["verdicts"]["PREPPED"] == 1


def test_tally_reports_counts_and_share(tmp_path, capsys):
    _prepped(tmp_path, "2026-09-28-a.md")
    _not_prepped(tmp_path, "2026-09-28-b.md")
    cli.main(["docs/plans", "--repo-root", str(tmp_path), "--tally"])
    out = capsys.readouterr().out
    assert "PREPPED" in out
    assert "NOT-PREPPED" in out
    assert "TOTAL" in out


def test_one_crashing_plans_engine_error_does_not_lose_a_siblings_verdict(tmp_path, monkeypatch, capsys):
    """DoE parity (`test_one_crashing_plans_engine_error_does_not_lose_a_siblings_verdict`).

    A target whose `gate_plan` call raises something other than the
    plan-authoring defects `gate_plan` itself turns into a DEFECT must not
    sink the whole batch — every sibling target still gets its own verdict,
    and the crashing one gets ENGINE_ERROR instead of vanishing.
    """
    good = _prepped(tmp_path, "2026-09-28-a.md")
    bad = _write_plan(tmp_path, slug="2026-09-28-b.md", frontmatter=_CLEAN_FM, spine=_CLEAN_SPINE)

    real_gate_plan = cli.gate_plan

    def _flaky(root, target, *a, **kw):
        if target == bad:
            raise RuntimeError("boom — a version-skewed engine symbol")
        return real_gate_plan(root, target, *a, **kw)

    monkeypatch.setattr(cli, "gate_plan", _flaky)

    code = cli.main(["docs/plans", "--repo-root", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    payload = __import__("json").loads(out)

    verdicts = {r["path"]: r["verdict"] for r in payload["reports"]}
    assert verdicts[str(good)] == "PREPPED"
    assert verdicts[str(bad)] == cli.ENGINE_ERROR
    assert code == cli.EXIT_ENGINE_ERROR
    assert payload["tally"]["verdicts"][cli.ENGINE_ERROR] == 1


def test_exit_status_engine_error_outranks_not_prepped(tmp_path, monkeypatch, capsys):
    _not_prepped(tmp_path, "2026-09-28-a.md")
    bad = _write_plan(tmp_path, slug="2026-09-28-b.md", frontmatter=_CLEAN_FM, spine=_CLEAN_SPINE)

    def _raises(root, target, *a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "gate_plan", _raises)

    code = cli.main(["docs/plans", "--repo-root", str(tmp_path)])
    capsys.readouterr()
    assert code == cli.EXIT_ENGINE_ERROR

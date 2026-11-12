"""The terminal-commit landing gate counts a red caused unless a baseline leg measured it red pre-change.

Contract per docs/delivery/2026-11/warp-2026-11-10-commit-on-red-lists-reds-gates-publish-594963-20261110T205651-e71dc3.md
R1 and the claim-model ruling 88252271a8: ``targeted-tests-failed`` is a provenance kind
(``landing_refusal_kinds.PROVENANCE_KINDS``), so a caused red lists as an ``advisory`` entry with
``targeted_tests_check == "failed"`` and the commit proceeds; publish gates on it. An unmeasured id is
never credited pre-existing (docs/plans/2026-10-27-l79-landing-never-credits-red-from-head.md)."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import invariant_measured_paths as imp
from coordinator_core.ops.dispatch_emit import landing_integrity, landing_retest, landing_targeted_tests
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_landing_integrity import (  # noqa: F401
    _fire as _fire_base,
    _git,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RED = "pkg/tests/test_a.py::test_x"
INV = "pkg/tests/test_inv.py::test_inv"


def _reviewed(repo) -> dict:
    sidecar = repo.parent / "rev-sidecar.md"
    sidecar.write_text("review\n", encoding="utf-8")
    return {
        "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
        "wave_reviews": [{"verdict": "PASS", "sidecar_path": str(sidecar)}],
    }


def _fire(repo, **extra) -> dict:
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
    extra.setdefault("inline_review", _reviewed(repo))
    return _fire_base(repo, "ok.py", **extra)


def _carried(baseline: dict) -> dict:
    return {"status": "fail", "tests_failed": 1, "baseline_method": "export", "baseline": baseline}


def _listed_as_failed(out: dict, ident: str) -> None:
    """The red is advised by name under the failing reason and never credited pre-existing."""
    assert "refused" not in out
    assert out["advisory"][0]["reason"] == landing_targeted_tests.TARGETED_TESTS_FAILED
    assert ident in out["advisory"][0]["detail"]
    assert out["targeted_tests_check"] == "failed"


def test_carried_foreign_red_without_evidence_is_refused_naming_the_id(repo):
    """Ruling 594963 R1: the unevidenced red is listed (advisory, check "failed"), not refused at commit."""
    out = _fire(repo, targeted_tests=_carried(
        {"caused": [], "pre_existing": [], "foreign": [RED], "unverified": []}))

    assert out["committed"] is True, out
    _listed_as_failed(out, RED)


def test_self_written_evidence_without_a_measured_base_log_is_refused_naming_the_id(repo):
    """Ruling 594963 R1: self-written evidence still does not credit the red; it is listed as failed."""
    out = _fire(repo, targeted_tests=_carried(
        {"caused": [], "pre_existing": [], "foreign": [RED], "unverified": [], "evidence": [RED]}))

    assert out["committed"] is True, out
    _listed_as_failed(out, RED)


def _write_retest(repo, baseline: dict) -> dict:
    """A fresh isolated-export retest record for run ``r1`` that ran the invariant and read fail."""
    from coordinator_core.ops.dispatch_emit import landing_retest as lr

    record = {
        "run_id": "r1", "head": _git(["rev-parse", "HEAD"], repo), "status": "fail",
        "tests": [INV.split("::")[0]], "tests_run": 3, "tests_failed": 1,
        "baseline": baseline, "covered": lr.landing_fingerprint(repo, ["ok.py"]),
    }
    path = landing_retest.fresh_verdict_path(repo, "r1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return {"run_id": "r1"}


def _owe_invariant(monkeypatch):
    monkeypatch.setattr(imp, "invariants_for_paths", lambda paths, root: [INV])
    monkeypatch.setattr(imp, "existing_invariants", lambda root, nodes: list(nodes))


def test_retest_whose_only_red_is_head_red_is_admitted_as_foreign_with_evidence(repo, monkeypatch):
    _owe_invariant(monkeypatch)
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
    params = _write_retest(repo, {"caused": [], "foreign": [INV], "evidence": [INV]})

    # The reply does not surface the gate's extras; the gate itself carries the foreign evidence.
    findings, _check = landing_integrity.landing_test_gate(repo, params, "r1", ["ok.py"])
    assert list(findings) == []
    assert findings.foreign_evidence == {INV: [INV]}

    out = _fire(repo, **params)

    assert out["committed"] is True, out
    assert "refused" not in out


def test_retest_red_without_head_evidence_is_refused_naming_the_invariant(repo, monkeypatch):
    """Ruling 594963 R1: a retest red with no head evidence is listed as failed naming the invariant."""
    _owe_invariant(monkeypatch)
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
    params = _write_retest(repo, {"caused": [], "foreign": [INV], "evidence": []})

    out = _fire(repo, **params)

    assert out["committed"] is True, out
    _listed_as_failed(out, INV)

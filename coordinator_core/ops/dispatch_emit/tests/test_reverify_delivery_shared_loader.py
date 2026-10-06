"""Delivery living only in `prep.whole_diff_sidecars.delivery` is read identically by the
resolver and `prior_unbacked_claims` (the loader mint shares)."""

from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.ops.dispatch_emit import reverify_delivery as rd


def _write(path: Path, fm: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + yaml.safe_dump(fm) + "---\n", encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    _write(
        tmp_path / "d.md",
        {"verdict": "FAIL", "claims_unbacked": [{"claim": "c", "anchor": "a"}]},
    )
    return tmp_path


def test_load_delivery_falls_back_to_prep_sidecar(tmp_path):
    repo = _repo(tmp_path)
    record = {"prep": {"whole_diff_sidecars": {"delivery": "d.md"}}}
    assert rd.load_delivery(repo, record)["verdict"] == "FAIL"
    _write(repo / "prep.md", {"whole_diff_sidecars": {"delivery": "d.md"}})
    assert rd.load_delivery(repo, {"prep_sidecar": "prep.md"})["verdict"] == "FAIL"


def test_resolver_and_claims_see_sidecar_only_delivery(tmp_path):
    repo = _repo(tmp_path)
    run = repo / "run.md"
    _write(run, {"prep": {"whole_diff_sidecars": {"delivery": "d.md"}}, "commit_range": {}})
    path, block = rd.resolve_delivery_in_force(repo, None, None, str(run))
    assert path == run and block["verdict"] == "FAIL"
    _, claims = rd.prior_unbacked_claims(run, repo)
    assert claims == [{"claim": "c", "anchor": "a"}]

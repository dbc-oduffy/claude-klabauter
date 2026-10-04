"""Tests for `coordinator/bin/check-citation-integrity.py`'s evidentiary
figures -- `moved_count` and `anchor_missing_count` -- on tmp_path corpora.
Neither figure is gated, baselined, or able to move the exit code."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import coordinator_core.citation_graph as cg

_CLI_PATH = Path(__file__).resolve().parents[1] / "check-citation-integrity.py"
_spec = importlib.util.spec_from_file_location("check_citation_integrity", _CLI_PATH)
cci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cci)  # type: ignore[union-attr]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _baseline_for(wiki_root: Path, repo_root: Path) -> dict:
    report = cg.scan_corpus(wiki_root=wiki_root, repo_root=repo_root)
    return cci.build_baseline_payload(report, wiki_root, baseline_sha="0" * 40)


def test_moved_citation_kept_out_of_rot_ratchet_and_reported_as_its_own_count(tmp_path: Path):
    wiki_root = tmp_path / "coordinator" / "docs" / "wiki"
    _write(wiki_root / "new-location" / "renamed.md", "The page, now living here.\n")
    _write(wiki_root / "citer.md", "See `docs/wiki/old-location/renamed.md` for detail.\n")
    baseline = _baseline_for(wiki_root, tmp_path)
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    exit_code, summary, human = cci.run(
        wiki_root=wiki_root, repo_root=tmp_path, baseline_path=baseline_path
    )

    assert exit_code == 0
    assert summary["clean"] is True
    assert summary["counts"].get("moved", 0) == 1
    assert "moved" not in cci.VIOLATION_CLASSES
    assert "moved" not in baseline["partitions"]
    assert summary.get("moved_count") == 1
    assert "moved" in human.lower()


def test_anchor_missing_is_reported_but_never_gated(tmp_path: Path):
    wiki_root = tmp_path / "wiki"
    _write(wiki_root / "alpha.md", "See [beta](beta.md#no-such-heading).\n")
    _write(wiki_root / "beta.md", "# Real Heading\n\nSee [alpha](alpha.md).\n")
    baseline = _baseline_for(wiki_root, tmp_path)
    assert set(baseline["partitions"].keys()) == set(cci.ALL_CLASSES)
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    exit_code, summary, human = cci.run(
        wiki_root=wiki_root, repo_root=tmp_path, baseline_path=baseline_path
    )

    assert exit_code == 0
    assert summary["anchor_missing_count"] == 1
    assert "evidentiary, not gated" in human
    assert "#no-such-heading" in human
    assert "docs/decisions/ is not scanned" in human
    assert "anchor_missing" not in cci.ALL_CLASSES

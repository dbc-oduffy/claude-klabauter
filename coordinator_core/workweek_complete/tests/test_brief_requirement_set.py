"""The workweek-complete brief's consumer-read set: BriefInputs fields and the recommend_level contract."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Optional

from coordinator_core.workweek_complete.brief_inputs import BriefInputs
from coordinator_core.workweek_complete.release import version_rule


def test_brief_inputs_defaults_are_the_empty_values():
    empty = BriefInputs()
    assert empty.repo_root is None
    assert empty.doc_verify_findings_by_doc == {}
    assert empty.version_recommendation is None
    assert BriefInputs().doc_verify_findings_by_doc is not empty.doc_verify_findings_by_doc


def test_recommend_level_signature():
    sig = inspect.signature(version_rule.recommend_level)
    assert list(sig.parameters) == ["repo_root"]
    hints = inspect.get_annotations(version_rule.recommend_level, eval_str=True)
    assert hints["repo_root"] is Path
    assert hints["return"] == Optional[dict[str, str]]


def _entry(root: Path, name: str, nature: str) -> None:
    d = root / "archive" / "completed" / "2026-10"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f'---\ntitle: "{name}"\ncreated: 2026-10-12\nnature: {nature}\n'
        "status: pending-release\n---\n\nbody\n",
        encoding="utf-8",
    )


def test_recommend_level_returns_none_with_no_pending_entry(tmp_path):
    assert version_rule.recommend_level(tmp_path) is None


def test_recommend_level_dict_has_exactly_disposition_and_rationale(tmp_path):
    _entry(tmp_path, "a", "infra")
    result = version_rule.recommend_level(tmp_path)
    assert set(result) == {"disposition", "rationale"}
    assert result["disposition"] in {"patch", "minor"}
    assert isinstance(result["rationale"], str)


def test_recommend_level_roadmap_gives_minor(tmp_path):
    _entry(tmp_path, "a", "roadmap")
    assert version_rule.recommend_level(tmp_path)["disposition"] == "minor"

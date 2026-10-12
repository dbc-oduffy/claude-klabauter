"""Tests for the distill.write_curated_clusters op (curated file for invocation B)."""

import json

import pytest

import coordinator_core.ops  # noqa: F401 -- registers every op
from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.ops.distill_write_curated_clusters import _handler

_COUNTS = {"git-safety": 3, "git-hooks": 2, "meta": 1}


def _common_dir(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path / ".git"


def test_registered_and_classified_mutating() -> None:
    assert "distill.write_curated_clusters" in ipc._REGISTRY
    assert OP_CLASSIFICATION["distill.write_curated_clusters"] is OpClass.MUTATING


def test_writes_curated_tags_object_with_threshold_fields(tmp_path) -> None:
    reply = _handler(
        {"tag_counts": _COUNTS, "output_path": "state/scratch/artifact-distillation/r1/curated.json"},
        repo_root=_common_dir(tmp_path),
    )
    body = json.loads((tmp_path / reply["output_path"]).read_text(encoding="utf-8"))
    assert set(body) == {
        "verdicts", "counts", "threshold_applied", "threshold_auto", "nugget_drop_share",
        "contradictionClusters", "contradictionClusterCount", "degraded",
    }
    by_tag = {v["tag"]: v for v in body["verdicts"]}
    assert set(by_tag) == set(_COUNTS)
    assert by_tag["meta"]["verdict"] == "drop"
    assert body["counts"]["total"] == reply["curated_verdict_count"] == 3
    assert body["contradictionClusterCount"] == len(body["contradictionClusters"])


@pytest.mark.parametrize("bad", ["../escape.json", "state/other/x.json", "state/scratch", "/abs/x.json"])
def test_refuses_output_path_outside_scratch(tmp_path, bad) -> None:
    with pytest.raises(ValueError):
        _handler({"tag_counts": _COUNTS, "output_path": bad}, repo_root=_common_dir(tmp_path))


def test_refuses_degraded_curation_and_writes_nothing(tmp_path) -> None:
    with pytest.raises(ValueError):
        _handler({"tag_counts": {}, "output_path": "state/scratch/x.json"}, repo_root=_common_dir(tmp_path))
    assert not (tmp_path / "state" / "scratch" / "x.json").exists()

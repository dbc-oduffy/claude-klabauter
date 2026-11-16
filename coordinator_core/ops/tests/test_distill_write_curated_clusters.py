"""Tests for the distill.write_curated_clusters op (curated file for invocation B)."""

import json

import pytest

import coordinator_core.ops  # noqa: F401 -- registers every op
from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.ops.distill_write_curated_clusters import _handler
from pathlib import Path

_COUNTS = {"git-safety": 3, "git-hooks": 2, "meta": 1}


def _common_dir(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path / ".git"


def test_registered_and_classified_mutating() -> None:
    assert "distill.write_curated_clusters" in ipc._REGISTRY
    assert OP_CLASSIFICATION["distill.write_curated_clusters"] is OpClass.MUTATING


def test_writes_curated_tags_object_with_threshold_fields(tmp_path: Path) -> None:
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
    assert by_tag["meta"]["verdict"] == "keep"
    assert body["counts"]["total"] == reply["curated_verdict_count"] == 3
    assert body["contradictionClusterCount"] == len(body["contradictionClusters"])


@pytest.mark.parametrize("bad", ["../escape.json", "state/other/x.json", "state/scratch", "/abs/x.json"])
def test_refuses_output_path_outside_scratch(tmp_path: Path, bad) -> None:
    with pytest.raises(ValueError):
        _handler({"tag_counts": _COUNTS, "output_path": bad}, repo_root=_common_dir(tmp_path))


def test_run_id_alone_defaults_output_path(tmp_path: Path) -> None:
    reply = _handler({"tag_counts": _COUNTS, "run_id": "r9"}, repo_root=_common_dir(tmp_path))
    assert reply["output_path"] == "state/scratch/artifact-distillation/r9/curated.json"
    assert (tmp_path / reply["output_path"]).is_file()
    assert reply["curated_verdict_count"] == 3


def test_explicit_output_path_wins_over_run_id(tmp_path: Path) -> None:
    reply = _handler(
        {"tag_counts": _COUNTS, "run_id": "r9", "output_path": "state/scratch/x.json"},
        repo_root=_common_dir(tmp_path),
    )
    assert reply["output_path"] == "state/scratch/x.json"


@pytest.mark.parametrize("bad", [None, "", "../x", "a/b", "..", 5])
def test_refuses_missing_or_unsafe_run_id_without_output_path(tmp_path: Path, bad) -> None:
    params = {"tag_counts": _COUNTS}
    if bad is not None:
        params["run_id"] = bad
    with pytest.raises(ValueError, match=r"run_id.*distill\.scope"):
        _handler(params, repo_root=_common_dir(tmp_path))
    assert not (tmp_path / "state").exists()


def test_refuses_degraded_curation_and_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _handler({"tag_counts": {}, "output_path": "state/scratch/x.json"}, repo_root=_common_dir(tmp_path))
    assert not (tmp_path / "state" / "scratch" / "x.json").exists()


def _write_a(tmp_path, run_id, body) -> None:
    d = tmp_path / "state" / "scratch" / "artifact-distillation" / run_id
    d.mkdir(parents=True)
    (d / "invocation-a.json").write_text(json.dumps(body), encoding="utf-8")


def test_derives_tag_counts_and_threshold_from_run_dir_invocation_a(tmp_path: Path) -> None:
    _write_a(tmp_path, "r5", {"tag_counts": _COUNTS, "recommended_keep_threshold": 1})
    reply = _handler({"run_id": "r5"}, repo_root=_common_dir(tmp_path))
    assert reply["curated_verdict_count"] == 3
    assert reply["threshold_applied"] == 1 and reply["threshold_auto"] is False
    assert (tmp_path / reply["output_path"]).is_file()


def test_explicit_keep_threshold_beats_recommended(tmp_path: Path) -> None:
    _write_a(tmp_path, "r5", {"tag_counts": _COUNTS, "recommended_keep_threshold": 1})
    reply = _handler({"run_id": "r5", "keep_threshold": 3}, repo_root=_common_dir(tmp_path))
    assert reply["threshold_applied"] == 3


def test_derives_from_inline_invocation_a_result(tmp_path: Path) -> None:
    reply = _handler(
        {"run_id": "r6", "invocation_a": {"tag_counts": _COUNTS, "run_id": "r6"}},
        repo_root=_common_dir(tmp_path),
    )
    assert reply["curated_verdict_count"] == 3


def test_derives_from_invocation_a_file_and_confines_it(tmp_path: Path) -> None:
    _write_a(tmp_path, "r7", {"tag_counts": _COUNTS})
    rel = "state/scratch/artifact-distillation/r7/invocation-a.json"
    common = _common_dir(tmp_path)
    reply = _handler({"run_id": "r8", "invocation_a_file": rel}, repo_root=common)
    assert reply["curated_verdict_count"] == 3
    with pytest.raises(ValueError):
        _handler({"run_id": "r8", "invocation_a_file": "../x.json"}, repo_root=common)


def test_caller_tag_counts_win_over_invocation_a(tmp_path: Path) -> None:
    _write_a(tmp_path, "r5", {"tag_counts": {"only-one": 4}})
    reply = _handler({"run_id": "r5", "tag_counts": _COUNTS}, repo_root=_common_dir(tmp_path))
    assert reply["curated_verdict_count"] == 3


def test_run_id_without_invocation_a_still_refuses_and_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"tag_counts.*distill\.launch_args"):
        _handler({"run_id": "r5"}, repo_root=_common_dir(tmp_path))
    assert not (tmp_path / "state").exists()


def _installer_verdict(tmp_path):
    reply = _handler({"tag_counts": {"installer": 2}, "run_id": "w1"}, repo_root=_common_dir(tmp_path))
    body = json.loads((tmp_path / reply["output_path"]).read_text(encoding="utf-8"))
    return body["verdicts"][0]


def test_wiki_page_stem_keeps_bare_tag_at_default_arguments(tmp_path: Path) -> None:
    (tmp_path / "docs" / "wiki").mkdir(parents=True)
    (tmp_path / "docs" / "wiki" / "installation.md").write_text("x", encoding="utf-8")
    v = _installer_verdict(tmp_path)
    assert v["verdict"] == "keep" and v["drop_cause"] is None


def test_wiki_folder_name_keeps_bare_tag(tmp_path: Path) -> None:
    (tmp_path / "docs" / "wiki" / "installation").mkdir(parents=True)
    assert _installer_verdict(tmp_path)["verdict"] == "keep"


def test_unmatched_bare_tag_is_kept_without_wiki_home(tmp_path) -> None:
    (tmp_path / "docs" / "wiki").mkdir(parents=True)
    (tmp_path / "docs" / "wiki" / "testing.md").write_text("x", encoding="utf-8")
    v = _installer_verdict(tmp_path)
    assert (v["verdict"], v["drop_cause"]) == ("keep", None)
    assert "wiki home" not in v["reason"]


def test_nested_wiki_entries_are_not_walked(tmp_path: Path) -> None:
    (tmp_path / "docs" / "wiki" / "other").mkdir(parents=True)
    (tmp_path / "docs" / "wiki" / "other" / "installation.md").write_text("x", encoding="utf-8")
    v = _installer_verdict(tmp_path)
    assert v["verdict"] == "keep" and "wiki home" not in v["reason"]


def test_bare_tags_with_no_sibling_and_no_wiki_home_are_all_kept_at_defaults(tmp_path) -> None:
    counts = {"uncategorized": 3, "core": 2, "decisions": 1, "git-safety": 3, "git-hooks": 2}
    reply = _handler({"tag_counts": counts, "run_id": "w2"}, repo_root=_common_dir(tmp_path))
    body = json.loads((tmp_path / reply["output_path"]).read_text(encoding="utf-8"))
    by_tag = {v["tag"]: v for v in body["verdicts"]}
    for tag in ("uncategorized", "core", "decisions"):
        assert by_tag[tag]["verdict"] == "keep" and by_tag[tag]["drop_cause"] is None
    assert all(v["drop_cause"] != "bare-no-sibling" for v in body["verdicts"])
    assert "bare-no-sibling" not in body["counts"]["drop_by_cause"]

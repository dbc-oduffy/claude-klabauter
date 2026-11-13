"""Tests for the engine-built Workflow launch args: inlining, the 4096-array limit, spawn-free."""

from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from coordinator_core.distill import launch_args as la
from coordinator_core.ops.distill_workflow_input import validate_workflow_input


def _payload(repo_root: str, n_batches: int = 2, extra: dict | None = None) -> dict:
    batches = [
        {"batchId": f"batch-{i}", "files": [f"f{i}.md"], "description": "d", "formatHints": {}}
        for i in range(1, n_batches + 1)
    ]
    slugs = {"g1": "docs/wiki/g1.md", "g2": "docs/wiki/g2.md"}
    return {
        "runId": "run-1",
        "repoRoot": repo_root,
        "batches": batches,
        "wikiDirs": ["docs/wiki"],
        "wikiSlugs": slugs,
        "batch_count": n_batches,
        "total_file_count": n_batches,
        "wikiSlugCount": len(slugs),
        **(extra or {}),
    }


def _max_array(value) -> int:
    if isinstance(value, dict):
        return max((_max_array(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return max([len(value)] + [_max_array(v) for v in value])
    return 0


def test_build_inlines_batches_slugs_and_curated_and_writes_file_with_matching_sha(tmp_path):
    payload = _payload(str(tmp_path), extra={"curated": {"clusters": [{"id": "c1", "members": ["a", "b"]}]}})

    built = la.build(payload)

    path = tmp_path / "state/scratch/artifact-distillation/run-1/launch-args.json"
    assert built["path"] == str(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == built["sha256"]
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == built["args"]
    assert on_disk["batches"] == payload["batches"]
    assert on_disk["wikiSlugs"] == payload["wikiSlugs"]
    assert on_disk["curated"] == payload["curated"]
    assert on_disk["argsInlined"] is True
    assert validate_workflow_input(on_disk) == []


def test_build_run_dir_override_is_used_not_a_fixed_root(tmp_path):
    built = la.build(_payload(str(tmp_path)), run_dir=tmp_path / "elsewhere")
    assert built["path"] == str(tmp_path / "elsewhere" / la.ARGS_FILE_NAME)
    assert not (tmp_path / "state").exists()


def test_build_requires_run_id_and_repo_root(tmp_path):
    with pytest.raises(ValueError):
        la.build({"batches": []})


def test_long_lists_are_chunked_to_the_array_limit_and_round_trip(tmp_path):
    n = la.MAX_ARRAY_ENTRIES * 2 + 5
    payload = _payload(str(tmp_path), n_batches=1)
    payload["batches"][0]["files"] = [f"f{i}.md" for i in range(n)]
    payload["total_file_count"] = n

    built = la.build(payload)

    assert _max_array(built["args"]) <= la.MAX_ARRAY_ENTRIES
    assert la.CHUNK_KEY in built["args"]["batches"][0]["files"]
    restored = la.unchunk(json.loads((tmp_path / "state/scratch/artifact-distillation/run-1/launch-args.json").read_text()))
    assert restored["batches"][0]["files"] == payload["batches"][0]["files"]


def test_list_at_the_limit_is_not_chunked_and_one_over_is():
    at = list(range(la.MAX_ARRAY_ENTRIES))
    assert la.chunk_arrays(at) == at
    over = la.chunk_arrays(at + [0])
    assert set(over) == {la.CHUNK_KEY} and [len(c) for c in over[la.CHUNK_KEY]] == [la.MAX_ARRAY_ENTRIES, 1]


def test_unchunk_leaves_a_plain_dict_with_the_marker_key_among_others_alone():
    value = {la.CHUNK_KEY: [[1]], "other": 1}
    assert la.unchunk(value) == value


def test_build_spawns_no_process(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    la.build(_payload(str(tmp_path)))

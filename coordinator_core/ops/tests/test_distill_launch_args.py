"""Tests for distill.launch_args: inlined batches, slugs and curated verdicts, digest-verified file."""

from __future__ import annotations

import hashlib
import json
import subprocess

from pathlib import Path

import pytest

from coordinator_core.distill.manifest_schema import make_scope_manifest
from coordinator_core.ops import distill_launch_args as dla
from coordinator_core.ops.distill_workflow_input import validate_workflow_input

RUN_ID = "2026-11-14-10h00"
_WIKI = "docs" + "/wiki/"


def _manifest() -> dict:
    return make_scope_manifest(
        run_id=RUN_ID,
        batches=[["a.md", "b.md"], ["c.md"]],
        wiki_dirs=["docs/wiki"],
        wiki_slugs=[{"slug": "foo", "path": _WIKI + "foo.md"}],
        cohorts={"harvest": ["a.md", "b.md", "c.md"]},
    )


def _params(tmp_path, **extra) -> dict:
    return {"manifest": _manifest(), "repo_root": str(tmp_path), **extra}


CURATED = {
    "verdicts": [{"tag": "alpha", "verdict": "keep", "count": 9}, {"tag": "beta", "verdict": "drop", "count": 1}],
    "counts": {"total": 2},
    "threshold_applied": 3,
    "threshold_auto": True,
    "nugget_drop_share": 0.1,
    "contradictionClusters": [["alpha", "beta"]],
    "contradictionClusterCount": 1,
    "degraded": False,
}


def _write_curated(tmp_path, rel: str | None = None, data=CURATED) -> None:
    target = tmp_path / (rel or dla.curated_default_rel(RUN_ID))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(data if isinstance(data, str) else json.dumps(data, indent=2), encoding="utf-8")


def test_default_call_inlines_batches_slugs_and_curated_tags_and_digest_verifies(tmp_path: Path) -> None:
    _write_curated(tmp_path)

    out = dla._handler(_params(tmp_path))

    written = tmp_path / "state/scratch/artifact-distillation" / RUN_ID / "launch-args.json"
    assert out["path"] == str(written)
    assert hashlib.sha256(written.read_bytes()).hexdigest() == out["sha256"]
    args = out["args"]
    assert json.loads(written.read_text(encoding="utf-8")) == args
    assert [b["files"] for b in args["batches"]] == [["a.md", "b.md"], ["c.md"]]
    assert args["wikiSlugs"] == {"foo": _WIKI + "foo.md"}
    assert args["curatedTags"] == CURATED["verdicts"]
    for key in ("threshold_applied", "threshold_auto", "nugget_drop_share", "contradictionClusters", "contradictionClusterCount"):
        assert args[key] == CURATED[key]
    assert "curatedFile" not in args and "curatedFileSha256" not in args
    assert "counts" not in args and "degraded" not in args
    assert args["argsInlined"] is True
    assert validate_workflow_input(args) == []


def test_dead_curated_file_constants_are_gone() -> None:
    assert not hasattr(dla, "CURATED_FILE_KEY") and not hasattr(dla, "CURATED_SHA_KEY")


def test_curated_file_override_is_inlined(tmp_path: Path) -> None:
    rel = "state/scratch/elsewhere/curated.json"
    _write_curated(tmp_path, rel)
    args = dla._handler(_params(tmp_path, curated_file=rel))["args"]
    assert args["curatedTags"] == CURATED["verdicts"]


def test_absent_curated_file_adds_no_curated_keys_and_still_builds(tmp_path: Path) -> None:
    out = dla._handler(_params(tmp_path))
    assert "curatedTags" not in out["args"] and "threshold_applied" not in out["args"]
    assert hashlib.sha256(open(out["path"], "rb").read()).hexdigest() == out["sha256"]


@pytest.mark.parametrize("data", ["{not json", "[1, 2]", json.dumps({"counts": {}}), json.dumps({"verdicts": "x"})])
def test_malformed_curated_file_raises_naming_it(tmp_path: Path, data) -> None:
    _write_curated(tmp_path, data=data)
    with pytest.raises(ValueError, match=r"curated\.json.*distill\.write_curated_clusters"):
        dla._handler(_params(tmp_path))


def test_realistic_64kb_curated_payload_inlines_whole(tmp_path: Path) -> None:
    verdicts = [{"tag": f"tag-{i:05d}", "verdict": "keep", "count": i % 17 + 1} for i in range(1400)]
    data = {**CURATED, "verdicts": verdicts}
    _write_curated(tmp_path, data=data)
    out = dla._handler(_params(tmp_path))
    assert out["args"]["curatedTags"] == verdicts
    assert len((tmp_path / "state/scratch/artifact-distillation" / RUN_ID / "curated.json").read_bytes()) > 60_000
    assert out["args_bytes"] == len(Path(out["path"]).read_bytes()) > 60_000


PREPARED_REL = f"state/scratch/artifact-distillation/{RUN_ID}/prepared.json"
PROMOTED_REL = f"state/scratch/artifact-distillation/{RUN_ID}/promoted.json"
SPLICE_REL = f"state/scratch/artifact-distillation/{RUN_ID}/splice.json"


def _leg2_inputs(tmp_path, promoted=(_WIKI + "foo.md",), splice=None):
    for rel, data in ((PROMOTED_REL, list(promoted)), (SPLICE_REL, splice or {"dispositions": [], "spliced": []})):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data), encoding="utf-8")
    return {"leg": 2, "run_id": RUN_ID, "repo_root": str(tmp_path),
            "promoted_path": PROMOTED_REL, "splice_report_path": SPLICE_REL}


def test_leg2_reuses_leg1_args_and_adds_promoted_and_splice_report(tmp_path):
    _write_curated(tmp_path)
    leg1 = dla._handler(_params(tmp_path))
    leg1_bytes = Path(leg1["path"]).read_bytes()
    out = dla._handler(_leg2_inputs(tmp_path))
    assert out["path"].endswith(dla.LEG2_FILE_NAME)
    assert Path(leg1["path"]).read_bytes() == leg1_bytes
    assert hashlib.sha256(Path(out["path"]).read_bytes()).hexdigest() == out["sha256"]
    args = out["args"]
    assert args["promoted"] == [_WIKI + "foo.md"] and args["spliceReport"] == {"dispositions": [], "spliced": []}
    assert {k: v for k, v in args.items() if k not in ("promoted", "spliceReport")} == leg1["args"]


def test_leg2_without_leg1_args_raises_naming_it(tmp_path):
    with pytest.raises(ValueError, match="launch-args.json"):
        dla._handler(_leg2_inputs(tmp_path))


@pytest.mark.parametrize("drop", ["promoted_path", "splice_report_path", "run_id"])
def test_leg2_missing_param_raises_naming_it(tmp_path, drop):
    dla._handler(_params(tmp_path))
    params = _leg2_inputs(tmp_path)
    del params[drop]
    with pytest.raises(ValueError, match=drop):
        dla._handler(params)


def test_leg2_wrong_shapes_and_unknown_leg_are_refused(tmp_path):
    dla._handler(_params(tmp_path))
    with pytest.raises(ValueError, match="JSON list"):
        dla._handler({**_leg2_inputs(tmp_path), "promoted_path": SPLICE_REL})
    with pytest.raises(ValueError, match="JSON object"):
        dla._handler({**_leg2_inputs(tmp_path), "splice_report_path": PROMOTED_REL})
    with pytest.raises(ValueError, match="leg must be 2"):
        dla._handler({**_leg2_inputs(tmp_path), "leg": 3})
    with pytest.raises(ValueError, match="state/scratch"):
        dla._handler({**_leg2_inputs(tmp_path), "promoted_path": "docs/promoted.json"})


def test_prepared_path_inlines_prepared_json_as_args_prepared(tmp_path):
    prepared = {"synth": {"payloads": [{"topic": "t", "nuggets": ["n1"]}], "schema": {"type": "object"}}}
    target = tmp_path / PREPARED_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(prepared), encoding="utf-8")
    out = dla._handler(_params(tmp_path, prepared_path=PREPARED_REL))
    assert out["args"][dla.PREPARED_KEY] == prepared
    assert json.loads(Path(out["path"]).read_text(encoding="utf-8"))["prepared"] == prepared


def _write_prepared(tmp_path, prepared):
    target = tmp_path / PREPARED_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(prepared), encoding="utf-8")


def test_prepared_with_packed_slugs_drops_top_level_wikislugs(tmp_path):
    prepared = {"wikiSlugsPacked": "packed-index", "synth": {}}
    _write_prepared(tmp_path, prepared)
    out = dla._handler(_params(tmp_path, prepared_path=PREPARED_REL))
    assert "wikiSlugs" not in out["args"]
    assert out["args"][dla.PREPARED_KEY]["wikiSlugsPacked"] == "packed-index"
    assert "wikiSlugs" not in json.loads(Path(out["path"]).read_text(encoding="utf-8"))


def test_prepared_without_packed_slugs_keeps_top_level_wikislugs(tmp_path):
    _write_prepared(tmp_path, {"synth": {}})
    out = dla._handler(_params(tmp_path, prepared_path=PREPARED_REL))
    assert out["args"]["wikiSlugs"] == {"foo": _WIKI + "foo.md"}


def test_no_prepared_path_keeps_top_level_wikislugs(tmp_path):
    assert dla._handler(_params(tmp_path))["args"]["wikiSlugs"] == {"foo": _WIKI + "foo.md"}


def test_no_prepared_path_adds_no_prepared_key(tmp_path):
    assert dla.PREPARED_KEY not in dla._handler(_params(tmp_path))["args"]


@pytest.mark.parametrize("data", [None, "{not json", "[1, 2]"])
def test_missing_or_malformed_prepared_file_raises_naming_it(tmp_path, data):
    if data is not None:
        target = tmp_path / PREPARED_REL
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data, encoding="utf-8")
    with pytest.raises(ValueError, match=r"prepared\.json"):
        dla._handler(_params(tmp_path, prepared_path=PREPARED_REL))


@pytest.mark.parametrize("rel", ["../prepared.json", "docs/prepared.json", "", 7])
def test_prepared_path_outside_scratch_or_not_a_string_is_refused(tmp_path, rel):
    with pytest.raises(ValueError, match="prepared_path"):
        dla._handler(_params(tmp_path, prepared_path=rel))


@pytest.mark.parametrize("rel", ["../outside.json", "docs/curated.json", "state/scratch", "/abs/elsewhere.json"])
def test_curated_file_outside_scratch_is_refused(tmp_path: Path, rel) -> None:
    with pytest.raises(ValueError, match="state/scratch"):
        dla._handler(_params(tmp_path, curated_file=rel))


def test_missing_manifest_and_repo_root_raise(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"distill\.launch_args requires a 'manifest'.*distill\.scope") as ei:
        dla._handler({"repo_root": str(tmp_path)})
    assert "distill.workflow_input" not in str(ei.value)
    with pytest.raises(ValueError):
        dla._handler({"manifest": _manifest()})


def _scope_result(tmp_path) -> dict:
    rel = f"state/scratch/artifact-distillation/{RUN_ID}/input.json"
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_manifest()), encoding="utf-8")
    return {"run_id": RUN_ID, "input_json_path": rel, "counts": {}}


def test_scope_result_as_manifest_loads_input_json(tmp_path: Path) -> None:
    out = dla._handler({"manifest": _scope_result(tmp_path), "repo_root": str(tmp_path)})

    assert out["args"]["wikiSlugs"] == {"foo": _WIKI + "foo.md"}
    assert validate_workflow_input(out["args"]) == []


def test_run_id_alone_loads_that_runs_input_json(tmp_path: Path) -> None:
    _scope_result(tmp_path)
    out = dla._handler({"run_id": RUN_ID, "repo_root": str(tmp_path)})

    assert out["args"]["wikiSlugs"] == {"foo": _WIKI + "foo.md"}


@pytest.mark.parametrize("bad", [{"run_id": RUN_ID}, {"run_id": RUN_ID, "batches": []}, {}])
def test_unknown_manifest_shape_raises_valueerror_naming_wanted_keys(tmp_path: Path, bad) -> None:
    with pytest.raises(ValueError, match="wiki_slugs"):
        dla._handler({"manifest": bad, "repo_root": str(tmp_path)})


def test_scope_result_pointing_outside_scratch_or_missing_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="state/scratch"):
        dla._handler({"manifest": {"input_json_path": "../x.json"}, "repo_root": str(tmp_path)})
    with pytest.raises(ValueError, match="unreadable"):
        dla._handler(
            {"manifest": {"input_json_path": "state/scratch/nope/input.json"}, "repo_root": str(tmp_path)}
        )


def test_spawns_no_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    dla._handler(_params(tmp_path))

"""Tests for distill.launch_args: inlined batches and slugs, curatedFile sha256, digest-verified file."""

from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from coordinator_core.distill.manifest_schema import make_scope_manifest
from coordinator_core.ops import distill_launch_args as dla
from coordinator_core.ops.distill_workflow_input import validate_workflow_input

RUN_ID = "2026-11-14-10h00"


def _manifest() -> dict:
    return make_scope_manifest(
        run_id=RUN_ID,
        batches=[["a.md", "b.md"], ["c.md"]],
        wiki_dirs=["docs/wiki"],
        wiki_slugs=[{"slug": "foo", "path": "docs/wiki/foo.md"}],
        cohorts={"harvest": ["a.md", "b.md", "c.md"]},
    )


def _params(tmp_path, **extra) -> dict:
    return {"manifest": _manifest(), "repo_root": str(tmp_path), **extra}


def _write_curated(tmp_path, rel: str | None = None) -> bytes:
    target = tmp_path / (rel or dla.curated_default_rel(RUN_ID))
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"verdicts": [], "counts": {"total": 0}}, indent=2).encode("utf-8")
    target.write_bytes(body)
    return body


def test_default_call_inlines_batches_slugs_and_curated_sha_and_digest_verifies(tmp_path):
    body = _write_curated(tmp_path)

    out = dla._handler(_params(tmp_path))

    written = tmp_path / "state/scratch/artifact-distillation" / RUN_ID / "launch-args.json"
    assert out["path"] == str(written)
    assert hashlib.sha256(written.read_bytes()).hexdigest() == out["sha256"]
    args = out["args"]
    assert json.loads(written.read_text(encoding="utf-8")) == args
    assert [b["files"] for b in args["batches"]] == [["a.md", "b.md"], ["c.md"]]
    assert args["wikiSlugs"] == {"foo": "docs/wiki/foo.md"}
    assert args[dla.CURATED_FILE_KEY] == dla.curated_default_rel(RUN_ID)
    assert args[dla.CURATED_SHA_KEY] == hashlib.sha256(body).hexdigest()
    assert args["argsInlined"] is True
    assert validate_workflow_input(args) == []


def test_curated_file_override_is_hashed(tmp_path):
    rel = "state/scratch/elsewhere/curated.json"
    body = _write_curated(tmp_path, rel)
    args = dla._handler(_params(tmp_path, curated_file=rel))["args"]
    assert args[dla.CURATED_FILE_KEY] == rel
    assert args[dla.CURATED_SHA_KEY] == hashlib.sha256(body).hexdigest()


def test_absent_curated_file_adds_neither_key_and_still_builds(tmp_path):
    out = dla._handler(_params(tmp_path))
    assert dla.CURATED_FILE_KEY not in out["args"] and dla.CURATED_SHA_KEY not in out["args"]
    assert hashlib.sha256(open(out["path"], "rb").read()).hexdigest() == out["sha256"]


@pytest.mark.parametrize("rel", ["../outside.json", "docs/curated.json", "state/scratch", "/abs/elsewhere.json"])
def test_curated_file_outside_scratch_is_refused(tmp_path, rel):
    with pytest.raises(ValueError, match="state/scratch"):
        dla._handler(_params(tmp_path, curated_file=rel))


def test_missing_manifest_and_repo_root_raise(tmp_path):
    with pytest.raises(ValueError):
        dla._handler({"repo_root": str(tmp_path)})
    with pytest.raises(ValueError):
        dla._handler({"manifest": _manifest()})


def test_spawns_no_process(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    dla._handler(_params(tmp_path))

"""End-to-end pin: distill.launch_args, called through its registered handler at default arguments, replies by scriptPath for both legs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.distill.manifest_schema import make_scope_manifest
from coordinator_core.ops import distill_launch_args  # noqa: F401  (registers the op)

RUN_ID = "2026-11-16-10h00"
RUN_REL = f"state/scratch/artifact-distillation/{RUN_ID}"
_TAIL = "return await workflow({scriptPath: "


def _handler():
    return ipc._REGISTRY["distill.launch_args"]


def _write(root: Path, rel: str, data) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def params(tmp_path: Path) -> dict:
    harvest = tmp_path / "harvest.workflow.js"
    harvest.write_text("// harvest", encoding="utf-8")
    manifest = make_scope_manifest(
        run_id=RUN_ID,
        batches=[["a.md"]],
        wiki_dirs=["docs/wiki"],
        wiki_slugs=[{"slug": "foo", "path": "docs/wiki/foo.md"}],
        cohorts={"harvest": ["a.md"]},
    )
    _write(tmp_path, f"{RUN_REL}/input.json", manifest)
    _write(tmp_path, f"{RUN_REL}/prepared.json", {"clusters": [{"content": "nugget text " * 40000}]})
    return {
        "repo_root": str(tmp_path),
        "run_id": RUN_ID,
        "prepared_path": f"{RUN_REL}/prepared.json",
        "workflow_script": str(harvest),
    }


def _assert_scriptpath_reply(out: dict, launcher_name: str, run_dir: Path) -> None:
    assert "args" not in out
    launcher = run_dir / launcher_name
    assert out["scriptPath"] == launcher.resolve().as_posix()
    source = launcher.read_text(encoding="utf-8")
    last = source.rstrip("\n").splitlines()[-1]
    assert last.startswith(_TAIL) and last.endswith("}, ARGS);")
    assert "args:" not in last
    assert Path(out["scriptPath"] + ".emitted.json").is_file()
    assert out["launcher_bytes"] == len(launcher.read_bytes())


def test_leg1_default_call_replies_by_scriptpath_with_launcher_and_receipt(tmp_path: Path, params: dict) -> None:
    out = _handler()(params, None)

    _assert_scriptpath_reply(out, "launch.workflow.mjs", tmp_path / RUN_REL)
    assert "nugget text" not in json.dumps(out)
    assert Path(out["args_path"]).stat().st_size > 400_000


def test_leg2_default_call_replies_by_scriptpath_with_launcher_and_receipt(tmp_path: Path, params: dict) -> None:
    _handler()(params, None)
    _write(tmp_path, f"{RUN_REL}/promoted.json", ["docs/wiki/foo.md"])
    _write(tmp_path, f"{RUN_REL}/splice.json", {"dispositions": [], "spliced": []})

    out = _handler()(
        {
            "leg": 2,
            "repo_root": params["repo_root"],
            "run_id": RUN_ID,
            "workflow_script": params["workflow_script"],
            "promoted_path": f"{RUN_REL}/promoted.json",
            "splice_report_path": f"{RUN_REL}/splice.json",
        },
        None,
    )

    _assert_scriptpath_reply(out, "launch-leg2.workflow.mjs", tmp_path / RUN_REL)


def test_missing_workflow_script_is_refused(params: dict) -> None:
    with pytest.raises(ValueError, match="workflow_script"):
        _handler()({k: v for k, v in params.items() if k != "workflow_script"}, None)

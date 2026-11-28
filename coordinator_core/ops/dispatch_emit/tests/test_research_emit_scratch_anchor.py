"""A research emit anchors every worker-visible scratch path to the run repo, whatever the caller's cwd."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import _run, repo  # noqa: F401


def _anchor(repo: Path, reply: dict) -> str:
    return (repo / reply["scratch_dir"]).as_posix()


def test_emitted_script_names_scratch_under_the_run_repo_from_a_foreign_cwd(repo, capsys, monkeypatch, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    monkeypatch.chdir(elsewhere)
    code, reply = _run(capsys, ["--repo-root", str(repo), "--research", "--ask", "What is X?", "--list", "questions=What is X?"])
    assert code == cli_module.EXIT_OK, reply
    scratch = reply["scratch_dir"]
    assert Path(scratch).is_absolute() and scratch.startswith(repo.as_posix())
    assert "\\" not in scratch
    script = Path(reply["path"]).read_text(encoding="utf-8")
    assert f"{scratch}/ask.md" in script
    relative = f"{op_module.RUN_DIR_ROOT}/{reply['run_id']}"
    bare = [script[m.start() - 80:m.end() + 40] for m in re.finditer(rf"(?<![/\w-]){re.escape(relative)}/", script)]
    assert not bare, bare
    assert (Path(scratch) / "ask.md").is_file()
    assert (Path(scratch) / "mail").is_dir()
    assert not (elsewhere / op_module.RUN_DIR_ROOT).exists()
    assert reply["next_action"]["params"]["scratch_dir"] == scratch


def test_the_ask_file_states_absolute_output_paths(repo, capsys):
    code, reply = _run(capsys, ["--research", "--ask", "What is X?", "--list", "questions=What is X?"])
    assert code == cli_module.EXIT_OK, reply
    scratch = _anchor(repo, reply)
    text = (Path(scratch) / "ask.md").read_text(encoding="utf-8")
    assert f"`{scratch}/synthesis.md`" in text


def test_route_setup_segments_carry_the_anchored_scratch_and_brief(repo, monkeypatch, tmp_path_factory):
    monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))
    ctx, pipeline_ctx, _ = op_module._research_route_setup(
        {"scratch_dir": "scratch/warp/anchor-run"}, repo, None, "What is X?"
    )
    want = (repo / "scratch/warp/anchor-run").as_posix()
    assert ctx["segments"]
    for _, inputs, _ in ctx["segments"]:
        assert inputs.scratch_dir == want
        assert inputs.brief.startswith(want + "/")
    assert pipeline_ctx["inputs"].scratch_dir == want


def test_a_relative_scratch_still_refuses_an_escape(repo):
    with pytest.raises(Exception, match="escapes repo root"):
        op_module._pipeline_scratch_rel(repo, "../outside", "run-1")


def test_the_receipt_and_reply_never_carry_a_backslash(repo, capsys):
    code, reply = _run(capsys, ["--research", "--ask", "What is X?", "--list", "questions=What is X?"])
    assert code == cli_module.EXIT_OK, reply
    assert "\\" not in json.dumps({k: reply[k] for k in ("scratch_dir", "brief", "run_output_root") if k in reply})

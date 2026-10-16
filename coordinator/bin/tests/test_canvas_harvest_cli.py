"""canvas-harvest CLI: output shape, refusal line, exit codes, handler params, inert module body."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
CLI = REPO / "coordinator" / "bin" / "canvas-harvest.py"


@pytest.fixture(scope="module")
def cli():
    sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location("canvas_harvest_cli", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake(result, seen):
    def op(params, repo_root=None):
        seen.append((params, repo_root))
        return result

    return op


ARGS = ["harvest", "--url", "https://x/y", "--out", "rec"]


def test_success_prints_record_then_deleted_lines(cli, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = []
    result = {
        "ok": True, "reason": "", "detail": "",
        "wrote": ["rec/frames/a.png", "rec/harvest.json"],
        "deleted": ["rec/frames/old.png", "rec/threads/gone.json"],
    }
    assert cli.main(ARGS, op=_fake(result, seen)) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        "rec/harvest.json", "deleted rec/frames/old.png", "deleted rec/threads/gone.json",
    ]
    assert err == ""
    params, root = seen[0]
    assert params == {"mode": "harvest", "url": "https://x/y", "out": "rec"}
    assert Path(root) == tmp_path


def test_optional_params_forwarded(cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = []
    ok = {"ok": True, "wrote": ["rec/harvest.json"], "deleted": []}
    argv = ["probe", "--url", "u", "--out", "o", "--session-id", "S", "--transcript", "t.jsonl"]
    assert cli.main(argv, op=_fake(ok, seen)) == 0
    assert seen[0][0] == {
        "mode": "probe", "url": "u", "out": "o", "session_id": "S", "transcript": "t.jsonl",
    }


def test_refusal_is_one_stderr_line_and_empty_stdout(cli, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = {"ok": False, "reason": "canvas-unreadable", "detail": "line one\n  line two\n"}
    assert cli.main(ARGS, op=_fake(result, [])) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "canvas-harvest: canvas-unreadable: line one line two\n"


def test_async_handler_is_awaited(cli, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def op(params, repo_root=None):
        return {"ok": True, "wrote": ["rec/harvest.json"], "deleted": []}

    assert cli.main(ARGS, op=op) == 0
    assert capsys.readouterr().out == "rec/harvest.json\n"


@pytest.mark.parametrize("argv", [[], ["harvest"], ["bogus", "--url", "u", "--out", "o"],
                                  ["harvest", "--url", "u"], ["probe", "--url", "u"]])
def test_usage_exits_2(cli, argv):
    assert cli.main(argv, op=_fake({}, [])) == 2


AID = "LvATpzxR9vvEBxJsy84rjx"
CANVAS = f"https://claude.ai/artifact/{AID}"


def test_reader_dispatch_prints_the_dispatch_json(cli, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = []
    dispatch = {"subagent_type": "general-purpose", "description": f"canvas-reader {AID}"}
    result = {"ok": True, "wrote": [], "deleted": [], "dispatch": dispatch}
    assert cli.main(["reader-dispatch", "--url", CANVAS], op=_fake(result, seen)) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == dispatch and err == ""
    assert seen[0][0] == {"mode": "reader-dispatch", "url": CANVAS}


def test_reader_flag_is_forwarded(cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = []
    ok = {"ok": True, "wrote": ["rec/harvest.json"], "deleted": []}
    assert cli.main(ARGS + ["--reader", "--transcript", "t"], op=_fake(ok, seen)) == 0
    assert seen[0][0]["reader"] is True


def _reader_fixture(tmp_path, meta_description):
    sub = tmp_path / "subagents"
    sub.mkdir()
    frames = sub / "frames" / "project"
    frames.mkdir(parents=True)
    body = b"<html>x</html>"
    (frames / "index.html").write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    ts = "2026-10-16T10:00:0%dZ"
    lines = [
        {"type": "assistant", "isSidechain": True, "agentId": "aX", "sessionId": "s", "timestamp": ts % 1,
         "message": {"content": [{"type": "tool_use", "id": "r1", "name": "Artifact",
                                  "input": {"action": "read", "url": CANVAS, "paths": ["index.html"]}}]}},
        {"type": "user", "isSidechain": True, "agentId": "aX", "sessionId": "s", "timestamp": ts % 2,
         "message": {"content": [{"type": "tool_result", "tool_use_id": "r1", "content": "rendered"}]},
         "toolUseResult": {"files_read": {"url": CANVAS, "title": "Doc", "ver": "v1",
                                          "files": [{"path": "project/index.html",
                                                     "saved_to": "frames/project/index.html", "sha256": sha}],
                                          "type": {"url": "", "title": "plain"}}}},
    ]
    transcript = sub / "agent-aX.jsonl"
    transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    if meta_description is not None:
        (sub / "agent-aX.meta.json").write_text(json.dumps({"description": meta_description}), encoding="utf-8")
    return transcript


def _real_op():
    from coordinator_core.ops import canvas_harvest

    return canvas_harvest._handler


def test_harvest_reader_writes_the_record(cli, capsys, tmp_path, monkeypatch):
    transcript = _reader_fixture(tmp_path, f"canvas-reader {AID}")
    out = tmp_path / "rec"
    monkeypatch.chdir(tmp_path)
    argv = ["harvest", "--url", CANVAS, "--out", str(out), "--reader", "--transcript", str(transcript),
            "--session-id", "s"]
    assert cli.main(argv, op=_real_op()) == 0
    assert capsys.readouterr().out.splitlines()[0] == str(out / "harvest.json")
    assert (out / "harvest.json").is_file()


def test_harvest_reader_with_mismatched_meta_is_refused(cli, capsys, tmp_path, monkeypatch):
    transcript = _reader_fixture(tmp_path, "canvas-reader OtherArtifact")
    monkeypatch.chdir(tmp_path)
    argv = ["harvest", "--url", CANVAS, "--out", str(tmp_path / "rec"), "--reader",
            "--transcript", str(transcript), "--session-id", "s"]
    assert cli.main(argv, op=_real_op()) == 1
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("canvas-harvest: reader-not-named:")


def test_engine_unresolvable_exits_3(cli, capsys, monkeypatch):
    def boom():
        raise RuntimeError("no engine\nhere")

    monkeypatch.setattr(cli, "_load_handler", boom)
    assert cli.main(ARGS) == 3
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "canvas-harvest: engine unavailable: no engine here\n"


def test_module_body_is_inert():
    src = CLI.read_text(encoding="utf-8")
    head = src.split("def ", 1)[0]
    assert "coordinator_core" not in head.replace("coordinator_core.ops", "").split('"""')[2]
    assert "os.getcwd" not in head and "Path.cwd" not in head

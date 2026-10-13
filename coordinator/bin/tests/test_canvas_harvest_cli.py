"""canvas-harvest CLI: output shape, refusal line, exit codes, handler params, inert module body."""

from __future__ import annotations

import importlib.util
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
                                  ["harvest", "--url", "u"]])
def test_usage_exits_2(cli, argv):
    assert cli.main(argv, op=_fake({}, [])) == 2


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

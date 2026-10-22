"""doe_pipeline_forward: in-process forwarding of a bare name to a DoE pipelines script."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

_LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import coordinator_data_root  # noqa: E402
import doe_pipeline_forward  # noqa: E402

RELPATH = "deep-research/notebooklm/stub.py"


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = tmp_path / "pipelines"
    (root / "deep-research" / "notebooklm").mkdir(parents=True)
    monkeypatch.setattr(coordinator_data_root, "data_root", lambda _n: root)
    monkeypatch.setattr(sys, "path", list(sys.path))
    return root


def _write_stub(root, code):
    stub = root / RELPATH
    stub.write_text(code, encoding="utf-8")
    return stub


def test_runs_stub_with_argv_and_propagates_exit_code(tree, monkeypatch):
    stub = _write_stub(
        tree,
        "import sys\nprint('ARGV', sys.argv[0], *sys.argv[1:])\nsys.exit(7)\n",
    )
    monkeypatch.setattr(sys, "argv", ["nlm-x", "a", "b"])
    with pytest.raises(SystemExit) as exc:
        doe_pipeline_forward.forward(RELPATH, "nlm-x")
    assert exc.value.code == 7
    assert sys.argv[0] == str(stub.resolve())


def test_stub_stdout_reaches_caller(tree, monkeypatch, capsys):
    _write_stub(tree, "print('{\"ok\": true}')\n")
    monkeypatch.setattr(sys, "argv", ["nlm-x"])
    with pytest.raises(SystemExit) as exc:
        doe_pipeline_forward.forward(RELPATH, "nlm-x")
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == '{"ok": true}'


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_answers_locally_without_resolving(flag, monkeypatch, capsys):
    def boom(_n):
        raise AssertionError("resolved the root on --help")

    monkeypatch.setattr(coordinator_data_root, "data_root", boom)
    monkeypatch.setattr(sys, "argv", ["nlm-x", flag])
    with pytest.raises(SystemExit) as exc:
        doe_pipeline_forward.forward(RELPATH, "nlm-x")
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "nlm-x" in out and RELPATH in out


def test_unresolvable_root_exits_127_one_line(monkeypatch, capsys):
    def fail(_n):
        raise RuntimeError("no root")

    monkeypatch.setattr(coordinator_data_root, "data_root", fail)
    monkeypatch.setattr(sys, "argv", ["nlm-x"])
    with pytest.raises(SystemExit) as exc:
        doe_pipeline_forward.forward(RELPATH, "nlm-x")
    assert exc.value.code == 127
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "Traceback" not in err


def test_missing_target_exits_127(tree, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["nlm-x"])
    with pytest.raises(SystemExit) as exc:
        doe_pipeline_forward.forward(RELPATH, "nlm-x")
    assert exc.value.code == 127
    err = capsys.readouterr().err
    assert "stub.py" in err and len(err.strip().splitlines()) == 1


def test_relpath_escaping_root_exits_127(tree, monkeypatch, capsys):
    outside = tree.parent / "outside.py"
    outside.write_text("raise SystemExit(0)\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["nlm-x"])
    with pytest.raises(SystemExit) as exc:
        doe_pipeline_forward.forward("../outside.py", "nlm-x")
    assert exc.value.code == 127
    assert "escapes" in capsys.readouterr().err


def test_forward_spawns_no_subprocess(tree, monkeypatch):
    _write_stub(tree, "raise SystemExit(3)\n")

    def no_spawn(*_a, **_k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    monkeypatch.setattr(sys, "argv", ["nlm-x"])
    with pytest.raises(SystemExit) as exc:
        doe_pipeline_forward.forward(RELPATH, "nlm-x")
    assert exc.value.code == 3

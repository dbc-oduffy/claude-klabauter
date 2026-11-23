"""Tests for coordinator/bin/runaway-pattern-scan.py; patterns here are test fixtures only."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
SCRIPT = BIN / "runaway-pattern-scan.py"
_spec = importlib.util.spec_from_file_location("runaway_pattern_scan", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _run(*args, stdin=""):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_help_exits_zero_and_documents_cli():
    r = _run("--help")
    assert r.returncode == 0
    assert "--pattern" in r.stdout and "--pattern-file" in r.stdout and "--json" in r.stdout


def test_hit_exits_one_and_reports_location(tmp_path):
    f = _write(tmp_path, "a.txt", "fine\nwhile true; do spawn; done\n")
    r = _run("--pattern", r"while true", f)
    assert r.returncode == 1
    assert f"{f}:2: [while true]" in r.stdout
    assert "1 hit(s) in 1 source(s)" in r.stdout


def test_clean_exits_zero(tmp_path):
    f = _write(tmp_path, "a.txt", "nothing here\n")
    r = _run("--pattern", "while true", f)
    assert r.returncode == 0
    assert "clean: 1 source(s)" in r.stdout


def test_stdin_default_and_dash():
    assert _run("--pattern", "boom", stdin="a\nboom\n").returncode == 1
    assert _run("--pattern", "boom", "-", stdin="a\n").returncode == 0


def test_json_output_shape(tmp_path):
    f = _write(tmp_path, "a.txt", "x\nboom\n")
    r = _run("--json", "--pattern", "boom", f)
    assert r.returncode == 1
    obj = json.loads(r.stdout)
    assert obj["hit_count"] == 1 and obj["sources"] == 1
    assert obj["hits"] == [{"source": f, "line": 2, "pattern": "boom", "text": "boom"}]


def test_pattern_file_skips_comments_and_blanks(tmp_path):
    pf = _write(tmp_path, "p.txt", "# note\n\nboom\n")
    f = _write(tmp_path, "a.txt", "# note\nboom\n")
    r = _run("--json", "--pattern-file", pf, f)
    assert json.loads(r.stdout)["hit_count"] == 1


def test_bad_regex_exits_two(tmp_path):
    f = _write(tmp_path, "a.txt", "x\n")
    r = _run("--pattern", "(unclosed", f)
    assert r.returncode == 2
    assert "bad pattern" in r.stderr


def test_no_pattern_exits_two(tmp_path):
    r = _run(_write(tmp_path, "a.txt", "x\n"))
    assert r.returncode == 2
    assert "no pattern" in r.stderr


def test_missing_input_and_pattern_file_exit_two(tmp_path):
    assert _run("--pattern", "x", str(tmp_path / "absent.txt")).returncode == 2
    assert _run("--pattern-file", str(tmp_path / "absent.txt")).returncode == 2


def test_main_in_process_stdin(capsys):
    rc = mod.main(["--pattern", "boom"], stdin=io.StringIO("boom\n"))
    assert rc == 1
    assert "<stdin>:1:" in capsys.readouterr().out


def test_long_line_text_is_trimmed():
    hits = mod.scan_stream("s", ["boom" + "x" * 500], [("boom", mod.re.compile("boom"))])
    assert len(hits[0]["text"]) == 200

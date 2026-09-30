"""--decisions-file wiring at roadmap-planning-assemble's argv loop: each
outcome the helper can return maps to this site's exit code and stderr idiom."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys

_ENGINE_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)

import coordinator_core.roadmap_planning_assemble as rpa  # noqa: E402

_BASE = ["--run-id", "r1", "--input-corpus", "docs/corpus"]


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = rpa.main(argv)
    return code, out.getvalue(), err.getvalue()


def test_decisions_file_is_read_and_echoed(tmp_path):
    payload = {"k": {"disposition": "skip"}}
    path = tmp_path / "d.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    code, out, _ = _run([*_BASE, "--decisions-file", str(path)])
    assert code == rpa.EXIT_OK
    assert json.loads(out)["decisions"] == payload


def test_inline_and_file_are_mutually_exclusive(tmp_path):
    path = tmp_path / "d.json"
    path.write_text("{}", encoding="utf-8")
    code, out, err = _run([*_BASE, "--decisions", "{}", "--decisions-file", str(path)])
    assert code == rpa.EXIT_USAGE
    assert out == ""
    assert "roadmap-planning-assemble: --decisions and --decisions-file are mutually exclusive" in err


def test_unreadable_decisions_file_is_usage_error(tmp_path):
    code, _, err = _run([*_BASE, "--decisions-file", str(tmp_path / "absent.json")])
    assert code == rpa.EXIT_USAGE
    assert "--decisions-file unreadable" in err


def test_malformed_decisions_file_is_usage_error(tmp_path):
    path = tmp_path / "d.json"
    path.write_text("{not-json", encoding="utf-8")
    code, _, err = _run([*_BASE, "--decisions-file", str(path)])
    assert code == rpa.EXIT_USAGE
    assert "malformed --decisions JSON (from " in err


def test_decisions_file_without_value_is_usage_error():
    code, _, err = _run([*_BASE, "--decisions-file"])
    assert code == rpa.EXIT_USAGE
    assert "--decisions-file requires a value" in err

"""
Tests for coordinator_core.ops.query_completions.

Spec backlink: docs/plans/2026-05-19-completion-log-phase1-foundational-loop.md § Chunk 2

Node-subprocess retirement: this module now calls
``coordinator_core.ops.ceremony.records_query.query_records`` in-process, so
these tests drive the native path against real ``tmp_path`` completion-log
fixtures instead of mocking a ``node`` subprocess spawn.
"""

from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from coordinator_core.ops import query_completions


def _write_completion(root: Path, month: str, name: str, frontmatter: str, body: str = "Body.\n") -> Path:
    d = root / "archive" / "completed" / month
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.md"
    f.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return f


def test_help_first_arg_prints_wrapper_usage_and_exits_zero():
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = query_completions.main(["--help"])
    assert rc == 0
    assert "query-completions.sh — Query completion-log entries." in buf.getvalue()
    assert "--type" not in buf.getvalue().split("\n")[0]  # sanity: not query-records' own help


def test_h_first_arg_also_triggers_help():
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = query_completions.main(["-h"])
    assert rc == 0
    assert "Native reader over completion-log entries" in buf.getvalue()


def test_markdown_list_default_format_matches_type_display_completion(tmp_path: Path):
    _write_completion(
        tmp_path, "2026-07", "a",
        "title: Landed the thing\nnature: fix\nchain: c1\ncommits:\n  - abc123\n  - def456\n",
    )
    with mock.patch.object(query_completions, "_detect_root", return_value=tmp_path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = query_completions.main([])
    assert rc == 0
    assert buf.getvalue() == "- **Landed the thing** [fix] (chain: c1) — abc123, def456\n"


def test_markdown_list_falls_back_to_none_and_no_commit(tmp_path: Path):
    _write_completion(tmp_path, "2026-07", "b", "title: Bare entry\nnature: chore\n")
    with mock.patch.object(query_completions, "_detect_root", return_value=tmp_path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = query_completions.main([])
    assert rc == 0
    assert buf.getvalue() == "- **Bare entry** [chore] (chain: none) — no-commit\n"


def test_format_json_emits_bare_stringify_array(tmp_path: Path):
    _write_completion(tmp_path, "2026-07", "c", "title: Entry C\nnature: fix\n")
    with mock.patch.object(query_completions, "_detect_root", return_value=tmp_path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = query_completions.main(["--format", "json"])
    assert rc == 0
    parsed = json.loads(buf.getvalue())
    assert parsed == [
        {
            "path": "archive/completed/2026-07/c.md",
            "frontmatter": {
                "title": "Entry C",
                "nature": "fix",
                "liveness": "LIVE",
                # "archived" is injected onto every record's frontmatter,
                # always present (coordinator_core/ops/records_query.py's own
                # module docstring, same collection-origin injection as
                # "liveness" -- not optional, not type-specific).
                "archived": False,
            },
        }
    ]


def test_format_paths(tmp_path: Path):
    _write_completion(tmp_path, "2026-07", "d", "title: Entry D\nnature: fix\n")
    with mock.patch.object(query_completions, "_detect_root", return_value=tmp_path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = query_completions.main(["--format", "paths"])
    assert rc == 0
    assert buf.getvalue() == "archive/completed/2026-07/d.md\n"


def test_since_filters_by_created(tmp_path: Path):
    _write_completion(tmp_path, "2026-07", "old", "title: Old\nnature: fix\ncreated: 2020-01-01\n")
    _write_completion(tmp_path, "2026-07", "new", "title: New\nnature: fix\ncreated: 2099-01-01\n")
    with mock.patch.object(query_completions, "_detect_root", return_value=tmp_path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = query_completions.main(["--since", "1d", "--format", "paths"])
    assert rc == 0
    assert buf.getvalue() == "archive/completed/2026-07/new.md\n"


def test_where_filter(tmp_path: Path):
    _write_completion(tmp_path, "2026-07", "keep", "title: Keep\nnature: fix\nstatus: pending-release\n")
    _write_completion(tmp_path, "2026-07", "drop", "title: Drop\nnature: fix\nstatus: shipped\n")
    with mock.patch.object(query_completions, "_detect_root", return_value=tmp_path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = query_completions.main(["--where", "status=pending-release", "--format", "paths"])
    assert rc == 0
    assert buf.getvalue() == "archive/completed/2026-07/keep.md\n"


def test_limit_defaults_to_fifty_matching_oracle(tmp_path: Path):
    for i in range(60):
        _write_completion(tmp_path, "2026-07", f"e{i:02d}", f"title: Entry {i}\nnature: fix\n")
    with mock.patch.object(query_completions, "_detect_root", return_value=tmp_path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = query_completions.main(["--format", "paths"])
    assert rc == 0
    assert len(buf.getvalue().splitlines()) == 50


def test_limit_zero_means_unlimited(tmp_path: Path):
    for i in range(60):
        _write_completion(tmp_path, "2026-07", f"f{i:02d}", f"title: Entry {i}\nnature: fix\n")
    with mock.patch.object(query_completions, "_detect_root", return_value=tmp_path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = query_completions.main(["--limit", "0", "--format", "paths"])
    assert rc == 0
    assert len(buf.getvalue().splitlines()) == 60


def test_root_flag_overrides_detected_root(tmp_path: Path):
    _write_completion(tmp_path, "2026-07", "g", "title: Entry G\nnature: fix\n")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = query_completions.main(["--root", str(tmp_path), "--format", "paths"])
    assert rc == 0
    assert buf.getvalue() == "archive/completed/2026-07/g.md\n"


def test_unsupported_flag_sort_fails_loud():
    with mock.patch.object(query_completions, "_detect_root", return_value=Path(".")):
        rc = query_completions.main(["--sort", "-created"])
    assert rc == 1


def test_unsupported_flag_older_than_fails_loud():
    with mock.patch.object(query_completions, "_detect_root", return_value=Path(".")):
        rc = query_completions.main(["--older-than", "7d"])
    assert rc == 1


def test_unknown_flag_fails_loud():
    rc = query_completions.main(["--bogus-flag", "foo"])
    assert rc == 1


def test_invalid_limit_fails_loud():
    rc = query_completions.main(["--limit", "not-a-number"])
    assert rc == 1


def test_invalid_format_fails_loud():
    rc = query_completions.main(["--format", "yaml"])
    assert rc == 1


def test_invalid_since_propagates_systemexit_as_nonzero(tmp_path: Path):
    with mock.patch.object(query_completions, "_detect_root", return_value=tmp_path):
        rc = query_completions.main(["--since", "not-a-date"])
    assert rc == 1


def test_detect_root_uses_root_flag_without_git(tmp_path: Path):
    assert query_completions._detect_root(str(tmp_path)) == tmp_path.resolve()


def test_detect_root_falls_back_to_cwd_when_git_fails(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout="")
        result = query_completions._detect_root(None)
    assert result == tmp_path

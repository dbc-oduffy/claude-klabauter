"""Rule branches of the stale executing plans reader."""
from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.workday_start.stale_plans import stale_executing_plans

NOW = 1_800_000_000.0
DAY = 86400.0


def _plan(root: Path, name: str, text: str, age_days: float) -> None:
    d = root / "docs" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    t = NOW - age_days * DAY
    os.utime(p, (t, t))


def _fm(status: str) -> str:
    return f"---\ntitle: x\nstatus: {status}\n---\n\nbody\n"


def test_absent_directory_is_silent(tmp_path):
    assert stale_executing_plans(tmp_path, now=NOW) == (0, "")


def test_empty_directory_is_silent(tmp_path):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    assert stale_executing_plans(tmp_path, now=NOW) == (0, "")


def test_fresh_executing_is_silent(tmp_path):
    _plan(tmp_path, "a.md", _fm("executing"), 2.5)
    assert stale_executing_plans(tmp_path, now=NOW) == (0, "")


def test_exactly_three_days_is_not_stale(tmp_path):
    _plan(tmp_path, "a.md", _fm("executing"), 3.0)
    assert stale_executing_plans(tmp_path, now=NOW) == (0, "")


def test_stale_executing_lines_sorted_by_path(tmp_path):
    _plan(tmp_path, "b.md", _fm("executing"), 5.2)
    _plan(tmp_path, "a.md", _fm("executing"), 3.5)
    code, text = stale_executing_plans(tmp_path, now=NOW)
    assert code == 0
    assert text.split("\n") == [
        "  - docs/plans/a.md (status: executing, untouched 3d)",
        "  - docs/plans/b.md (status: executing, untouched 5d)",
    ]


def test_non_executing_statuses_are_ignored(tmp_path):
    for i, s in enumerate(("done", "superseded", "executing-x", "'executing-later'", "")):
        _plan(tmp_path, f"p{i}.md", _fm(s), 30)
    assert stale_executing_plans(tmp_path, now=NOW) == (0, "")


def test_quoted_and_commented_status_matches(tmp_path):
    _plan(tmp_path, "q.md", "---\nstatus: \"executing\"  # live\n---\n", 10)
    assert "docs/plans/q.md" in stale_executing_plans(tmp_path, now=NOW)[1]


def test_status_outside_frontmatter_is_ignored(tmp_path):
    _plan(tmp_path, "body.md", "# T\n\nstatus: executing\n", 10)
    _plan(tmp_path, "after.md", "---\ntitle: x\n---\nstatus: executing\n", 10)
    _plan(tmp_path, "unclosed.md", "---\ntitle: x\n", 10)
    assert stale_executing_plans(tmp_path, now=NOW) == (0, "")


def test_non_markdown_and_subdirectories_are_ignored(tmp_path):
    _plan(tmp_path, "a.txt", _fm("executing"), 10)
    (tmp_path / "docs" / "plans" / "sub.md").mkdir()
    assert stale_executing_plans(tmp_path, now=NOW) == (0, "")


def test_long_frontmatter_beyond_one_chunk_is_still_read(tmp_path):
    pad = "".join(f"k{i}: {'v' * 60}\n" for i in range(200))
    _plan(tmp_path, "long.md", f"---\n{pad}status: executing\n---\n", 9)
    assert "docs/plans/long.md" in stale_executing_plans(tmp_path, now=NOW)[1]


def test_crlf_frontmatter(tmp_path):
    _plan(tmp_path, "crlf.md", "---\r\nstatus: executing\r\n---\r\n", 9)
    assert "docs/plans/crlf.md" in stale_executing_plans(tmp_path, now=NOW)[1]

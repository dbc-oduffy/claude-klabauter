"""plans_completed adapter: view path primary, git-free filesystem scan fallback marked degraded."""
from __future__ import annotations

import datetime
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import daily_log_source as src

pytestmark = pytest.mark.complete

UTC = datetime.timezone.utc
SINCE = datetime.datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
UNTIL = datetime.datetime(2026, 1, 16, 0, 0, tzinfo=UTC)


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _plan(title: str, status: str, size: str, completed: str | None = None) -> str:
    extra = f"completed: {completed}\n" if completed else ""
    return (
        f"---\ntitle: \"{title}\"\nstatus: {status}\n{extra}sizing_object: \"state/sizings/{size}.yaml\"\n---\n\n"
        f"# {title}\n\n## Problem\n\nThe {title} summary sentence.\nSecond line.\n\n## Tasks\n"
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    _write(r, "state/sizings/small.yaml", "estimate:\n  tshirt: S\n")
    _write(r, "state/sizings/big.yaml", "estimate:\n  tshirt: L\n")
    _write(r, "docs/plans/2026-01/a.md", _plan("alpha", "complete", "small", "2026-01-15"))
    _write(r, "docs/plans/2026-01/b.md", _plan("bravo", "implemented", "big", "2026-01-15"))
    _write(r, "docs/plans/2026-01/d.md", _plan("delta", "complete", "small", "2026-01-17"))
    _write(r, "docs/plans/2026-01/e.md", _plan("echo", "executing", "small", "2026-01-15"))
    _write(r, "docs/plans/2026-01/a.review.md", _plan("alpha-review", "complete", "small", "2026-01-15"))
    _write(r, "docs/plans/2025-11/old.md", _plan("old", "complete", "small", "2026-01-15"))
    _write(r, "docs/plans/flat.md", _plan("flat", "landed", "small", "2026-01-15"))
    m = SINCE.timestamp() + 3600
    _write(r, "docs/plans/2026-01/mt.md", _plan("mtime", "complete", "big"))
    os.utime(r / "docs/plans/2026-01/mt.md", (m, m))
    _write(r, "docs/plans/2026-01/mt-out.md", _plan("mtime-out", "complete", "big"))
    os.utime(r / "docs/plans/2026-01/mt-out.md", (m + 5 * 86400, m + 5 * 86400))
    return r


def _view_db(path: Path, rows: "list[tuple]") -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE _pc (plan, summary_line, sha, sizing, completed_at)")
    con.executemany("INSERT INTO _pc VALUES (?,?,?,?,?)", rows)
    con.execute("CREATE VIEW plans_completed AS SELECT * FROM _pc")
    con.commit()
    con.close()
    return path


def _by_plan(rows: "list[dict]") -> "dict[str, dict]":
    return {r["plan"]: r for r in rows}


def test_scan_fallback_lists_window_plans_with_null_sha_and_degraded_marker(repo, tmp_path: Path) -> None:
    res = src.plans_completed_result(repo, SINCE, UNTIL, settings_home=tmp_path / "no-home")
    got = _by_plan(res["rows"])
    assert set(got) == {
        "docs/plans/2026-01/a.md", "docs/plans/2026-01/b.md", "docs/plans/flat.md", "docs/plans/2026-01/mt.md",
    }
    assert got["docs/plans/2026-01/a.md"] == {
        "plan": "docs/plans/2026-01/a.md", "summary_line": "The alpha summary sentence.",
        "sha": None, "sizing": "S",
    }
    assert got["docs/plans/2026-01/b.md"]["sizing"] == "L"
    assert res["degraded"]["mode"] == "view-absent" and res["degraded"]["reason"]
    assert [x["plan"] for x in src.rank_by_sizing(res["rows"])][:2] == [
        "docs/plans/2026-01/b.md", "docs/plans/2026-01/mt.md",
    ]


def test_scan_refuses_out_of_window_non_terminal_and_sidecar_plans(repo, tmp_path: Path) -> None:
    got = _by_plan(src.plans_completed(repo, SINCE, UNTIL, settings_home=tmp_path / "h"))
    for refused in ("d.md", "e.md", "a.review.md", "mt-out.md"):
        assert f"docs/plans/2026-01/{refused}" not in got
    assert "docs/plans/2025-11/old.md" not in got


def test_window_is_half_open(repo, tmp_path: Path) -> None:
    t = datetime.datetime(2026, 1, 16, 0, 0, tzinfo=UTC)
    got = _by_plan(src.plans_completed(repo, t, t + datetime.timedelta(days=1), settings_home=tmp_path / "h"))
    assert "docs/plans/2026-01/a.md" not in got
    assert not src.plans_completed(repo, t - datetime.timedelta(days=3), t - datetime.timedelta(days=2),
                                   settings_home=tmp_path / "h")


def test_view_path_is_primary_and_not_degraded(repo, tmp_path: Path) -> None:
    mid = int(SINCE.timestamp()) + 3600
    db = _view_db(
        tmp_path / "doc_files.sqlite",
        [("docs/plans/gone.md", "deleted plan", "c" * 40, "M", mid),
         ("docs/plans/z.md", "outside", "f" * 40, "S", int(UNTIL.timestamp()) + 5)],
    )
    res = src.plans_completed_result(repo, SINCE, UNTIL, view_db=db)
    assert res["degraded"] is None
    assert res["rows"] == [
        {"plan": "docs/plans/gone.md", "summary_line": "deleted plan", "sha": "c" * 40, "sizing": "M"}
    ]


def test_view_missing_or_broken_falls_back_to_scan(repo, tmp_path: Path) -> None:
    bare = tmp_path / "bare.sqlite"
    sqlite3.connect(bare).close()
    for db in (bare, tmp_path / "absent.sqlite"):
        res = src.plans_completed_result(repo, SINCE, UNTIL, view_db=db)
        assert len(res["rows"]) == 4 and res["degraded"]["mode"] == "view-absent"


def test_fallback_spawns_nothing(repo, tmp_path: Path, monkeypatch) -> None:
    spawns: "list[object]" = []
    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawns.append(a) or real(*a, **k))
    src.plans_completed(repo, SINCE, UNTIL, settings_home=tmp_path / "none")
    assert spawns == []
    assert not hasattr(src, "run_git")


def test_summary_line_falls_back_to_title() -> None:
    text = "---\ntitle: \"Only a title\"\nstatus: complete\n---\n\n# x\n"
    assert src.summary_line(text) == "Only a title"


def test_rank_by_sizing_orders_largest_first_unknown_last() -> None:
    rows = [
        {"plan": "p/a", "sizing": "S"}, {"plan": "p/b", "sizing": None},
        {"plan": "p/c", "sizing": "XL"}, {"plan": "p/d", "sizing": "M"},
    ]
    assert [x["plan"] for x in src.rank_by_sizing(rows)] == ["p/c", "p/d", "p/a", "p/b"]


_REAL_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(not (_REAL_ROOT / "docs" / "plans").is_dir(), reason="no HEAD corpus")
def test_scan_fallback_on_the_head_corpus_at_default_window_holds_the_500ms_bar(tmp_path: Path) -> None:
    import time

    until = datetime.datetime.now(UTC)
    start = time.process_time()
    res = src.plans_completed_result(_REAL_ROOT, until - datetime.timedelta(hours=24), until, settings_home=tmp_path)
    assert (time.process_time() - start) * 1000 < 500
    assert res["degraded"]["mode"] == "view-absent"

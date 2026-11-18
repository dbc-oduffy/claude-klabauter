"""bookkeeping-plan-ledger.py lists terminal-status plans per window, ranked by sizing tshirt.

Every test drives the script as a subprocess against a tmp_path git repo, the production
entry point; dates are pinned through GIT_COMMITTER_DATE so no test depends on the clock
except the two default-argument tests, which commit "now".
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bookkeeping-plan-ledger.py"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LONG_AGO = "2026-01-05T10:00:00+00:00"


def _git(repo: Path, *args: str, when: str | None = None) -> str:
    env = dict(os.environ)
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
         *args],
        cwd=str(repo), env=env, capture_output=True, text=True, creationflags=NO_WINDOW,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _plan(repo: Path, name: str, status: str, *, title: str, sizing: str | None = None,
          summary: str | None = None) -> str:
    rel = f"docs/plans/{name}.md"
    lines = ["---", f'title: "{title}"', f"status: {status}  # lifecycle"]
    if summary:
        lines.append(f"summary: {summary}")
    if sizing:
        lines.append(f'sizing_object: "state/sizings/{sizing}.yaml"')
    lines += ["---", "", f"# {title}", "", "## Problem", "", f"{title} problem line.", "", "## Goal", "", "x", ""]
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return rel


def _sizing(repo: Path, name: str, tshirt: str) -> None:
    path = repo / "state" / "sizings" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"schema: sizing-object\nname: {name}\nestimate:\n  tshirt: {tshirt}  # note\n  provisional: true\nroute: x\n",
        encoding="utf-8", newline="\n",
    )


def _commit(repo: Path, message: str, when: str | None = None) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message, when=when)
    return _git(repo, "rev-parse", "HEAD")


def _run(*argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv], cwd=str(cwd) if cwd else None, capture_output=True,
        text=True, creationflags=NO_WINDOW,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    for name, tshirt in (("big", "L"), ("small", "S"), ("mid", "M")):
        _sizing(r, name, tshirt)
    _plan(r, "2026-11-01-flipped-small", "approved", title="Flipped small", sizing="small")
    _plan(r, "2026-11-01-flipped-big", "executing", title="Flipped big", sizing="big", summary="Big summary line")
    _plan(r, "2026-11-01-no-sizing", "draft", title="No sizing")
    _plan(r, "2026-11-01-stays-draft", "draft", title="Stays draft", sizing="mid")
    _plan(r, "2026-11-01-already-done", "implemented", title="Already done", sizing="big")
    _commit(r, "seed", when=LONG_AGO)
    _plan(r, "2026-11-01-flipped-small", "landed", title="Flipped small", sizing="small")
    _plan(r, "2026-11-01-flipped-big", "implemented", title="Flipped big", sizing="big", summary="Big summary line")
    _plan(r, "2026-11-01-no-sizing", "abandoned", title="No sizing")
    _plan(r, "2026-11-01-stays-draft", "approved", title="Stays draft", sizing="mid")
    _plan(r, "2026-11-01-already-done", "implemented", title="Already done edited", sizing="big")
    _plan(r, "2026-11-18-born-terminal", "landed", title="Born terminal", sizing="mid")
    (r / "docs/plans/2026-11-18-born-terminal.review.md").write_text(
        "---\nstatus: complete\n---\nreview\n", encoding="utf-8", newline="\n"
    )
    _commit(r, "flip")
    return r


def _paths(out: str) -> list[str]:
    return [ln.rsplit("(", 1)[1].split(" @ ")[0] for ln in out.splitlines() if ln.startswith("- [")]


def test_daily_default_args_lists_flips_ranked_by_tshirt(repo: Path) -> None:
    done = _run("daily", cwd=repo)
    assert done.returncode == 0, done.stderr
    tshirts = [ln.split("]")[0][3:] for ln in done.stdout.splitlines() if ln.startswith("- [")]
    assert tshirts == ["L", "M", "S", "?"]
    assert _paths(done.stdout) == [
        "docs/plans/2026-11-01-flipped-big.md",
        "docs/plans/2026-11-18-born-terminal.md",
        "docs/plans/2026-11-01-flipped-small.md",
        "docs/plans/2026-11-01-no-sizing.md",
    ]
    head = _git(repo, "rev-parse", "HEAD")
    assert all(ln.endswith(f" @ {head[:10]})") for ln in done.stdout.splitlines())
    assert "Flipped big -- Big summary line" in done.stdout
    assert "Born terminal -- Born terminal problem line." in done.stdout


def test_daily_excludes_unflipped_and_sidecars(repo: Path) -> None:
    out = _run("daily", str(repo)).stdout
    assert "already-done" not in out
    assert "stays-draft" not in out
    assert ".review" not in out


def test_weekly_candidates_default_args_and_max(repo: Path) -> None:
    full = _run("weekly-candidates", cwd=repo)
    assert full.returncode == 0, full.stderr
    assert set(_paths(full.stdout)) == {
        "docs/plans/2026-11-01-flipped-big.md",
        "docs/plans/2026-11-18-born-terminal.md",
        "docs/plans/2026-11-01-flipped-small.md",
        "docs/plans/2026-11-01-no-sizing.md",
    }
    top = _run("weekly-candidates", "--repo", str(repo), "--max", "1").stdout
    assert _paths(top) == ["docs/plans/2026-11-01-flipped-big.md"]


def test_weekly_preview_is_opt_in(repo: Path) -> None:
    assert "## Problem" not in _run("weekly-candidates", str(repo)).stdout
    shown = _run("weekly-candidates", str(repo), "--max", "1", "--preview-chars", "60").stdout
    assert "title: " in shown


def test_json_format_rows(repo: Path) -> None:
    rows = json.loads(_run("daily", str(repo), "--format", "json").stdout)
    assert [r["tshirt"] for r in rows] == ["L", "M", "S", "?"]
    assert set(rows[0]) == {"plan_path", "title", "summary", "tshirt", "commit", "status"}
    assert rows[0]["status"] == "implemented"


def test_window_is_pinned_by_commit_date(tmp_path: Path) -> None:
    r = tmp_path / "dated"
    r.mkdir()
    _git(r, "init", "-q")
    _sizing(r, "m", "M")
    _plan(r, "2026-11-02-wk", "executing", title="Week plan", sizing="m")
    _commit(r, "seed", when="2026-11-02T09:00:00+00:00")
    _plan(r, "2026-11-02-wk", "landed", title="Week plan", sizing="m")
    _commit(r, "flip", when="2026-11-11T12:00:00+00:00")
    hit = _run("weekly-candidates", str(r), "--week-start", "2026-11-09")
    miss_next = _run("weekly-candidates", str(r), "--week-start", "2026-11-16")
    miss_prev = _run("weekly-candidates", str(r), "--week-start", "2026-11-02")
    assert _paths(hit.stdout) == ["docs/plans/2026-11-02-wk.md"]
    assert _paths(miss_next.stdout) == [] and miss_next.returncode == 0
    assert _paths(miss_prev.stdout) == []
    daily = _run("daily", str(r), "--since", "2026-11-11T00:00:00+00:00", "--until", "2026-11-12T00:00:00+00:00")
    assert _paths(daily.stdout) == ["docs/plans/2026-11-02-wk.md"]
    assert _paths(_run("daily", str(r), "--since", "2026-11-12", "--until", "2026-11-13").stdout) == []


def test_a_plan_born_terminal_in_the_first_commit_is_listed(tmp_path: Path) -> None:
    r = tmp_path / "first"
    r.mkdir()
    _git(r, "init", "-q")
    _plan(r, "2026-11-18-only", "landed", title="Only plan")
    _commit(r, "first")
    assert _paths(_run("daily", str(r)).stdout) == ["docs/plans/2026-11-18-only.md"]


def test_bad_arguments_and_non_repo(tmp_path: Path) -> None:
    assert _run("weekly-candidates", str(tmp_path), "--week-start", "2026-11-10").returncode == 2
    assert _run("daily", str(tmp_path), "--since", "not-a-date").returncode == 2
    plain = tmp_path / "plain"
    plain.mkdir()
    done = _run("daily", str(plain))
    assert done.returncode == 1 and "bookkeeping-plan-ledger" in done.stderr


def test_terminal_set_covers_the_engine_terminal_statuses() -> None:
    spec = importlib.util.spec_from_file_location("bookkeeping_plan_ledger", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from coordinator_core.lifecycle_constants import PLAN_TERMINAL_STATUS

    assert PLAN_TERMINAL_STATUS <= mod.TERMINAL_STATUSES
    assert {"implemented", "landed"} <= mod.TERMINAL_STATUSES

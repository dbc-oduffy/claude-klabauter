"""commit-archive-reset stages state/week-changelog plus every named prior week's archive dir."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "workweek-start-goal-and-priorities.py"


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("wsgp_under_test", _BIN)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    calls: list[list[str]] = []

    def fake_git(args):
        calls.append(list(args))
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(m, "_git", fake_git)
    monkeypatch.setattr(m, "_resolve_branch", lambda: "main")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state/week-changelog").mkdir(parents=True)
    m.calls = calls
    return m


def _run(m, *weeks):
    argv = ["commit-archive-reset"]
    for w in weeks:
        argv += ["--prior-week-start", w]
    args = m._build_parser().parse_args(argv)
    return m.cmd_commit_archive_reset(args)


def _add_paths(m):
    add = next(c for c in m.calls if c[0] == "add")
    return add[2:]


def test_every_named_existing_week_is_staged_in_one_add(mod, tmp_path):
    for w in ("2026-09-28", "2026-10-05"):
        (tmp_path / "archive/week-changelogs" / w).mkdir(parents=True)
    assert _run(mod, "2026-09-28", "2026-10-05") == 0
    assert sum(1 for c in mod.calls if c[0] == "add") == 1
    assert _add_paths(mod) == [
        str(Path("state/week-changelog")),
        str(Path("archive/week-changelogs/2026-09-28")),
        str(Path("archive/week-changelogs/2026-10-05")),
    ]


def test_a_named_week_with_no_archive_dir_is_not_staged(mod, tmp_path):
    (tmp_path / "archive/week-changelogs/2026-10-05").mkdir(parents=True)
    assert _run(mod, "2026-09-28", "2026-10-05") == 0
    assert _add_paths(mod) == [str(Path("state/week-changelog")), str(Path("archive/week-changelogs/2026-10-05"))]


def test_one_flag_stages_exactly_that_week(mod, tmp_path):
    (tmp_path / "archive/week-changelogs/2026-10-05").mkdir(parents=True)
    assert _run(mod, "2026-10-05") == 0
    assert len(_add_paths(mod)) == 2


@pytest.mark.parametrize("bad", ["../etc", "2026-10-5", "2026-10-05/x", ""])
def test_a_non_date_value_is_rejected_at_parse_time(mod, bad):
    with pytest.raises(SystemExit):
        mod._build_parser().parse_args(["commit-archive-reset", "--prior-week-start", bad])


def test_the_flag_is_still_required(mod):
    with pytest.raises(SystemExit):
        mod._build_parser().parse_args(["commit-archive-reset"])

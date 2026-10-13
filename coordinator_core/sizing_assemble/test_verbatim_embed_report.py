"""`sizing-assemble --write` reports its pm_verbatims embed on stderr; `--verbatim-turns A-B` embeds exactly A..B."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.baton_pm_turns as pm_turns
import coordinator_core.sizing_assemble as sizing_assemble
from coordinator_core.session import record_homes
from coordinator_core.session_baton import store

_DRAFT = """schema: sizing-object
intent: "Ship the thing"
estimate:
  tshirt: XS
  provisional: true
route: dispatch
detents: []
fork: null
xl_exit: null
status: draft
premise:
  provenance: unrecorded
  evidence: PLACEHOLDER - cite the evidence
deliverable_id: null
"""

_ARGS = ["--tshirt", "M", "--intent", "Ship the thing", "--premise-provenance", "read",
         "--premise-evidence", "read it"]


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    Path(record_homes.home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
    return tmp_path


@pytest.fixture()
def sid(repo: Path, monkeypatch) -> str:
    sdir = repo / ".git" / "coordinator-sessions" / "sid-vr"
    sdir.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: sdir.parent / s)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sid-vr")
    return "sid-vr"


def _sizing(repo: Path, text: str = _DRAFT) -> Path:
    path = Path(record_homes.record_path(str(repo), "sizings", "2026-10-11-x.yaml"))
    path.write_text(text, encoding="utf-8")
    return path


def _run(path: Path, capsys, *extra: str):
    code = sizing_assemble.main([*_ARGS, "--write", str(path), *extra])
    out, err = capsys.readouterr()
    return code, out, err


def _record(count: int, size: int = 2500) -> None:
    for n in range(count):
        pm_turns.append_turn(f"t{n} " + "x" * size, session_id="sid-vr")


def test_default_window_embeds_every_turn_since_the_last_sizing(repo, sid, capsys) -> None:
    _record(6, size=2500)
    code, out, err = _run(_sizing(repo), capsys)
    assert code == 0
    assert err.strip().endswith("embedded 6 pm_verbatims (turns 0-5)")
    assert store.read_baton(sid, str(repo))["sized_through_turn"] == 5
    json.loads(out)


def test_second_sizing_embeds_only_turns_after_the_mark(repo, sid, capsys) -> None:
    _record(6, size=10)
    pm_turns.mark_sized_through(2, session_id=sid, cwd=str(repo))
    path = _sizing(repo)
    code, _, err = _run(path, capsys)
    assert code == 0
    assert err.strip().endswith("embedded 3 pm_verbatims (turns 3-5)")
    assert [e["turn"] for e in yaml.safe_load(path.read_text(encoding="utf-8"))["pm_verbatims"]] == [5, 4, 3]


def test_example_game_repo_shape_embeds_turn_9_with_no_prior_sizing(repo, sid, capsys) -> None:
    _record(11, size=700)
    path = _sizing(repo)
    code, _, _ = _run(path, capsys)
    assert code == 0
    assert 9 in [e["turn"] for e in yaml.safe_load(path.read_text(encoding="utf-8"))["pm_verbatims"]]


def test_refused_write_leaves_the_mark(repo, sid, capsys) -> None:
    _record(3, size=10)
    path = _sizing(repo, _DRAFT + "pm_verbatims:\n  - {turn: 0, ts: null, text: kept}\n")
    code, _, _ = _run(path, capsys, "--verbatim-turns", "0-1")
    assert code == sizing_assemble.EXIT_BUSINESS_FAIL
    assert store.read_baton(sid, str(repo))["sized_through_turn"] is None


def test_verbatim_turns_advances_the_mark(repo, sid, capsys) -> None:
    _record(5, size=10)
    code, _, _ = _run(_sizing(repo), capsys, "--verbatim-turns", "1-2")
    assert code == 0
    assert store.read_baton(sid, str(repo))["sized_through_turn"] == 2


def test_no_log_reports_zero(repo, sid, capsys) -> None:
    code, out, err = _run(_sizing(repo), capsys)
    assert code == 0
    assert err.strip() == "embedded 0 pm_verbatims"
    json.loads(out)


def test_already_present_reports_zero(repo, sid, capsys) -> None:
    _record(2, size=10)
    path = _sizing(repo, _DRAFT + "pm_verbatims:\n  - {turn: 0, ts: null, text: kept}\n")
    code, out, err = _run(path, capsys)
    assert code == 0
    assert err.strip() == "embedded 0 pm_verbatims"
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["pm_verbatims"][0]["text"] == "kept"
    json.loads(out)


def test_verbatim_turns_embeds_exactly_the_range_newest_first(repo, sid, capsys) -> None:
    _record(5, size=10)
    path = _sizing(repo)
    code, out, err = _run(path, capsys, "--verbatim-turns", "1-2")
    assert code == 0
    assert err.strip().endswith("embedded 2 pm_verbatims (turns 1-2)")
    assert [e["turn"] for e in yaml.safe_load(path.read_text(encoding="utf-8"))["pm_verbatims"]] == [2, 1]
    assert json.loads(out)["write"]["pm_verbatims"] == {"embedded": 2, "turns": [1, 2]}


def test_missing_index_is_refused_and_file_unchanged(repo, sid, capsys) -> None:
    _record(3, size=10)
    path = _sizing(repo)
    before = path.read_bytes()
    code, out, err = _run(path, capsys, "--verbatim-turns", "1-5")
    assert code == sizing_assemble.EXIT_BUSINESS_FAIL
    assert "no recorded turn" in err
    assert path.read_bytes() == before
    assert out == ""


def test_existing_pm_verbatims_refuses_re_embed(repo, sid, capsys) -> None:
    _record(3, size=10)
    path = _sizing(repo, _DRAFT + "pm_verbatims:\n  - {turn: 0, ts: null, text: kept}\n")
    before = path.read_bytes()
    code, _, err = _run(path, capsys, "--verbatim-turns", "0-1")
    assert code == sizing_assemble.EXIT_BUSINESS_FAIL
    assert "hand-edit" in err
    assert path.read_bytes() == before


@pytest.mark.parametrize("raw", ["2-1", "a-b", "-1-2", "3", "1-"])
def test_bad_range_is_usage_error(repo, sid, capsys, raw) -> None:
    code, _, _ = _run(_sizing(repo), capsys, "--verbatim-turns", raw)
    assert code == sizing_assemble.EXIT_USAGE


def test_verbatim_turns_without_write_is_usage_error(capsys) -> None:
    assert sizing_assemble.main([*_ARGS, "--verbatim-turns", "0-1"]) == sizing_assemble.EXIT_USAGE


def test_write_derives_a_register_covering_every_turn(repo, sid, capsys) -> None:
    _record(2, size=10)
    code, out, err = _run(_sizing(repo), capsys)
    assert code == 0
    assert "derived 2 requirement_register rows" in err and "not covered" not in err
    assert json.loads(out)["write"]["register_coverage"] == {
        "turns_total": 2,
        "turns_uncovered": [],
    }

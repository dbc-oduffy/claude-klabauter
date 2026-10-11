"""`sizing-assemble --intent-turn N` writes turn N's prompt byte-identical into `intent`."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.baton_pm_turns as pm_turns
import coordinator_core.sizing_assemble as sizing_assemble
from coordinator_core.session import record_homes
from coordinator_core.session_baton import store

_DRAFT = """schema: sizing-object
intent: "A real, non-placeholder intent"
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

_ARGS = ["--tshirt", "M", "--premise-provenance", "read", "--premise-evidence", "read it"]
_PROMPT = '- fix: the "thing" and more\n'


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    Path(record_homes.home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
    return tmp_path


@pytest.fixture()
def sid(repo: Path, monkeypatch) -> str:
    sdir = repo / ".git" / "coordinator-sessions" / "sid-it"
    sdir.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: sdir.parent / s)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sid-it")
    pm_turns.append_turn("zero", session_id="sid-it")
    pm_turns.append_turn(_PROMPT, session_id="sid-it")
    return "sid-it"


def _path(repo: Path) -> Path:
    return Path(record_homes.record_path(str(repo), "sizings", "2026-10-11-x.yaml"))


def _run(path: Path, capsys, *extra: str):
    code = sizing_assemble.main([*_ARGS, "--write", str(path), *extra])
    out, err = capsys.readouterr()
    return code, out, err


def _prompt_of_turn_one() -> str:
    return pm_turns.read_turns(turn=1, session_id="sid-it")["turns"][0]["prompt"]


@pytest.mark.parametrize("existing", [False, True])
def test_turn_text_round_trips_byte_identical(repo, sid, capsys, existing) -> None:
    path = _path(repo)
    if existing:
        path.write_text(_DRAFT, encoding="utf-8")
    code, out, err = _run(path, capsys, "--intent-turn", "1")
    assert code == 0, err
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc["intent"] == _prompt_of_turn_one()
    assert doc["intent"].endswith("\n") and " " in doc["intent"]
    assert doc["intent_source"] == "pm-verbatim"


def test_no_restatement_advisory(repo, sid, capsys) -> None:
    import json

    code, out, _ = _run(_path(repo), capsys, "--intent-turn", "1")
    assert code == 0
    decision = json.loads(out)
    assert "intent_em_elaborated" not in decision["detents"]
    assert "restatement" not in decision["next_move"]


@pytest.mark.parametrize(
    "extra",
    [
        ["--intent-turn", "1", "--intent", "x"],
        ["--intent-turn", "1", "--intent-source", "em-elaborated"],
        ["--intent-turn"],
        ["--intent-turn", "-1"],
        ["--intent-turn", "abc"],
        ["--intent-turn", "9"],
    ],
)
def test_refusals_exit_2_and_write_nothing(repo, sid, capsys, extra) -> None:
    path = _path(repo)
    path.write_text(_DRAFT, encoding="utf-8")
    before = path.read_bytes()
    code, out, err = _run(path, capsys, *extra)
    assert code == sizing_assemble.EXIT_USAGE
    assert path.read_bytes() == before
    assert out == ""


def test_unrecorded_turn_message_names_n_and_count(repo, sid, capsys) -> None:
    _, _, err = _run(_path(repo), capsys, "--intent-turn", "9")
    assert "9" in err and "holds 2" in err
    assert not _path(repo).exists()

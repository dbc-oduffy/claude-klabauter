"""A revise row whose verbatim holds backslash-r / backslash-n (a Windows path) round-trips byte-exact:
sizing YAML, the requirements.md view, and a composed script with no raw control character."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import ask_compose, requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests import test_ask_compose_stage_handle as handle
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

RUN = "ask-20261124T000000-cccccc"
REL = "state/sizings/2026-11-24-backslash-round-trip.yaml"
BS = chr(92)
PATH = "E:" + BS + "research" + BS + "new"
WORDS = "use " + PATH + " as the root"
_RAW_CONTROL = re.compile("[\x00\x02-\x09\x0b-\x1f  ]")


def _sizing() -> dict:
    return {
        "schema": "sizing-object", "intent": "do it", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "lint the thing"}],
    }


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / REL).write_text(yaml.safe_dump(_sizing(), sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL)
    return tmp_path


@pytest.fixture
def minted(repo: Path) -> str:
    (repo / "v.txt").write_bytes(WORDS.encode("utf-8"))
    out = rl._handler({"run_id": RUN, "verbatim_file": "v.txt"}, repo_root=repo)
    assert out.get("ok"), out
    return out["item"]


def test_sizing_yaml_parses_back_to_the_exact_source_text(repo: Path, minted: str) -> None:
    raw = (repo / REL).read_bytes()
    assert b"\r" not in raw
    rows = yaml.safe_load(raw.decode("utf-8"))[rr.REGISTER_KEY][rr.ROWS_KEY]
    row = next(r for r in rows if r[rr.ROW_ID] == minted)
    assert row[rr.ROW_TEXT] == WORDS
    assert row[rr.ROW_TEXT].count(BS) == 2


def test_register_reader_returns_the_exact_source_text(repo: Path, minted: str) -> None:
    rows = rr.read_register((repo / REL).read_text(encoding="utf-8")).rows
    assert [r[rr.ROW_TEXT] for r in rows if r[rr.ROW_ID] == minted] == [WORDS]


def test_requirements_md_carries_the_literal_backslashes(repo: Path, minted: str) -> None:
    view = (rl.run_dir(repo, RUN) / rl.LEDGER_VIEW_FILE).read_bytes().decode("utf-8")
    assert PATH in view
    assert "\r" not in view.replace("\r\n", "")


def test_composed_script_holds_the_row_text_with_no_raw_control_character(
    repo: Path, minted: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = rl.read_ledger(repo, rl.run_dir(repo, RUN))
    item = next(i for i in ledger.items if i.id == minted)
    assert item.text == WORDS
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    script = compose_ask_script(
        repo_root=str(repo), prompt=None, sizing_rel=handle._SIZING, run_id=RUN, session_id=None,
        wrap_stage=lambda _t: (handle._BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, carried_notes=[], **REVIEW_KW,
    )
    assert f"{minted}: use {PATH.replace(BS, BS * 2)} as the root" in script
    assert not _RAW_CONTROL.search(script)
    assert "\r" not in script


def test_a_row_holding_real_control_characters_still_round_trips_through_the_sizing(repo: Path) -> None:
    decoded = "use E:\r" + "esearch\n" + "ew as the root"
    out = rl._handler({"run_id": RUN, "verbatim": decoded}, repo_root=repo)
    assert out.get("ok"), out
    raw = (repo / REL).read_bytes()
    assert b"\r" not in raw
    rows = yaml.safe_load(raw.decode("utf-8"))[rr.REGISTER_KEY][rr.ROWS_KEY]
    assert next(r for r in rows if r[rr.ROW_ID] == out["item"])[rr.ROW_TEXT] == decoded

"""coordinator_core/ops/rulings/tests/test_rulings_index.py — "rulings.index".

Claim under test: append lands one yaml file per ruling and list reads it back; a ruling that
cannot land is refused by name in the reply, never raised. Fixtures are a bare tmp_path tree
with a ``.git`` directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.rulings import index as mod


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)
    return tmp_path / ".git"


def _r(rid="rl-one", **kw) -> dict:
    return {
        "id": rid,
        "pm_turn": "state/sizings/x.yaml R2",
        "scope": ["repo", "rulings"],
        "text": "This is a private repo.",
        **kw,
    }


def _call(common: Path, **params) -> dict:
    return mod._handler(params, common)


def test_append_then_list_round_trips(tmp_path):
    common = _repo(tmp_path)
    reply = _call(common, verb="append", ruling=_r(recorded="2026-11-21", enforcement="a/test.py"))
    assert reply == {"applied": ["rl-one"], "refused": []}
    assert (tmp_path / "state/pm-rulings/rl-one.yaml").is_file()
    listed = _call(common, verb="list")["rulings"]
    assert [x["id"] for x in listed] == ["rl-one"]
    assert listed[0]["scope"] == ["repo", "rulings"]
    assert listed[0]["enforcement"] == "a/test.py"
    assert listed[0]["status"] == "accepted"


def test_duplicate_id_is_refused_and_first_survives(tmp_path):
    common = _repo(tmp_path)
    _call(common, verb="append", ruling=_r())
    reply = _call(common, verb="append", ruling=_r(text="changed"))
    assert reply["applied"] == [] and "duplicate" in reply["refused"][0]["reason"]
    assert _call(common, verb="list")["rulings"][0]["text"] == "This is a private repo."


def test_bad_rulings_are_refused_by_name_not_raised(tmp_path):
    common = _repo(tmp_path)
    cases = [
        (_r(text="  "), "text"),
        (_r(rid="../evil"), "id"),
        (_r(status="bogus"), "status"),
        (_r(status="superseded", superseded_by="absent"), "absent"),
        (_r(superseded_by="absent"), "accepted ruling cannot name superseded_by"),
    ]
    for ruling, needle in cases:
        reply = _call(common, verb="append", ruling=ruling)
        assert reply["applied"] == [], ruling
        assert needle in reply["refused"][0]["reason"], ruling
    assert not (tmp_path / "state/pm-rulings").exists() or not list(
        (tmp_path / "state/pm-rulings").glob("*.yaml")
    )


def test_non_object_ruling_is_refused(tmp_path):
    reply = _call(_repo(tmp_path), verb="append", ruling="nope")
    assert reply["applied"] == [] and reply["refused"][0]["id"] == ""


def test_superseding_names_an_existing_id(tmp_path):
    common = _repo(tmp_path)
    _call(common, verb="append", ruling=_r("rl-new"))
    reply = _call(common, verb="append", ruling=_r("rl-old", status="superseded", superseded_by="rl-new"))
    assert reply["applied"] == ["rl-old"]
    only_old = _call(common, verb="list", status="superseded")["rulings"]
    assert [x["id"] for x in only_old] == ["rl-old"]
    assert [x["id"] for x in _call(common, verb="list", status="accepted")["rulings"]] == ["rl-new"]


def test_list_names_an_unreadable_file_instead_of_dropping_it(tmp_path):
    common = _repo(tmp_path)
    d = tmp_path / "state/pm-rulings"
    d.mkdir(parents=True)
    (d / "broken.yaml").write_text("id: [unterminated\n", encoding="utf-8")
    reply = _call(common, verb="list")
    assert reply["rulings"] == [] and reply["unreadable"][0]["id"] == "broken"


def test_unknown_verb_and_missing_root_raise(tmp_path):
    with pytest.raises(ValueError):
        _call(_repo(tmp_path), verb="delete")
    with pytest.raises(ValueError):
        mod._handler({"verb": "list"}, None)
    with pytest.raises(ValueError):
        _call(_repo(tmp_path), verb="list", status="bogus")

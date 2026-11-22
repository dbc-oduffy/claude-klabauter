"""Store behaviour: round-trip, refusals by name, no subprocess."""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.rulings import store
from coordinator_core.rulings.contract import Ruling


def _ruling(rid="r-one", **kw) -> Ruling:
    base = dict(
        id=rid,
        pm_turn="state/sizings/x.yaml R2",
        scope=("repo", "plan.prep_gate"),
        text='He said: "no hedging": verbatim',
        recorded="2026-11-21",
    )
    base.update(kw)
    return Ruling(**base)


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)


def test_load_missing_dir_is_empty(tmp_path):
    assert store.load(tmp_path) == []


def test_append_then_load_round_trips(tmp_path):
    r = _ruling(enforcement="coordinator_core/x/test_y.py")
    assert store.append(tmp_path, r) == {"applied": ["r-one"], "refused": []}
    assert (tmp_path / "state" / "pm-rulings" / "r-one.yaml").is_file()
    assert store.load(tmp_path) == [r]


def test_duplicate_id_refused_by_name(tmp_path):
    store.append(tmp_path, _ruling())
    out = store.append(tmp_path, _ruling(text="other"))
    assert out["applied"] == []
    assert out["refused"][0]["id"] == "r-one"
    assert "duplicate" in out["refused"][0]["reason"]
    assert store.load(tmp_path)[0].text != "other"


def test_superseded_by_absent_id_refused_and_present_id_accepted(tmp_path):
    out = store.append(tmp_path, _ruling("old", status="superseded", superseded_by="new"))
    assert "absent id 'new'" in out["refused"][0]["reason"]
    assert store.load(tmp_path) == []
    store.append(tmp_path, _ruling("new"))
    out = store.append(tmp_path, _ruling("old", status="superseded", superseded_by="new"))
    assert out["applied"] == ["old"]


def test_accepted_ruling_naming_superseded_by_refused(tmp_path):
    store.append(tmp_path, _ruling("new"))
    out = store.append(tmp_path, _ruling("old", superseded_by="new"))
    assert out["applied"] == [] and "accepted" in out["refused"][0]["reason"]


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", ".hidden", "a b"])
def test_unsafe_id_refused_never_raises(tmp_path, bad):
    out = store.append(tmp_path, _ruling(bad))
    assert out["applied"] == [] and out["refused"]
    assert not (tmp_path / "state").exists()


def test_empty_text_refused(tmp_path):
    out = store.append(tmp_path, _ruling(text="  "))
    assert out["refused"]


def test_malformed_file_reported_not_raised(tmp_path):
    store.append(tmp_path, _ruling())
    d = tmp_path / "state" / "pm-rulings"
    (d / "bad.yaml").write_text("id: bad\nstatus: maybe\n", encoding="utf-8")
    (d / "worse.yaml").write_text(": : [", encoding="utf-8")
    rulings, problems = store.scan(tmp_path)
    assert [r.id for r in rulings] == ["r-one"]
    assert {p["id"] for p in problems} == {"bad", "worse"}
    assert [r.id for r in store.load(tmp_path)] == ["r-one"]


def test_yaml_date_value_coerced_to_iso_string(tmp_path):
    d = tmp_path / "state" / "pm-rulings"
    d.mkdir(parents=True)
    (d / "d.yaml").write_text(
        "id: d\npm_turn: p\ntext: t\nscope: [repo]\nrecorded: 2026-11-21\n", encoding="utf-8"
    )
    (r,) = store.load(tmp_path)
    assert r.recorded == "2026-11-21" and r.status == "accepted"


def test_load_sorted_by_id(tmp_path):
    for rid in ("b", "a", "c"):
        store.append(tmp_path, _ruling(rid))
    assert [r.id for r in store.load(tmp_path)] == ["a", "b", "c"]

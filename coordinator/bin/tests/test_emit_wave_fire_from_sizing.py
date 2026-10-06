"""`emit-wave-fire --from-sizing`: one collected refusal, mint-or-reuse binding, fire hold."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

_BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("emit_wave_fire_fs", _BIN / "emit-wave-fire.py")
ewf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ewf)

SIZING_REL = "state/sizings/s1.yaml"
BATON_REL = "state/handoffs/b1.md"
BATON = (
    "---\nhandoff_id: hnd-1\ntitle: Minted baton\nstatus: open\n---\n\n# body\n"
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-install"))
    monkeypatch.setattr(
        ewf, "_bind",
        lambda script_path, args, live=False: "const args = " + json.dumps(args) + ";\n",
    )


def _plugin(tmp_path):
    root = tmp_path / "doctrine" / "coordinator"
    (root / "workflows").mkdir(parents=True, exist_ok=True)
    (root / "workflows" / "plan-blitz.mjs").write_text("// stub\n", encoding="utf-8")
    (root / "agents").mkdir(exist_ok=True)
    return root


def _sizing(tshirt="M", **over):
    body = {
        "schema": "sizing-object",
        "status": "sized",
        "premise": {"provenance": "unrecorded"},
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "appetite": "medium",
        "estimate": {"tshirt": tshirt, "provisional": True},
        "route": "plan",
        "interaction_mode": "hands-on",
        "intent": "do the thing",
        "exit_criterion": {
            "statement": "it works",
            "accepted": {"pm_quote": "ok", "on": "2026-10-01", "mode": "hands-on"},
        },
    }
    body.update(over)
    return {k: v for k, v in body.items() if not (k in over and v is None)}


def _setup(tmp_path, sizing, baton=BATON):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(sizing), encoding="utf-8")
    if baton is not None:
        (tmp_path / "state" / "handoffs").mkdir(parents=True)
        (tmp_path / BATON_REL).write_text(baton, encoding="utf-8")
    (tmp_path / "trail").mkdir()
    (tmp_path / ".git").mkdir()


def _fire(tmp_path):
    return ewf.main([
        "--repo-root", str(tmp_path), "--trail-dir", str(tmp_path / "trail"),
        "--plugin-root", str(_plugin(tmp_path)), "--from-sizing", SIZING_REL, "--live-engine-tree",
    ])


def _stub_mint(monkeypatch, calls=None):
    def mint(sizing_rel, repo_root, **kw):
        if calls is not None:
            calls.append((sizing_rel, kw))
        return {"id": "hnd-1", "path": BATON_REL, "title": "Minted baton", "created": True}

    monkeypatch.setattr(ewf, "_load_mint", lambda: mint)


def _bound(tmp_path):
    text = (tmp_path / "trail" / "fire-0-1.mjs").read_text(encoding="utf-8")
    return json.loads(text[len("const args = "):].rstrip().rstrip(";"))["batons"][0]


def test_bare_sizing_names_every_failing_field_once(tmp_path, capsys):
    _setup(tmp_path, {"route": "plan", "estimate": {"tshirt": "S"}}, baton=None)
    assert _fire(tmp_path) == ewf.EXIT_REFUSED
    err = capsys.readouterr().err
    assert err.count("REFUSED") == 1
    for field in ("exit_criterion.statement", "exit_criterion.accepted", "interaction_mode"):
        assert field in err


def test_s_shape_with_missing_interaction_mode_names_tshirt_and_mode(tmp_path, capsys):
    _setup(tmp_path, _sizing("S", route="shape", interaction_mode=None), baton=None)
    assert _fire(tmp_path) == ewf.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "estimate.tshirt" in err and "interaction_mode" in err


def test_fire_field_and_mint_field_failures_refuse_together(tmp_path, capsys, monkeypatch):
    _stub_mint(monkeypatch)
    _setup(tmp_path, _sizing("M", interaction_mode=None, intent=None))
    assert _fire(tmp_path) == ewf.EXIT_REFUSED
    err = capsys.readouterr().err
    assert err.count("REFUSED") == 1
    assert "interaction_mode" in err and "intent" in err


def test_m_sizing_binds_the_minted_baton(tmp_path, monkeypatch):
    _stub_mint(monkeypatch)
    _setup(tmp_path, _sizing("M"))
    assert _fire(tmp_path) == ewf.EXIT_OK
    b = _bound(tmp_path)
    assert (b["id"], b["path"], b["title"]) == ("hnd-1", BATON_REL, "Minted baton")
    assert b["exitCriterion"] == "it works"
    assert b["sizingObject"] == SIZING_REL


def test_second_fire_reuses_the_same_baton(tmp_path, monkeypatch):
    _stub_mint(monkeypatch)
    _setup(tmp_path, _sizing("M"))
    assert _fire(tmp_path) == ewf.EXIT_OK
    first = _bound(tmp_path)
    assert _fire(tmp_path) == ewf.EXIT_OK
    assert _bound(tmp_path) == first
    text = (tmp_path / BATON_REL).read_text(encoding="utf-8")
    assert text.count("plan_blitz_hold_reason") == 1


def test_fire_hold_is_stamped_after_the_fire(tmp_path, monkeypatch):
    _stub_mint(monkeypatch)
    _setup(tmp_path, _sizing("L"))
    assert _fire(tmp_path) == ewf.EXIT_OK
    text = (tmp_path / BATON_REL).read_text(encoding="utf-8")
    assert 'plan_blitz_hold_reason: "plan-blitz fire in flight"' in text
    assert "plan_blitz_hold_cite: " in text and "fire-0-1.mjs.emitted.json" in text


def test_human_hold_refuses_and_stays_byte_identical(tmp_path, monkeypatch, capsys):
    _stub_mint(monkeypatch)
    held = BATON.replace("status: open\n", "status: open\nplan_blitz_hold_reason: PM said wait\n")
    _setup(tmp_path, _sizing("M"), baton=held)
    assert _fire(tmp_path) == ewf.EXIT_REFUSED
    assert "PM said wait" in capsys.readouterr().err
    assert (tmp_path / BATON_REL).read_text(encoding="utf-8") == held
    assert not (tmp_path / "trail" / "fire-0-1.mjs").exists()


def test_hold_write_failure_never_fails_the_emit(tmp_path, monkeypatch, capsys):
    _stub_mint(monkeypatch)
    _setup(tmp_path, _sizing("M"))
    import coordinator_core.locked_write as lw

    def boom(*a, **k):
        raise RuntimeError("lock exploded")

    monkeypatch.setattr(lw, "locked_rmw", boom)
    assert _fire(tmp_path) == ewf.EXIT_OK
    assert (tmp_path / "trail" / "fire-0-1.mjs").is_file()
    assert "lock exploded" in capsys.readouterr().err


@pytest.mark.parametrize("tshirt", ["XS", "S", None])
def test_a_sub_m_sizing_is_refused_and_never_mints(tmp_path, monkeypatch, capsys, tshirt):
    def no_mint():
        raise AssertionError("a sub-M sizing must not mint")

    monkeypatch.setattr(ewf, "_load_mint", no_mint)
    _setup(tmp_path, _sizing(tshirt), baton=BATON)
    assert _fire(tmp_path) == ewf.EXIT_REFUSED
    assert "only an M+ sizing mints a baton" in capsys.readouterr().err
    assert not (tmp_path / "trail" / "fire-0-1.mjs").exists()


def test_real_mint_binds_and_holds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _setup(tmp_path, _sizing("M", name="Real mint case"), baton=None)
    assert _fire(tmp_path) == ewf.EXIT_OK
    b = _bound(tmp_path)
    assert b["path"].startswith("state/handoffs/")
    assert 'plan_blitz_hold_reason: "plan-blitz fire in flight"' in (
        tmp_path / b["path"]
    ).read_text(encoding="utf-8")
    assert yaml.safe_load((tmp_path / SIZING_REL).read_text(encoding="utf-8"))["baton"] == b["path"]
    assert _fire(tmp_path) == ewf.EXIT_OK
    assert _bound(tmp_path)["id"] == b["id"]


def _real_fire_baton_text(tmp_path, monkeypatch, sizing):
    monkeypatch.chdir(tmp_path)
    _setup(tmp_path, sizing, baton=None)
    assert ewf.main([
        "--repo-root", str(tmp_path), "--trail-dir", str(tmp_path / "trail"),
        "--plugin-root", str(_plugin(tmp_path)), "--live-engine-tree",
        "--from-sizing", str(tmp_path / SIZING_REL),
    ]) == ewf.EXIT_OK
    return (tmp_path / _bound(tmp_path)["path"]).read_text(encoding="utf-8")


def _full_sizing(tmp_path):
    return _sizing(
        "M", name="Complete baton case", plan="docs/plans/p.md",
        intent=f"touch {tmp_path}/coordinator/x.py and {str(tmp_path).replace('/', chr(92))}\\y.py",
        premise={"provenance": "unrecorded", "evidence": f"seen in {tmp_path}/a.py"},
    )


def test_emitted_baton_carries_no_absolute_paths(tmp_path, monkeypatch):
    text = _real_fire_baton_text(tmp_path, monkeypatch, _full_sizing(tmp_path))
    assert str(tmp_path) not in text
    assert str(tmp_path).replace("\\", "/") not in text
    assert "sizing_object: \"state/sizings/s1.yaml\"" in text


def test_emitted_baton_is_complete_and_schema_valid(tmp_path, monkeypatch):
    text = _real_fire_baton_text(tmp_path, monkeypatch, _full_sizing(tmp_path))
    mod = ewf._load_mint.__globals__["sys"].modules["coordinator_doc_new_for_fire"]
    mod._assert_scaffold_content_valid(text, str(tmp_path / "state/handoffs/x.md"), str(tmp_path))
    assert 'governing_plan: "docs/plans/p.md"' in text
    assert "- Estimate: M" in text
    assert "- Interaction mode: hands-on" in text
    assert "- Exit criterion: it works" in text
    assert "- Plan: docs/plans/p.md" in text
    assert "1. Read the plan at docs/plans/p.md." in text
    assert "Verify the exit criterion: it works" in text
    assert "3-7 numbered steps" not in text


EXISTING = (
    "---\nhandoff_id: hnd-existing\ntitle: Existing baton\nstatus: open\n"
    "deliverable_id: dlv-aaa\n---\n\n# body\n"
)


def _fire_with(tmp_path, *extra):
    return ewf.main([
        "--repo-root", str(tmp_path), "--trail-dir", str(tmp_path / "trail"),
        "--plugin-root", str(_plugin(tmp_path)), "--from-sizing", SIZING_REL,
        "--live-engine-tree", *extra,
    ])


def test_baton_and_deliverable_id_are_forwarded_to_the_mint(tmp_path, monkeypatch):
    calls = []
    _stub_mint(monkeypatch, calls)
    _setup(tmp_path, _sizing("M"))
    assert _fire_with(tmp_path, "--baton", BATON_REL, "--deliverable-id", "dlv-aaa") == ewf.EXIT_OK
    assert calls == [(SIZING_REL, {"baton": BATON_REL, "deliverable_id": "dlv-aaa"})]


@pytest.mark.parametrize("flag,value", [("--baton", BATON_REL), ("--deliverable-id", "dlv-aaa")])
def test_baton_flags_without_from_sizing_are_refused(tmp_path, capsys, flag, value):
    rc = ewf.main([
        "--repo-root", str(tmp_path), "--trail-dir", str(tmp_path / "trail"),
        flag, value,
    ])
    assert rc == ewf.EXIT_REFUSED
    assert "--from-sizing" in capsys.readouterr().err


def test_null_acceptance_names_both_accept_forms(tmp_path):
    sizing = _sizing("M")
    sizing["exit_criterion"]["accepted"] = None
    msg = " ".join(ewf._collect_sizing_refusals(sizing))
    assert "--pm-quote" in msg and "--apm-ruling" in msg


def test_existing_baton_is_fired_without_minting_a_new_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _setup(tmp_path, _sizing("M"), baton=EXISTING)
    before = sorted(p.name for p in (tmp_path / "state" / "handoffs").iterdir())
    assert _fire_with(tmp_path, "--baton", BATON_REL, "--deliverable-id", "dlv-aaa") == ewf.EXIT_OK
    assert _bound(tmp_path)["id"] == "hnd-existing"
    assert sorted(p.name for p in (tmp_path / "state" / "handoffs").iterdir()) == before
    sz = yaml.safe_load((tmp_path / SIZING_REL).read_text(encoding="utf-8"))
    assert sz["baton"] == BATON_REL and sz["deliverable_id"] == "dlv-aaa"


def test_deliverable_mismatch_refuses_naming_both_ids(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _setup(tmp_path, _sizing("M"), baton=EXISTING)
    assert _fire_with(tmp_path, "--baton", BATON_REL, "--deliverable-id", "dlv-zzz") == ewf.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "dlv-aaa" in err and "dlv-zzz" in err
    assert not (tmp_path / "trail" / "fire-0-1.mjs").exists()

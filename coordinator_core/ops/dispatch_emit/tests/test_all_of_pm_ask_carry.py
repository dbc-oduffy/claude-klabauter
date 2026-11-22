"""`all_of_pm_ask` reads one value across an ask run and the warp run that continues it, and a
`pm_verbatims` row forces it true."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

ASK = "ask-20261121T164624-926dfb"
WARP = "warp-2026-11-21-casper-proof-926dfb-20261121T164930-af8fd9"
OLDER = "warp-2026-11-21-casper-proof-926dfb-20261121T160000-aaaaaa"
REL = "state/sizings/2026-11/2026-11-21-carry.yaml"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _dir(repo: Path, run_id: str) -> Path:
    return repo / RECORD_DIR_ROOT / run_id


def _flag(repo: Path, run_id: str) -> bool:
    return bool(rl.read_run(_dir(repo, run_id))["all_of_pm_ask"])


def _view_flag(repo: Path, run_id: str) -> str:
    text = (_dir(repo, run_id) / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")
    return next(line for line in text.splitlines() if "(all_of_pm_ask)" in line).rsplit(": ", 1)[1]


def _reads(repo: Path, run_id: str, expected: bool) -> None:
    ledger = rl.read_ledger(repo, _dir(repo, run_id))
    assert ledger.all_of_pm_ask is expected
    assert _flag(repo, run_id) is expected
    assert _view_flag(repo, run_id) == ("yes" if expected else "no")


def _emit_warp(repo: Path, **kw) -> None:
    rl.start(repo, WARP, chain_parent=ASK, **kw)


def test_revise_yes_before_emit_reaches_the_warp_run(repo: Path) -> None:
    rl.start(repo, ASK)
    rl.revise(repo, ASK, all_of_pm_ask=True)
    _emit_warp(repo)
    _reads(repo, ASK, True)
    _reads(repo, WARP, True)


def test_revise_false_before_emit_is_not_undone_by_a_stale_true_on_the_ask(repo: Path) -> None:
    rl.start(repo, ASK, all_of_pm_ask=True)
    rl.revise(repo, ASK, all_of_pm_ask=False)
    _emit_warp(repo)
    _reads(repo, WARP, False)


def test_revise_false_after_emit_reaches_both_where_the_warp_says_true(repo: Path) -> None:
    rl.start(repo, ASK)
    _emit_warp(repo, all_of_pm_ask=True)
    _reads(repo, WARP, True)
    rl.revise(repo, ASK, all_of_pm_ask=False)
    _reads(repo, WARP, False)
    _reads(repo, ASK, False)


def test_revise_yes_after_emit_reaches_the_warp_run_read_through(repo: Path) -> None:
    rl.start(repo, ASK)
    _emit_warp(repo)
    rl.revise(repo, ASK, all_of_pm_ask=True)
    assert rl.read_ledger(repo, _dir(repo, WARP)).all_of_pm_ask is True
    rl._write_view(repo, _dir(repo, WARP), WARP)
    _reads(repo, WARP, True)
    _reads(repo, ASK, True)


def test_revise_on_the_warp_run_mirrors_to_the_ask_run(repo: Path) -> None:
    rl.start(repo, ASK, all_of_pm_ask=True)
    _emit_warp(repo)
    rl.revise(repo, WARP, all_of_pm_ask=False)
    _reads(repo, WARP, False)
    _reads(repo, ASK, False)


def test_a_legacy_ask_true_is_not_dropped_by_the_warp_emit(repo: Path) -> None:
    rl._write_run(_dir(repo, ASK), {"all_of_pm_ask": True, "sizing_rel": None})
    _emit_warp(repo)
    _reads(repo, WARP, True)


def test_a_resumed_run_reads_its_predecessors_explicit_set(repo: Path) -> None:
    rl.start(repo, OLDER)
    rl.revise(repo, OLDER, all_of_pm_ask=True)
    rl.start(repo, WARP, resumed_from=OLDER)
    _reads(repo, WARP, True)


def test_a_predecessor_cycle_terminates(repo: Path) -> None:
    rl.start(repo, ASK, resumed_from=WARP)
    rl.start(repo, WARP, resumed_from=ASK)
    rl.revise(repo, WARP, all_of_pm_ask=True)
    _reads(repo, ASK, True)


def _sizing_with_pm_words(repo: Path) -> None:
    doc = {
        "schema": "sizing-object", "intent": "carry the flag", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "my asks should always be included in full"}],
    }
    (repo / REL).parent.mkdir(parents=True)
    (repo / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def test_a_pm_verbatim_row_forces_the_flag_true_on_the_warp_run(repo: Path) -> None:
    _sizing_with_pm_words(repo)
    rl.start(repo, ASK)
    _emit_warp(repo)
    rl.bind(repo, WARP, REL)
    _reads(repo, WARP, True)


def test_pm_words_win_over_an_explicit_false_on_every_hop(repo: Path) -> None:
    _sizing_with_pm_words(repo)
    rl.start(repo, ASK, all_of_pm_ask=True)
    rl.revise(repo, ASK, all_of_pm_ask=False)
    _emit_warp(repo)
    rl.bind(repo, WARP, REL)
    rl.revise(repo, WARP, all_of_pm_ask=False)
    _reads(repo, WARP, True)
    rl.start(repo, OLDER, resumed_from=WARP)
    rl.bind(repo, OLDER, REL)
    _reads(repo, OLDER, True)


def test_a_run_without_pm_words_stays_false(repo: Path) -> None:
    rl.start(repo, ASK)
    _emit_warp(repo)
    _reads(repo, WARP, False)


def test_the_9d0c1c_witness_pm_origin_r1_never_reads_no_in_the_ledger_view(repo: Path) -> None:
    _sizing_with_pm_words(repo)
    rl.start(repo, WARP)
    rl.bind(repo, WARP, REL)
    text = (_dir(repo, WARP) / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")
    assert "## R1 (pm_verbatims" in text
    assert _view_flag(repo, WARP) == "yes"


def _emit_ask(repo: Path, source: str, run_id: str = ASK) -> None:
    from coordinator_core.ops.dispatch_emit import op

    ctx = {"root": repo, "run_id": run_id, "sizing_rel": None, "ask_source": source}
    op._start_requirements(ctx, all_of_pm_ask=False)


def test_a_fresh_pm_sourced_ask_mints_the_flag_true_and_the_warp_run_reads_it(repo: Path) -> None:
    _emit_ask(repo, "pm")
    _reads(repo, ASK, True)
    _emit_warp(repo)
    _reads(repo, WARP, True)


def test_a_fresh_relayed_ask_still_mints_the_flag_false(repo: Path) -> None:
    _emit_ask(repo, "relayed")
    _reads(repo, ASK, False)


RULING = "drop the carry requirement, it is out of scope"


def _ruling_row() -> dict:
    return {
        rr.ROW_ID: "R9", rr.ROW_TEXT: RULING, rr.ROW_ANCHOR: "pm_verbatims turn 2", rr.ROW_SURFACE: rl.ROW_SURFACE,
        rr.ROW_STATUS: rr.STATUS_DEFERRED, rr.ROW_CLAIMED_BY: [],
        rr.ROW_RULING: {rr.RULING_SOURCE: "pm", rr.RULING_QUOTE: RULING, rr.RULING_REF: "T2", rr.RULING_ON: "2026-11-21"},
    }


def _bound_pm_run(repo: Path) -> None:
    _sizing_with_pm_words(repo)
    rl.start(repo, WARP)
    rl.bind(repo, WARP, REL)


def test_out_of_scope_on_a_pm_row_without_a_pm_ruling_is_refused_naming_the_row(repo: Path) -> None:
    _bound_pm_run(repo)
    out = rl._handler({"run_id": WARP, "verbatim": RULING, "out_of_scope": ["R1"]}, repo_root=repo)
    assert not out.get("ok")
    assert "R1" in str(out) and "no PM verbatim ruling" in str(out)
    row = next(r for r in rr.read_register((repo / REL).read_text(encoding="utf-8")).rows if r["id"] == "R1")
    assert row["status"] != rr.STATUS_DEFERRED


def test_out_of_scope_on_a_pm_row_with_a_pm_ruling_verbatim_is_accepted(repo: Path) -> None:
    _bound_pm_run(repo)
    rl._mutate_register(repo, REL, lambda rows: [*rows, _ruling_row()])
    out = rl._handler({"run_id": WARP, "verbatim": RULING, "out_of_scope": ["R1"]}, repo_root=repo)
    assert out["ok"] and out["withheld"] == ["R1"]

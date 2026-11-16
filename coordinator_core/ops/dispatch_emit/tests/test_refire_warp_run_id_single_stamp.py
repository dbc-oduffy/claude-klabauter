"""A refire of a halted warp run whose id already ends -YYYYMMDDTHHMMSS-xxxxxx keeps one stamp, and the
emit reply's run_id is the id the emitted script carries."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.tests.test_refire_resumes_halted_run import (  # noqa: F401
    REL as FIXTURE_REL,
    _emit_by,
    repo,
)

pytestmark = pytest.mark.complete

STEM = "2026-11-14-extract-roi-decode-memory-da0987"
STAMP = "20261114T111656-1c9aeb"
HALTED = f"warp-{STEM}-{STAMP}"
SIZING = f"state/sizings/{STEM}.yaml"
ONE_STAMP = re.compile(rf"^warp-{STEM}-\d{{8}}T\d{{6}}-[0-9a-f]{{6}}$")


def _halt(repo, sizing=SIZING):
    (repo / sizing).write_text((repo / FIXTURE_REL).read_text(encoding="utf-8"), encoding="utf-8")
    rl.start(repo, HALTED)
    rl.bind(repo, HALTED, sizing, ask_text="the ask")
    write_halt_marker(record_dir(repo, HALTED), halted="criterion", sizing_rel=sizing)


def _fire(repo, sizing=SIZING, **extra):
    return op._dispatch_emit({"ask": True, "sizing_path": sizing, **extra}, repo_root=repo)


def _script_run_ids(reply):
    text = open(reply["path"], encoding="utf-8").read()
    return set(re.findall(r"warp-[0-9a-z-]*?\d{8}T\d{6}-[0-9a-f]{6}(?:-\d{8}T\d{6}-[0-9a-f]{6})*", text))


def test_a_rebound_refire_reuses_the_stamped_id_verbatim_and_the_script_carries_it(repo):
    _halt(repo)
    reply = _fire(repo)
    assert reply["run_id"] == HALTED
    assert _script_run_ids(reply) == {HALTED}


def test_a_refire_whose_halted_id_is_not_rebindable_mints_one_stamp_and_the_script_agrees(repo, monkeypatch):
    _halt(repo)
    _emit_by(repo, "dead-peer", False, monkeypatch, run_id=HALTED)
    reply = _fire(repo)
    assert reply["run_id"] != HALTED
    assert ONE_STAMP.fullmatch(reply["run_id"]), reply["run_id"]
    assert _script_run_ids(reply) == {reply["run_id"]}


@pytest.mark.parametrize("carried", [f"{STEM}-{STAMP}", f"warp-{STEM}-{STAMP}", f"{STEM}-{STAMP}-{STAMP}"])
def test_a_sizing_named_by_a_stamped_or_doubly_stamped_id_mints_exactly_one_stamp(repo, carried):
    named = f"state/sizings/{carried}.yaml"
    (repo / named).write_text((repo / FIXTURE_REL).read_text(encoding="utf-8"), encoding="utf-8")
    reply = _fire(repo, named)
    assert ONE_STAMP.fullmatch(reply["run_id"]), reply["run_id"]
    assert reply["run_id"] in _script_run_ids(reply)
    assert op._unstamped_stem(named) == STEM


def test_a_halt_resume_hint_naming_the_stamped_id_reuses_it(repo):
    _halt(repo)
    reply = _fire(repo, reserved_run_id=HALTED)
    assert reply["run_id"] == HALTED
    assert _script_run_ids(reply) == {HALTED}

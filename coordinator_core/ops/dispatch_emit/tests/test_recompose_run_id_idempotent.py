"""A launcher child's emit of a halted stamped warp run replies with that run id verbatim: one stamp tail,
which `fan_launcher._isOwnRecompose` and the `child.run_id` equality both accept."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.tests.test_refire_resumes_halted_run import (  # noqa: F401
    REL as FIXTURE_REL,
    repo,
)

pytestmark = pytest.mark.complete

STEM = "2026-11-23-r3-render-normals-pass-3ca570"
STAMP = "20261123T070608-967f03"
HALTED = f"warp-{STEM}-{STAMP}"
SIZING = f"state/sizings/2026-11/{STEM}.yaml"
SESSION = "11111111-2222-4333-8444-555555555555"
ONE_STAMP = re.compile(rf"^warp-{STEM}-\d{{8}}T\d{{6}}-[0-9a-f]{{6}}$")


def _halt(repo):
    (repo / SIZING).parent.mkdir(parents=True, exist_ok=True)
    (repo / SIZING).write_text((repo / FIXTURE_REL).read_text(encoding="utf-8"), encoding="utf-8")
    rl.start(repo, HALTED)
    rl.bind(repo, HALTED, SIZING, ask_text="the ask")
    write_halt_marker(record_dir(repo, HALTED), halted="criterion", sizing_rel=SIZING)


@pytest.mark.parametrize("extra", [{}, {"reserved_run_id": HALTED}, {"preminted_run_id": HALTED}, {"ask_brief": "x"}])
def test_a_launcher_child_emit_of_a_halted_stamped_run_replies_with_its_id_verbatim(repo, extra):
    _halt(repo)
    params = {"ask": True, "sizing_path": SIZING, "target_root": str(repo), "session_id": SESSION, "launcher_child": True, **extra}
    reply = op._dispatch_emit(params, repo_root=repo)
    assert reply["run_id"] == HALTED
    assert ONE_STAMP.fullmatch(reply["run_id"])


def test_a_chain_hop_off_the_halted_run_mints_one_fresh_stamp(repo):
    _halt(repo)
    params = {"ask": True, "sizing_path": SIZING, "target_root": str(repo), "session_id": SESSION,
              "launcher_child": True, "chain_parent": HALTED}
    reply = op._dispatch_emit(params, repo_root=repo)
    assert reply["run_id"] != HALTED
    assert ONE_STAMP.fullmatch(reply["run_id"]), reply["run_id"]

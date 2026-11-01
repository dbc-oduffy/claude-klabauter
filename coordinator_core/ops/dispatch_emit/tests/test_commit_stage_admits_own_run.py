"""The commit stage admits its own run: the four refusal shapes that halted finished, reviewed runs.

Reproduction map (each case in a tmp_path git repo):
- case a, params file lost on a `carry_prior` re-mint: dc1283 ("terminal_commit params file ... was
  never written"); a refire re-mints the run id, `mint` unlinks the params file, and the no-prompt
  terminal commit finds nothing to read though the held token still admits.
- case b, held token lost across re-mints and removers: d9c5e3 ("params.run_token does not match a
  live, unspent token"); the run holds its first token, the retired receipt is a single slot and
  each re-mint or remover overwrites it, so the halted-record commit refuses `run-token`.
- case c, spent receipt erased by a re-mint: a run whose product already sits in HEAD is told
  `already_landed`, not `run-token`.
- case d, warm restart: tokens live in the git common dir, so a fresh import of `run_token` still
  admits (guards the refuted restart-orphans-memory hypothesis; passes at base by design).
"""

from __future__ import annotations

import importlib
import json
import os

import pytest

from coordinator_core.ops.dispatch_emit import abandoned_runs, run_token
from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as channel
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM
from coordinator_core.ops.dispatch_emit.tests.test_abandoned_runs import (  # noqa: F401
    FOO_OLD,
    _record,
    _reap,
    bare,
)
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    REASON,
    RUN,
    _call,
    _git,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SCRIPT = "run.mjs"


def _assert_admitted(out: dict) -> None:
    assert out.get("refused") != "run-token", out
    assert not out.get("error"), out


def test_case_a_carry_prior_remint_keeps_the_params_file_the_held_token_needs(repo):
    first = run_token.mint(repo, RUN, SCRIPT)
    params_file = channel.params_path(repo, RUN)
    assert channel.materialize(params_file, {"run_id": RUN, "run_token": first, "script_path": SCRIPT})

    run_token.mint(repo, RUN, SCRIPT, carry_prior=True)

    assert params_file.is_file(), "the terminal commit's params file was unlinked by the re-mint"
    held = json.loads(params_file.read_text(encoding="ascii"))["run_token"]
    assert run_token.admits(repo, RUN, held, SCRIPT)
    assert run_token.redeem(repo, RUN, held, SCRIPT)


@pytest.mark.parametrize("remover", ["none", "consume", "discard"])
def test_case_b_first_held_token_survives_two_remints_and_a_remover(repo, remover):
    first = run_token.mint(repo, RUN, SCRIPT)
    run_token.mint(repo, RUN, SCRIPT)
    run_token.mint(repo, RUN, SCRIPT)
    if remover == "consume":
        run_token.consume(repo, RUN)
    elif remover == "discard":
        run_token.discard(repo, RUN)
    _write_record(repo, RUN)

    assert run_token.halt_admits(repo, RUN, first, SCRIPT)
    _assert_admitted(_call(repo, run_token=first))


def test_case_b_idle_reaper_leaves_the_first_held_token_admitted(bare):
    first = run_token.mint(bare, FOO_OLD, SCRIPT, session_id="s")
    run_token.mint(bare, FOO_OLD, SCRIPT, session_id="s")
    run_token.mint(bare, FOO_OLD, SCRIPT, session_id="s")
    idle = abandoned_runs.TOKENLESS_QUIET_SECONDS + 600
    _record(bare, FOO_OLD, age=idle)
    live = run_token._record_path(bare, FOO_OLD)
    record = json.loads(live.read_text(encoding="utf-8"))
    record["minted_at"] -= idle
    live.write_text(json.dumps(record), encoding="utf-8")
    os.utime(live, (record["minted_at"],) * 2)
    assert _reap(bare)["reaped"] == [FOO_OLD]
    assert not run_token.is_live(bare, FOO_OLD)
    (bare / SCRIPT).write_text("// script\n", encoding="utf-8")
    _write_record(bare, FOO_OLD)

    assert run_token.halt_admits(bare, FOO_OLD, first, SCRIPT)
    params = {
        "script_path": SCRIPT, "run_id": FOO_OLD, "run_token": first,
        HALTED_RECORD_PARAM: {"halted": "refusal", "reason": REASON},
    }
    from coordinator_core.ops.dispatch_emit import terminal_commit

    _assert_admitted(terminal_commit._handler(params, repo_root=bare / ".git"))


def test_case_c_product_already_in_head_is_answered_already_landed_after_a_remint(repo):
    token = run_token.mint(repo, RUN, SCRIPT)
    assert run_token.redeem(repo, RUN, token, SCRIPT)
    head = _git(["rev-parse", "HEAD"], repo).strip()
    run_token.consume(repo, RUN, landed={"sha": head, "paths": ["product.py"]})
    run_token.mint(repo, RUN, SCRIPT)
    _write_record(repo, RUN)

    out = _call(repo, run_token=token)

    _assert_admitted(out)
    assert out.get("already_landed") is True and out.get("sha") == head
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


def test_case_d_token_admits_after_a_warm_restart(repo):
    token = run_token.mint(repo, RUN, SCRIPT)
    try:
        reloaded = importlib.reload(run_token)
        assert reloaded.admits(repo, RUN, token, SCRIPT)
        assert reloaded.redeem(repo, RUN, token, SCRIPT)
    finally:
        importlib.reload(run_token)

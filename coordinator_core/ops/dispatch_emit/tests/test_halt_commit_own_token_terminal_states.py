"""A halted run's record-only commit admits the run's own token in every terminal state, at any age,
and still refuses another run's token, a forged one and a different script."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    OTHER,
    RUN,
    _call,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SCRIPT = "run.mjs"


def _age(repo, **kind) -> None:
    """Rewrite the record's `minted_at` to past `TTL_SECONDS`."""
    path = run_token._record_path(repo, RUN, **kind)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["minted_at"] = time.time() - 2 * run_token.TTL_SECONDS
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")


def _admitted(out: dict) -> bool:
    return out.get("refused") != "run-token" and not (out.get("error") and "run_token" in out["error"])


def test_a_live_token_is_admitted(repo):
    _write_record(repo, RUN)
    token = run_token.mint(repo, RUN, SCRIPT)

    assert _admitted(_call(repo, run_token=token))


def test_a_claimed_token_is_admitted(repo):
    _write_record(repo, RUN)
    token = run_token.mint(repo, RUN, SCRIPT)
    assert run_token.redeem(repo, RUN, token, SCRIPT)

    assert _admitted(_call(repo, run_token=token))


def test_a_retired_token_is_admitted(repo):
    _write_record(repo, RUN)
    token = run_token.mint(repo, RUN, SCRIPT)
    run_token.consume(repo, RUN)

    assert _admitted(_call(repo, run_token=token))


def test_a_spent_token_is_admitted_even_when_aged(repo):
    _write_record(repo, RUN)
    token = run_token.mint(repo, RUN, SCRIPT)
    assert run_token.redeem(repo, RUN, token, SCRIPT)
    run_token.consume(repo, RUN, landed={"sha": "f" * 40, "paths": ["product.py"]})
    _age(repo, spent=True)

    assert not run_token.halt_admits(repo, RUN, token, SCRIPT)
    assert _admitted(_call(repo, run_token=token))


@pytest.mark.parametrize("kind", [{}, {"claimed": True}, {"retired": True}], ids=["live", "claimed", "retired"])
def test_a_token_aged_past_ttl_is_admitted(repo, kind):
    _write_record(repo, RUN)
    token = run_token.mint(repo, RUN, SCRIPT)
    if kind.get("claimed"):
        assert run_token.redeem(repo, RUN, token, SCRIPT)
    elif kind.get("retired"):
        run_token.consume(repo, RUN)
    _age(repo, **kind)

    assert not run_token.halt_admits(repo, RUN, token, SCRIPT)
    assert _admitted(_call(repo, run_token=token))


def test_a_token_held_only_by_the_halted_landing_receipt_is_admitted(repo):
    _write_record(repo, RUN)
    token = run_token.mint(repo, RUN, SCRIPT)
    assert _call(repo, run_token=token)["committed"] is True
    assert run_token._record_path(repo, RUN, halted=True).is_file()
    _age(repo, halted=True)
    run_token._record_path(repo, RUN).unlink()

    assert not run_token.halt_admits(repo, RUN, token, SCRIPT)
    assert _admitted(_call(repo, run_token=token))


def test_a_token_held_only_by_its_escrow_is_admitted(repo):
    _write_record(repo, RUN)
    token = run_token.mint(repo, RUN, SCRIPT)
    assert run_token.escrow(repo, RUN, token)
    run_token._record_path(repo, RUN).unlink()

    assert not run_token.halt_admits(repo, RUN, token, SCRIPT)
    assert _admitted(_call(repo, run_token=token))


def test_the_halt_gate_still_refuses_another_runs_forged_and_wrong_script_tokens(repo):
    _write_record(repo, RUN)
    own = run_token.mint(repo, RUN, SCRIPT)
    other = run_token.mint(repo, OTHER, SCRIPT)
    _age(repo)

    assert _call(repo, run_token=other)["refused"] == "run-token"
    assert _call(repo, run_token="0" * 32)["refused"] == "run-token"
    assert _call(repo, run_token=own, script_path="other.mjs")["refused"] == "run-token"
    assert not run_token.halt_own_token(repo, RUN, own, "other.mjs")


def test_an_escrow_beside_a_live_record_does_not_bypass_the_script_binding(repo):
    _write_record(repo, RUN)
    token = run_token.mint(repo, RUN, SCRIPT)
    assert run_token.escrow(repo, RUN, token)

    assert not run_token.halt_own_token(repo, RUN, token, "other.mjs")
    assert run_token.halt_own_token(repo, RUN, token, SCRIPT)


def test_an_escrow_refuses_a_token_other_than_its_own(repo):
    _write_record(repo, RUN)
    token = run_token.mint(repo, RUN, SCRIPT)
    assert run_token.escrow(repo, RUN, token)
    run_token._record_path(repo, RUN).unlink()

    assert _call(repo, run_token="1" * 32)["refused"] == "run-token"


def test_the_product_commit_gate_still_refuses_an_aged_token(repo):
    _write_record(repo, RUN)
    token = run_token.mint(repo, RUN, SCRIPT)
    _age(repo)

    assert not run_token.admits(repo, RUN, token, SCRIPT)
    assert run_token.spent_receipt(repo, RUN, token, SCRIPT) is None

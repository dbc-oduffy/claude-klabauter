"""An XS run whose product landed in a checkpoint and whose terminal commit halted re-lands through the
printed remedy line: the new run's commit lands no product and no foreign dirty path, pays its close
legs against that commit, and a second call lands nothing again."""

from __future__ import annotations

import json
import re

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import review_edits, sibling_claims
from coordinator_core.ops.dispatch_emit.ask_contract import RESUMED_RUN_ID_KEY, RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_gate import _handler as ask_gate_handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_commit_halt_refire_e2e import (  # noqa: F401
    RUN,
    SIZING,
    _commit,
    _git,
    _second_run,
    _write,
    halted,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PRODUCT = "a.py"
FOREIGN = "coordinator/tests/test_foreign_shape.py"
FOREIGN_BYTES = "def test_a():\n    assert True\n# in-flight edit of a concurrent run\n"


def _make_xs(root) -> None:
    doc = yaml.safe_load((root / SIZING).read_text(encoding="utf-8"))
    doc.update(route="dispatch", estimate={"tshirt": "XS", "provisional": True})
    (root / SIZING).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    stem = SIZING.rsplit("/", 1)[-1].removesuffix(".yaml")
    run = f"warp-{stem}-20261021T191648-4195de"
    _write(root, f"{RUN_DIR_ROOT}/{run}/manifest.json",
           json.dumps({"rows": [{"id": "X1", "writes": [PRODUCT]}]}))


def _printed_params() -> dict:
    line = sibling_claims.reland_remedy(SIZING)
    args = json.loads(re.search(r"args: (\{.*?\})\}\)", line).group(1))
    return {"sizing_path": args["sizing"]}


def _changed(root, sha: str) -> set:
    return set(_git(root, "show", "--name-only", "--format=", sha).split())


def _shas(out: dict) -> list:
    return [out["sha"]] + ([out["process_sha"]] if out.get("process_sha") else [])


def test_the_printed_line_relands_a_checkpoint_landed_xs_run_and_pays_the_close_legs(halted, monkeypatch):
    _make_xs(halted)
    _git(halted, "add", SIZING)
    _git(halted, "commit", "-q", "-m", "sizing: xs")
    _write(halted, FOREIGN, "def test_a():\n    assert True\n")
    _git(halted, "add", FOREIGN)
    _git(halted, "commit", "-q", "-m", "foreign seed")
    _write(halted, FOREIGN, FOREIGN_BYTES)

    verdict = ask_gate_handler(_printed_params(), repo_root=halted)
    assert verdict["halt"] is None, verdict
    inline_review = verdict["resume_ledger"]["inline_review"]
    assert inline_review[RESUMED_RUN_ID_KEY] == RUN

    monkeypatch.setattr(review_edits, "review_writes", lambda root, sid, since: {})
    out = _commit(halted, _second_run(halted), {**inline_review, "prep": {"slice_files": [FOREIGN]}})

    assert out["committed"] is True, out
    landed = set().union(*(_changed(halted, sha) for sha in _shas(out)))
    assert PRODUCT not in landed and FOREIGN not in landed, landed
    assert (halted / FOREIGN).read_text(encoding="utf-8") == FOREIGN_BYTES
    assert FOREIGN in _git(halted, "status", "--porcelain")
    legs = out["close"]["legs"]
    assert legs["sizing-ship"]["status"] == "ran", legs
    assert legs["completion-entry"]["status"] == "ran", legs
    assert legs["baton-ship"]["status"] in {"ran", "queued", "not-applicable"}, legs
    assert out["sizing_shipped"]["sha"] == out["sha"]
    assert out["no_delta"] == [PRODUCT]


def test_a_second_call_lands_nothing_again(halted, monkeypatch):
    _make_xs(halted)
    verdict = ask_gate_handler(_printed_params(), repo_root=halted)
    inline_review = verdict["resume_ledger"]["inline_review"]
    monkeypatch.setattr(review_edits, "review_writes", lambda root, sid, since: {})
    first = _commit(halted, _second_run(halted), inline_review)
    assert first["committed"] is True, first
    head = _git(halted, "rev-parse", "HEAD").strip()

    second = _commit(halted, _second_run(halted), inline_review)

    assert _git(halted, "rev-parse", "HEAD").strip() == head, second

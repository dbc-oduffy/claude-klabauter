"""The XS re-land line `sibling_claims.reland_remedy` prints carries no `writes`; `dispatch.ask_gate`
fills them from the halted run's recorded `manifest.json` X1 row, so the line is accepted as printed."""

from __future__ import annotations

import json
import re
import shlex

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_gate, sibling_claims
from coordinator_core.ops.dispatch_emit.ask_contract import RESUMED_RUN_ID_KEY, RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_commit_halt_refire_e2e import (  # noqa: F401
    RUN,
    SIZING,
    _write,
    halted,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

XS_WRITES = ["a.py", "docs/note.md"]
XS_REFUSAL = "XS needs `writes`"


def _make_xs(root, *, record_writes: bool) -> None:
    doc = yaml.safe_load((root / SIZING).read_text(encoding="utf-8"))
    doc.update(route="dispatch", estimate={"tshirt": "XS", "provisional": True})
    (root / SIZING).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    if record_writes:
        stem = SIZING.rsplit("/", 1)[-1].removesuffix(".yaml")
        run = f"warp-{stem}-20261021T191648-4195de"
        rows = [{"id": "X1", "writes": XS_WRITES}]
        _write(root, f"{RUN_DIR_ROOT}/{run}/manifest.json", json.dumps({"rows": rows}))


def _printed_params(sizing: str) -> dict:
    line = sibling_claims.reland_remedy(sizing)
    quoted = re.search(r"dispatch\.ask_gate\s+('.*?')\s+--", line).group(1)
    return json.loads(shlex.split(quoted)[0])


def test_the_printed_xs_reland_line_resumes_the_commit_halt_without_writes(halted):
    _make_xs(halted, record_writes=True)
    params = _printed_params(SIZING)
    assert "writes" not in params

    out = ask_gate._handler(params, repo_root=halted)

    assert out["halt"] is None, out
    assert out["arm"] == "xs"
    assert out["resume_ledger"]["inline_review"][RESUMED_RUN_ID_KEY] == RUN


def test_explicit_writes_win_over_the_recorded_footprint(halted):
    _make_xs(halted, record_writes=True)
    seen: list = []
    real = ask_gate.collect_fire_refusals

    def spy(sizing, **kw):
        seen.append(list(kw["writes"]))
        return real(sizing, **kw)

    ask_gate.collect_fire_refusals = spy
    try:
        ask_gate._handler({"sizing_path": SIZING, "writes": ["z.py"]}, repo_root=halted)
    finally:
        ask_gate.collect_fire_refusals = real
    assert seen == [["z.py"]]


def test_no_recorded_manifest_still_refuses_xs_needs_writes(halted):
    _make_xs(halted, record_writes=False)
    out = ask_gate._handler({"sizing_path": SIZING}, repo_root=halted)
    assert XS_REFUSAL in out["halt"]["reason"]

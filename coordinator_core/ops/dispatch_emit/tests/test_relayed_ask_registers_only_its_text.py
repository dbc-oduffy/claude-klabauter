"""A relayed-memo ask registers only its own text; the session's PM turns are unrelated_turns."""

from __future__ import annotations

from pathlib import Path

import yaml

import coordinator_core.ops.baton_pm_turns as pm_turns
import coordinator_core.sizing_assemble as sizing_assemble
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.session import record_homes

RUN = "ask-20261014T000000-abc123"
ASK = "fix the relayed thing"
_DRAFT = """schema: sizing-object
intent: "Ship the thing"
estimate:
  tshirt: XS
  provisional: true
route: dispatch
detents: []
fork: null
xl_exit: null
status: draft
premise:
  provenance: unrecorded
  evidence: PLACEHOLDER - cite the evidence
deliverable_id: null
"""


def test_relayed_ask_registers_only_its_text(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    Path(record_homes.home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
    window = [{"turn": n, "ts": "2026-10-08T10:00:00Z", "text": f"session chatter {n}"} for n in range(44, 48)]
    monkeypatch.setattr(pm_turns, "bound_window", lambda *_a, **_k: list(window))
    path = Path(record_homes.record_path(str(tmp_path), "sizings", "2026-10-14-relayed.yaml"))
    path.write_text(_DRAFT, encoding="utf-8")
    rel = path.relative_to(tmp_path).as_posix()

    decision = sizing_assemble.route(estimate={"tshirt": "M"}, intent="Ship the thing")
    sizing_assemble.write_back(tmp_path, rel, decision, relayed_ask_text=ASK)
    rl.start(tmp_path, RUN, all_of_pm_ask=False)
    rl.bind(tmp_path, RUN, rel, relayed_text=ASK)

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    register = doc["requirement_register"]
    assert [r["source_text"] for r in register["rows"]] == [ASK]
    assert [e["turn"] for e in register["unrelated_turns"]] == [44, 45, 46, 47]
    assert [e["turn"] for e in doc["pm_verbatims"]] == [44, 45, 46, 47]
    assert rr.coverage_gaps(doc) == []
    items = rl.read_ledger(tmp_path, tmp_path / RECORD_DIR_ROOT / RUN).items
    assert [i.origin for i in items] == ["relayed"]

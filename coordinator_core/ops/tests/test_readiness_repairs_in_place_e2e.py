"""End to end: a plan pulled only for a corrected class-1 citation, a register row added by
workflow.revise, or an UNCHECKABLE anchor premise reaches `ready` after plan.apply_mechanical_repairs;
workflow.revise accepts `notes`. Every op is called through its registered handler with default params.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ipc import get_op_handler
from coordinator_core.ops import plan_apply_mechanical_repairs  # noqa: F401  (registers the op)
from coordinator_core.ops import plan_readiness_verdict  # noqa: F401  (registers the op)
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

SIZING_REL = "state/sizings/2026-10-30-e2e-fixture.yaml"
RUN_ID = "run-e2e"
OLD, NEW = "src/old/mod.py", "src/new/mod.py"
SPINE = f"""- id: C1
  title: Ship it
  body: Do the named work and pin it with a test.
  change_kind: code-edit
  surface: src/new/mod.py
  writes: [src/new/mod.py]
  reads_at_head: [{{reads}}]
  register_rows: [R1, R2]
  traces_to_brief: first ask and second requirement
  queue_scope: project
  disposition: open
"""


def _call(op: str, params: dict, root: Path) -> dict:
    return get_op_handler(op)(params, repo_root=root / ".git")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    for d in ("state/sizings", "docs/plans", "src/new", "data"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / NEW).write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "data" / "fixture.txt").write_text("marker\n", encoding="utf-8")
    doc = {
        "schema": "sizing-object", "name": "e2e fixture", "intent": "exercise the repairs",
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-e2e-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}},
    }
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _register(root: Path):
    return rr.read_register((root / SIZING_REL).read_text(encoding="utf-8"))


def _plan(root: Path, slug: str, rows: list[str], *, reads: str = NEW, anchor: bool = False) -> str:
    """Write a plan claiming `rows` of the sizing (none: no register claims at all)."""
    pins = rr.register_pins(_register(root), rows)
    claims = (
        f"sizing_object: {SIZING_REL}\n"
        f"register_claims:\n  sizing: {SIZING_REL}\n  rows: {yaml.safe_dump(rows, default_flow_style=True).strip()}\n"
        f"  pins: {yaml.safe_dump(pins, default_flow_style=True).strip()}\n"
        if rows else ""
    )
    anchors = (
        "anchor_premises:\n  - id: AP1\n    claim: the fixture file holds one marker\n"
        "    check: {command: 'grep -c marker data/fixture.txt', expect: '1'}\n"
        if anchor else "anchor_premises: []\n"
    )
    rel = f"docs/plans/{slug}.md"
    (root / rel).write_text(
        "---\ntitle: fixture\nauthor: fixture\nstatus: draft\ncreated: 2026-10-30\n"
        f"{claims}census: []\ncapabilities: []\n{anchors}"
        "prime_exit_criterion:\n  statement: done\n"
        f"  derived_from: {SIZING_REL}\n"
        "---\n\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n" + SPINE.format(reads=reads) + "```\n",
        encoding="utf-8", newline="\n",
    )
    return rel


def _add_r2(root: Path) -> None:
    rl.start(root, RUN_ID)
    rl.bind(root, RUN_ID, SIZING_REL, ask_text="first ask")
    reply = _call("workflow.revise", {"run_id": RUN_ID, "verbatim": "second requirement", "from": "peer"}, root)
    assert reply.get("ok") is True, reply
    assert [r["id"] for r in _register(root).rows] == ["R1", "R2"]


def _verdict(root: Path, rel: str, **extra) -> dict:
    return _call("plan.readiness_verdict", {"plan": rel, **extra}, root)


def _repair_and_recheck(root: Path, rel: str, pulled: dict, kind: str) -> dict:
    assert (pulled["verdict"], pulled["pull_class"]) == ("pulled", "mechanical"), pulled["reason"]
    repairs = pulled["mechanical_repairs"]
    assert kind in {r["kind"] for r in repairs}
    applied = _call("plan.apply_mechanical_repairs", {"plan": rel, "repairs": repairs}, root)
    assert applied["ok"] is True and applied["skipped"] == [], applied
    assert kind in {a["kind"] for a in applied["applied"]}
    final = _verdict(root, rel)
    assert (final["verdict"], final["pull_class"]) == ("ready", "none"), final["mechanical_repairs"]
    return applied


def test_class_1_citation_with_corrected_path_in_evidence_reaches_ready(repo):
    _add_r2(repo)
    rel = _plan(repo, "citation", ["R1", "R2"], reads=OLD)
    row = {"verdict": "UNRESOLVED", "citation": OLD, "evidence": f"not found at head; corrected: `{NEW}`"}
    pulled = _verdict(repo, rel, premise={"rows": [row]})
    _repair_and_recheck(repo, rel, pulled, "premise-citation-corrected")
    text = (repo / rel).read_text(encoding="utf-8")
    assert f"reads_at_head: [{NEW}]" in text and OLD not in text


def test_register_row_added_by_revise_is_claimed_and_pinned(repo):
    _add_r2(repo)
    rel = _plan(repo, "register", ["R1"])
    pulled = _verdict(repo, rel)
    kind = pulled["prep_gate"]["classes"]["REGISTER"]["kind"]
    assert kind in {"register-rows-unclaimed", "register-pin-missing"}
    _repair_and_recheck(repo, rel, pulled, kind)
    fm = yaml.safe_load((repo / rel).read_text(encoding="utf-8").split("---\n")[1])
    assert fm["register_claims"]["rows"] == ["R1", "R2"]
    assert fm["register_claims"]["pins"] == rr.register_pins(_register(repo), ["R1", "R2"])


@pytest.mark.parametrize(
    "claimed",
    [[], ["R1", "R2"]],
    ids=["plan-without-register", "plan-with-register"],
)
def test_uncheckable_anchor_premise_becomes_an_execution_time_falsifier_row(repo, claimed):
    _add_r2(repo)
    rel = _plan(repo, "anchor", claimed, anchor=True)
    row = {"verdict": "UNCHECKABLE", "citation": "the fixture file holds one marker", "anchor": "AP1"}
    pulled = _verdict(repo, rel, premise={"rows": [row]})
    _repair_and_recheck(repo, rel, pulled, "uncheckable-to-falsifier-row")
    block = (repo / rel).read_text(encoding="utf-8").split("```yaml plan-tasks\n")[1].split("```")[0]
    rows = yaml.safe_load(block)
    assert [r["id"] for r in rows] == ["C1", "F1"]
    assert rows[1]["performer"] == "em" and rows[1]["writes"] == []


def test_contradicted_row_without_corrected_citation_still_pulls_judgment(repo):
    _add_r2(repo)
    rel = _plan(repo, "contradicted", ["R1", "R2"])
    row = {"verdict": "CONTRADICTED", "citation": f"{NEW}:1", "evidence": "says otherwise"}
    out = _verdict(repo, rel, premise={"rows": [row]})
    assert out["verdict"] != "ready" and out["mechanical_repairs"] == []


def test_workflow_revise_accepts_notes(repo):
    _add_r2(repo)
    reply = _call("workflow.revise", {"run_id": RUN_ID, "notes": "fyi from the e2e"}, repo)
    assert reply.get("ok") is True, reply
    notes = (rl.run_dir(repo, RUN_ID) / rl.NOTES_FILE).read_text(encoding="utf-8")
    assert "fyi from the e2e" in notes

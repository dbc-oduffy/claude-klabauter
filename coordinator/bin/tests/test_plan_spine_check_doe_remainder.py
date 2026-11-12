"""plan-spine-check emits research-layout, micro-chunk-siblings and model-override undispatchable findings
as at:<class> rows and in report.prep_gate_classes; drives the real script against tmp_path plans."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "plan-spine-check.py"

_SIZING_YAML = """\
schema: sizing-object
name: s
requirement_register:
  sources:
  - doc: state/sizings/s.yaml
    ref: ask
  rows:
  - id: R1
    source_text: Do the thing.
    anchor: relayed
    surface: internal
    status: open
    claimed_by: []
  rollup:
    open: 1
    partial: 0
"""

FLAT = "docs/research/2026-11-04-s2-face-height-shortfall.md"
CONFORMING = "docs/research/s2/2026-11-04-face-height/synthesis.md"


def _row(rid, writes, *, kind="doc-edit", model="sonnet", agent="coordinator:executor", extra=""):
    return (
        f"- id: {rid}\n  title: do {rid}\n  change_kind: {kind}\n  surface: {writes[0]}\n"
        f"  writes: [{', '.join(writes)}]\n  reads_at_head: []\n  consumes: []\n  model: {model}\n"
        f"  effort: medium\n  agent_type: {agent}\n  register_rows: [R1]\n"
        f'  traces_to_brief: "do the thing"\n  acceptance: "short"\n  queue_scope: project\n'
        f"  disposition: open\n{extra}"
        "  body: |\n    Edit the file so that it parses the register and prints each row id.\n"
    )


def _plan(root: Path, *rows: str) -> Path:
    (root / ".git").mkdir(exist_ok=True)
    (root / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (root / "state" / "sizings" / "s.yaml").write_text(_SIZING_YAML, encoding="utf-8")
    (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    text = (
        '---\ntitle: t\nstatus: executing\nsizing_object: "state/sizings/s.yaml"\n'
        'register_claims:\n  sizing: "state/sizings/s.yaml"\n  rows: ["R1"]\n'
        "anchor_premises: []\n---\n\n# t\n\n## Tasks\n\n"
        f"```yaml plan-tasks\n{''.join(rows)}```\n"
    )
    plan = root / "docs" / "plans" / "p.md"
    plan.write_text(text, encoding="utf-8")
    return plan


def _run(plan: Path, *extra: str):
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *extra, str(plan)],
        capture_output=True,
        text=True,
        cwd=plan.parents[2],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _report(plan: Path) -> dict:
    return json.loads(_run(plan, "--json").stdout)["reports"][0]


def _classes(plan: Path) -> list:
    return [c["class"] for c in _report(plan).get("prep_gate_classes", [])]


def test_flat_research_write_is_flagged_with_conforming_path(tmp_path):
    report = _report(_plan(tmp_path, _row("C1", [FLAT, "a.py"], kind="code-edit")))
    hits = [c for c in report["prep_gate_classes"] if c["class"] == "research-layout"]
    assert len(hits) == 1
    assert "/2026-11-04-s2-face-height-shortfall/synthesis.md" in hits[0]["detail"]
    assert "row C1" in hits[0]["detail"]
    assert any(r["at"] == "research-layout" and r["class"] == "structural" for r in report["rows"])
    assert report["verdict"] == "INVALID"


@pytest.mark.parametrize("path", [CONFORMING, "docs/other/flat.md"])
def test_conforming_and_non_research_paths_pass(tmp_path, path):
    assert "research-layout" not in _classes(_plan(tmp_path, _row("C1", [path, "a.py"], kind="code-edit")))


def test_existing_top_level_entry_is_grandfathered(tmp_path):
    (tmp_path / "docs" / "research").mkdir(parents=True)
    (tmp_path / "docs" / "research" / "legacy-notes.md").write_text("x", encoding="utf-8")
    plan = _plan(tmp_path, _row("C1", ["docs/research/legacy-notes.md", "a.py"], kind="code-edit"))
    assert "research-layout" not in _classes(plan)


def test_subfloor_same_wave_siblings_are_flagged(tmp_path):
    plan = _plan(tmp_path, _row("C1", ["a.md"]), _row("C2", ["b.md"]))
    report = _report(plan)
    assert "micro-chunk-siblings" in [c["class"] for c in report["prep_gate_classes"]]
    assert any(r["at"] == "micro-chunk-siblings" for r in report["rows"])


def test_disjoint_parallel_siblings_are_exempt(tmp_path):
    rows = [_row(r, [f"{r}.md"], extra="  disjoint_parallel: true\n") for r in ("C1", "C2")]
    assert "micro-chunk-siblings" not in _classes(_plan(tmp_path, *rows))


def test_model_override_contradicting_agent_pin_is_refused_in_text_and_json(tmp_path):
    plan = _plan(tmp_path, _row("C1", ["a.py"], kind="code-edit", model="opus"))
    proc = _run(plan)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "agent-type-undispatchable" in proc.stdout
    assert "pinned to 'sonnet'" in proc.stdout
    assert "drop agent_model" in proc.stdout
    classes = _report(plan)["prep_gate_classes"]
    assert [c["class"] for c in classes] == ["agent-type-undispatchable"]
    assert "drop agent_model" in classes[0]["detail"]


def test_model_override_matching_agent_pin_passes(tmp_path):
    plan = _plan(tmp_path, _row("C1", ["a.py"], kind="code-edit", model="sonnet"))
    proc = _run(plan)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "prep_gate_classes" not in _report(plan)


def test_clean_plan_has_no_prep_gate_classes(tmp_path):
    assert "prep_gate_classes" not in _report(_plan(tmp_path, _row("C1", ["a.py"], kind="code-edit")))

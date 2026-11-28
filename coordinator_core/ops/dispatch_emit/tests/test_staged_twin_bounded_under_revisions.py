"""A halted run's staged twin stays bounded as revision rows accrue: the twin references the run's resume record,
requirements ledger and plan by repo-relative path and inlines none of their text."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, ask_stage, emit, halt_resume_record as hrr, halted_run, prompt_fragments
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

CAP = 524288
HEADROOM_FLOOR = 64 * 1024
GROWTH_SLACK = 4 * 1024
RUN = "warp-2026-11-24-staged-twin-aaaaaa-20261124T000000-bbbbbb"
PLAN_REL = "docs/plans/2026-11/2026-11-24-staged-twin.md"
SIZING_REL = "state/sizings/2026-11/2026-11-24-staged-twin.yaml"
RUN_REL = f"state/warp/{RUN}"
BLITZ_BYTES = 279000

PLAN_MARK = "PLANBODYMARK"
RESUME_MARK = "RESUMEBODYMARK"
REVISION_MARK = "REVISIONBODYMARK"

_STUBS = dict(
    wrap_stage=lambda _t, **_k: (
        "  async function planBlitz(args) {\n    return { ready: [] };\n  }\n  const __pad = '" + "x" * BLITZ_BYTES + "';",
        ["Size", "Plan"],
    ),
    plan_blitz_text="stub",
    roadmap_blitz_text="export const meta = { name: 'rb', phases: [{ title: 'Synthesize', detail: 'x' }] };\nreturn {};",
)


def _plan(root: Path) -> None:
    rows = "".join(
        f"- id: C{i}\n  title: t{i}\n  change_kind: doc-edit\n  surface: docs/x/C{i}.md\n  writes:\n    - docs/x/C{i}.md\n"
        f"  body: |\n    {'row body. ' * 40}\n"
        for i in range(8)
    )
    prose = (f"{PLAN_MARK} " + "plan prose sentence. " * 50 + "\n\n") * 70
    path = root / PLAN_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: p\ndeliverable_id: dlv-twin-abc123\n---\n\n# p\n\n{prose}## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
        encoding="utf-8",
        newline="\n",
    )


def _sizing(root: Path) -> None:
    doc = {
        "schema": "sizing-object", "intent": "bound the twin", "estimate": {"tshirt": "M", "provisional": False},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "bound the twin"}], "interaction_mode": "pm",
        "deliverable_id": "dlv-twin-abc123", "plan": PLAN_REL,
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-11-24", "mode": "pm"}},
    }
    path = root / SIZING_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _ledger(revisions: int) -> str:
    quote = f"{REVISION_MARK} " + "revise the staged twin so it stays small. " * 30
    rows = "".join(f"\n## R{i} (relayed)\n\n> {quote.strip()} {i}\n" for i in range(1, revisions + 1))
    return f"# Requirements -- run {RUN}\n{rows}"


def _halted_run(root: Path, revisions: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """run.json, a halt marker, a 150 KB resume record, requirements.md with `revisions` rows, and the
    launcher-child run script the production composer renders over that ledger."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    _plan(root)
    _sizing(root)
    rd = record_dir(root, RUN)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(json.dumps({"sizing_rel": SIZING_REL}), encoding="utf-8")
    (rd / "requirements.md").write_text(_ledger(revisions), encoding="utf-8")
    monkeypatch.setattr(halted_run, "_RESUME_SPAWNER", lambda r, run_id: None)
    halted_run.write_halt_marker(rd, halted="criterion", sizing_rel=SIZING_REL, run_base_sha="a" * 40)
    hrr.write(rd, {"schema": hrr.SCHEMA, "hoisted_fragments": {"f": (RESUME_MARK + " ") * 12000}, "fingerprint": {}})
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    script = ask_compose.compose_ask_script(
        repo_root=root.as_posix(), prompt=None, sizing_rel=SIZING_REL, run_id=RUN, session_id=None,
        script_path=f"scratch/warp/{RUN}.workflow.mjs", ask_source="pm", launcher_child=True, staged_out={},
        **REVIEW_KW, **_STUBS,
    )
    out = root / "scratch" / "warp" / f"{RUN}.workflow.mjs"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(script, encoding="utf-8", newline="\n")


def _staged_twin(root: Path) -> str:
    reply = ask_stage._handler({"run_id": RUN, "plan_path": PLAN_REL, "sizing_path": SIZING_REL}, repo_root=root)
    assert "error" not in reply, reply
    twin_rel = reply["twin_path"]
    assert not Path(twin_rel).is_absolute()
    return (root / twin_rel).read_text(encoding="utf-8")


@pytest.fixture
def _production_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", CAP)


def _twin_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revisions: int) -> tuple[str, int]:
    _halted_run(tmp_path, revisions, monkeypatch)
    twin = _staged_twin(tmp_path)
    return twin, len(twin.encode("utf-8"))


@pytest.mark.parametrize("revisions", [30, 60])
def test_the_twin_of_a_halted_run_keeps_the_pinned_headroom_under_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _production_cap: None, revisions: int
) -> None:
    _, size = _twin_bytes(tmp_path, monkeypatch, revisions)
    assert size <= CAP - HEADROOM_FLOOR, f"{revisions} revision rows: twin is {size} bytes, floor {CAP - HEADROOM_FLOOR}"


def test_twin_size_does_not_grow_with_further_revision_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _production_cap: None
) -> None:
    _, at_30 = _twin_bytes(tmp_path / "r30", monkeypatch, 30)
    _, at_60 = _twin_bytes(tmp_path / "r60", monkeypatch, 60)
    assert at_60 <= at_30 + GROWTH_SLACK, f"twin grew {at_60 - at_30} bytes from 30 to 60 revision rows (slack {GROWTH_SLACK})"


@pytest.mark.parametrize("revisions", [30, 60])
def test_the_twin_names_the_run_files_by_repo_relative_path_and_holds_none_of_their_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _production_cap: None, revisions: int
) -> None:
    twin, _ = _twin_bytes(tmp_path, monkeypatch, revisions)
    for rel in (f"{RUN_REL}/resume.json", f"{RUN_REL}/requirements.md", PLAN_REL):
        assert rel in twin, f"twin does not name {rel}"
    for mark in (PLAN_MARK, RESUME_MARK, REVISION_MARK):
        assert mark not in twin, f"twin inlines a body carrying {mark}"

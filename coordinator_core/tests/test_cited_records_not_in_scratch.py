"""Guard: no engine writer routes a record another artifact cites under a scratch directory."""

from __future__ import annotations

import pytest

from coordinator_core.backlog_grind_assemble import readers_blitz, readers_mise
from coordinator_core.ops.dispatch_emit import ask_compose, cli
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT, RUN_RECORD_FILES

_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
_SCRATCH_SEGMENTS = {"scratch", "tasks"}


@pytest.mark.parametrize(
    "template",
    [
        readers_blitz._DONE_SUMMARY_CONSTRAINT_TEMPLATE,
        readers_mise._DONE_SUMMARY_CONSTRAINT_TEMPLATE,
        readers_mise._MISE_VERIFIER_OUTPUT_PATH_TEMPLATE,
    ],
    ids=["blitz-done", "mise-done", "mise-verify"],
)
def test_grind_evidence_templates_live_under_queue_grind(template):
    tokens = [t.strip("`'\".,;:()") for t in template.split() if "queue-grind" in t or "evidence/" in t]
    candidates = tokens or [template]
    assert any(c.startswith("state/queue-grind/") for c in candidates), (
        f"grind evidence belongs at state/queue-grind/<profile>/evidence/<run-id>/<item-id>.<kind>.md: {template!r}"
    )
    for c in candidates:
        if c.startswith("state/queue-grind/"):
            assert not _SCRATCH_SEGMENTS & set(c.split("/")), (
                f"grind evidence belongs under state/queue-grind/, not a scratch or tasks segment: {c!r}"
            )


def test_warp_run_records_land_under_record_root_not_run_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    assert cli.main(["--ask", "add a thing", "--all-of-pm-ask", "--repo-root", str(tmp_path)]) == 0

    record_names = set(RUN_RECORD_FILES)
    in_records = [p for p in (tmp_path / RECORD_DIR_ROOT).rglob("*") if p.is_file()]
    assert in_records, f"the run wrote no record under {RECORD_DIR_ROOT}; cited records belong there"
    assert {"requirements.jsonl", "requirements.md"} <= {p.name for p in in_records}

    run_root = tmp_path / RUN_DIR_ROOT
    stray = [
        p.relative_to(tmp_path).as_posix()
        for p in (run_root.rglob("*") if run_root.exists() else [])
        if p.is_file() and (p.name in record_names or p.name.endswith(".spine.md"))
    ]
    assert not stray, f"records belong under {RECORD_DIR_ROOT}/<run-id>/, not {RUN_DIR_ROOT}: {stray}"

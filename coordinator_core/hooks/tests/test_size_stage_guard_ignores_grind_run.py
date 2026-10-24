"""The size-stage guard reaches only raw-ask runs: a grind run's spawns (no sizing, same session)
may write, an ask run's still may not, and a record minted without `kind` reads as an ask."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core import warp_live
from coordinator_core.hooks import nudge_em_code_dispatch as dispatch
from coordinator_core.hooks.tests import test_size_stage_guard_run_scoped as base
from coordinator_core.ops.dispatch_emit import run_token


def _session(tmp_path: Path, monkeypatch, *, kind: str | None):
    """base._session with the token re-minted as `kind`; None strips the field (a pre-kind record)."""
    root, session_transcript, run_dir = base._session(tmp_path, monkeypatch)
    script = str(root / "scratch" / f"{base.RUN_ID}.mjs")
    if kind is not None:
        run_token.mint(root, base.RUN_ID, script, session_id=base.SESSION, kind=kind)
    else:
        path = run_token._record_path(root, base.RUN_ID)
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("kind", None)
        path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    base._touch(run_dir / f"agent-{base.AGENT_A}.jsonl")
    return root, session_transcript, run_dir


def test_grind_token_is_not_a_size_stage_run(tmp_path, monkeypatch):
    root, session_transcript, run_dir = _session(tmp_path, monkeypatch, kind="grind")

    assert warp_live.size_stage_run(root, base.SESSION, str(run_dir)) is None
    assert base._edit(root, session_transcript, base.AGENT_A) is None


@pytest.mark.parametrize("kind", ["ask", None])
def test_ask_token_still_names_the_run(tmp_path, monkeypatch, kind):
    root, session_transcript, run_dir = _session(tmp_path, monkeypatch, kind=kind)

    assert warp_live.size_stage_run(root, base.SESSION, str(run_dir)) == base.RUN_ID
    denied = base._edit(root, session_transcript, base.AGENT_A)
    assert denied is not None
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_mint_records_kind_and_refuses_unknown(tmp_path):
    root = base._repo(tmp_path)
    run_token.mint(root, base.RUN_ID, "s.mjs", kind="grind")
    assert next(run_token.live_records(root))["kind"] == "grind"
    with pytest.raises(ValueError):
        run_token.mint(root, base.RUN_ID, "s.mjs", kind="other")

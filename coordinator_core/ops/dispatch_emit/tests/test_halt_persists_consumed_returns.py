"""Oracle: a dependents_unstarted halt persists the consumed return; the refire keeps both finished rows."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import (
    ask_compose,
    ask_gate,
    kept_work,
    land_run_halt,
    reply_sidecar,
    requirements_ledger,
    row_returns,
)
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_DEPENDENTS_UNSTARTED, HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.emit import _dispatch_report_path
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261015T000000-aaaaaa"
NEW_RUN = "ask-20261016T000000-bbbbbb"
PLAN_REL = "docs/plans/2026-10-01-gate.md"
C1_PATH = "some/C1.py"
FIGURES = "DONE: figures 960449 B"
STUB = "DONE: C2 kept from prior (report on disk)"

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"

_ROWS = (
    "- id: C1\n  title: tC1\n  change_kind: code-edit\n  surface: some/C1.py\n  disposition: open\n"
    "  writes:\n    - some/C1.py\n"
    "- id: C2\n  title: tC2\n  change_kind: code-edit\n  surface: some/C2.py\n  disposition: open\n"
    "  writes: []\n"
    "- id: C3\n  title: tC3\n  change_kind: code-edit\n  surface: some/C3.py\n  disposition: open\n"
    "  writes:\n    - some/C3.py\n"
    "  depends_on:\n"
    "    - chunk: C2\n      gate_kind: output-consumption-runtime\n      note: reads the figures\n"
    "    - chunk: C1\n      gate_kind: output-consumption-runtime\n      note: reads the file\n"
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _write(tmp_path, "seed.txt", "seed\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    _put(tmp_path, tshirt="S", route="spec-dispatch")
    _write(
        tmp_path,
        PLAN_REL,
        f"---\nrun_id: fixture\n---\n\n# keep\n\n## Tasks\n\n```yaml plan-tasks\n{_ROWS}```\n",
    )
    return tmp_path


def _marker_script(root: Path) -> None:
    built = []
    for cid, paths in {"C1": [C1_PATH], "C2": []}.items():
        report = _dispatch_report_path(PLAN_REL, cid)
        _write(root, report, f"DONE: {cid}\n")
        for p in paths:
            _write(root, p, f"{p} = 1\n")
        built.append(ChunkCommit(id=cid, title=cid, paths=tuple(paths), report=report))
    marker = render_marker(CommitRequest(chunks=tuple(built), session_id=None, repo_root=None, plan_path=None))
    _write(root, "run.mjs", f"{marker}\n")


def _halt(root: Path, run: str, consumed: dict) -> dict:
    _write(root, f"{RECORD_DIR_ROOT}/{run}/ask.md", "ask.md\n")
    _write(root, f"{RECORD_DIR_ROOT}/{run}/run.json", json.dumps({"sizing_rel": REL}) + "\n")
    halted = {"halted": HALT_DEPENDENTS_UNSTARTED, "reason": "C3 (held by C9)"}
    params = {
        "script_path": "run.mjs",
        "run_id": run,
        HALTED_RECORD_PARAM: halted,
        row_returns.CONSUMED_RETURNS_PARAM: consumed,
    }
    out = land_run_halt.land(root, root, params)
    assert out["committed"] is True, out
    return out


def _head_blob(root: Path, rel: str) -> dict:
    return json.loads(_git(root, "show", f"HEAD:{rel}"))


def _refire(root: Path) -> dict:
    reply = ask_gate._handler({"sizing_path": REL, "run_id": NEW_RUN}, repo_root=root / ".git")
    assert "manifest_handle" in reply, reply
    return reply_sidecar.read_reply(root, reply["manifest_handle"])


def _brief(root: Path, manifest: dict, rid: str) -> str:
    row = next(r for r in manifest["rows"] if r["id"] == rid)
    return (root / row["brief_path"]).read_text(encoding="utf-8")


def test_a_dependents_unstarted_halt_persists_the_return_and_the_refire_keeps_both_rows(repo):
    _marker_script(repo)
    returns_rel = row_returns.persisted_path(f"{RECORD_DIR_ROOT}/{RUN}")
    kept_rel = f"{RECORD_DIR_ROOT}/{RUN}/{kept_work.KEPT_FILE}"

    out = _halt(repo, RUN, {"C2": FIGURES})

    assert out["returns"] == ["C2"]
    names = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert returns_rel in names and kept_rel in names
    stored = _head_blob(repo, returns_rel)
    assert stored["C2"]["text"] == FIGURES
    kept = _head_blob(repo, kept_rel)
    assert set(kept["chunks"]) == {"C1", "C2"}
    assert kept["chunks"]["C2"] == {"paths": {}, "return": stored["C2"]["sha256"]}

    manifest = _refire(repo)
    rows = {r["id"]: r for r in manifest["rows"]}
    assert rows["C1"]["kept_from"] == RUN and rows["C2"]["kept_from"] == RUN
    assert not rows["C3"].get("kept_from")
    brief = _brief(repo, manifest, "C3")
    assert returns_rel in brief and 'under key "C2"' in brief

    _write(repo, f"{RECORD_DIR_ROOT}/{NEW_RUN}/manifest.json", json.dumps({"rows": manifest["rows"]}))
    _halt(repo, NEW_RUN, {"C2": STUB})

    carried = _head_blob(repo, row_returns.persisted_path(f"{RECORD_DIR_ROOT}/{NEW_RUN}"))
    assert "960449 B" in carried["C2"]["text"] and carried["C2"]["text"] != STUB


def test_a_note_naming_the_writes_empty_row_refires_it_fresh(repo):
    _marker_script(repo)
    _halt(repo, RUN, {"C2": FIGURES})
    notes = repo / RECORD_DIR_ROOT / RUN / requirements_ledger.NOTES_FILE
    notes.write_text('{"note": "redo C2 by hand"}\n', encoding="utf-8", newline="\n")

    rows = {r["id"]: r for r in _refire(repo)["rows"]}

    assert not rows["C2"].get("kept_from")


def test_the_composed_script_passes_the_halt_params_key_the_engine_reads(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    script = compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel=REL,
        run_id=NEW_RUN,
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        **REVIEW_KW,
    )
    assert f"{row_returns.CONSUMED_RETURNS_PARAM}: _consumedReturns" in script

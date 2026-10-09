"""`--hold`: rows kept out of the waves without touching the plan, named incomplete in the digest."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.emit import compose_script, emit_script, landed_rows_from_text
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit import wake_digest as wd
from coordinator_core.win_portability import no_console_creationflags

from .conftest import REVIEW_KW


def _plan(rows) -> str:
    out = ""
    for row_id, dep in rows:
        edge = (
            f"  depends_on:\n    - chunk: {dep}\n      gate_kind: output-consumption-runtime\n"
            if dep
            else ""
        )
        out += (
            f"- id: {row_id}\n  title: t{row_id}\n  change_kind: doc-edit\n"
            f"  surface: docs/x/{row_id}.md\n  writes:\n    - docs/x/{row_id}.md\n"
            f"  queue_scope: project\n  disposition: open\n{edge}  body: |\n    Do {row_id}.\n"
        )
    return (
        '---\ntitle: "p"\nsizing_object: null\n---\n\n# p\n\n## Goal\n\ng\n\n'
        f"## Tasks\n\n```yaml plan-tasks\n{out}```\n"
    )


@pytest.fixture
def plan(tmp_path):
    path = tmp_path / "p.md"
    path.write_text(
        _plan([("C1", None), ("C8", None), ("C9", "C8"), ("C10", "C9"), ("C11", None)]),
        encoding="utf-8",
    )
    return path


def _emit(plan, tmp_path, **kw):
    held = {}
    script = emit_script(
        plan, repo_root=tmp_path, hold_rows=frozenset({"C8"}), hold_reason="peer deploy lane",
        held_out=held, **REVIEW_KW, **kw,
    )
    return script, held


def _registered(script: str) -> set:
    return set(re.findall(r"_rows\['(\w+)'\] = _runRow\(", script))


def test_held_row_and_its_transitive_dependents_are_absent_from_the_waves(plan, tmp_path):
    script, held = _emit(plan, tmp_path)

    assert _registered(script) == {"C1", "C11"}
    assert held["rows"] == ["C8"]
    assert held["dependents"] == {"C9": ["C8"], "C10": ["C9"]}
    assert held["reason"] == "peer deploy lane"


def test_plan_bytes_are_unchanged(plan, tmp_path):
    before = hashlib.sha256(plan.read_bytes()).hexdigest()

    _emit(plan, tmp_path)

    assert hashlib.sha256(plan.read_bytes()).hexdigest() == before


def _run_digest(script: str, tmp_path) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable to execute the generated script")
    held_line = next(l for l in script.splitlines() if l.startswith("  const _heldRows = "))
    tail = script[script.index("function _cap("):]
    harness = tmp_path / "digest.js"
    harness.write_text(
        "let _halted = null, _incompleteChunks = [], _unansweredBriefs = [], _stoppedBy = [], "
        "_blockedChunks = [], _verifications = [], _alreadyDone = new Set(), _falsifierBroken = new Map(), "
        "_routedOut = [], _skippedDone = [], _unusableChecks = [], _reviews = [], "
        "_reviewPrep = null, _reviewWave = null, _deliveryVerdict = null, _reviewIntegration = null, "
        "_testResult = null, _falsifier = null;\n"
        "const _notStarted = [];\n" + held_line + "\n_notStarted.push(...Object.keys(_heldRows));\n"
        "console.log(JSON.stringify((function(){\n" + tail + "\n})()));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(harness)], capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_digest_lists_held_rows_incomplete_with_the_reason(plan, tmp_path):
    script, _ = _emit(plan, tmp_path)

    digest = _run_digest(script, tmp_path)

    assert digest["outcome"] == "incomplete" and digest["completed"] is False
    by_chunk = {d["chunk"]: d for d in digest["deviations"]}
    assert set(by_chunk) == {"C8", "C9", "C10"}
    assert all(d["kind"] == "held" for d in by_chunk.values())
    assert "peer deploy lane" in by_chunk["C8"]["anchor"]
    assert "C8" in by_chunk["C9"]["anchor"]
    assert wd.validate_digest(digest) == []


def test_unknown_hold_id_is_refused(plan, tmp_path):
    with pytest.raises(ValueError, match="not dispatchable"):
        emit_script(
            plan, repo_root=tmp_path, hold_rows=frozenset({"C99"}), hold_reason="x", **REVIEW_KW
        )


def test_hold_without_a_reason_is_refused(plan, tmp_path):
    with pytest.raises(ValueError, match="hold_reason"):
        emit_script(plan, repo_root=tmp_path, hold_rows=frozenset({"C8"}), hold_reason=" ", **REVIEW_KW)


def test_holding_every_row_is_refused(tmp_path):
    path = tmp_path / "p.md"
    path.write_text(_plan([("C1", None)]), encoding="utf-8")
    with pytest.raises(ValueError, match="every dispatchable row is held"):
        emit_script(path, repo_root=tmp_path, hold_rows=frozenset({"C1"}), hold_reason="x", **REVIEW_KW)


def test_receipt_records_rows_reason_who_and_when_and_plan_stays_put(plan, tmp_path):
    before = plan.read_bytes()
    out = tmp_path / "held.mjs"

    reply = _dispatch_emit(
        {
            "plan_path": str(plan), "output_path": str(out), "session_id": "sess-1",
            "hold_rows": ["C8"], "hold_reason": "peer deploy lane",
        }
    )

    receipt = json.loads((tmp_path / "held.mjs.emitted.json").read_text(encoding="utf-8"))
    assert receipt["hold"]["rows"] == ["C8"]
    assert receipt["hold"]["reason"] == "peer deploy lane"
    assert receipt["hold"]["by"] == "sess-1"
    assert len(receipt["hold"]["at"]) == len("2026-10-08T12:00:00")
    assert receipt["hold"]["dependents"] == {"C9": ["C8"], "C10": ["C9"]}
    assert reply["hold"] == receipt["hold"]
    assert plan.read_bytes() == before


def test_only_incomplete_reemit_picks_the_held_rows_up_once_the_hold_clears(plan, tmp_path):
    first = tmp_path / "first.mjs"
    _dispatch_emit(
        {"plan_path": str(plan), "output_path": str(first), "hold_rows": ["C8"], "hold_reason": "lane busy"}
    )
    landed = landed_rows_from_text("abc checkpoint(wave 1): 2 rows — C1, C11")
    second = tmp_path / "second.mjs"

    _dispatch_emit(
        {"plan_path": str(plan), "output_path": str(second), "landed_rows": sorted(landed)}
    )

    assert _registered(first.read_text(encoding="utf-8")) == {"C1", "C11"}
    assert _registered(second.read_text(encoding="utf-8")) == {"C8", "C9", "C10"}
    assert "hold" not in json.loads((tmp_path / "second.mjs.emitted.json").read_text(encoding="utf-8"))


def test_no_hold_leaves_the_script_free_of_hold_machinery(plan, tmp_path):
    script = emit_script(plan, repo_root=tmp_path, **REVIEW_KW)

    assert "_heldRows" not in script and "'held'" not in script


def test_hold_params_need_the_plan_route(plan, tmp_path):
    with pytest.raises(ValueError, match="hold_reason requires hold_rows"):
        _dispatch_emit(
            {"plan_path": str(plan), "output_path": str(tmp_path / "x.mjs"), "hold_reason": "r"}
        )


def _cli_repo(plan, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    target = repo / "docs" / "plans" / "p.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(plan.read_bytes())
    return repo, target


def test_cli_hold_reports_dependents_on_stderr_and_leaves_the_plan_alone(plan, tmp_path, monkeypatch, capsys):
    from coordinator_core.ops.dispatch_emit import cli

    repo, target = _cli_repo(plan, tmp_path)
    before = target.read_bytes()
    monkeypatch.chdir(repo)

    code = cli.main(
        ["--plan", str(target), "--out", str(repo / "p.workflow.mjs"),
         "--hold", "C8", "--hold-reason", "peer deploy lane"]
    )

    assert code == cli.EXIT_OK
    err = capsys.readouterr().err
    assert "held C8 (peer deploy lane)" in err and '"C9": ["C8"]' in err and '"C10": ["C9"]' in err
    assert target.read_bytes() == before


def test_cli_hold_requires_a_reason_and_the_plan_route(plan, tmp_path, monkeypatch, capsys):
    from coordinator_core.ops.dispatch_emit import cli

    repo, target = _cli_repo(plan, tmp_path)
    monkeypatch.chdir(repo)

    assert cli.main(["--plan", str(target), "--hold", "C8"]) == cli.EXIT_USAGE
    assert "--hold requires --hold-reason" in capsys.readouterr().err
    assert cli.main(["--hold", "C8", "--hold-reason", "r"]) == cli.EXIT_USAGE
    assert cli.main(["--plan", str(target), "--hold-reason", "r"]) == cli.EXIT_USAGE


def test_only_incomplete_given_run_text_instead_of_a_path_names_the_file_contract(plan, tmp_path, capsys):
    from coordinator_core.ops.dispatch_emit import cli

    rc = cli.main([
        "--plan", str(plan), "--out", str(tmp_path / "x.workflow.mjs"),
        "--only-incomplete", "abc checkpoint(wave 1): 2 rows C1, C11",
    ])

    assert rc != 0
    assert "--only-incomplete takes a file path" in capsys.readouterr().err

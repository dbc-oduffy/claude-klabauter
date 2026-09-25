"""
coordinator_core.ops.dispatch_emit.tests.test_queue_emit

Purpose: pins C8's entrypoint -- ``queue_emit.py`` plus `dispatch.emit`'s
queue route (``op.py``) and the CLI flags (``cli.py``) -- against every named
assertion in docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md's C8 row.
One file, per overengineering-reviewer #9.

Spec backlink: docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md § Design
§ Entrypoint, Tasks § C8.
"""
from __future__ import annotations

import inspect
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import pytest

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.op import (
    QueuePlanConflictError,
    _dispatch_emit,
    emission_receipt_path,
)
from coordinator_core.ops.dispatch_emit.queue_emit import (
    QueueEmission,
    QueuePathEscapeError,
    emit_queue_script,
)
from coordinator_core.ops.dispatch_emit.queue_select import select_rows

_FIXTURE_PROFILE_DIR = Path(__file__).parent / "fixtures" / "queue-profiles"


def _write_row(path: Path, **fields) -> None:
    lines = []
    for key, value in fields.items():
        if isinstance(value, list):
            rendered = "[" + ", ".join(json.dumps(v) for v in value) + "]"
            lines.append(f"{key}: {rendered}")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {json.dumps(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _base_row(**overrides) -> dict:
    row = dict(
        created="2026-09-21",
        title="a bug row",
        body="a body",
        status="open",
        surface="coordinator_core/x",
        severity="P2",
    )
    row.update(overrides)
    return row


def _write_ledger_line(ledger_path: Path, **fields) -> None:
    with ledger_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(fields, sort_keys=True) + "\n")


def _setup_repo(tmp_path: Path, *, n_rows: int = 3) -> tuple[Path, Path, Path]:
    """A tmp repo with a queue dir of ``n_rows`` fixture rows plus the run
    dir queue-grind emits into. Returns (repo_root, queue_dir, run_dir)."""
    repo_root = tmp_path
    (repo_root / ".git").mkdir()
    queue_dir = repo_root / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    for i in range(n_rows):
        _write_row(queue_dir / f"row{i}.yaml", **_base_row(severity="P0"))
    run_dir = repo_root / "state" / "queue-grind" / "fixture" / "run-1"
    run_dir.mkdir(parents=True)
    return repo_root, queue_dir, run_dir


def _emit(tmp_path: Path, *, n_rows: int = 3, **overrides):
    repo_root, queue_dir, run_dir = _setup_repo(tmp_path, n_rows=n_rows)
    kwargs: dict = dict(
        profile="fixture",
        appetite="standard",
        overrides=None,
        queue=[queue_dir],
        profile_dir=_FIXTURE_PROFILE_DIR,
        repo_root=repo_root,
        run_dir=run_dir,
        session_id="sess1",
        agent_type_host=None,
    )
    kwargs.update(overrides)
    return emit_queue_script(kwargs.pop("profile"), kwargs.pop("appetite"), kwargs.pop("overrides"), **kwargs)


# ---------------------------------------------------------------------------
# End-to-end over a tmp repo + receipt extras
# ---------------------------------------------------------------------------


def test_emit_queue_script_end_to_end(tmp_path):
    emission = _emit(tmp_path)
    assert isinstance(emission, QueueEmission)
    assert emission.script
    extras = emission.receipt_extras
    assert extras["profile"] == "fixture"
    assert extras["appetite"] == "standard"
    assert "profile_digest" in extras and len(extras["profile_digest"]) == 64
    assert "manifest_digest" in extras and len(extras["manifest_digest"]) == 64
    assert extras["source"] is None
    assert isinstance(extras["resolved_knobs"], dict)
    assert extras["reemit"][0] == "emit-dispatch-workflow.py"
    assert extras["reemit"][1:3] == ["--profile", "fixture"]
    assert "plan" not in extras


def test_receipt_extras_reemit_argv_round_trips_through_cli(tmp_path):
    repo_root, queue_dir, run_dir = _setup_repo(tmp_path)
    emission = emit_queue_script(
        "fixture",
        "standard",
        {"limit": 2},
        queue=[queue_dir],
        profile_dir=_FIXTURE_PROFILE_DIR,
        repo_root=repo_root,
        run_dir=run_dir,
        session_id="sess1",
    )
    reemit = emission.receipt_extras["reemit"]
    assert "--limit" in reemit
    assert reemit[reemit.index("--limit") + 1] == "2"


# ---------------------------------------------------------------------------
# Containment refusals
# ---------------------------------------------------------------------------


def test_queue_dir_outside_repo_root_refused(tmp_path):
    repo_root, queue_dir, run_dir = _setup_repo(tmp_path)
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    with pytest.raises(QueuePathEscapeError):
        emit_queue_script(
            "fixture",
            "standard",
            None,
            queue=[outside],
            profile_dir=_FIXTURE_PROFILE_DIR,
            repo_root=repo_root,
            run_dir=run_dir,
        )


def test_run_dir_outside_repo_root_refused(tmp_path):
    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path)
    outside_run_dir = tmp_path.parent / f"outside-run-{tmp_path.name}"
    with pytest.raises(QueuePathEscapeError):
        emit_queue_script(
            "fixture",
            "standard",
            None,
            queue=[queue_dir],
            profile_dir=_FIXTURE_PROFILE_DIR,
            repo_root=repo_root,
            run_dir=outside_run_dir,
        )


# ---------------------------------------------------------------------------
# Cross-repo contract pin (eng-director F1)
# ---------------------------------------------------------------------------


def _expected_signature() -> inspect.Signature:
    def emit_queue_script(
        profile: str,
        appetite: str = "standard",
        overrides: Optional[Mapping[str, Any]] = None,
        *,
        queue: Sequence[Path],
        profile_dir: Path,
        repo_root: Path,
        run_dir: Path,
        session_id: Optional[str] = None,
        agent_type_host: Optional[str] = None,
        preamble: Optional[str] = None,
    ) -> QueueEmission: ...

    return inspect.signature(emit_queue_script)


def test_entrypoint_signature_pinned_to_the_design_doc():
    assert inspect.signature(emit_queue_script) == _expected_signature()


def test_queue_emission_fields_pinned_to_the_design_doc():
    assert QueueEmission._fields == ("script", "receipt_extras")


# ---------------------------------------------------------------------------
# Perf: a synthetic 900-row queue, half seeded in the ledger, under 500ms,
# zero spawns (overengineering-reviewer #8; eng-director F7 -- seeded, not
# cold-path-only)
# ---------------------------------------------------------------------------


def test_synthetic_900_row_queue_seeded_ledger_perf_zero_spawn(tmp_path, monkeypatch):
    repo_root = tmp_path
    (repo_root / ".git").mkdir()
    queue_dir = repo_root / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    run_dir = repo_root / "state" / "queue-grind" / "fixture" / "run-1"
    run_dir.mkdir(parents=True)

    n = 900
    severities = ["P0", "P1", "P2", "P3"]
    row_digests = {}
    for i in range(n):
        row_path = queue_dir / f"row{i}.yaml"
        _write_row(row_path, **_base_row(severity=severities[i % 4]))
        row_digests[f"row{i}"] = __import__("hashlib").sha256(row_path.read_bytes()).hexdigest()

    ledger_dir = repo_root / "state" / "queue-grind" / "fixture"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = ledger_dir / "seed.jsonl"
    for i in range(0, n, 2):  # half the rows
        row_id = f"row{i}"
        _write_ledger_line(
            ledger_path,
            row_id=row_id,
            digest=row_digests[row_id],
            stage="triage",
            verdict="confirmed-bug",
        )

    calls = []
    original_init = subprocess.Popen.__init__

    def _counting_init(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", _counting_init)

    start = time.process_time()
    emission = emit_queue_script(
        "fixture",
        "standard",
        None,
        queue=[queue_dir],
        profile_dir=_FIXTURE_PROFILE_DIR,
        repo_root=repo_root,
        run_dir=run_dir,
        session_id="sess1",
    )
    elapsed_ms = (time.process_time() - start) * 1000.0

    assert calls == []
    assert elapsed_ms < 500.0
    assert emission.script


# ---------------------------------------------------------------------------
# op.py: LF-only write, plan: null, refusals, CLI round-trip
# ---------------------------------------------------------------------------


def _dispatch_queue_emit(tmp_path, **extra):
    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path)
    output_path = tmp_path / "queue-grind" / "emitted.mjs"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    params = {
        "queue": [str(queue_dir)],
        "profile": "fixture",
        "profile_dir": str(_FIXTURE_PROFILE_DIR),
        "output_path": str(output_path),
    }
    params.update(extra)
    result = _dispatch_emit(params, repo_root=repo_root)
    return result, output_path, repo_root, queue_dir


def test_op_writes_script_and_receipt_lf_only_with_plan_null(tmp_path):
    result, output_path, _repo_root, _queue_dir = _dispatch_queue_emit(tmp_path)

    raw_script = output_path.read_bytes()
    assert b"\r\n" not in raw_script

    receipt_path = emission_receipt_path(output_path.resolve())
    raw_receipt = receipt_path.read_text(encoding="utf-8")
    assert "\r\n" not in raw_receipt
    receipt = json.loads(raw_receipt)
    assert receipt["plan"] is None
    assert receipt["profile"] == "fixture"
    assert receipt["queue"] == [str(_queue_dir)]
    assert result["ok"] is True


def test_op_refuses_queue_and_plan_together(tmp_path):
    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path)
    output_path = tmp_path / "emitted.mjs"
    params = {
        "queue": [str(queue_dir)],
        "profile": "fixture",
        "profile_dir": str(_FIXTURE_PROFILE_DIR),
        "plan_path": "some/plan.md",
        "output_path": str(output_path),
    }
    with pytest.raises(QueuePlanConflictError):
        _dispatch_emit(params, repo_root=repo_root)


def test_op_refuses_foreign_overwrite(tmp_path):
    result, output_path, repo_root, queue_dir = _dispatch_queue_emit(tmp_path)

    output_path.write_bytes(b"// a different session's emission\n")

    params = {
        "queue": [str(queue_dir)],
        "profile": "fixture",
        "profile_dir": str(_FIXTURE_PROFILE_DIR),
        "output_path": str(output_path),
    }
    from coordinator_core.ops.dispatch_emit.op import ForeignEmissionError

    with pytest.raises(ForeignEmissionError):
        _dispatch_emit(params, repo_root=repo_root)


def test_op_refuses_queue_route_with_no_repo_root_or_target_root(tmp_path):
    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path)
    output_path = tmp_path / "emitted.mjs"
    params = {
        "queue": [str(queue_dir)],
        "profile": "fixture",
        "profile_dir": str(_FIXTURE_PROFILE_DIR),
        "output_path": str(output_path),
    }
    with pytest.raises(op_module.QueueRootMissingError):
        _dispatch_emit(params, repo_root=None)


def test_op_resolves_relative_queue_dir_against_target_root(tmp_path):
    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path)
    output_path = repo_root / "queue-grind" / "emitted.mjs"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    relative_queue = str(queue_dir.relative_to(repo_root))
    params = {
        "queue": [relative_queue],
        "profile": "fixture",
        "profile_dir": str(_FIXTURE_PROFILE_DIR),
        "output_path": str(output_path),
        "target_root": str(repo_root),
    }
    result = _dispatch_emit(params, repo_root=None)
    assert result["ok"] is True


def test_cli_round_trip_produces_same_bytes_as_the_op(tmp_path):
    result, output_path, repo_root, queue_dir = _dispatch_queue_emit(tmp_path)
    op_bytes = output_path.read_bytes()

    # Same parent directory as the op's own output_path -- `run_dir` is the
    # guarded output path's PARENT, so a different parent would legitimately
    # emit a different script (the run_dir string is baked into it).
    cli_output = output_path.parent / "cli-out.workflow.mjs"
    argv = [
        "--queue", str(queue_dir),
        "--profile", "fixture",
        "--profile-dir", str(_FIXTURE_PROFILE_DIR),
        "--out", str(cli_output),
        "--repo-root", str(repo_root),
    ]
    exit_code = cli_module.main(argv)
    assert exit_code == cli_module.EXIT_OK
    assert cli_output.read_bytes() == op_bytes


def test_cli_omitted_profile_dir_defaults_to_content_root_queue_profiles(tmp_path, monkeypatch):
    # klabauter#51: every published command emits without --profile-dir.
    result, output_path, repo_root, queue_dir = _dispatch_queue_emit(tmp_path)
    content_root = tmp_path / "plugin-root"
    (content_root / "queue-profiles").mkdir(parents=True)
    (content_root / "queue-profiles" / "fixture.yaml").write_bytes(
        (_FIXTURE_PROFILE_DIR / "fixture.yaml").read_bytes()
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(content_root))
    monkeypatch.delenv("COORDINATOR_SOURCE_MODE", raising=False)
    cli_output = output_path.parent / "cli-default-dir.workflow.mjs"
    argv = [
        "--queue", str(queue_dir),
        "--profile", "fixture",
        "--out", str(cli_output),
        "--repo-root", str(repo_root),
    ]
    assert cli_module.main(argv) == cli_module.EXIT_OK
    receipt = json.loads(emission_receipt_path(cli_output.resolve()).read_text(encoding="utf-8"))
    reemit = receipt["reemit"]
    assert reemit[reemit.index("--profile-dir") + 1] == str(content_root / "queue-profiles")


def test_cli_omitted_profile_dir_with_no_default_profile_names_the_probed_path(
    tmp_path, monkeypatch, capsys
):
    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path)
    content_root = tmp_path / "plugin-root"
    content_root.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(content_root))
    monkeypatch.delenv("COORDINATOR_SOURCE_MODE", raising=False)
    argv = [
        "--queue", str(queue_dir),
        "--profile", "fixture",
        "--out", str(tmp_path / "out.mjs"),
        "--repo-root", str(repo_root),
    ]
    assert cli_module.main(argv) == cli_module.EXIT_USAGE
    assert str(content_root / "queue-profiles" / "fixture.yaml") in capsys.readouterr().err


def test_cli_malformed_where_json_is_exit_usage(tmp_path):
    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path)
    argv = [
        "--queue", str(queue_dir),
        "--profile", "fixture",
        "--profile-dir", str(_FIXTURE_PROFILE_DIR),
        "--out", str(tmp_path / "out.mjs"),
        "--repo-root", str(repo_root),
        "--where", "{not valid json",
    ]
    assert cli_module.main(argv) == cli_module.EXIT_USAGE


def test_cli_where_without_queue_route_is_exit_usage(tmp_path):
    repo_root, _queue_dir, _run_dir = _setup_repo(tmp_path)
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# a plan\n", encoding="utf-8")
    argv = [
        "--plan", str(plan_path),
        "--out", str(tmp_path / "out.mjs"),
        "--repo-root", str(repo_root),
        "--where", '{"a": 1}',
    ]
    assert cli_module.main(argv) == cli_module.EXIT_USAGE


# ---------------------------------------------------------------------------
# End-to-end resume (apm review -- C2/C8 fold-in over a real emit/ledger/
# re-emit cycle)
# ---------------------------------------------------------------------------


def test_end_to_end_resume_over_a_seeded_ledger(tmp_path):
    repo_root = tmp_path
    (repo_root / ".git").mkdir()
    queue_dir = repo_root / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    run_dir = repo_root / "state" / "queue-grind" / "fixture" / "run-1"
    run_dir.mkdir(parents=True)

    import hashlib

    rows = {
        "committed": _base_row(severity="P0"),
        "midbatch": _base_row(severity="P0"),
        "changed": _base_row(severity="P0"),
    }
    digests = {}
    for row_id, fields in rows.items():
        row_path = queue_dir / f"{row_id}.yaml"
        _write_row(row_path, **fields)
        digests[row_id] = hashlib.sha256(row_path.read_bytes()).hexdigest()

    ledger_dir = repo_root / "state" / "queue-grind" / "fixture"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = ledger_dir / "run.jsonl"

    # `committed`: passed every stage through commit, digest matches -- a
    # partial run that fully landed.
    for stage in ("triage", "fix", "verify", "commit"):
        _write_ledger_line(
            ledger_path, row_id="committed", digest=digests["committed"], stage=stage
        )
    # `midbatch`: passed triage and fix, but the run stopped before verify/
    # commit -- digest matches, so triage/fix are skipped and the row resumes
    # from verify.
    for stage in ("triage", "fix"):
        _write_ledger_line(
            ledger_path, row_id="midbatch", digest=digests["midbatch"], stage=stage
        )
    # `changed`: the ledger's last digest does not match the row's current
    # bytes -- the row content changed since the ledger was written, so it
    # re-runs from scratch despite carrying ledger history.
    _write_ledger_line(
        ledger_path, row_id="changed", digest="0" * 64, stage="triage"
    )

    # The exact selector call `emit_queue_script` makes internally, over the
    # SAME inputs re-emit would use -- the manifest is what "the manifest
    # skips exactly the committed-and-digest-matching stages" is about.
    manifest = select_rows(
        [queue_dir],
        where=None,
        order=("severity", "asc"),
        limit=None,
        batch_key=["severity"],
        batch_sizes={"default": 4, "@unkeyed": 4},
        row_id_key="@stem",
        profile="fixture",
        repo_root=repo_root,
        absent_sentinels={"owner": ("none", "N/A")},
    )
    by_id = {e.row_id: e for e in manifest.entries}

    assert set(by_id["committed"].skip_stages) == {"triage", "fix", "verify", "commit"}
    assert set(by_id["midbatch"].skip_stages) == {"triage", "fix"}
    assert "commit" not in by_id["midbatch"].skip_stages
    assert by_id["changed"].skip_stages == ()

    # Reconcile counts against the seeded ledger: exactly one row already
    # settled (skip covers commit), one resumes mid-flight (skip nonempty,
    # short of commit), one re-runs from scratch (skip empty).
    settled = [e for e in manifest.entries if "commit" in e.skip_stages]
    resumed = [
        e for e in manifest.entries if e.skip_stages and "commit" not in e.skip_stages
    ]
    rerun = [e for e in manifest.entries if not e.skip_stages]
    assert [e.row_id for e in settled] == ["committed"]
    assert [e.row_id for e in resumed] == ["midbatch"]
    assert [e.row_id for e in rerun] == ["changed"]

    # `emit_queue_script` -- the public entrypoint -- runs cleanly over the
    # same inputs and folds in the same ledger without raising.
    emission = emit_queue_script(
        "fixture",
        "standard",
        None,
        queue=[queue_dir],
        profile_dir=_FIXTURE_PROFILE_DIR,
        repo_root=repo_root,
        run_dir=run_dir,
        session_id="sess1",
    )
    assert emission.script
    assert emission.receipt_extras["manifest_digest"] == manifest.digest

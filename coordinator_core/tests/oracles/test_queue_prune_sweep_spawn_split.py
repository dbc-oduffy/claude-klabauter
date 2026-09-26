
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import coordinator_core.ops.updatedocs_gates as udg


def _seed_queues(repo_root: Path, names: list[str]) -> None:
    (repo_root / "state").mkdir(parents=True, exist_ok=True)
    for name in names:
        (repo_root / "state" / name).write_text("## Open\n- a stale entry\n", encoding="utf-8")


def _patch_run(monkeypatch, fake_run) -> list[list[str]]:
    calls: list[list[str]] = []

    def logging_run(argv, **kwargs):
        calls.append(list(argv))
        return fake_run(argv, **kwargs)

    monkeypatch.setattr(udg.subprocess, "run", logging_run)
    return calls


_QUEUES = ["improvement-queue.md", "bug-backlog.md"]


def test_default_callee_batches_n_queue_files_into_one_spawn(tmp_path, monkeypatch):
    _seed_queues(tmp_path, _QUEUES)
    calls = _patch_run(
        monkeypatch, lambda argv, **kw: SimpleNamespace(returncode=0, stdout="", stderr="")
    )

    result = udg._gate_queue_prune_sweep(tmp_path, tmp_path, {})

    assert len(calls) == 1, (
        f"default-callee legacy-markdown leg issued {len(calls)} spawns for "
        f"{len(_QUEUES)} queue file(s) -- expected exactly 1 (batched); calls={calls}"
    )
    positionals = calls[0][2:]
    assert len(positionals) == len(_QUEUES), (
        f"the one spawn must carry all {len(_QUEUES)} queue files as positionals, "
        f"not a subset; positionals={positionals}"
    )
    assert result.verdict in (udg.GateVerdict.CLEAN, udg.GateVerdict.FINDING)


def test_override_cli_issues_one_spawn_per_queue_file(tmp_path, monkeypatch):
    _seed_queues(tmp_path, _QUEUES)
    calls = _patch_run(
        monkeypatch, lambda argv, **kw: SimpleNamespace(returncode=0, stdout="", stderr="")
    )

    result = udg._gate_queue_prune_sweep(
        tmp_path, tmp_path, {"prune_cli": str(tmp_path / "bin" / "single-arg-prune.py")}
    )

    assert len(calls) == len(_QUEUES), (
        f"override-cli leg issued {len(calls)} spawns for {len(_QUEUES)} queue file(s) -- "
        f"expected exactly {len(_QUEUES)} (one per queue, never batched); calls={calls}"
    )
    for call in calls:
        positionals = call[2:]
        assert len(positionals) == 1, (
            f"each override-cli call must carry exactly ONE queue file positional -- "
            f"got {positionals} in call {call}"
        )
    assert result.verdict in (udg.GateVerdict.CLEAN, udg.GateVerdict.FINDING)


def test_default_callee_names_the_failing_queue_in_batched_stderr(tmp_path, monkeypatch):
    _seed_queues(tmp_path, _QUEUES)
    _patch_run(
        monkeypatch,
        lambda argv, **kw: SimpleNamespace(
            returncode=1, stdout="", stderr="ERROR: file not found: .../bug-backlog.md\n",
        ),
    )

    result = udg._gate_queue_prune_sweep(tmp_path, tmp_path, {})

    assert result.severity == udg.Severity.BLOCKING
    assert any("bug-backlog.md" in line for line in result.detail["lines"]), (
        f"a batched-call failure must still name the offending queue in the surfaced "
        f"lines, not just report the sweep failed; lines={result.detail['lines']}"
    )


def test_override_cli_names_the_failing_queue_by_its_own_isolated_call(tmp_path, monkeypatch):
    _seed_queues(tmp_path, _QUEUES)

    def fake_run(argv, **kw):
        if argv[-1].endswith("bug-backlog.md"):
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    _patch_run(monkeypatch, fake_run)

    result = udg._gate_queue_prune_sweep(
        tmp_path, tmp_path, {"prune_cli": str(tmp_path / "bin" / "single-arg-prune.py")}
    )

    assert result.severity == udg.Severity.BLOCKING
    assert any("bug-backlog.md" in line for line in result.detail["lines"]), (
        f"an override-path per-queue failure must name ITS OWN queue -- "
        f"lines={result.detail['lines']}"
    )
    assert not any("improvement-queue.md" in line for line in result.detail["lines"]), (
        f"the sibling queue's independent success must not be reported as failed just "
        f"because a DIFFERENT queue's own call failed; lines={result.detail['lines']}"
    )


def test_oracle_fails_if_default_path_reintroduces_a_per_queue_spawn(tmp_path, monkeypatch):
    _seed_queues(tmp_path, _QUEUES)

    def fake_run(argv, **kw):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    calls: list[list[str]] = []

    def regressed_run(argv, **kw):
        calls.append(list(argv))
        result = fake_run(argv, **kw)
        for q in _QUEUES:
            calls.append(["sys.executable", "prune-resolved-queue-entries.py", q])
        return result

    monkeypatch.setattr(udg.subprocess, "run", regressed_run)

    udg._gate_queue_prune_sweep(tmp_path, tmp_path, {})

    with pytest.raises(AssertionError):
        assert len(calls) == 1, (
            f"default-callee legacy-markdown leg issued {len(calls)} spawns -- expected 1"
        )


def test_oracle_fails_if_override_path_collapses_to_one_spawn(tmp_path, monkeypatch):
    _seed_queues(tmp_path, _QUEUES)
    calls: list[list[str]] = []

    def collapsing_run(argv, **kw):
        if not calls:
            calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(udg.subprocess, "run", collapsing_run)

    udg._gate_queue_prune_sweep(
        tmp_path, tmp_path, {"prune_cli": str(tmp_path / "bin" / "single-arg-prune.py")}
    )

    with pytest.raises(AssertionError):
        assert len(calls) == len(_QUEUES), (
            f"override-cli leg issued {len(calls)} spawns -- expected {len(_QUEUES)}"
        )

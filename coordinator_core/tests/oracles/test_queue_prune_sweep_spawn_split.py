"""Oracle for the `updatedocs.gates` 11i queue-prune-sweep exemption
(`coordinator_core/ops/updatedocs_gates.py::_gate_queue_prune_sweep::_run`).

The 2026-08-19 amplification burn-down batched the DEFAULT legacy-markdown-queue callee
(`bin/prune-resolved-queue-entries.py`, whose one-positional arity turned out to be
self-imposed, not a real per-item constraint of the CLI) into ONE spawn covering every
existing queue file. The exemption register key survives only on the `overrides
["prune_cli"]` path: a caller-substituted CLI is not guaranteed to accept multiple
positionals the way the shipped CLI now does, so that leg deliberately keeps the
original one-spawn-per-queue loop -- see `_gate_queue_prune_sweep`'s own comment
above the `if override_cli:` branch.

This oracle pins exactly that split as an observed fact, not a description of it:

  (a) no override, N queue files -> ONE spawn, covering all N as positionals.
  (b) an override IS set, N queue files -> N spawns, one queue per call.
  (c) per-queue failure attribution survives on BOTH paths -- a bad queue file is
      still NAMED in `result.detail["lines"]`, whether the failure surfaced through
      the batched call's stderr (default path) or through that queue's own call
      failing outright (override path).

`subprocess.run` is monkeypatched at `updatedocs_gates`'s own module reference (never
a real interpreter spawned) -- this stays fast-tier, matching the oracles package's
`test_dep_probe_varying_program.py` idiom (varying-program claim, same monkeypatch
seam) rather than the real-git `spawns_process`/`cadence` idiom `_common.py`'s
archive_and_commit oracle needs (that claim depends on genuine git index behaviour;
this one does not -- `_gate_queue_prune_sweep`'s spawn-count and attribution logic is
pure argv/stderr bookkeeping around whatever `_run` returns).
"""

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
    """Monkeypatch `subprocess.run` at `updatedocs_gates`'s own module reference and
    return the call log (each entry is the argv `_run` built, sys.executable prefix
    included -- callers slice `[2:]` to get just the queue-file positionals)."""
    calls: list[list[str]] = []

    def logging_run(argv, **kwargs):
        calls.append(list(argv))
        return fake_run(argv, **kwargs)

    monkeypatch.setattr(udg.subprocess, "run", logging_run)
    return calls


_QUEUES = ["improvement-queue.md", "bug-backlog.md"]


def test_default_callee_batches_n_queue_files_into_one_spawn(tmp_path, monkeypatch):
    """(a) No overrides["prune_cli"] -- N=2 queue files collapse to exactly one spawn,
    carrying both queue files as positionals in that one call."""
    _seed_queues(tmp_path, _QUEUES)
    calls = _patch_run(
        monkeypatch, lambda argv, **kw: SimpleNamespace(returncode=0, stdout="", stderr="")
    )

    result = udg._gate_queue_prune_sweep(tmp_path, tmp_path, {})

    assert len(calls) == 1, (
        f"default-callee legacy-markdown leg issued {len(calls)} spawns for "
        f"{len(_QUEUES)} queue file(s) -- expected exactly 1 (batched); calls={calls}"
    )
    positionals = calls[0][2:]  # argv[0]=sys.executable, argv[1]=cli path
    assert len(positionals) == len(_QUEUES), (
        f"the one spawn must carry all {len(_QUEUES)} queue files as positionals, "
        f"not a subset; positionals={positionals}"
    )
    assert result.verdict in (udg.GateVerdict.CLEAN, udg.GateVerdict.FINDING)


def test_override_cli_issues_one_spawn_per_queue_file(tmp_path, monkeypatch):
    """(b) overrides["prune_cli"] set -- the same N=2 queue files must NOT collapse:
    exactly N spawns, one queue file per call, because a caller-substituted CLI is not
    guaranteed to accept multiple positionals."""
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
    """(c), default path: the ONE batched spawn fails; the queue named in its stderr
    (not a generic "prune failed") must survive into `result.detail["lines"]"""
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
    """(c), override path: ONE of N per-queue calls fails (the other succeeds) -- the
    failure must be attributed to ITS OWN queue, and the sibling queue's success must
    not be swallowed by it."""
    _seed_queues(tmp_path, _QUEUES)

    def fake_run(argv, **kw):
        # argv[-1] is this call's sole queue-file positional (per test (b) above).
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
    """Non-vacuousness proof for (a), run through the REAL `_gate_queue_prune_sweep`:
    simulate an incomplete revert (the pre-loop batch call still fires, but a per-queue
    spawn was also reintroduced alongside it -- an easy regression shape, since the
    per-queue loop already exists a few lines below for the override branch) by having
    the monkeypatched `subprocess.run` log one EXTRA synthetic call per queue file the
    instant the real batched call lands. The `len(calls) == 1` claim this module makes
    above must then fail against the same call log a passing run would have produced
    a bug-free version of."""
    _seed_queues(tmp_path, _QUEUES)

    def fake_run(argv, **kw):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    calls: list[list[str]] = []

    def regressed_run(argv, **kw):
        calls.append(list(argv))
        result = fake_run(argv, **kw)
        # This IS the real batched call (only one fires in the un-regressed function) --
        # simulate a reintroduced per-queue spawn riding alongside it.
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
    """Non-vacuousness proof for (b), run through the REAL `_gate_queue_prune_sweep`:
    simulate a regression toward batching on the override-cli leg by having the
    monkeypatched `subprocess.run` DROP every call after the first from the observed
    log (the loop in `_gate_queue_prune_sweep` still genuinely calls `_run` once per
    queue -- nothing about its control flow was touched -- but the log a collapsed
    implementation would produce has only one entry, and this test proves the
    `len(calls) == len(_QUEUES)` claim above catches exactly that shape)."""
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

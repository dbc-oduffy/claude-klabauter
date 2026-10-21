"""Standing gate: one `commit_paths` call on a fresh isolated clone costs at most 150ms of process time.

DERIVATION. DR-368 sets the engine op target at 290ms; the cold-invoke floor is 134ms; what an
op that commits may spend on the commit leg is therefore 290 - 134 = 156ms, rounded down to
150ms. The leg is `parent_ms + children_ms` from `commit_leg_profile` (the `commit_paths`
call in this process plus every descendant it spawned), process time via `time.process_time`
brackets and the descendant accountant, never wall clock.

SHAPE. Five fresh clones per arm would be the exit evidence; this gate takes
`_RUNS_PER_ARM` and asserts the median, because one commit consumes its clone (the plan's five-clone falsifier run is out of band). Each run is
`python -I commit_leg_profile.py <clone> phased --paths 30 --sign <arm>`.

ARMS. `off` writes `commit.gpgsign=false` into the clone and asserts the bound and
`spawns == 0`. `config` leaves the box's signing configuration as git resolves it and asserts
the bound only when that configuration resolves to the in-process ssh-agent signer with a
reachable agent holding the key; otherwise it skips with the reason, so a host with no signer
is first-class rather than red.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks.isolated_clone import fresh_git_clone

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BUDGET_MS = 150.0
_PATHS = 30
_RUNS_PER_ARM = 3
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROFILE = "coordinator_core/benchmarks/commit_leg_profile.py"


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _run_arm(sign: str) -> list:
    """One fresh clone per run; returns the JSON reports."""
    source = _source_root()
    reports = []
    for i in range(_RUNS_PER_ARM):
        with fresh_git_clone(source, prefix="commit-leg-budget-", label=f"commit-leg-{sign}-{i}") as clone:
            proc = subprocess.run(
                [sys.executable, "-I", str(clone / _PROFILE), str(clone), "phased",
                 "--paths", str(_PATHS), "--sign", sign],
                cwd=str(clone), capture_output=True, text=True, timeout=300,
                creationflags=_NO_WINDOW,
            )
            assert proc.returncode == 0, proc.stderr[-2000:]
            report = json.loads(proc.stdout)
            assert report["committed"] is True, report
            reports.append(report)
    return reports


def _leg_ms(report: dict) -> float:
    return report["parent_ms"] + report["children_ms"]


def _signing_unskippable_reason(source: Path) -> "str | None":
    """None when the box's signing config resolves to the in-process agent signer and the agent
    holds the key; else the reason the config arm cannot assert the bound."""
    from coordinator_core.git import commit, sshsig

    if not commit._gpgsign_enabled(source):
        return "commit.gpgsign is not enabled on this host"
    if commit._env_overrides_signing_config():
        return "signing config is injected through the environment"
    cfg = commit._signing_config(source)
    program = cfg.get("gpg.ssh.program", "")
    if cfg.get("gpg.format", "").lower() != "ssh":
        return "gpg.format is not ssh"
    if program and not sshsig.is_onepassword_signer(program):
        return f"gpg.ssh.program {program!r} is not an in-process signer"
    if commit._CONFIG_HAS_INCLUDE in cfg:
        return "signing config is reached through an include"
    pub = sshsig.load_signing_key(cfg.get("user.signingkey", ""))
    if pub is None:
        return "user.signingkey does not resolve to a public key"
    address = sshsig.agent_address_for(program, os.environ) if program else sshsig.agent_address(os.environ)
    if address is None:
        return "no ssh-agent address on this host"
    try:
        replies = sshsig._exchange(address, [bytes([11])], 3.0)
    except sshsig.AgentUnavailable as exc:
        return f"ssh-agent unreachable: {exc}"
    if pub not in sshsig._identities(replies[0]):
        return "the ssh-agent does not hold the signing key"
    return None


def test_unsigned_commit_leg_is_within_budget_and_spawns_nothing():
    reports = _run_arm("off")
    legs = [_leg_ms(r) for r in reports]
    assert statistics.median(legs) <= _BUDGET_MS, (
        f"unsigned commit leg median {statistics.median(legs):.1f}ms > {_BUDGET_MS}ms; legs={legs}"
    )
    assert [r["spawns"] for r in reports] == [0] * len(reports), [r["spawns"] for r in reports]


def test_signed_commit_leg_is_within_budget():
    reason = _signing_unskippable_reason(_source_root())
    if reason is not None:
        pytest.skip(f"config arm needs the in-process signer: {reason}")
    reports = _run_arm("config")
    legs = [_leg_ms(r) for r in reports]
    assert statistics.median(legs) <= _BUDGET_MS, (
        f"signed commit leg median {statistics.median(legs):.1f}ms > {_BUDGET_MS}ms; legs={legs}"
    )

"""Standing gate: `judge_reach_claims` under the 500ms bar, by process time, with zero spawns.

Measures the judge in-process on a tmp_path git repo with CENSUS_FILES production code files, one
withheld path (HeadView read) and an unreachable plus a reachable claim. Each reading is the min of
`time.process_time()` deltas over SAMPLES (process time, never wall clock). Spawns are counted by
wrapping `subprocess.Popen` around the measured calls only.

THRESHOLD: 500ms is the brightline bar itself (DR-344), not a derived ratchet.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.reach_claims import ReachClaim
from coordinator_core.ops.dispatch_emit.reach_claims_tree import judge_reach_claims
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

CENSUS_FILES = 2739
SAMPLES = 5
BUDGET_MS = 500.0

_BODY = "def helper_{i}(x):\n    return x + {i}\n\n\nclass Thing{i}:\n    value = {i}\n" * 20


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )
    return res.stdout.strip()


def _build_repo(root: Path) -> Path:
    repo = root / "repo"
    for i in range(CENSUS_FILES):
        d = repo / "pkg" / f"mod{i % 40}"
        d.mkdir(parents=True, exist_ok=True)
        imp = f"from pkg.mod{i % 40} import file_{i - 40}\n" if i >= 40 and i % 7 == 0 else ""
        (d / f"file_{i}.py").write_text(imp + _BODY.format(i=i), encoding="utf-8")
    (repo / "pkg" / "wired.py").write_text("from pkg.mod1.file_1 import helper_1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "budget@example.invalid")
    _git(repo, "config", "user.name", "budget")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "pkg" / "landing_new.py").write_text("def landing_new_fn():\n    return 1\n", encoding="utf-8")
    return repo


@pytest.fixture(scope="module")
def judge_measurement(tmp_path_factory, request):
    repo = _build_repo(tmp_path_factory.mktemp("reach_claims_budget"))
    claims = [
        ReachClaim("helper_5", "helper_9", "`helper_5` reaches `helper_9`"),
        ReachClaim("landing_new_fn", "helper_1", "`landing_new_fn` reaches `helper_1`"),
    ]
    spawns = []
    real_popen = subprocess.Popen

    class _Counting(real_popen):
        def __init__(self, *a, **k):
            spawns.append(a)
            super().__init__(*a, **k)

    subprocess.Popen = _Counting
    try:
        best_ms, verdicts = float("inf"), ()
        for _ in range(SAMPLES):
            t0 = time.process_time()
            verdicts = judge_reach_claims(
                repo, claims, landing_paths=["pkg/landing_new.py"],
                withheld_paths=["pkg/wired.py"],
            )
            best_ms = min(best_ms, (time.process_time() - t0) * 1000)
    finally:
        subprocess.Popen = real_popen
    detail = f"judge_reach_claims, {CENSUS_FILES} files, process_ms: {best_ms:.1f}, spawns: {len(spawns)} (min of {SAMPLES})"
    print(f"REACH-CLAIMS-BUDGET {detail}")
    return {"ms": best_ms, "spawns": len(spawns), "verdicts": verdicts, "detail": detail}


def test_judge_reach_claims_is_under_the_500ms_bar_with_zero_spawns(judge_measurement):
    m = judge_measurement
    assert [v.verdict for v in m["verdicts"]] == ["unreachable", "unreachable"], m["detail"]
    assert m["spawns"] == 0, f"spawn on the scan path: {m['detail']}"
    assert 0 < m["ms"] < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"

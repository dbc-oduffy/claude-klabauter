"""Standing gate: `review.reachability` with default entry kinds under the 500ms bar, by process time.

Measures the registered op in-process on a tmp_path git repo with CENSUS_FILES production code
files and a range adding one entry of each kind (exported function, bin script, hook body, wiki
doc, `@register_op` module). The op reports its own `cost.process_ms` (process time, never wall
clock) and `cost.spawns`; each reading is the min over SAMPLES so peer load does not inflate it.

THRESHOLD: 500ms is the brightline bar itself (DR-344), not a derived ratchet.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.review_reachability import _handler
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


def _build_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    for i in range(CENSUS_FILES):
        d = repo / "pkg" / f"mod{i % 40}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"file_{i}.py").write_text(_BODY.format(i=i), encoding="utf-8")
    (repo / "hooks").mkdir()
    (repo / "hooks" / "__init__.py").write_text("", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "budget@example.invalid")
    _git(repo, "config", "user.name", "budget")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "pkg" / "feature.py").write_text("def brand_new_entry(x):\n    return x\n", encoding="utf-8")
    (repo / "bin").mkdir()
    (repo / "bin" / "newtool.py").write_text("print('tool')\n", encoding="utf-8")
    (repo / "hooks" / "newhook.py").write_text("def run():\n    return 0\n", encoding="utf-8")
    (repo / "docs" / "wiki").mkdir(parents=True)
    (repo / "docs" / "wiki" / "newdoc.md").write_text("# New doc\n\nBody.\n", encoding="utf-8")
    (repo / "pkg" / "newop.py").write_text(
        "from coordinator_core.ipc import register_op\n\n\n@register_op('bench.newop')\n"
        "def _h(params):\n    return {}\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "range")
    return repo, base


@pytest.fixture(scope="module")
def reachability_measurement(tmp_path_factory):
    repo, base = _build_repo(tmp_path_factory.mktemp("review_reachability_budget"))
    params = {"repo_root": str(repo), "base_sha": base, "head_sha": "HEAD"}
    results = [_handler(params) for _ in range(SAMPLES)]
    best = min(results, key=lambda r: r["cost"].get("process_ms", float("inf")))
    detail = (
        f"review.reachability default kinds, {CENSUS_FILES} files, process_ms: "
        f"{best['cost'].get('process_ms')}, spawns: {best['cost'].get('spawns')} (min of {SAMPLES})"
    )
    print(f"REVIEW-REACHABILITY-BUDGET {detail}")
    return {"best": best, "detail": detail}


def test_review_reachability_default_kinds_is_under_the_500ms_bar(reachability_measurement):
    m = reachability_measurement
    assert m["best"]["error"] is None, f"op failed: {m['best']['error']}"
    assert any(e["kind"] == "exported-function" for e in m["best"]["entries"]), f"no entry found: {m['detail']}"
    kinds = {e["kind"] for e in m["best"]["entries"]}
    assert {"cli", "hook", "doc", "op"} <= kinds, f"default kinds not all emitted ({sorted(kinds)}): {m['detail']}"
    assert m["best"]["cost"]["spawns"] == 1, f"spawn count drifted: {m['detail']}"
    assert 0 < m["best"]["cost"]["process_ms"] < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"

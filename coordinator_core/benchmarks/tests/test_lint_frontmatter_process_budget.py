"""Standing gate: the lint-frontmatter whole-tree sweep under the 500ms bar, by tree process time.

The fixture is a `git init` repo at census scale (live and archived handoffs, plans, research and
decisions counted off the claude-klabauter tree at test time). One planted violation makes rc 1 prove the
walk completed. The store is primed once, then the CLI is sampled five times.

UNIT: process time and spawn count from `batched_process_time_ms`, never wall clock (CLAUDE.md,
The brightline). THRESHOLD: 500ms is the brightline bar itself (DR-344); procs_per_call < 3 is the
PM's exit criterion for this sweep. STATISTIC: the median of the samples, not the min.
"""

from __future__ import annotations

import statistics
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SAMPLES = 5
BUDGET_MS = 500.0
MAX_PROCS = 3

_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[3]
_SHIM = _CLAUDE_KLABAUTER_ROOT / "coordinator" / "bin" / "lint-frontmatter.py"

_ARCHIVE_MONTHS = ("2026-05", "2026-06", "2026-07", "2026-08", "2026-09")


def _count_md(rel: str, *, recursive: bool = False) -> int:
    base = _CLAUDE_KLABAUTER_ROOT / rel
    if not base.is_dir():
        return 0
    it = base.rglob("*.md") if recursive else base.glob("*.md")
    return sum(1 for _ in it)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _git(root: Path, *args: str) -> None:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=root, capture_output=True, text=True, **no_console_creationflags(),
    )
    assert done.returncode == 0, f"git {args}: {done.stderr}"


def _handoff(title: str, hid: str, predecessor: str, extra: str = "") -> str:
    return (
        f'---\ntitle: "{title}"\ncreated: "2026-10-01"\nbranch: "work/x/2026-10-01"\n'
        f"status: open\npredecessor: {predecessor}\nhandoff_id: {hid}\nkind: session-handoff\n{extra}---\n\nbody\n"
    )


def _archived(title: str, hid: str) -> str:
    return (
        f'---\ntitle: "{title}"\ncreated: "2026-06-01"\nbranch: "work/x/2026-06-01"\n'
        f"status: claimed\npredecessor: none\nhandoff_id: {hid}\n---\n\nbody\n"
    )


def _build_fixture(root: Path) -> dict:
    live = _count_md("state/handoffs") or 601
    archived = _count_md("archive/handoffs", recursive=True) or 1361
    plans = _count_md("docs/plans") or 300
    research = _count_md("docs/research") or 300
    decisions = _count_md("docs/decisions") or 100

    for i in range(archived):
        month = _ARCHIVE_MONTHS[i % len(_ARCHIVE_MONTHS)]
        _write(root / "archive" / "handoffs" / month / f"a{i:05d}.md", _archived(f"archived {i}", f"hnd-arch-{i:05d}"))

    for i in range(live):
        if i % 3 == 1 and i > 0:
            pred, extra = f"state/handoffs/h{i - 1:05d}.md", ""
        elif i % 3 == 2:
            pred, extra = "none", f"predecessor_id: hnd-arch-{i % archived:05d}\n"
        else:
            pred, extra = "none", ""
        _write(root / "state" / "handoffs" / f"h{i:05d}.md", _handoff(f"live {i}", f"hnd-live-{i:05d}", pred, extra))

    for i in range(plans):
        _write(
            root / "docs" / "plans" / f"2026-10-01-p{i:04d}.md",
            f'---\ntitle: "plan {i}"\ncreated: "2026-10-01"\nauthor: t\nstatus: draft\n---\n\n# plan\n',
        )
    for i in range(research):
        _write(root / "docs" / "research" / f"2026-10-01-r{i:04d}.md", f'---\ntitle: "research {i}"\n---\n\nbody\n')
    for i in range(decisions):
        _write(
            root / "docs" / "decisions" / f"DR-{i:04d}-d.md",
            f'---\ntitle: "decision {i}"\ncreated: "2026-10-01"\nstatus: accepted\ndeciders: [pm]\n---\n\nbody\n',
        )

    _write(
        root / "docs" / "plans" / "2026-10-01-planted.md",
        '---\ntitle: "planted"\ncreated: "2026-10-01"\nauthor: t\nstatus: not-a-real-status\n---\n\n# planted\n',
    )

    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "fixture")
    return {"live": live, "archived": archived, "plans": plans, "research": research, "decisions": decisions}


@pytest.fixture(scope="module")
def lint_fixture(tmp_path_factory):
    root = tmp_path_factory.mktemp("lint_frontmatter_budget")
    scale = _build_fixture(root)
    return root, scale


@pytest.fixture(scope="module")
def sweep_measurement(lint_fixture):
    root, scale = lint_fixture
    argv = [sys.executable, str(_SHIM), "--root", str(root), "--json"]

    primed = subprocess.run(argv, capture_output=True, text=True, **no_console_creationflags())
    assert primed.returncode == 1, f"prime run rc {primed.returncode}, expected 1 (planted violation): {primed.stderr[-500:]}"
    assert "2026-10-01-planted.md" in primed.stdout, f"rc 1 not from the planted violation: {primed.stdout[-500:]}"

    try:
        samples = [batched_process_time_ms(argv, k=1) for _ in range(SAMPLES)]
    except NotImplementedError as exc:
        pytest.skip(f"process-time primitive unsupported here: {exc}")
    for s in samples:
        assert s["rc"] == 1, f"sweep exited {s['rc']}, expected 1 (planted violation): {s!r}"

    median_ms = round(statistics.median(s["process_time_ms"] for s in samples), 3)
    median_procs = statistics.median(s["procs_per_call"] for s in samples)
    detail = (
        f"lint-frontmatter whole-tree sweep, median tree process time: {median_ms}ms, "
        f"procs_per_call: {median_procs} (k=1, median of {SAMPLES}; fixture {scale})"
    )
    print(f"LINT-FRONTMATTER-BUDGET {detail}")
    return {"ms": median_ms, "procs": median_procs, "detail": detail}


def test_lint_frontmatter_sweep_is_under_the_500ms_bar(sweep_measurement):
    m = sweep_measurement
    assert m["ms"] > 0, f"sweep cost not observed: {m['detail']}"
    assert m["ms"] <= BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"


def test_lint_frontmatter_sweep_spawns_fewer_than_three_processes(sweep_measurement):
    m = sweep_measurement
    assert m["procs"] < MAX_PROCS, f"procs_per_call not under {MAX_PROCS}: {m['detail']}"

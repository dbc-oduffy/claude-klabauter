"""
coordinator_core.workstream_complete.test_terminal_sweep_directive_ordering
— pins the one property neither the generic cross-consistency guard nor any
existing test asserted: the terminal-handoff and terminal-sizings sweep
directives are emitted after everything that stamps, in that order, from
`build_directives`, followed only by the close-ceremony reindex.

Purpose: the C3 commit that added `d-sweep-terminal-sizings`
(docs/plans/2026-09-03-close-verb-archival-stops-asking-for-wri.md) calls
ordering load-bearing in its own commit message ("every stamp this
ceremony performed must land before the sweep classifies") but shipped no
test asserting it. `coordinator_core/authz/tests/test_assembler_
dispatchable.py`'s cross-consistency guard only catches a CLI present in
`CONSUMES_MANIFEST` but missing from `ASSEMBLER_DISPATCHABLE` (or the
reverse) — it says nothing about WHERE in the list a directive lands. This
gap predates the sizings sweep: `d-sweep-terminal-handoffs` was never
order-pinned either, so this file closes both at once rather than only the
newer one.

Negative-spec: does NOT assert anything about the OTHER directives'
ordering, relative positions, or presence/absence — only that the two
terminal sweeps and the reindex are the final three entries, handoffs
immediately before sizings, the reindex last.
"""

from __future__ import annotations

from pathlib import Path

import coordinator_core.workstream_complete as wsc
from coordinator_core.ops.ceremony.wsc_disposition import SINGLE_SESSION
import pytest

# The spawn is statically reachable from the code under test; tiered so a future change cannot spawn on the fast tier.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _gate() -> wsc.SessionShapeGate:
    return wsc.SessionShapeGate(
        sid="testsid-terminal-sweep-order",
        disposition=SINGLE_SESSION,
        consumed_handoff="",
        diagnostics=[],
        consumed_handoff_paths=(),
    )


def test_terminal_sweeps_are_last_in_the_main_pass_then_post_close(tmp_path: Path) -> None:
    directives = wsc.build_directives(_gate(), {}, tmp_path)
    key = wsc.directives_session_hygiene.AFTER_CLOSE_COMMIT_KEY

    main_pass = [d for d in directives if not d.get(key)]
    post_close = [d for d in directives if d.get(key)]

    assert [d["id"] for d in main_pass[-2:]] == [
        "d-sweep-terminal-handoffs",
        "d-sweep-terminal-sizings",
    ], f"got {[d['id'] for d in main_pass]!r}"
    assert main_pass[-2]["cli"] == "sweep-terminal-handoffs"
    assert main_pass[-1]["cli"] == "sweep-terminal-sizings"

    assert [d["id"] for d in post_close] == ["d-structural-index-refresh", "d-ceremony-reindex"]
    assert directives[-2:] == post_close
    assert post_close[0]["args"] == ["--root", str(tmp_path), "--flag-only"]
    assert post_close[1]["args"] == [
        "--ceremony", "workstream-complete", "--repo-root", str(tmp_path),
    ]
    assert all(d["best_effort"] for d in post_close)
    # The post-close pass runs no gate check, so a held-back directive may
    # never depend on a judgment point or claim to be already satisfied.
    assert all(d["depends_on"] is None and not d["already_satisfied"] for d in post_close)


def test_workstream_close_runs_the_sizing_drain_in_process(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.ops.fleet import terminal_sizing_drain

    seen: list = []

    def fake_drain(root, *, dry_run=False):
        seen.append((Path(root), dry_run))
        return {"acted": [{"id": "a"}], "candidates": [{}], "skipped": [], "failed": [], "scan_skipped": []}

    monkeypatch.setattr(terminal_sizing_drain, "drain", fake_drain)
    directives = wsc.build_directives(_gate(), {}, tmp_path, drain_sizings=True)

    sizing = next(d for d in directives if d["id"] == "d-sweep-terminal-sizings")
    assert seen == [(tmp_path, False)]
    assert sizing["drain"]["archived"] == 1 and sizing["already_satisfied"] is True


def test_workstream_close_survives_a_drain_fault(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.ops.fleet import terminal_sizing_drain

    def boom(root, *, dry_run=False):
        raise RuntimeError("drain exploded")

    monkeypatch.setattr(terminal_sizing_drain, "drain", boom)
    directives = wsc.build_directives(_gate(), {}, tmp_path, drain_sizings=True)

    sizing = next(d for d in directives if d["id"] == "d-sweep-terminal-sizings")
    assert "drain exploded" in sizing["drain"]["error"]
    assert sizing["already_satisfied"] is False


def test_brief_cli_passes_commit_and_brief_threads_it_to_the_drain(tmp_path: Path, monkeypatch) -> None:
    seen: dict = {}

    def fake_brief(decisions=None, repo_root=None, *, commit=False):
        seen["commit"] = commit
        raise wsc.TransportFailure("stop after capture")

    monkeypatch.setattr(wsc, "brief", fake_brief)
    wsc._main_brief([])
    assert seen == {"commit": True}


def test_brief_drains_only_under_commit(tmp_path: Path, monkeypatch) -> None:
    captured: list = []
    real_build = wsc.build_directives

    def spy_build(*a, **kw):
        captured.append(kw.get("drain_sizings"))
        return real_build(*a, **{**kw, "drain_sizings": False})

    monkeypatch.setattr(wsc, "build_directives", spy_build)
    for flag in (True, False):
        wsc.brief({"stage_paths": []}, repo_root=tmp_path, commit=flag)
    assert captured == [True, False]


def test_default_build_directives_does_not_drain(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.ops.fleet import terminal_sizing_drain

    def forbidden(root, *, dry_run=False):
        raise AssertionError("a read-only brief must not drain")

    monkeypatch.setattr(terminal_sizing_drain, "drain", forbidden)
    directives = wsc.build_directives(_gate(), {}, tmp_path)

    sizing = next(d for d in directives if d["id"] == "d-sweep-terminal-sizings")
    assert "drain" not in sizing and sizing["already_satisfied"] is not True

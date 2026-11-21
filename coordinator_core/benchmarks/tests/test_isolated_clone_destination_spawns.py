"""The clone destination is refused when it resolves under no git root.

Guards `benchmarks.isolated_clone.mkdtemp_for_clone`'s runtime assertion --
the 2026-08-27 incident's first half (68 clone directories minted on a bare
drive root, outside every repo, invisible to write confinement).

WHAT THIS FILE DOES NOT COVER, STATED SO NOBODY READS IT AS SUFFICIENT. The
assertion fires only for callers that GO THROUGH the helper. The literal
2026-08-27 defect did not: it was a direct `tempfile.mkdtemp(dir=source_root.
parent)` in a fixture, which never consults this code path at all. Measured
during the spike (docs/research/spike-verdicts/2026-08-27-the-choke-point-
cannot-see-its-own-bypass.md): with this assertion in place, re-introducing
that exact line raises nothing and turns nothing red. The bypass-detector
named in that verdict is what closes it; this file guards the other half.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _rooted_sleeper(root: Path):
    import subprocess
    import sys

    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_reap_signals_descendant_holder(tmp_path: Path) -> None:
    """A rooted process in our own tree is signalled and reported reaped."""
    from coordinator_core.benchmarks.isolated_clone import reap_processes_under

    proc = _rooted_sleeper(tmp_path)
    try:
        reaped = reap_processes_under(tmp_path)
        assert proc.pid in reaped
        assert reaped.foreign == []
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_reap_reports_foreign_holder_without_signalling(tmp_path: Path, monkeypatch) -> None:
    """A rooted process outside our tree is returned on `.foreign` and left alive."""
    import psutil

    from coordinator_core.benchmarks.isolated_clone import reap_processes_under

    proc = _rooted_sleeper(tmp_path)
    try:
        monkeypatch.setattr(psutil.Process, "children", lambda self, recursive=False: [])
        reaped = reap_processes_under(tmp_path)
        assert proc.pid not in reaped
        assert proc.pid in reaped.foreign
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=10)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_reap_stale_clones_never_raises_on_foreign_holder(tmp_path: Path, monkeypatch) -> None:
    import os
    import psutil

    from coordinator_core.benchmarks.isolated_clone import _reap_stale_clones

    stale = tmp_path / "stale"
    stale.mkdir()
    old = stale.stat().st_mtime - 7200
    os.utime(stale, (old, old))
    proc = _rooted_sleeper(stale)
    try:
        monkeypatch.setattr(psutil.Process, "children", lambda self, recursive=False: [])
        _reap_stale_clones(tmp_path)
        assert proc.poll() is None
        assert stale.exists()
    finally:
        proc.kill()
        proc.wait(timeout=10)

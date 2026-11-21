"""ASSERTION and EXECUTION legs for distill.scope's `run_git` site in the composition gate
(`_LEGITIMIZED_SITES`, keyed `"distill.scope"`, in test_no_uncounted_spawn_on_budgeted_path.py).

The site is `run_watermark.changed_since`'s git fallback: with a watermark and no example-retrieval-repo
doc_files view, it issues exactly ONE `git diff --name-only` per run, never one per file.
The counter patches `subprocess.Popen` itself (seam-independent), so a hidden extra spawn shows.
"""
from __future__ import annotations


import pytest

from coordinator_core import run_watermark
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from pathlib import Path


def test_first_run_spawns_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_watermark, "registry_get", lambda key: None)
    with _count_spawns_attributed(monkeypatch) as spawns:
        run_watermark.changed_since(tmp_path, None, scope_dirs=["docs"])
    assert spawns == []

"""A gitignored ``writes_under:`` prefix is probed in the one batched
check-ignore spawn and dropped from the terminal-commit marker, narrated."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

from .conftest import REVIEW_KW

# `_repo` builds a real git repo for the check-ignore probe.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _repo(tmp_path):
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)],
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    (tmp_path / ".gitignore").write_text("out/\n", encoding="utf-8")
    return tmp_path


def _row(prefix):
    return WaveRow(
        id="P1",
        title="prefix row",
        surface="x",
        writes=[],
        reads=[],
        depends_on=[],
        writes_under=(prefix,),
    )


def _compose(tmp_path, prefix):
    return compose_script(
        [[_row(prefix)]],
        name="wf",
        description="d",
        repo_root=_repo(tmp_path),
        plan_path="docs/plans/fake-plan.md",
        **REVIEW_KW,
    )


def _marker_prefixes(script):
    line = next(
        (ln for ln in script.splitlines() if ln.startswith("//") and "prefixes" in ln),
        None,
    )
    return line


def test_ignored_prefix_is_dropped_and_narrated(tmp_path):
    script = _compose(tmp_path, "out/")
    marker = _marker_prefixes(script)
    assert marker is None or '"out/"' not in marker
    assert "GITIGNORED writes_under" in script
    assert "row P1: out/" in script


def test_unignored_prefix_stays_in_the_marker(tmp_path):
    script = _compose(tmp_path, "state/audits/")
    marker = _marker_prefixes(script)
    assert marker is not None and "state/audits/" in marker
    assert "GITIGNORED writes_under" not in script


def test_degraded_filter_drops_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        emit,
        "_gitignored_paths",
        lambda paths, **kw: emit._GitignoreFilterResult(frozenset(), degraded=True),
    )
    script = _compose(tmp_path, "out/")
    assert "GITIGNORE FILTER DID NOT RUN" in script
    assert "GITIGNORED writes_under" not in script
    assert "out/" in _marker_prefixes(script)


def test_probe_normalises_terminators():
    assert emit._prefix_probe("a/b") == "a/b/.coordinator-ignore-probe"
    assert emit._prefix_probe("a/b//") == "a/b/.coordinator-ignore-probe"
    assert emit._prefix_probe("a\\b\\") == "a/b/.coordinator-ignore-probe"

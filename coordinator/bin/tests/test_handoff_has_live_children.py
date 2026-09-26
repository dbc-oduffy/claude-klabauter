from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_HERE = os.path.dirname(os.path.abspath(__file__))
VENEER = os.path.normpath(os.path.join(_HERE, "..", "handoff-has-live-children.py"))


def _run_veneer(*args):
    proc = subprocess.run(
        [sys.executable, VENEER, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        **no_console_creationflags(),
    )
    return proc.returncode, proc.stdout, proc.stderr


@pytest.fixture
def repo(tmp_path):
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)],
        check=True, capture_output=True, **no_console_creationflags(),
    )
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def candidate(repo):
    path = repo / "state" / "handoffs" / "test-candidate.md"
    path.write_text(
        "---\n"
        "title: \"py-test candidate\"\n"
        "created: 2026-07-19\n"
        "branch: work/test/2026-07-19\n"
        "status: active\n"
        "predecessor: none\n"
        "category: test\n"
        "summary: \"Temporary python-port test fixture -- safe to delete.\"\n"
        "---\n"
        "# Test candidate\n",
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture
def successor(repo, candidate):
    path = repo / "state" / "handoffs" / "test-successor.md"
    path.write_text(
        "---\n"
        "title: \"py-test successor\"\n"
        "created: 2026-07-19\n"
        "branch: work/test/2026-07-19\n"
        "status: active\n"
        "predecessor: {candidate_basename}\n"
        "category: test\n"
        "summary: \"Temporary python-port test fixture -- safe to delete.\"\n"
        "---\n"
        "# Test successor\n".format(candidate_basename=os.path.basename(candidate)),
        encoding="utf-8",
    )
    return str(path)


def test_missing_arg_exits_2():
    rc, _, _ = _run_veneer()
    assert rc == 2, "expected exit 2 (missing arg), got {}".format(rc)


def test_candidate_not_found_exits_2():
    rc, _, _ = _run_veneer("/tmp/hlc-py-test-nonexistent-{}.md".format(int(time.time())))
    assert rc == 2, "expected exit 2 (candidate not found), got {}".format(rc)


def test_unknown_flag_exits_2(candidate):
    rc, _, _ = _run_veneer("--unknown-flag", candidate)
    assert rc == 2, "expected exit 2 (unknown flag), got {}".format(rc)


def test_baseline_has_live_children(stamped_engine_env, candidate, successor):
    del successor, stamped_engine_env
    rc, out, err = _run_veneer(candidate)
    assert rc == 0, "expected exit 0 (has live children), got {} stdout={!r} stderr={!r}".format(rc, out, err)


def test_exclude_successor_safe_to_archive(stamped_engine_env, candidate, successor):
    rc, out, err = _run_veneer("--exclude", successor, candidate)
    assert rc == 1, "expected exit 1 (safe to archive), got {} stdout={!r} stderr={!r}".format(rc, out, err)


def test_edge_kinds_passthrough(stamped_engine_env, candidate, successor):
    del successor, stamped_engine_env
    rc, out, err = _run_veneer("--edge-kinds", "predecessor,additional_predecessors", candidate)
    assert rc == 0, "expected exit 0 (predecessor-field reference detected), got {} stdout={!r} stderr={!r}".format(rc, out, err)

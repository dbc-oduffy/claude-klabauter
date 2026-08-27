"""Pins PROCESS COUNT DOES NOT GROW WITH N for `path_resolution_report._check_posix`.

Spec backlink: `_KNOWN_SITES` in
`coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`
(`path_resolution_report.py`, `_check_posix`, `run`) -- the gate's own comment names
the batch primitive: PATH is built once at login-shell startup, not per name looked
up inside it, so one combined `-lc` script looping the entrypoints keeps the
fresh-shell property and drops N spawns to 1.

Model: `test_schema_drift_watch.py::TestSchemaAdvisoryBatch::
test_process_count_does_not_grow_with_the_set`.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from coordinator_core.install import path_resolution_report as prr


def _fake_completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["sh", "-lc", "..."], returncode=0, stdout=stdout, stderr="")


def _combined_stdout(names: "tuple[str, ...]") -> str:
    """Build the same marker-delimited combined stdout `_check_posix` itself would
    parse back apart, for an arbitrary N so the process-count assertion below is not
    tied to the fixed 2-entrypoint `_ENTRYPOINTS` default."""
    parts = []
    for name in names:
        parts.append(f"{prr._POSIX_ENTRY_MARKER}{name}\n/usr/local/bin/{name}\nRC=0\n")
    return "".join(parts)


class TestCheckPosixSpawnBatch:
    def test_process_count_does_not_grow_with_the_set(self, monkeypatch):
        """One `subprocess.run` call for the whole `names` tuple, whatever N is --
        never one spawn per entrypoint."""
        call_count = {"n": 0}

        def _fake_run(args, **kwargs):
            call_count["n"] += 1
            # The single combined script must reference every requested name.
            script = args[-1]
            for name in _NAMES_UNDER_TEST:
                assert name in script
            return _fake_completed(_combined_stdout(_NAMES_UNDER_TEST))

        monkeypatch.setattr(prr.subprocess, "run", _fake_run)
        monkeypatch.setattr(prr, "_EXEC_PROOF_ARGS", {n: () for n in _NAMES_UNDER_TEST})

        report = prr._check_posix(_NAMES_UNDER_TEST)

        assert call_count["n"] == 1, (
            f"expected exactly one subprocess.run call for N={len(_NAMES_UNDER_TEST)} "
            f"entrypoints, got {call_count['n']}"
        )
        assert len(report.checks) == len(_NAMES_UNDER_TEST)
        assert all(c.resolved_path and c.executed_ok for c in report.checks)

    def test_per_entrypoint_results_still_attributed_correctly(self, monkeypatch):
        """Batching into one spawn must not lose per-name attribution -- a NOTFOUND
        entry and a resolved entry in the same combined stdout are told apart."""
        names = ("alpha-entry", "beta-entry")

        def _fake_run(args, **kwargs):
            combined = (
                f"{prr._POSIX_ENTRY_MARKER}alpha-entry\n/usr/local/bin/alpha-entry\nRC=0\n"
                f"{prr._POSIX_ENTRY_MARKER}beta-entry\nRC=NOTFOUND\n"
            )
            return _fake_completed(combined)

        monkeypatch.setattr(prr.subprocess, "run", _fake_run)
        monkeypatch.setattr(prr, "_EXEC_PROOF_ARGS", {n: () for n in names})

        report = prr._check_posix(names)
        by_name = {c.name: c for c in report.checks}

        assert by_name["alpha-entry"].resolved_path == "/usr/local/bin/alpha-entry"
        assert by_name["alpha-entry"].executed_ok is True
        assert by_name["beta-entry"].resolved_path is None
        assert by_name["beta-entry"].executed_ok is False


_NAMES_UNDER_TEST = ("coordinator-invoke", "coordinator-cockpit-emit-schema", "a-third-entrypoint")

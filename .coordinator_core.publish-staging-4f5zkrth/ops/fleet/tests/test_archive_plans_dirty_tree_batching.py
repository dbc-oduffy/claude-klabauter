"""Pins that the dirty-tree pre-check batches across candidates instead of
spawning one `git status --porcelain` per plan (amplification burn-down,
2026-08-19 C12).

A single-item test alone would pass identically before and after the
batching change (this exact gap shipped `_own_frozen_diff_shas` wrong on
2026-08-19, per this chunk's brief) — so the primary assertion here is
CALL-COUNT: many candidate paths must still collapse to exactly one
`asyncio.create_subprocess_exec` invocation, with per-path membership in
the returned dirty set matching the fabricated `git status --porcelain`
output verbatim.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest import mock

from coordinator_core.ops.fleet import archive_plans as _op


class _FakeProc:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, b""


class DirtyTreeBatchingTest(unittest.IsolatedAsyncioTestCase):
    async def test_many_paths_collapse_to_one_subprocess_call(self):
        rel_paths = [
            "docs/plans/2026-08-01-a-plan.md",
            "docs/plans/2026-08-02-b-plan.md",
            "docs/plans/2026-08-03-c-plan.md",
        ]
        # Only the second path is reported dirty by the fabricated porcelain
        # output — verifies per-path granularity survived the batching.
        porcelain = b" M docs/plans/2026-08-02-b-plan.md\n"

        calls = []

        async def _fake_create_subprocess_exec(*argv, **kwargs):
            calls.append(argv)
            return _FakeProc(porcelain)

        with mock.patch.object(
            asyncio, "create_subprocess_exec", side_effect=_fake_create_subprocess_exec
        ):
            dirty = await _op._plan_worktree_dirty_batch(Path("/tmp/worktree"), rel_paths)

        self.assertEqual(len(calls), 1, "expected exactly one batched git status call")
        argv = calls[0]
        self.assertEqual(argv[:4], ("git", "status", "--porcelain", "--"))
        for p in rel_paths:
            self.assertIn(p, argv)
        self.assertEqual(dirty, {"docs/plans/2026-08-02-b-plan.md"})

    async def test_single_path_still_one_call(self):
        calls = []

        async def _fake_create_subprocess_exec(*argv, **kwargs):
            calls.append(argv)
            return _FakeProc(b"")

        with mock.patch.object(
            asyncio, "create_subprocess_exec", side_effect=_fake_create_subprocess_exec
        ):
            dirty = await _op._plan_worktree_dirty_batch(
                Path("/tmp/worktree"), ["docs/plans/2026-08-01-a-plan.md"]
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(dirty, set())

    async def test_empty_input_spawns_nothing(self):
        async def _fail(*argv, **kwargs):
            raise AssertionError("should not spawn for an empty path list")

        with mock.patch.object(asyncio, "create_subprocess_exec", side_effect=_fail):
            dirty = await _op._plan_worktree_dirty_batch(Path("/tmp/worktree"), [])

        self.assertEqual(dirty, set())

    async def test_subprocess_failure_degrades_to_not_dirty(self):
        async def _raise_oserror(*argv, **kwargs):
            raise OSError("spawn failed")

        with mock.patch.object(asyncio, "create_subprocess_exec", side_effect=_raise_oserror):
            dirty = await _op._plan_worktree_dirty_batch(
                Path("/tmp/worktree"), ["docs/plans/2026-08-01-a-plan.md"]
            )

        self.assertEqual(dirty, set())

    async def test_rename_porcelain_form_keys_on_new_path(self):
        rel_paths = ["docs/plans/2026-08-02-new-name.md"]
        porcelain = b"R  docs/plans/2026-08-01-old-name.md -> docs/plans/2026-08-02-new-name.md\n"

        async def _fake_create_subprocess_exec(*argv, **kwargs):
            return _FakeProc(porcelain)

        with mock.patch.object(
            asyncio, "create_subprocess_exec", side_effect=_fake_create_subprocess_exec
        ):
            dirty = await _op._plan_worktree_dirty_batch(Path("/tmp/worktree"), rel_paths)

        self.assertEqual(dirty, {"docs/plans/2026-08-02-new-name.md"})


if __name__ == "__main__":
    unittest.main()

"""Oracles for the act-time TOCTOU recheck at `_findings_reap.reap_findings` and
`distill_apply_disposal.apply_disposal_manifest`.

Both sites already batch CLASSIFICATION (`_is_tracked_batch`, one spawn for the whole survivor
set). What survives per-item is a second, narrower call: immediately before EACH untracked
survivor's `unlink()`, `_is_tracked` re-probes that one path, closing the window between
partition-time classification and this row's own delete on the shared worktree. The claim is that
hoisting this recheck out of the per-item position -- batching it ahead of the delete loop, or
merging it back into the classification pass -- necessarily widens that window for every survivor
after the first, because a hoisted recheck's answer is fixed before any of THIS call's own
unlinks have run.

A test that only asserts the final partition (who ended up in `failed` vs `reaped`) cannot tell
these two shapes apart -- both a per-item recheck and a hoisted batch-recheck-then-unlink can
reach the same end state on a single fabricated status map. So the oracle instead pins ADJACENCY:
a shared call log records every `_is_tracked` probe and every `Path.unlink`, and the assertion is
that a probe reporting "untracked" is followed, with NOTHING in between, by the unlink of that
SAME path. A hoisted recheck (all probes before any unlinks) produces a log where the probes and
the unlinks form two separate runs -- adjacency fails, and the oracle goes red. Verified by hand
against a hand-rolled hoisted variant of the `reap_findings` loop before this file was written;
see this chunk's run-report sidecar.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Dict, List, Tuple
from unittest import mock

from coordinator_core.ops import distill_apply_disposal as _dad
from coordinator_core.ops.fleet import _findings_reap as _reap

#: Filenames processed in this fixed order by every test below -- the middle one is the survivor
#: whose tracked-status flips between classification and its own act-time recheck.
_SAFE_BEFORE = "safe-before.md"
_FLIPPED = "flipped.md"
_SAFE_AFTER = "safe-after.md"
_ORDER = (_SAFE_BEFORE, _FLIPPED, _SAFE_AFTER)


def _make_probes(worktree_root: Path) -> Tuple[List[Tuple[str, str]], object, object]:
    """Build a (call_log, fake_is_tracked, fake_is_tracked_batch) triple.

    `fake_is_tracked_batch` reports every survivor "untracked" (the classification pass never
    sees the flip -- it only becomes visible at act time). `fake_is_tracked` reports "tracked"
    for `_FLIPPED` and "untracked" for everything else, and appends every probe to `call_log`
    under the SAME key `Path.unlink` is patched to append under below, so the two interleave in
    call order regardless of which function patches which name.
    """
    call_log: List[Tuple[str, str]] = []

    async def fake_is_tracked_batch(_worktree_root: Path, paths: List[Path]) -> Dict[Path, str]:
        call_log.append(("batch", ",".join(sorted(p.name for p in paths))))
        return {p: "untracked" for p in paths}

    async def fake_is_tracked(_worktree_root: Path, path: Path) -> str:
        call_log.append(("recheck", path.name))
        return "tracked" if path.name == _FLIPPED else "untracked"

    return call_log, fake_is_tracked, fake_is_tracked_batch


def _assert_recheck_adjacent_to_own_unlink(call_log: List[Tuple[str, str]]) -> None:
    """The property under test, applied to the interleaved log: every "untracked" recheck for a
    surviving path is IMMEDIATELY (no other log entry between) followed by that path's own
    "unlink" -- and the flipped path's recheck is never followed by an unlink at all, anywhere in
    the log. A hoisted recheck (all probes first, all unlinks after) fails this on the very first
    pair, since `_SAFE_BEFORE`'s recheck would be followed by `_FLIPPED`'s recheck, not its own
    unlink."""
    for name in (_SAFE_BEFORE, _SAFE_AFTER):
        idx = call_log.index(("recheck", name))
        assert call_log[idx + 1] == ("unlink", name), (
            f"recheck of {name!r} was not immediately followed by its own unlink -- log was "
            f"{call_log!r}. The act-time recheck has been hoisted away from its unlink, which "
            "widens the TOCTOU window this recheck exists to narrow."
        )
    assert ("recheck", _FLIPPED) in call_log, "flipped survivor's recheck never ran"
    assert ("unlink", _FLIPPED) not in call_log, (
        "flipped survivor was unlinked despite its act-time recheck reporting tracked -- the "
        "recheck result is not gating the delete."
    )


def test_reap_findings_recheck_fires_adjacent_to_its_own_unlink(tmp_path: Path) -> None:
    for name in _ORDER:
        (tmp_path / name).write_text("x", encoding="utf-8")
    paths = [tmp_path / name for name in _ORDER]

    call_log, fake_is_tracked, fake_is_tracked_batch = _make_probes(tmp_path)
    orig_unlink = Path.unlink

    def logging_unlink(self: Path, *args, **kwargs):
        call_log.append(("unlink", self.name))
        return orig_unlink(self, *args, **kwargs)

    with mock.patch.object(_reap, "_is_tracked_batch", side_effect=fake_is_tracked_batch), \
         mock.patch.object(_reap, "_is_tracked", side_effect=fake_is_tracked), \
         mock.patch.object(Path, "unlink", logging_unlink):
        reaped, skipped, failed = asyncio.run(
            _reap.reap_findings(
                worktree_root=tmp_path,
                paths=paths,
                classify=lambda _p: "note",
                subject_builder=lambda n: f"reap {n} tracked",
            )
        )

    _assert_recheck_adjacent_to_own_unlink(call_log)

    assert not (tmp_path / _SAFE_BEFORE).exists()
    assert not (tmp_path / _SAFE_AFTER).exists()
    assert (tmp_path / _FLIPPED).exists(), "flipped file must survive an aborted delete"

    reaped_ids = {r["id"] for r in reaped}
    assert any(_SAFE_BEFORE in rid for rid in reaped_ids)
    assert any(_SAFE_AFTER in rid for rid in reaped_ids)

    flipped_failures = [f for f in failed if _FLIPPED in f["id"]]
    assert len(flipped_failures) == 1
    assert flipped_failures[0]["reason"] == "tracked-status-changed-before-unlink: now tracked"


def test_apply_disposal_manifest_recheck_fires_adjacent_to_its_own_unlink(tmp_path: Path) -> None:
    for name in _ORDER:
        (tmp_path / name).write_text("x", encoding="utf-8")

    call_log, fake_is_tracked, fake_is_tracked_batch = _make_probes(tmp_path)
    orig_unlink = Path.unlink

    def logging_unlink(self: Path, *args, **kwargs):
        call_log.append(("unlink", self.name))
        return orig_unlink(self, *args, **kwargs)

    fake_plan = _dad.ApplyPlan(
        survivors=[{"path": name, "artifact_class": "EPHEMERAL"} for name in _ORDER],
    )

    async def fake_delete_tracked_and_append_log(*_args, **_kwargs):
        # No tracked_rows in this fixture -- the tracked-commit path is out of scope for the
        # recheck property under test, so it is stubbed rather than stood up with a real repo.
        return [], [], [], []

    manifest = {"run_id": "run-toctou-oracle"}

    with mock.patch.object(_dad, "verify_stamp_and_throttle", return_value=None), \
         mock.patch.object(_dad, "verify_drain_ordering", new=_AsyncNoop()), \
         mock.patch.object(_dad, "compute_apply_plan", return_value=fake_plan), \
         mock.patch.object(_dad, "_is_tracked_batch", side_effect=fake_is_tracked_batch), \
         mock.patch.object(_dad, "_is_tracked", side_effect=fake_is_tracked), \
         mock.patch.object(
             _dad, "_delete_tracked_and_append_log", side_effect=fake_delete_tracked_and_append_log
         ), \
         mock.patch.object(Path, "unlink", logging_unlink):
        result = asyncio.run(
            _dad.apply_disposal_manifest(
                worktree_root=tmp_path,
                manifest=manifest,
                harvest_committed_sha="deadbeef",
            )
        )

    _assert_recheck_adjacent_to_own_unlink(call_log)

    assert not (tmp_path / _SAFE_BEFORE).exists()
    assert not (tmp_path / _SAFE_AFTER).exists()
    assert (tmp_path / _FLIPPED).exists(), "flipped file must survive an aborted delete"

    assert sorted(result.deleted_untracked) == sorted([_SAFE_BEFORE, _SAFE_AFTER])

    flipped_failures = [f for f in result.failed if f["path"] == _FLIPPED]
    assert len(flipped_failures) == 1
    assert flipped_failures[0]["reason"] == "tracked-status-changed-before-unlink: now tracked"


class _AsyncNoop:
    """Callable stand-in for `verify_drain_ordering` -- an async no-op, since
    `mock.patch.object(..., return_value=None)` on an `async def` target yields a coroutine
    function whose CALL returns `None` directly rather than an awaitable, which the `await` in
    `apply_disposal_manifest` rejects."""

    def __call__(self, *args, **kwargs):
        async def _coro():
            return None

        return _coro()

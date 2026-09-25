"""Regression test for dispatch.emit inlining one row's `writes:` path list
into the emitted script multiple times over (preflight, executor prompt,
commit pathspec, ...).

Spec backlink: state/cross-repo/inbox/2026-09-25-doe-claude-em-engine-
friction-sizing-scaffold-memo-owner-safe-commit-emit-cap.md, item 5. A row
with ~1.2k write paths pushed the composed script's UTF-8 byte size past the
Workflow runner's ``_WORKFLOW_SCRIPT_BYTE_CAP`` (524288) even alone -- the
list was baked into the static prompt text at every call site that needed
it, rather than serialized once as a shared runtime array every phase reads
back from.
"""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import (
    _WORKFLOW_SCRIPT_BYTE_CAP,
    compose_script,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow


def _mega_row(n: int) -> WaveRow:
    # `.yaml`, not `.py`: `candidate_test_additions` widens a `.py` write
    # with a stem-derived test-file guess, which would otherwise inflate
    # the commit/preflight pathspec beyond the raw declared list and muddy
    # this test's byte-budget assertion.
    writes = [f"state/audits/mega_row_sentinel_{i:04d}.yaml" for i in range(n)]
    return WaveRow(
        id="C1",
        title="a single row with a very large writes: list",
        surface="state/audits",
        writes=writes,
        reads=[],
        depends_on=[],
        agent_type=None,
        agent_model=None,
    )


def test_one_mega_row_stays_under_the_runner_byte_cap():
    waves = [[_mega_row(1200)]]

    script = compose_script(waves, name="mega", description="one row, ~1200 writes")

    assert len(script.encode("utf-8")) <= _WORKFLOW_SCRIPT_BYTE_CAP


def test_one_mega_row_writes_list_is_serialized_once():
    """A single declared write path -- not the whole list -- names exactly
    where in the emitted script the write-set is actually spelled out as
    literal text. Before the dedup fix this sentinel occurred once per
    call site that inlined the row's `writes:` (preflight prompt, the
    executor's footprint-constraint clause, its DONE-summary porcelain
    command, and the commit-phase pathspec sentence) -- four or more times
    over. After the fix, the SAME list is registered once as a runtime
    array (`_sharedPaths`) and every one of those sites reads it back via
    `.join(...)` at runtime, so the sentinel's literal text appears exactly
    once in the composed script.
    """
    waves = [[_mega_row(1200)]]

    script = compose_script(waves, name="mega", description="one row, ~1200 writes")

    sentinel = "mega_row_sentinel_0000.yaml"
    assert script.count(sentinel) == 1, (
        "the sentinel write path should be serialized exactly once in the "
        f"emitted script; found {script.count(sentinel)} occurrences"
    )

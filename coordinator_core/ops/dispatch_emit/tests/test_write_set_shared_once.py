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
    waves = [[_mega_row(1200)]]

    script = compose_script(waves, name="mega", description="one row, ~1200 writes")

    sentinel = "mega_row_sentinel_0000.yaml"
    assert script.count(sentinel) == 1, (
        "the sentinel write path should be serialized exactly once in the "
        f"emitted script; found {script.count(sentinel)} occurrences"
    )

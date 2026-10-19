"""End to end: a composed plan-route script's digest carries the executor's block reason per chunk.

Rows A and B have no deps and C depends on A; the stubbed executor answers A `BLOCKED:` and B `PARTIAL:`,
so C is held behind A. The whole script runs under node (review legs stubbed to a one-slice prep and empty
replies) and the returned digest is read. B names a different source plan so the composer does not fold
A and B into one shared agent call, which would answer both from a single reply.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import wake_digest as wd
from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PREP = {
    "verdict": "single-reviewer-ok",
    "product_files": 1,
    "foreign_claims": [],
    "run_base_sha": "a" * 40,
    "whole_diff_path": "d.diff",
    "slices": [{"id": "s", "files": ["A.py"], "diff_path": "d.diff", "sidecar_path": "s.md", "contract_blocks": 0}],
}
_NODE_PROGRAM = r"""
const src = process.env.SRC.replace(/^export const meta/m, 'const meta');
const cfg = JSON.parse(process.env.CFG);
const agent = async (prompt, opts) => {
  const label = String((opts && opts.label) || '');
  for (const [id, reply] of Object.entries(cfg.replies)) {
    if (label === 'work:' + id) return reply;
  }
  if (label === 'review:coordinator:review-prep') return cfg.prep;
  if (label === 'review:coordinator:integrator') return { fixes_applied: 0 };
  return {};
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'log', src);
run({}, agent, async (xs) => Promise.all(xs.map((f) => f())), async () => ({}), () => {}, () => {})
  .then((result) => console.log(JSON.stringify(result)))
  .catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""


def _row(row_id, deps=(), plan="docs/plans/x.md"):
    return WaveRow(
        id=row_id,
        title=f"title-{row_id}",
        surface="s",
        writes=[f"{row_id}.py"],
        reads=[],
        depends_on=[{"chunk": d, "gate_kind": "output-consumption-runtime"} for d in deps],
        body=f"Spec: {plan} ({row_id})\nSummary: does {row_id}.\n",
    )


def _digest(replies: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable to execute the generated script")
    script = compose_script(
        [[_row("A"), _row("B", plan="docs/plans/y.md")], [_row("C", ["A"])]],
        name="wf",
        description="reason end to end",
        plan_path="docs/plans/x.md",
        run_base_sha="a" * 40,
        **REVIEW_KW,
    )
    proc = subprocess.run(
        [node, "-e", _NODE_PROGRAM],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env={**os.environ, "SRC": script, "CFG": json.dumps({"replies": replies, "prep": _PREP})},
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_blocked_partial_and_held_rows_carry_their_reason():
    digest = _digest({"A": "BLOCKED: no pnpm on host", "B": "PARTIAL: C3 tests unwritten"})

    by_chunk = {d["chunk"]: d for d in digest["deviations"]}
    assert (by_chunk["A"]["kind"], by_chunk["A"]["reason"]) == ("blocked", "no pnpm on host")
    assert (by_chunk["B"]["kind"], by_chunk["B"]["reason"]) == ("partial", "C3 tests unwritten")
    assert by_chunk["C"]["kind"] == "not_started"
    assert "A" in by_chunk["C"]["reason"]
    assert all(d["anchor"] == "" for d in by_chunk.values())
    # Trap: a delivery-exempt roster (this fixture's) emits inline_review.delivery_not_run, which the
    # schema's closed inline_review refuses; that mismatch is not this test's subject.
    digest["next_action"]["params"]["inline_review"].pop("delivery_not_run")
    assert wd.validate_digest(digest) == []

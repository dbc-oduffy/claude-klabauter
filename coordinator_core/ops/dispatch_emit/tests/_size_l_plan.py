"""Synthetic size-L plan fixture for `dispatch_emit.ask_stage.stage`: rows, sizing, claims, live peers.

`build_size_l_plan` writes under a caller-given tmp repo root and returns `(repo_root, plan_rel, run_id)`.
Never reads a sibling repo. Live peers are minted run tokens with session_id None, so `warp_live`
reads each as live; the caller isolates `warp_live`'s launch dirs (see `isolate_launch_dirs`).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import run_token

DEFAULT_ROWS = 14
DEFAULT_LIVE_TOKENS = 50
DEFAULT_PLAN_BYTES = 74_000
PLAN_REL = "docs/plans/2026-10-22-size-l-fixture.md"
SIZING_REL = "state/sizings/2026-10-22-size-l-fixture.yaml"
RUN_ID = "size-l-run"
PEER_SCRIPT = "scratch/warp/peer.mjs"

_PROSE = (
    "Read the named module before editing it. The change keeps every capability the incumbent has, "
    "and its covering tests stay green. Negative spec: no new spawn, no widened timeout, no edit "
    "outside the declared writes. Done when the scoped tests pass and the evidence is recorded. "
)


def _row(i: int, rows: int, body_bytes: int) -> dict:
    row = {
        "id": f"C{i}",
        "title": f"Size-L fixture row {i}",
        "change_kind": "code-edit",
        "surface": f"pkg/mod_{i}.py",
        "writes": [f"pkg/mod_{i}.py", f"pkg/tests/test_mod_{i}.py"],
        "reads_at_head": [f"pkg/mod_{i}.py"],
        "queue_scope": "project",
        "disposition": "open",
        "traces_to_brief": "size-L fixture requirement",
        "body": (_PROSE * (body_bytes // len(_PROSE) + 1))[:body_bytes],
    }
    if i > 1:
        row["depends_on"] = [{"chunk": f"C{i - 1}", "gate_kind": "output-consumption-runtime"}]
        if i > 4 and i % 3 == 0:
            row["depends_on"].append({"chunk": f"C{i - 4}", "gate_kind": "output-consumption-runtime"})
    return row


def isolate_launch_dirs(monkeypatch, tmp_path: Path) -> None:
    """Point `warp_live`'s launch dirs at an empty dir beside `tmp_path`."""
    from coordinator_core import warp_live

    managed = tmp_path.parent / (tmp_path.name + "-managed")
    managed.mkdir(exist_ok=True)
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)


def build_size_l_plan(
    repo_root: Path,
    *,
    rows: int = DEFAULT_ROWS,
    live_tokens: int = DEFAULT_LIVE_TOKENS,
    plan_bytes: int = DEFAULT_PLAN_BYTES,
) -> tuple[Path, str, str]:
    """Write a `rows`-row plan of about `plan_bytes`, its sizing, and `live_tokens` live peer runs
    (each staged, with a manifest footprint disjoint from the plan's). The run's own token is minted
    too. Returns `(repo_root, plan_rel, run_id)`."""
    root = Path(repo_root)
    (root / ".git").mkdir(exist_ok=True)
    (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (root / "state" / "sizings").mkdir(parents=True, exist_ok=True)

    register_rows = [
        {
            "id": f"R{n}",
            "source_text": f"Size-L fixture requirement {n}: the staged run delivers row C{n}.",
            "anchor": "relayed",
            "surface": "internal",
            "status": "open",
            "claimed_by": [PLAN_REL],
        }
        for n in range(1, 3)
    ]
    sizing = {
        "schema": "sizing-object",
        "name": "size-l-fixture",
        "intent": "Stage a size-L plan under the brightline.",
        "intent_source": "em-elaborated",
        "pm_verbatims": [],
        "estimate": {"tshirt": "L", "provisional": False},
        "route": "plan",
        "status": "routed",
        "plan": PLAN_REL,
        "exit_criterion": {"statement": "the size-L plan stages within budget"},
        "deliverable_id": "dlv-size-l-fixture-abc123",
        "requirement_register": {"rows": register_rows},
    }
    (root / SIZING_REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8", newline="\n")

    register = rr.read_register((root / SIZING_REL).read_text(encoding="utf-8"))
    pins = rr.register_pins(register, ["R1", "R2"])
    spine = [_row(i, rows, 0) for i in range(1, rows + 1)]
    overhead = len(yaml.safe_dump(spine, sort_keys=False)) + 600
    body_bytes = max(0, (plan_bytes - overhead) // rows)
    spine = [_row(i, rows, body_bytes) for i in range(1, rows + 1)]
    front = (
        "---\n"
        "title: size-L fixture\n"
        "status: executing\n"
        "plan_id: pln-size-l-fixture-abc123\n"
        "deliverable_id: dlv-size-l-fixture-abc123\n"
        f"sizing_object: {SIZING_REL}\n"
        "register_claims:\n"
        f"  sizing: {SIZING_REL}\n"
        '  rows: ["R1", "R2"]\n'
        f"  pins: {json.dumps(pins)}\n"
        "---\n"
        "# Size-L fixture\n\n## Tasks\n\n```yaml plan-tasks\n"
        + yaml.safe_dump(spine, sort_keys=False)
        + "```\n"
    )
    (root / PLAN_REL).write_text(front, encoding="utf-8", newline="\n")

    run_token.mint(root, RUN_ID, PEER_SCRIPT, session_id=None)
    for k in range(live_tokens):
        peer = f"peer-{k:03d}"
        run_token.mint(root, peer, PEER_SCRIPT, session_id=None)
        peer_dir = root / "scratch" / "warp" / peer
        peer_dir.mkdir(parents=True, exist_ok=True)
        footprint = [f"peer{k}/mod_{j}.py" for j in range(rows)]
        (peer_dir / "manifest.json").write_text(
            json.dumps({"rows": [{"id": f"P{j}", "writes": [footprint[j]]} for j in range(rows)]}),
            encoding="utf-8",
        )
    return root, PLAN_REL, RUN_ID

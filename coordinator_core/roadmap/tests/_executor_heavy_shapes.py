"""Shared plan-spine fixtures for the three EXECUTOR_HEAVY shapes, plus the repair applier.

These are SYNTHESIZED reproductions of the row shapes the pulled XXL cells hit, not re-runs of
the r40/r47/r51 waves: their run SHAs are not in this tree (r47 727121a98a and r51 df74d6bf2c are
klabauter engine SHAs recorded in experiments:docs/research/2026-10-25-ladder-cell-audit-levers/
synthesis.md, read at experiments b6459b2).

Sources of each shape:
  PURE_RETYPE  a no-writes verify row running a path-scoped heavy command (`pnpm test <path>`).
  WRITES_SPLIT a writes-bearing row running a heavy command (doe 9bcd34ebb8, fix-history row 6).
  BUILD_CLASS  a no-writes build row (`pnpm run build`, fix-history row 6).
  MIXED        one row of each shape beside a non-heavy executor row.
  R47, R51     the 9bcd34ebb8 plan shape: a writes-bearing row and a `pnpm run build` row
               (R51 adds a pure-retype row).
  R40          no halted r40 plan exists under state/warp, and the audit synthesis row 6 (cites
               r40:L448-552) names only that EXECUTOR_HEAVY pulled it twice before any executor
               ran, so R40 is built as MIXED.

Every spine keeps a non-heavy executor row (A1), so a repair that EM-performs a heavy row cannot
leave zero dispatchable rows.

SEMICOLON_CMD is a writes-bearing row whose command span contains ``;``, so a repair that names
the span must not be split on it.

`apply_repair` reads only the spine and the repair string; it is a test-side reading of the prose
that the plan-readiness reviser (``plan_readiness_verdict`` mechanical_repairs) applies. A repair segment (split on ``;`` outside
backticks, so a command span such as `a; b` never splits one) is
attributed to a row by the row id as a whole token; it carries the target as
``performer: em`` or ``agent_type: coordinator:<role>`` (the first such token), and a segment
that names ``<id>-V`` (or ``<id>-V2``...) is a split: the row loses its command and a new verify
row carrying it takes the target.
"""

from __future__ import annotations

import re
from typing import List

import yaml

from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap.tests.test_prep_gate_four_legs import _CLEAN_FM, _write_plan

__all__ = [
    "PURE_RETYPE", "WRITES_SPLIT", "BUILD_CLASS", "MIXED", "R40", "R47", "R51", "SEMICOLON_CMD",
    "apply_repair", "agents", "gate", "write_plan", "CLEAN_FM",
]

CLEAN_FM = _CLEAN_FM
write_plan = _write_plan

_NON_HEAVY = """- id: A1
  title: Author the module
  change_kind: code-edit
  surface: src/a.py
  writes: [src/a.py]
  disposition: open
  body: |
    Run `python -m pytest src/test_a.py`.
"""
_RETYPE = """- id: P1
  title: Verify the scoped suite
  change_kind: verification
  surface: src/a.py
  writes: []
  disposition: open
  body: |
    Run `pnpm test src/a.test.ts`.
"""
_WRITES = """- id: W1
  title: Author and baseline
  change_kind: code-edit
  surface: src/b.py
  writes: [src/b.py]
  disposition: open
  body: |
    Run `cargo build --release`.
"""
_WRITES_SCOPED = """- id: W2
  title: Author and verify
  change_kind: code-edit
  surface: src/c.py
  writes: [src/c.py]
  disposition: open
  body: |
    Run `pnpm test src/c.test.ts`.
"""
_BUILD = """- id: B1
  title: Build the bundle
  change_kind: verification
  surface: src/a.py
  writes: []
  disposition: open
  body: |
    Run `pnpm run build`.
"""

PURE_RETYPE = _NON_HEAVY + _RETYPE
WRITES_SPLIT = _NON_HEAVY + _WRITES_SCOPED
BUILD_CLASS = _NON_HEAVY + _BUILD
MIXED = _NON_HEAVY + _RETYPE + _WRITES_SCOPED + _BUILD
R47 = _NON_HEAVY + _WRITES + _BUILD
R51 = _NON_HEAVY + _RETYPE + _WRITES + _BUILD
R40 = MIXED
SEMICOLON_CMD = _NON_HEAVY + """- id: S1
  title: Author and build
  change_kind: code-edit
  surface: src/d.py
  writes: [src/d.py]
  disposition: open
  body: |
    Run `cargo build --release; cargo build`.
"""

_TARGET = re.compile(r"performer: em|agent_type: (coordinator:(?!executor\b)[\w-]+)")


def agents(tmp_path, monkeypatch, admits):
    """Point the gate at a fake DoE agent dir: ``admits`` maps role name to frontmatter lines."""
    d = tmp_path / "agents"
    d.mkdir(exist_ok=True)
    for name, fm in admits.items():
        (d / f"{name}.md").write_text(f"---\nname: {name}\n{fm}---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(pg, "_agent_definitions_dir", lambda: d)


def gate(tmp_path, spine):
    """Write ``spine`` as a plan under ``tmp_path`` and return ``gate_plan``'s result."""
    (tmp_path / "src").mkdir(exist_ok=True)
    return pg.gate_plan(tmp_path, write_plan(tmp_path, frontmatter=CLEAN_FM, spine=spine))


def _split_outside_backticks(text: str) -> List[str]:
    """Split ``text`` on ``;`` that sits outside a backtick span (a command may contain ``;``)."""
    parts: List[str] = []
    buf: List[str] = []
    inside = False
    for ch in text:
        if ch == "`":
            inside = not inside
        if ch == ";" and not inside:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _owns(segment: str, row_id: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(row_id)}(?![\w-])", segment) is not None


def _run_command(body: str) -> str:
    m = re.search(r"`([^`]+)`", body)
    assert m, f"no backticked command in body: {body!r}"
    return m.group(1)


def apply_repair(spine: str, repair: str) -> str:
    """Apply the per-row edits named in ``repair`` to ``spine`` and return the edited spine."""
    rows: List[dict] = yaml.safe_load(spine)
    taken = {str(r["id"]) for r in rows}
    segments = [s for s in _split_outside_backticks(repair) if s.strip()]
    added: List[dict] = []
    for row in rows:
        rid = str(row["id"])
        seg = next((s for s in segments if _owns(s, rid)), None)
        if seg is None:
            continue
        target = _TARGET.search(seg)
        assert target, f"repair segment for {rid} carries no target token: {seg!r}"
        verify = re.search(rf"(?<![\w-]){re.escape(rid)}-V\d*(?![\w-])", seg)
        token = {"performer": "em"} if target.group(0) == "performer: em" else {"agent_type": target.group(1)}
        if verify is None:
            row.update(token)
            continue
        vid = verify.group(0)
        assert vid not in taken, f"verify id {vid} already in the spine"
        taken.add(vid)
        command = _run_command(row["body"])
        row["body"] = f"Author the change; row {vid} runs the verification.\n"
        added.append({
            "id": vid,
            "title": f"Verify {rid}",
            "change_kind": "verification",
            "surface": row["surface"],
            "writes": [],
            "depends_on": [{"chunk": rid, "gate_kind": "output-consumption-runtime", "note": "verifies the authoring row"}],
            **token,
            "disposition": "open",
            "body": f"Run `{command}`.\n",
        })
    return yaml.safe_dump(rows + added, sort_keys=False, width=1000)

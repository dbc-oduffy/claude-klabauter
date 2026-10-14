"""Corpus builder for measuring `baton-chain-closure.py signal` at live corpus size.

    python -m coordinator_core.benchmarks.baton_chain_closure_signal <dir>

Builds, once per directory, 577 live and 1355 archived handoffs and 749 plans, with one closed
three-member chain among the live handoffs, then prints the repo-relative path of a chain member.
Spawns no git; the corpus needs no repository because the CLI is given `--repo`.
"""

from __future__ import annotations

import sys
from pathlib import Path

LIVE = 577
ARCHIVED = 1355
PLANS = 749
CHAIN_PLAN_ID = "pln-bench-chain-000001"
CHAIN_MEMBER = "state/handoffs/2026-10-01-bench-chain-c.md"
_BODY = "Body line for sizing the file like a real handoff.\n" * 120


def _handoff(name: str, **extra: str) -> str:
    fields = {
        "title": f'"{name}"',
        "created": "2026-10-01",
        "branch": '"work/bench/2026-10-01"',
        "status": "open",
        "kind": "roadmap-baton",
        "roadmap_id": '"bench-roadmap"',
        "stub_id": '"bench-stub"',
        "wave": "2  # topo_number",
        "summary": '"One sentence outcome. Then forward-looking prose."',
        "deployment_state": "shipped",
        "predecessor": "none",
        **extra,
    }
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items()) + "---\n\n" + _BODY


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_corpus(parent: Path) -> tuple:
    """(repo root, repo-relative member path); a populated `parent` is reused as is."""
    repo = Path(parent)
    member = repo / CHAIN_MEMBER
    if member.exists():
        return repo, CHAIN_MEMBER
    live = repo / "state" / "handoffs"
    for i in range(LIVE - 3):
        _write(
            live / f"2026-09-{i:04d}-live.md",
            _handoff(f"live-{i}", handoff_id=f"hnd-live-{i:06d}", deliverable_id=f"dlv-live-{i:06d}"),
        )
    for i in range(ARCHIVED):
        _write(
            repo / "archive" / "handoffs" / f"2026-0{1 + i % 8}" / f"arch-{i:05d}.md",
            _handoff(f"arch-{i}", handoff_id=f"hnd-arch-{i:06d}", deliverable_id=f"dlv-arch-{i // 2:06d}"),
        )
    previous = "none"
    for tag in ("a", "b", "c"):
        name = f"2026-10-01-bench-chain-{tag}.md"
        _write(
            live / name,
            _handoff(
                name,
                handoff_id=f"hnd-bench-chain-{tag}",
                deliverable_id="dlv-bench-chain-000001",
                predecessor=previous,
                plan_ids=f"['{CHAIN_PLAN_ID}']",
            ),
        )
        previous = name
    plans = repo / "docs" / "plans"
    _write(
        plans / "2026-10-01-bench-chain.md",
        f"---\ntitle: bench\nplan_id: {CHAIN_PLAN_ID}\nstatus: implemented\n---\n\n"
        "```yaml plan-tasks\n- id: C1\n  disposition: coded\n```\n",
    )
    for i in range(PLANS - 1):
        _write(
            plans / f"2026-09-{i:04d}-plan.md",
            f"---\ntitle: p{i}\nstatus: implemented\nplan_id: pln-bench-{i:06d}\n---\n\n" + _BODY,
        )
    return repo, CHAIN_MEMBER


def main(argv: list) -> int:
    if len(argv) != 1:
        print("usage: baton_chain_closure_signal <dir>", file=sys.stderr)
        return 2
    _repo, member = build_corpus(Path(argv[0]))
    print(member)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Standing-red registry for the fast tier: separates new red from registered red.

Registry file: ``state/baselines/fast-tier-standing-red.yaml``. Node ids are the
ones ``test_red_record.parse_failing_nodeids`` emits; this module never parses
test output itself.

CLI: ``python3 -m coordinator_core.ops.fast_red_registry {check | delta [--machine M] [--scope-full]}``
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

import yaml

from coordinator_core.session import record_homes

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = Path(record_homes.record_path(str(REPO_ROOT), "baselines", "fast-tier-standing-red.yaml"))

SHAPES = frozenset({"collection-import", "assertion", "fixture-env"})
DISPOSITIONS = frozenset({"fix", "named-reason-red", "cross-repo"})


@dataclass(frozen=True)
class Delta:
    new: tuple[str, ...]
    standing: tuple[str, ...]
    fixed: tuple[str, ...] | None


def load_registry(path: Path | None = None) -> dict:
    """Load the registry; a missing or non-mapping file raises ValueError."""
    p = path or REGISTRY_PATH
    if not p.is_file():
        raise ValueError(f"fast-red registry missing: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"fast-red registry is not a mapping: {p}")
    data.setdefault("clusters", {})
    data.setdefault("entries", {})
    data["clusters"] = data["clusters"] or {}
    data["entries"] = data["entries"] or {}
    return data


def _applies(cluster: dict, platform: str) -> bool:
    platforms = cluster.get("platforms")
    return not platforms or platform in platforms


def compute_delta(
    failing: Iterable[str],
    registry: dict,
    *,
    platform: str,
    scope: Sequence[str] | None,
) -> Delta:
    clusters = registry.get("clusters") or {}
    entries = registry.get("entries") or {}
    failing_set = set(failing)

    def registered(nodeid: str) -> bool:
        cluster = clusters.get(entries.get(nodeid))
        return cluster is not None and _applies(cluster, platform)

    new = tuple(sorted(n for n in failing_set if not registered(n)))
    standing = tuple(sorted(n for n in failing_set if registered(n)))

    def in_scope(nodeid: str) -> bool:
        return scope is None or any(nodeid.startswith(s) for s in scope)

    fixed = tuple(
        sorted(
            n
            for n in entries
            if n not in failing_set
            and registered(n)
            and not clusters[entries[n]].get("intermittent")
            and in_scope(n)
        )
    )
    return Delta(new=new, standing=standing, fixed=fixed)


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def registry_problems(registry: dict, *, today: date, repo_root: Path) -> list[str]:
    problems: list[str] = []
    clusters = registry.get("clusters") or {}
    entries = registry.get("entries") or {}

    for nodeid, slug in entries.items():
        if slug not in clusters:
            problems.append(f"entry {nodeid!r} names unknown cluster {slug!r}")

    used = set(entries.values())
    for slug, c in clusters.items():
        if c.get("shape") not in SHAPES:
            problems.append(f"cluster {slug}: shape {c.get('shape')!r} not in {sorted(SHAPES)}")
        if c.get("disposition") not in DISPOSITIONS:
            problems.append(
                f"cluster {slug}: disposition {c.get('disposition')!r} not in {sorted(DISPOSITIONS)}"
            )
        owner = str(c.get("owner") or "").strip()
        if not owner:
            problems.append(f"cluster {slug}: owner is empty")
        elif not (repo_root / owner).exists():
            problems.append(f"cluster {slug}: owner {owner!r} does not resolve under repo root")
        if not str(c.get("reason") or "").strip():
            problems.append(f"cluster {slug}: reason is empty")
        review_by = _as_date(c.get("review_by"))
        if review_by is None:
            problems.append(f"cluster {slug}: review_by {c.get('review_by')!r} is not a date")
        elif review_by < today:
            problems.append(f"cluster {slug}: review_by {review_by} is past")
        if slug not in used:
            problems.append(f"cluster {slug}: has no entries")
    return problems


def _cmd_check() -> int:
    try:
        problems = registry_problems(load_registry(), today=date.today(), repo_root=REPO_ROOT)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for p in problems:
        print(p)
    return 1 if problems else 0


def _cmd_delta(machine: str | None, scope_full: bool) -> int:
    from coordinator_core.machine_resolver import compute_machine

    machine = machine or compute_machine()
    record = Path(record_homes.record_path(str(REPO_ROOT), "test-red", f"{machine}.yaml"))
    try:
        data = yaml.safe_load(record.read_text(encoding="utf-8")) or {}
    except OSError:
        data = {}
    tier = ((data.get("tiers") or {}).get("fast")) or {}
    failing = tier.get("failing")
    if failing is None:
        print(f"fast-red delta: unavailable (no failing set in {record.name})", file=sys.stderr)
        return 2
    delta = compute_delta(
        failing,
        load_registry(),
        platform=sys.platform,
        scope=None if scope_full else [],
    )
    print(f"new={len(delta.new)} standing={len(delta.standing)}", end="")
    print(f" fixed={len(delta.fixed)}" if scope_full else " fixed=n/a")
    for n in delta.new:
        print(n)
    return 1 if delta.new else 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fast_red_registry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    d = sub.add_parser("delta")
    d.add_argument("--machine")
    d.add_argument("--scope-full", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "check":
        return _cmd_check()
    return _cmd_delta(args.machine, args.scope_full)


if __name__ == "__main__":
    sys.exit(main())

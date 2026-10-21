"""Successor/predecessor and merged-branch evidence for the baton close backstop.

Emits plain evidence dicts (SUCCESSOR_CLAIMED, SUCCESSOR_UNCLAIMED, SUCCESSOR_LIVE,
MERGED_BRANCH) in the shape pinned by ``baton_close_predicate``; imports nothing from it
or from ``baton_drift_sweep``.

NEGATIVE-SPEC: read-only. No write of any kind. The successor edge is the primary
``predecessor`` field only, single hop. ``merged_branches`` spawns exactly one git
process (one batched ``for-each-ref``), never one per baton; with no resolvable main ref
it spawns none and reports ``skipped``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from coordinator_core.archival import claimed_or_shipped_at_path
from coordinator_core.git.run import run_git
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT

_ARCHIVE_HANDOFFS = ("archive", "handoffs")
_HEADER_BYTES = 8192
_FIELD_RE = re.compile(r"^(predecessor|deployment_state)\s*:\s*(.*?)\s*$")
_REF_LINE_RE = re.compile(r"^ref:\s*(\S+)")


def _norm(value: object) -> str:
    """Strip quotes and a trailing comment; normalise separators to forward slash."""
    text = str(value).strip()
    if text[:1] in ("'", '"'):
        quote = text[0]
        end = text.find(quote, 1)
        text = text[1:end] if end > 0 else text[1:]
    else:
        text = text.split(" #", 1)[0].strip()
    return text.replace("\\", "/")


def _header_fields(path: Path) -> dict[str, str]:
    """Read ``predecessor`` and ``deployment_state`` from a file's frontmatter head."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(_HEADER_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return {}
    lines = head.splitlines()
    out: dict[str, str] = {}
    seen_open = False
    for line in lines:
        if line.strip() == "---":
            if seen_open:
                break
            seen_open = True
            continue
        if not seen_open and (not line.strip() or line.lstrip().startswith("<!--")):
            continue
        if not seen_open:
            break
        match = _FIELD_RE.match(line)
        if match:
            out[match.group(1)] = _norm(match.group(2))
    return out


def _evidence(kind: str, reads: str, strength: str, proposes: str | None, source: str, detail: str) -> dict:
    return {
        "kind": kind,
        "reads": reads,
        "strength": strength,
        "proposes": proposes,
        "source": source,
        "detail": detail,
    }


def _predecessor_keys(predecessor: str) -> tuple[str, str] | None:
    if not predecessor or predecessor.lower() in {"none", "null", "~", "git-history"}:
        return None
    return predecessor, predecessor.rsplit("/", 1)[-1]


def successor_evidence(worktree_root: Path, batons: dict[str, dict]) -> dict[str, list[dict]]:
    """Map each baton path to the evidence its successors give.

    ``batons`` is baton repo-relative posix path -> frontmatter. A successor terminal or
    archived reads SUCCESSOR_CLAIMED when the baton was claimed or shipped, else
    SUCCESSOR_UNCLAIMED; a live successor reads SUCCESSOR_LIVE.
    """
    root = Path(worktree_root)
    # successor entries: (source_rel, is_live_terminal_or_archived, predecessor_path, predecessor_basename)
    successors: list[tuple[str, bool, str, str]] = []
    for rel, fm in batons.items():
        keys = _predecessor_keys(_norm(fm.get("predecessor", "") or ""))
        if keys is None:
            continue
        state = _norm(fm.get("deployment_state", "") or "")
        successors.append((rel, state in HANDOFF_TERMINAL_DEPLOYMENT, keys[0], keys[1]))

    archive_dir = root.joinpath(*_ARCHIVE_HANDOFFS)
    if archive_dir.is_dir():
        for dirpath, _dirs, files in os.walk(archive_dir):
            for name in files:
                if not name.endswith(".md"):
                    continue
                full = Path(dirpath) / name
                keys = _predecessor_keys(_header_fields(full).get("predecessor", ""))
                if keys is None:
                    continue
                rel = full.relative_to(root).as_posix()
                successors.append((rel, True, keys[0], keys[1]))

    by_path: dict[str, list[tuple[str, bool]]] = {}
    by_base: dict[str, list[tuple[str, bool]]] = {}
    for source, terminal, pred_path, pred_base in successors:
        by_path.setdefault(pred_path, []).append((source, terminal))
        by_base.setdefault(pred_base, []).append((source, terminal))

    out: dict[str, list[dict]] = {}
    for rel in batons:
        base = rel.rsplit("/", 1)[-1]
        seen: set[str] = set()
        matches: list[tuple[str, bool]] = []
        for source, terminal in by_path.get(rel, []) + by_base.get(base, []):
            if source != rel and source not in seen:
                seen.add(source)
                matches.append((source, terminal))
        if not matches:
            continue
        claimed: bool | None = None
        items: list[dict] = []
        for source, terminal in matches:
            if not terminal:
                items.append(
                    _evidence("SUCCESSOR_LIVE", "live", "weak", None, source, "successor still live")
                )
                continue
            if claimed is None:
                claimed = claimed_or_shipped_at_path(str(root / rel))
            if claimed:
                items.append(
                    _evidence(
                        "SUCCESSOR_CLAIMED", "terminal", "strong", "continued", source,
                        "terminal successor names this baton as predecessor",
                    )
                )
            else:
                items.append(
                    _evidence(
                        "SUCCESSOR_UNCLAIMED", "terminal", "weak", "abandoned", source,
                        "terminal successor, baton never claimed",
                    )
                )
        out[rel] = items
    return out


def _packed_ref_exists(common_dir: Path, refname: str) -> bool:
    try:
        text = (common_dir / "packed-refs").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(line.rstrip().endswith(" " + refname) for line in text.splitlines())


def _main_ref(common_dir: Path) -> str | None:
    """Resolve the main ref by file reads only: origin/HEAD, else refs/heads/main."""
    try:
        head = (common_dir / "refs" / "remotes" / "origin" / "HEAD").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        head = ""
    match = _REF_LINE_RE.match(head.strip())
    if match:
        return match.group(1)
    if (common_dir / "refs" / "heads" / "main").is_file() or _packed_ref_exists(
        common_dir, "refs/heads/main"
    ):
        return "refs/heads/main"
    return None


def merged_branches(worktree_root: Path) -> tuple[frozenset[str] | None, str]:
    """Return ``(merged branch names, leg status)`` from one batched ``for-each-ref``.

    ``(None, "skipped: <why>")`` when the main ref cannot be read from the repo's own refs
    (no spawn) or git exits non-zero.
    """
    root = Path(worktree_root)
    try:
        common_dir = git_common_dir(root)
    except Exception:
        return None, "skipped: no main ref"
    ref = _main_ref(Path(common_dir))
    if ref is None:
        return None, "skipped: no main ref"
    result = run_git(
        [
            "-C", str(root), "for-each-ref", f"--merged={ref}",
            "--format=%(refname:short)", "refs/heads", "refs/remotes",
        ]
    )
    if result.returncode != 0:
        first = (result.stderr or "").strip().splitlines()
        return None, "skipped: " + (first[0] if first else f"git exit {result.returncode}")
    names: set[str] = set()
    for line in result.stdout.splitlines():
        name = line.strip()
        if not name or name == "origin":
            continue
        names.add(name)
        if name.startswith("origin/"):
            names.add(name[len("origin/"):])
    return frozenset(names), "ran"


def merged_branch_evidence(
    batons: dict[str, dict], merged: frozenset[str] | None
) -> dict[str, list[dict]]:
    """MERGED_BRANCH for each in-flight baton whose branch is merged (never ``candidate``)."""
    out: dict[str, list[dict]] = {}
    if not merged:
        return out
    for rel, fm in batons.items():
        branch = _norm(fm.get("branch", "") or "")
        if not branch or branch == "candidate" or branch not in merged:
            continue
        if _norm(fm.get("deployment_state", "") or "") != "in_flight":
            continue
        out[rel] = [
            _evidence("MERGED_BRANCH", "terminal", "weak", "shipped", rel, f"branch {branch} merged into main")
        ]
    return out

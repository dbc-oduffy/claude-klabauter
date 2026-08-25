"""
coordinator_core.ops.workday_start_cross_repo_memo_outbox_surface — stale
outbox-draft nudge surfacer.

Purpose: scan a repo's `state/memo-outbox/` directory (resolved via the
`coordinator_state_root` seam) for draft memos whose mtime exceeds the stale
threshold (default: 24h, `COORDINATOR_OUTBOX_STALE_HOURS`). Emits one nudge
line per stale draft so the EM can act (send / compose / discard). Silent
when the outbox is absent, empty, or all drafts are under the threshold.

Output format (one line per stale draft):
    Outbox draft <topic> staged <N>h ago -> <to>  :: <title>
      -> send | compose | discard

Exit: always 0. Emits nothing when no qualifying drafts exist (silent per
spec) — this is an orientation surfacer, never a gate.

Spec backlink: docs/plans/2026-06-15-cross-repo-memo-draft-lifecycle.md § C4
               docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Negative-spec:
    - Surfacer is offer-shape ONLY — emits three action verbs as options,
      never auto-discards, never auto-sends. Lifecycle mutation lives solely
      in the CLI subcommands per the /workstream-start surfaces, /pickup acts
      boundary (cross-repo-communication.md:315).
    - Resolves the coordinator_state_root seam via the native in-process
      port (`coordinator_core.state_root.coordinator_state_root`, bare/
      Rule-5 call) rather than shelling out to `coordinator-state-root.sh`
      (2026-07-21 de-bash cutover — see git log; was previously a
      `bash -c 'source ...; coordinator_state_root'` subshell mirroring
      ops/dirty_tree_gate.py::_resolve_handoffs_dir). A resolver failure
      (StateRootError) still degrades to an empty outbox path (no drafts
      found, silent exit 0) rather than raising — the silent-degrade
      behaviour is preserved exactly, only the resolution mechanism changed.
    - Does NOT parse full YAML frontmatter — reads only the first `to:` and
      `title:` lines via regex, byte-identical in shape to the oracle's
      `grep -E '^to:|^title:' | head -1 | sed ... | tr -d '"'"'"'` idiom
      (strip key, strip quotes, strip surrounding whitespace).
    - Does NOT recurse into subdirectories of the outbox — top-level `*.md`
      only, exactly as the oracle's `for f in "${OUTBOX_DIR}"/*.md` glob.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.state_root import StateRootError, coordinator_state_root as _native_state_root
from coordinator_core.win_portability import no_console_creationflags

_TO_RE = re.compile(r"^to:\s*(.*)$")
_TITLE_RE = re.compile(r"^title:\s*(.*)$")


def _coordinator_state_root(repo_root: str) -> Optional[str]:
    """Resolve the coordinator state root via the native in-process seam.

    *repo_root* is unused — the native seam always resolves against the
    caller's own ``os.getcwd()`` (the sole call site below already invokes
    this with ``os.getcwd()``, so behaviour is unchanged); retained only for
    call-site compatibility. Silent-degrade on any resolution failure
    (StateRootError) — returns None, never raises. See module negative-spec.
    """
    del repo_root  # unused: native seam resolves against process cwd
    try:
        return _native_state_root() or None
    except StateRootError:
        print(f"skip: _coordinator_state_root: return _native_state_root() or None failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None


def _resolve_outbox_dir(repo_root_arg: str) -> Optional[str]:
    """Resolve the outbox directory. Priority: env override -> REPO_ROOT arg -> git root.

    Returns None when not in a git repo and no override/arg was given (the
    oracle's "not in a git repo — stay silent" branch).
    """
    env_override = os.environ.get("COORDINATOR_OUTBOX_DIR", "")
    if env_override:
        return env_override

    if repo_root_arg and os.path.isdir(repo_root_arg):
        return os.path.join(repo_root_arg, "state", "memo-outbox")

    if not show_toplevel(os.getcwd()):
        # Not in a git repo — stay silent per spec.
        return None

    state_root = _coordinator_state_root(os.getcwd())
    if not state_root:
        return None
    return os.path.join(state_root, "memo-outbox")


def _get_mtime(path: str) -> int:
    try:
        return int(os.path.getmtime(path))
    except OSError:
        print(f"skip: _get_mtime: return int(os.path.getmtime(path)) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return 0


def _parse_frontmatter_field(text: str, pattern: re.Pattern) -> Optional[str]:
    """Extract the first line matching `pattern`, key-stripped and quote-stripped.

    Mirrors the oracle's `grep -E '^key:' | head -1 | sed 's/^key:[[:space:]]*//' |
    tr -d '"'"'" | xargs` chain: strip the key prefix, strip ALL single/double
    quote characters anywhere in the value (tr -d, not a paired-quote strip),
    then `xargs`-normalize (collapse all internal whitespace runs to a single
    space AND trim surrounding whitespace — xargs re-splits and re-joins its
    argv, it does not merely trim edges).
    """
    for line in text.splitlines():
        m = pattern.match(line)
        if m:
            raw = m.group(1)
            raw = raw.replace('"', "").replace("'", "")
            raw = " ".join(raw.split())
            return raw if raw else None
    return None


def _list_md_files(outbox_dir: str) -> List[str]:
    try:
        names = os.listdir(outbox_dir)
    except OSError:
        print(f"skip: _list_md_files: names = os.listdir(outbox_dir) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return []
    return sorted(
        os.path.join(outbox_dir, n) for n in names if n.endswith(".md")
    )


def main(argv: List[str]) -> int:
    repo_root_arg = argv[0] if argv else ""

    outbox_dir = _resolve_outbox_dir(repo_root_arg)
    if not outbox_dir or not os.path.isdir(outbox_dir):
        return 0

    stale_hours_raw = os.environ.get("COORDINATOR_OUTBOX_STALE_HOURS", "24")
    try:
        stale_hours = int(stale_hours_raw)
    except ValueError:
        stale_hours = 24
    stale_threshold_seconds = stale_hours * 3600

    now_epoch = int(time.time())

    lines: List[str] = []
    for f in _list_md_files(outbox_dir):
        if not os.path.isfile(f):
            continue

        mtime = _get_mtime(f)
        if mtime == 0 or now_epoch == 0:
            continue

        age_seconds = now_epoch - mtime
        if age_seconds < stale_threshold_seconds:
            continue

        age_hours = age_seconds // 3600

        topic = Path(f).name
        if topic.endswith(".md"):
            topic = topic[: -len(".md")]

        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = ""

        to_val = _parse_frontmatter_field(text, _TO_RE) or "unknown"
        title_val = _parse_frontmatter_field(text, _TITLE_RE) or "untitled"

        lines.append(
            f"Outbox draft {topic} staged {age_hours}h ago → {to_val}  :: {title_val}"
        )
        lines.append("  → send | compose | discard")

    if lines:
        sys.stdout.write("\n".join(lines) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

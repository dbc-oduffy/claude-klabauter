"""
coordinator_core.ops.workday_start_cross_repo_memo_surface — inbound
cross-repo memo surfacer for /workday-start Step 1.45.

Purpose: glob THIS repo's `cross-repo/inbox/` directory (receiver-inbound),
parse frontmatter, filter to `status: open` / `status: in_progress` memos
(skipping grandfathered pre-cutoff ones), compute staleness, emit one line
per qualifying memo. Emits nothing when zero memos qualify — callers skip
the section heading on empty output.

Single-delivery-copy model: sender writes ONE dirty file into receiver's
`cross-repo/`. This module surfaces memos awaiting THIS repo's EM action.
Receiver flips `status: open` -> `status: actioned` in place (Edit + commit);
the actioned memo then auto-sweeps to `cross-repo/archive/` (cross-repo/README.md).

Auto-sweep caller -- RETIRED 2026-07-23 (wsc-tail-slim-down C14): this module
briefly (2026-07-20 -> 2026-07-23) ran the actioned-memo sweep itself before
surfacing, duplicating `session.boot_sweep`'s `archive_actioned_memos` call
(same offender class as C2's `wsc_tail.py` sweep calls). The duplicate call
was ALSO wrapped in a bare `except Exception: return`, silently swallowing
sweep failures with zero signal -- a fail-loud-principle violation on top of
the duplication. Both were removed outright rather than patched: this module
does not need its own archival occasion, and a read-only surfacer gains
nothing by re-implementing a swallow with better wording. `session.boot_sweep`
is now the sole memo-archival occasion; its failures surface via the shared
housekeeping-failures log + orientation-cache `## Housekeeping` section
(C17a/C17b), not via this module.

Output format (one line per qualifying memo, newest-urgency-first):
    - <created> from <sender>: <title> (<age_days> days old)[STALE flag][kind flag]
and, if truncated:
    (<remaining> more -- see <inbox_dir> for full list)

Exit: always 0. Emits nothing when no qualifying memos exist (silent per
spec) -- this is an orientation surfacer, never a gate.

Spec backlink: docs/plans/2026-05-23-cross-repo-single-surface-and-canonical-scaffold.md § Chunk 3
Prior spec:    docs/plans/2026-05-21-cross-repo-memo-discoverability.md § Chunk 3
Migration spec: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Negative-spec (retired/faithfully-NOT-reimplemented bash-oracle shapes):
    - Does NOT use `yaml.safe_load` (optional dep, oracle deliberately
      dropped it per its own F3 review note) -- uses a flat regex
      key: value frontmatter extractor, matching the oracle exactly.
      Multi-line / nested YAML values are NOT supported; this is faithful
      to the oracle's scope, not a port gap.
    - Directory scan is top-level `*.md` only, no recursion -- mirrors the
      oracle's `for f in "${INBOX_DIR}"/*.md` glob.
    - Sort order mirrors the oracle's `sort` over the pipe-joined
      "<band_rank>|<created>|<sender>|<title>|<kind>" line (full-string
      lexicographic ascending), not a band_rank/created-only tuple sort --
      replicated here via a joined-string sort key for byte-parity with
      ties broken by sender/title/kind exactly as the oracle's `sort` did.
    - `title` pipe-sanitization uses an EN dash "–" (bash oracle F4);
      the STALE flag and truncation line use an EM dash "—" -- these
      are deliberately different Unicode dashes in the oracle, preserved
      here verbatim (not a typo to "fix").
    - Does NOT archive actioned memos (git mv + commit) on this path, and
      must NOT regain that call. `main()` is READ-ONLY: it globs, parses,
      and prints. Archival is `session.boot_sweep`'s sole occasion --
      re-adding a sweep call here would recreate the exact duplication
      this module shipped and then removed (2026-07-20 -> 2026-07-23, see
      "Auto-sweep caller -- RETIRED" above). If this module is ever found
      to need an archival-freshness signal, that is a new, explicit,
      fail-loud dependency on `session.boot_sweep`'s own output -- never
      a second sweep invocation of its own.
"""

from __future__ import annotations

import os
import re
import subprocess
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.git.repo_root import show_toplevel
import sys
from datetime import date
from typing import List, Optional, Tuple

_CUTOFF_DATE = "2026-05-21"
_MAX_ENTRIES = 8
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FM_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)")
_FM_BLOCK_RE = re.compile(r"^---\r?\n(.*?)\r?\n---", re.DOTALL)

# subprocess.run timeout for the `git rev-parse` root probe (rule 2, porter
# brief addendum): a single hung/prompting git invocation must never block
# the /workday-start ceremony that surfaces this output.
_GIT_TIMEOUT_SECS = 5


def _resolve_inbox_dir(cwd: Optional[str] = None) -> str:
    """Resolve THIS repo's cross-repo/inbox/ directory.

    Priority: CROSS_REPO_INBOX_DIR env override -> git root -> cwd.
    Mirrors the bash oracle's `git rev-parse --show-toplevel` probe exactly
    (subprocess, not a native git-root walk) -- the resolver silently
    falls back to cwd on any git-probe failure, matching the oracle's
    `elif git_root=$(...); else INBOX_DIR="$(pwd)/cross-repo/inbox"` ladder.
    """
    env_override = os.environ.get("CROSS_REPO_INBOX_DIR", "")
    if env_override:
        return env_override

    cwd = cwd or os.getcwd()
    git_root = show_toplevel(cwd)
    if not git_root:
        return os.path.join(cwd, "cross-repo", "inbox")
    return os.path.join(git_root, "cross-repo", "inbox")


def _parse_frontmatter(text: str) -> dict:
    """Flat key: value frontmatter extraction -- mirrors the oracle's inline
    Python regex block exactly (no yaml.safe_load dependency).
    """
    m = _FM_BLOCK_RE.match(text)
    if not m:
        return {}

    fm: dict = {}
    for kv_line in m.group(1).splitlines():
        kv_m = _FM_LINE_RE.match(kv_line)
        if kv_m:
            k, v = kv_m.group(1), kv_m.group(2).strip()
            if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
                v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            fm[k] = v
    return fm


def _qualify_memo(path: str) -> Optional[str]:
    """Parse one memo file and return its "<band_rank>|<created>|<sender>|
    <title>|<kind>" line, or None if it does not qualify. Mirrors the
    oracle's embedded per-file Python subprocess block, minus the
    subprocess hop (in-process here).
    """
    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        print(f"skip: _qualify_memo: with open(path, encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None

    fm = _parse_frontmatter(content)
    if not fm:
        return None

    created = str(fm.get("created", "")).strip()
    sender = str(fm.get("from", "")).strip()
    title = str(fm.get("title", "")).strip()
    status = str(fm.get("status", "")).strip()
    picked_up_by = str(fm.get("picked_up_by", "")).strip()

    if not status:
        return None

    if created and created <= _CUTOFF_DATE:
        return None

    if status not in ("open", "in_progress"):
        return None

    kind = str(fm.get("kind", "ask")).strip() or "ask"
    band_rank = "1" if kind == "fyi" else "0"

    title = title.replace("|", "–")
    if status == "in_progress":
        who = picked_up_by.replace("|", "–") if picked_up_by else "unknown"
        title = f"{title} [CLAIMED by {who}]"

    return f"{band_rank}|{created}|{sender}|{title}|{kind}"


def _age_days(created: str, today: date) -> int:
    """Compute age in days from an ISO date string; 0 on any parse issue --
    mirrors the oracle's positional-arg Python subprocess (arg-passed, not
    shell-interpolated, per its own F15 note) collapsed in-process here.
    """
    if not _DATE_RE.match(created):
        return 0
    try:
        c = date.fromisoformat(created)
    except ValueError:
        print(f"skip: _age_days: c = date.fromisoformat(created) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return 0
    return (today - c).days


def _resolve_today(mock_today: str) -> date:
    mock = (mock_today or "").strip()
    if mock:
        try:
            return date.fromisoformat(mock)
        except ValueError:
            print(f"skip: _resolve_today: return date.fromisoformat(mock) failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
    return date.today()


def _format_line(band_rank: str, created: str, sender: str, title: str, kind: str, today: date) -> str:
    age_days = _age_days(created, today)

    stale_flag = ""
    if age_days > 7 and "[CLAIMED" not in title:
        stale_flag = " [STALE — awaiting your action]"

    kind_flag = ""
    if kind == "fyi":
        kind_flag = " [fyi]"

    return f"- {created} from {sender}: {title} ({age_days} days old){stale_flag}{kind_flag}"


def _list_qualifying_lines(inbox_dir: str) -> List[str]:
    try:
        names = sorted(os.listdir(inbox_dir))
    except OSError:
        print(f"skip: _list_qualifying_lines: names = sorted(os.listdir(inbox_dir)) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return []

    lines: List[str] = []
    for name in names:
        if not name.endswith(".md"):
            continue
        path = os.path.join(inbox_dir, name)
        if not os.path.isfile(path):
            continue
        line = _qualify_memo(path)
        if line:
            lines.append(line)
    return lines


def _split_line(line: str) -> Tuple[str, str, str, str, str]:
    band_rank, created, sender, title, kind = line.split("|", 4)
    return band_rank, created, sender, title, kind


def main(argv: List[str]) -> int:
    inbox_dir = _resolve_inbox_dir()
    mock_today = os.environ.get("MOCK_TODAY", "")
    today = _resolve_today(mock_today)

    if not os.path.isdir(inbox_dir):
        return 0

    memo_lines = _list_qualifying_lines(inbox_dir)
    if not memo_lines:
        return 0

    # Full-string lexicographic sort over the pipe-joined line, byte-parity
    # with the oracle's `sort` invocation (see module negative-spec).
    sorted_lines = sorted(memo_lines)

    output_lines: List[str] = []
    for line in sorted_lines:
        band_rank, created, sender, title, kind = _split_line(line)
        output_lines.append(_format_line(band_rank, created, sender, title, kind, today))

    total = len(output_lines)
    emit_count = min(total, _MAX_ENTRIES)

    for i in range(emit_count):
        print(output_lines[i])

    if total > _MAX_ENTRIES:
        remaining = total - _MAX_ENTRIES
        print(f"({remaining} more — see {inbox_dir} for full list)")

    # One footer line naming the close command, printed once regardless of
    # how many memos qualified — NOT one command per memo (that would be up
    # to _MAX_ENTRIES-plus-remainder lines of noise on a boot-hot-path
    # surface). Mirrors the outbox surfacer's "→ <verbs>" action-line
    # convention (workday_start_cross_repo_memo_outbox_surface.py) but named
    # once at the bottom rather than per-entry, since closing is a single
    # command applied per memo path, not a set of bare verbs.
    print("  → close one: archive-stamp-cli resolve-memo <memo_path> [disposition-flags]")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""DR-084 corpus migration: handoff-lifecycle vocabulary overhaul (open/claimed, continued/closed).

Usage: dr084-migrate-handoff-vocabulary.py [-h] [--dry-run] [repo_root]
  repo_root   Repo root to migrate (default: cwd). $DR084_REPO_ROOT, if set to a
              non-empty value, always takes precedence over this positional --
              preserved from the tool's original env-var-wins semantics.
  --dry-run   Run the full analysis and print the identical changed/unchanged
              report, but write nothing to disk.

Portable across repos: takes the repo root as an argument rather than a hardcoded path,
so a sibling repo can run it against its own corpus. Proven on 74 claude-klabauter records
(41 live + 31 archived + 2 in a hidden .archive subdir) at C5/C8 commit e2cf1a08.


Rewrites the YAML frontmatter block of every state/handoffs and archive/handoffs
record in place, line-by-line, leaving formatting/quoting/comments and the body
untouched:
  - status: active            -> status: open
  - status: consumed          -> status: claimed
  - consumed_at:  (key only)  -> claimed_at:
  - consumed_by:  (key only)  -> claimed_by:
  - status: superseded        -> left untouched (archived-schema grandfather)
  - deployment_state: abandoned ->
      continued (+ continued_into: <successor>) if a succession child is found,
      else closed (+ closed_reason: stale + a migration provenance comment)

Idempotent: records already on new vocabulary are left alone.

Writes are atomic (temp file in the same directory + os.replace) so an interruption
mid-corpus leaves every file either fully migrated or fully untouched -- never a
half-written record.

A frontmatter `status:`/`deployment_state:` line whose value doesn't match the
expected bare-token shape (a block scalar, a quoted multi-word value, a folded
string, ...) is not migrated -- silently, in the original tool. This version
surfaces each such line as a non-fatal WARN, counted in the summary, so a record
that should have migrated but didn't is visible rather than invisible.

Live-residue duplicate guard: if a `handoff_id` is found in BOTH a
`state/handoffs/` file and an `archive/handoffs/` file at the same time (a
stray live-path copy of an already-archived record -- residue from an
unrelated upstream archival-flow bug, never created by this tool itself),
the live copy is SKIPPED rather than migrated. Only the archived copy (the
authoritative one) is edited. Earlier behavior migrated both copies with no
duplicate-id awareness, which silently made a stale live residue look like a
normal, currently-migrated record -- see `find_live_duplicate_ids()` for the
full incident this guards against (DR-084 C8 corpus run, commit 339b269a).
"""
import argparse
import glob
import os
import re
import sys
import tempfile

MIGRATION_DATE = "2026-07-22"

STATUS_RE = re.compile(r"^status:\s*(\S+)\s*$")
DEPLOYMENT_RE = re.compile(r"^deployment_state:\s*(\S+)\s*$")
CONSUMED_AT_RE = re.compile(r"^consumed_at:(\s*.*)$")
CONSUMED_BY_RE = re.compile(r"^consumed_by:(\s*.*)$")
PREDECESSOR_RE = re.compile(r"^predecessor:\s*(.*)$")
HANDOFF_ID_RE = re.compile(r'^handoff_id:\s*"?([^"\s]+)"?\s*$')
ADDL_PRED_START_RE = re.compile(r"^additional_predecessors:\s*$")
LIST_ITEM_RE = re.compile(r"^\s*-\s*(.*)$")

# Matches the key of a status/deployment_state line regardless of whether the
# value parses as a bare token -- used only to detect the unparseable case
# above, never to drive migration.
WARNABLE_KEY_RE = re.compile(r"^(status|deployment_state):\s*(.*)$")


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="DR-084 corpus migration: handoff-lifecycle vocabulary overhaul.",
    )
    parser.add_argument(
        "repo_root",
        nargs="?",
        default=None,
        help="Repo root to migrate (default: cwd). $DR084_REPO_ROOT, if set to a "
        "non-empty value, always takes precedence over this positional.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze and report as usual, but write nothing to disk.",
    )
    return parser


def resolve_repo_root(args):
    env_value = os.environ.get("DR084_REPO_ROOT")
    if env_value:
        return env_value
    return args.repo_root or os.getcwd()


def collect_files(repo_root):
    files = []
    files += glob.glob(os.path.join(repo_root, "state/handoffs/*.md"))
    files += glob.glob(os.path.join(repo_root, "archive/handoffs/*.md"))
    files += glob.glob(os.path.join(repo_root, "archive/handoffs/*/*.md"))
    return sorted(set(files))


def frontmatter_bounds(lines):
    """Locate the (opener, closer) line indices of a record's YAML frontmatter.

    Skips leading blank lines and complete leading HTML comment blocks
    (`<!-- ... -->`, possibly multiple, possibly multi-line) before requiring
    the next non-skippable line to be exactly `---`. Returns None if no opener
    is found (including when a leading `<!--` is never closed), if the first
    non-skippable line isn't `---`, or if no closer line follows the opener.
    """
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].rstrip("\r\n")
        if stripped.strip() == "":
            i += 1
            continue
        if stripped.lstrip().startswith("<!--"):
            close_line, close_pos = None, None
            j = i
            while j < n:
                pos = lines[j].find("-->")
                if pos != -1:
                    close_line, close_pos = j, pos
                    break
                j += 1
            if close_line is None:
                return None
            trailing = lines[close_line].rstrip("\r\n")[close_pos + len("-->"):]
            if trailing.strip() == "":
                i = close_line + 1
                continue
            stripped = trailing
            i = close_line
        if stripped != "---":
            return None
        opener = i
        for k in range(opener + 1, n):
            if lines[k].rstrip("\r\n") == "---":
                return (opener, k)
        return None
    return None


def strip_quotes(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_record(path, lines, bounds):
    start, end = bounds
    predecessor = None
    handoff_id = None
    deployment_state = None
    status = None
    additional_predecessors = []
    warnings = []
    i = start + 1
    while i < end:
        line = lines[i].rstrip("\r\n")
        m = PREDECESSOR_RE.match(line)
        if m:
            predecessor = strip_quotes(m.group(1))
            i += 1
            continue
        m = HANDOFF_ID_RE.match(line)
        if m:
            handoff_id = m.group(1)
            i += 1
            continue
        m = DEPLOYMENT_RE.match(line)
        if m:
            deployment_state = m.group(1)
            i += 1
            continue
        m = STATUS_RE.match(line)
        if m:
            status = m.group(1)
            i += 1
            continue
        if ADDL_PRED_START_RE.match(line):
            i += 1
            while i < end:
                m2 = LIST_ITEM_RE.match(lines[i].rstrip("\r\n"))
                if not m2:
                    break
                additional_predecessors.append(strip_quotes(m2.group(1)))
                i += 1
            continue
        wm = WARNABLE_KEY_RE.match(line)
        if wm:
            warnings.append(wm.group(1))
        i += 1
    return {
        "path": path,
        "basename": os.path.basename(path),
        "predecessor": predecessor,
        "handoff_id": handoff_id,
        "deployment_state": deployment_state,
        "status": status,
        "additional_predecessors": additional_predecessors,
        "warnings": warnings,
    }


def repo_relative(path, repo_root):
    return os.path.relpath(path, repo_root)


def _is_live_handoff_path(path, repo_root):
    rel = repo_relative(path, repo_root).replace(os.sep, "/")
    return rel.startswith("state/handoffs/")


def _is_archived_handoff_path(path, repo_root):
    rel = repo_relative(path, repo_root).replace(os.sep, "/")
    return rel.startswith("archive/handoffs/")


def find_live_duplicate_ids(records_by_path, repo_root):
    """Detect a `handoff_id` that exists both under `state/handoffs/` and
    `archive/handoffs/` at the same time -- a record that has already been
    archived but has a stray, un-deleted live-path twin (residue from an
    unrelated archival-flow bug upstream of this migration, not created by
    this tool). Returns {live_path: archived_twin_path} for every such live
    residue found, so the caller can skip migrating it instead of silently
    touching both copies and disguising the collision as "migrated, normal".

    Negative-spec: earlier behavior migrated every file `collect_files`
    returned with no duplicate-id awareness at all. When a live residue and
    its archived twin shared a `handoff_id` (residue is not this tool's own
    doing -- see module docstring), both got vocabulary-edited independently
    in the same run, which left the live residue looking like a normal,
    currently-migrated, in-date record instead of the orphaned pre-migration
    artifact it actually was. That silent legitimization is what let the
    crash-orphan reaper (`reap-orphaned-in-flight-handoffs.py`) later read
    one such residue as a real live baton and flip it back to
    open/ready_to_fire, resurrecting already-closed work. Confirmed via
    the DR-084 C8 corpus run (commit 339b269a) -- see
    docs/decisions/DR-084 addenda and archive/handoffs 2026-07-22 records
    hnd-execution-mega-gate-100600 and hnd-re-fork-the-abandoned-b1-remai.
    """
    by_id = {}
    for path, record in records_by_path.items():
        handoff_id = record.get("handoff_id")
        if not handoff_id:
            continue
        by_id.setdefault(handoff_id, []).append(path)

    skip = {}
    for handoff_id, paths in by_id.items():
        live_paths = [p for p in paths if _is_live_handoff_path(p, repo_root)]
        archived_paths = [p for p in paths if _is_archived_handoff_path(p, repo_root)]
        if not live_paths or not archived_paths:
            continue
        for live_path in live_paths:
            skip[live_path] = archived_paths[0]
    return skip


def find_successor(abandoned, records):
    candidates = [abandoned["basename"]]
    if abandoned["handoff_id"]:
        candidates.append(abandoned["handoff_id"])
    for rec in records:
        if rec["path"] == abandoned["path"]:
            continue
        refs = []
        if rec["predecessor"]:
            refs.append(rec["predecessor"])
        refs.extend(rec["additional_predecessors"])
        for ref in refs:
            ref_basename = os.path.basename(ref)
            for cand in candidates:
                if ref == cand or ref_basename == cand:
                    return rec
    return None


def _atomic_write(path, lines):
    directory = os.path.dirname(path) or "."
    try:
        original_mode = os.stat(path).st_mode
    except OSError:
        original_mode = None
    fd, tmp_path = tempfile.mkstemp(prefix=".dr084-tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.writelines(lines)
        if original_mode is not None:
            os.chmod(tmp_path, original_mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def migrate_file(path, records_by_path, repo_root, dry_run=False):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        lines = fh.readlines()

    bounds = frontmatter_bounds(lines)
    if bounds is None:
        return ("skipped-no-frontmatter", [])

    start, end = bounds
    record = records_by_path[path]
    actions = []
    new_lines = list(lines)
    out = []
    idx = start + 1
    while idx < end:
        line = new_lines[idx]
        stripped = line.rstrip("\r\n")
        newline_suffix = line[len(stripped):]

        m = STATUS_RE.match(stripped)
        if m and m.group(1) == "active":
            out.append("status: open" + newline_suffix)
            actions.append("status:active->open")
            idx += 1
            continue
        if m and m.group(1) == "consumed":
            out.append("status: claimed" + newline_suffix)
            actions.append("status:consumed->claimed")
            idx += 1
            continue

        m = CONSUMED_AT_RE.match(stripped)
        if m:
            out.append("claimed_at:" + m.group(1) + newline_suffix)
            actions.append("consumed_at->claimed_at")
            idx += 1
            continue

        m = CONSUMED_BY_RE.match(stripped)
        if m:
            out.append("claimed_by:" + m.group(1) + newline_suffix)
            actions.append("consumed_by->claimed_by")
            idx += 1
            continue

        m = DEPLOYMENT_RE.match(stripped)
        if m and m.group(1) == "abandoned":
            successor = find_successor(record, list(records_by_path.values()))
            if successor is not None:
                target = successor["handoff_id"] or repo_relative(successor["path"], repo_root)
                out.append("deployment_state: continued" + newline_suffix)
                out.append("continued_into: " + target + newline_suffix)
                actions.append("deployment_state:abandoned->continued(%s)" % target)
            else:
                out.append("deployment_state: closed" + newline_suffix)
                out.append("closed_reason: stale" + newline_suffix)
                out.append(
                    "# migration: DR-084 C8 re-expression %s (was: abandoned)"
                    % MIGRATION_DATE
                    + newline_suffix
                )
                actions.append("deployment_state:abandoned->closed(stale)")
            idx += 1
            continue

        out.append(line)
        idx += 1

    if not actions:
        return ("unchanged", [])

    new_lines[start + 1:end] = out
    if not dry_run:
        _atomic_write(path, new_lines)
    return ("changed", actions)


def main():
    args = build_arg_parser().parse_args()
    repo_root = resolve_repo_root(args)
    dry_run = args.dry_run

    if dry_run:
        print("=== DRY RUN -- no files written ===")
        print()

    files = collect_files(repo_root)
    records_by_path = {}
    for path in files:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            lines = fh.readlines()
        bounds = frontmatter_bounds(lines)
        if bounds is None:
            continue
        records_by_path[path] = parse_record(path, lines, bounds)

    live_duplicate_skip = find_live_duplicate_ids(records_by_path, repo_root)

    summary = {}
    warning_summary = {}
    changed_count = 0
    duplicate_skip_count = 0
    for path in files:
        rel = repo_relative(path, repo_root)
        if path not in records_by_path:
            print("SKIP (no frontmatter): %s" % rel)
            continue

        if path in live_duplicate_skip:
            archived_twin_rel = repo_relative(live_duplicate_skip[path], repo_root)
            print(
                "SKIP (live residue duplicates archived handoff_id -- archived "
                "twin is authoritative, not touching live copy): %s "
                "(archived twin: %s)" % (rel, archived_twin_rel)
            )
            duplicate_skip_count += 1
            continue

        for key in records_by_path[path]["warnings"]:
            print("WARN: %s: unparseable %s value -- skipped" % (rel, key))
            warning_summary[key] = warning_summary.get(key, 0) + 1

        outcome, actions = migrate_file(path, records_by_path, repo_root, dry_run=dry_run)
        if outcome == "unchanged":
            print("unchanged: %s" % rel)
            continue
        changed_count += 1
        print("changed: %s -- %s" % (rel, "; ".join(actions)))
        for a in actions:
            key = a.split("(")[0]
            summary[key] = summary.get(key, 0) + 1

    print()
    print("=== summary ===")
    print("files changed: %d / %d" % (changed_count, len(files)))
    if duplicate_skip_count:
        print(
            "live residue duplicates skipped (archived twin authoritative): %d"
            % duplicate_skip_count
        )
    for key in sorted(summary):
        print("  %s: %d" % (key, summary[key]))
    if warning_summary:
        print()
        print("=== warnings ===")
        for key in sorted(warning_summary):
            print("  unparseable %s: %d" % (key, warning_summary[key]))

    if dry_run:
        print()
        print("=== DRY RUN -- no files written ===")

    return 0


if __name__ == "__main__":
    sys.exit(main())

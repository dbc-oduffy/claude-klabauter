"""
coordinator_core.ops.fan_out_integrator — ported from
coordinator/bin/fan-out-integrator.sh (example-doctrine-repo, 469 lines, DOE-PORT clean-slate
migration, variant #1 — direct-import trampoline, no registered op; example-doctrine-repo keeps the
`.sh` filename as a polyglot trampoline over this module on cutover — nothing shells
out to this basename from another script, so no caller repoint was needed).

Purpose: fan-out-integrator compiler — given a slice-spec (TSV: slice-id, reviewer
sidecar path, comma-separated file list), validates each cited reviewer sidecar
exists on disk, runs a pairwise file-overlap pass across slices, then emits N
paste-ready `coordinator:review-integrator` dispatch prompts (one per slice) to
stdout, plus EM-only reminders to stderr. Collapses the parallel-integrator
dispatch ceremony into one EM-side call so fanning out integrators is the path of
least resistance (see spec backlink below).

Spec backlink: docs/plans/2026-06-09-partitioned-review-integrator-fan-out.md §Chunk 1
Port spec: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Input format (TSV on stdin or --spec <file>), one row per slice:
    <slice-id>TAB<reviewer-sidecar-path>TAB<comma-separated-file-paths>

Reviewer sidecar path (column 2): the path each reviewer RETURNED after
self-persisting its findings — lives under state/review-trail/findings/ (the sole
sanctioned dir). Spec backlink (self-persist design):
cross-repo/inbox/2026-07-01-reviewer-selfpersist-confinement-redirect.md

Exit codes (verbatim from the bash oracle — this module's own contract; a
DEDICATED, higher exit code for a claude-klabauter-import/transport failure is owned by the
.sh trampoline's own try/except around `_import_main()`, NOT by this module — this
module's `main()` never emits anything outside the three codes below):
    0 — success, N blocks emitted to stdout
    1 — content validation error (missing sidecar, file overlap, malformed row)
    2 — invocation error (usage, environment — including this module's own
        PLUGIN_ROOT/snippet-file resolution failure, which folds into the oracle's
        existing "environment" exit-2 bucket rather than inventing a new code,
        since it is a purely local/deterministic example-doctrine-repo-side resolution, not a
        claude-klabauter-engine transport failure; see the trampoline's own exit-3 contract
        for the transport-failure case, per docs/wiki addendum § 3b)

PLUGIN_ROOT resolution: this module needs to locate two example-doctrine-repo-side snippet files
(snippets/peer-scope-block.md, snippets/text-only-recovery-preamble.md) that live
in the example-doctrine-repo repo, not in claude-klabauter. `CLAUDE_PLUGIN_ROOT` env var is rung 1 and
is ALWAYS set by the trampoline before it calls this module's `main()` (the
trampoline trivially knows its own on-disk location, so this is a zero-failure
path in the normal call shape). The remaining rungs delegate to the shared
`coordinator_core.resolve_coordinator_clone.resolve_content_root` port of
`resolve-coordinator-clone.sh --content-root` (C11 resolver centralization —
`coordinator_core.ops.sync_plugin_wiki._resolve_plugin_root` consumes the same
shared resolver rather than a second independent rung ladder) so this module
stays independently callable/testable outside the trampoline (e.g. from pytest,
or a future non-example-doctrine-repo-trampoline caller).

Negative-spec (faithfully reproduced from the bash oracle — do NOT "fix" mid-port):
    - `slice_id` newline/tab containment check (mirrors bash oracle lines 184-189,
      "code-reviewer" review note about sed -n indexing corruption) is DEAD CODE
      here exactly as it was in the oracle: a row is split on real newlines before
      any field is extracted, so a field can never itself contain a literal
      newline, and the tab-count precondition (exactly 2 tabs) already rules out
      an embedded tab in `slice_id`. Kept for byte-for-byte behavioral parity with
      the oracle's defensive intent, not because it's reachable.
    - Whitespace trim on comma-split file-path entries uses Python's default
      `str.strip()` (POSIX `[:space:]`-equivalent — space/tab/newline/CR/FF/VT),
      matching the oracle's `[![:space:]]` pattern-match trim.

Scope simplification (NOT a behavior regression — see addendum § 7 "silent
scope-drop" caveat, which does not apply here): the oracle's `{{peer_chunks}}`
substitution shelled out to `python3 -c` specifically because bash's
`${var/pat/repl}` corrupts backslashes in Windows paths, and the oracle's own
pre-flight `command -v python3` check existed ONLY to fail loudly before that
subprocess call rather than mid-emission. This port IS Python — `str.replace()`
is the native equivalent with the same "plain string, no backslash/& reinterpre-
tation" semantics the oracle's `python3 -c` step existed to guarantee. There is
no longer a subprocess hop, so the `command -v python3` pre-flight and its
dedicated exit-2 branch have no successor state to guard — this removes an
internal implementation detail (an invisible plumbing subprocess), not any
user-visible behavior; the oracle never surfaced "python3 required" to a user
whose environment already has this module's own Python interpreter running it.

Port source: coordinator/bin/fan-out-integrator.sh (example-doctrine-repo, 469 lines,
retained as a polyglot trampoline over this module on cutover).
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Optional, Sequence, Tuple

import coordinator_core.resolve_coordinator_clone as rcc
from coordinator_core.win_portability import no_console_creationflags


_CREATIONFLAGS = no_console_creationflags()

_SUBPROCESS_TIMEOUT_SECS = 10

_USAGE_TEXT = """Usage: fan-out-integrator.sh [--spec <file>]
       printf 'slice-id\\tsidecar-path\\tfile1,file2\\n' | fan-out-integrator.sh

Input: TSV on stdin or --spec <file>, one row per slice:
  <slice-id>TAB<reviewer-sidecar-path>TAB<comma-separated-file-paths>

  slice-id              — e.g. slice-A, slice-B
  reviewer-sidecar-path — path the reviewer RETURNED (lives under state/review-trail/findings/; MUST exist on disk)
  comma-separated-file-paths — the slice's review scope (same paths as the source reviewer)

Output: N paste-ready coordinator:review-integrator dispatch prompts to stdout.
        EM reminders (parallel dispatch, commit discipline) to stderr.

Errors on:
  - missing reviewer sidecar (exit 1)
  - file-overlap between slices (exit 1)
  - malformed row / wrong field count (exit 1)
  - non-git-repo cwd (exit 2)
  - empty spec (exit 2)
"""


class _UsageError(Exception):
    """Raised for exit-2-class errors (usage/environment); message already
    written to stderr by the raiser at the point of failure."""


class _ContentError(Exception):
    """Raised for exit-1-class errors (content validation); message already
    written to stderr by the raiser at the point of failure."""


# ---------------------------------------------------------------------------
# PLUGIN_ROOT / snippet resolution
# ---------------------------------------------------------------------------


def _resolve_plugin_root() -> str:
    """PLUGIN_ROOT resolution — see module docstring. Rung 1 (`CLAUDE_PLUGIN_ROOT`)
    is checked inline for parity with the historical rung order; everything past
    it (COORDINATOR_ROOT, registry live_path, versioned cache, .doe-root pointer,
    flat-layout manifest) is delegated to the shared native port of
    `resolve-coordinator-clone.sh --content-root`
    (`coordinator_core.resolve_coordinator_clone.resolve_content_root`) — see C11's
    resolver centralization. Returns "" on total resolution failure (matches the
    oracle's own behavior: caller treats an empty/unresolvable path as a missing-
    snippet usage error, not a distinct resolver-failure code)."""
    env_override = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_override:
        return env_override

    try:
        return rcc.resolve_content_root()
    except rcc.ResolveCoordinatorCloneError:
        print(f"skip: _resolve_plugin_root: return rcc.resolve_content_root() failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------------------
# Git preconditions
# ---------------------------------------------------------------------------


def _git_is_inside_work_tree() -> bool:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _git_is_inside_work_tree: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return proc.returncode == 0


def _git_current_branch() -> str:
    try:
        proc = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _git_current_branch: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------


class _Slice:
    __slots__ = ("slice_id", "sidecar_path", "files")

    def __init__(self, slice_id: str, sidecar_path: str, files: List[str]) -> None:
        self.slice_id = slice_id
        self.sidecar_path = sidecar_path
        self.files = files


def _parse_rows(spec_content: str, err: List[str]) -> List[Tuple[str, str, str]]:
    """Parse+validate TSV rows, mirroring the bash oracle's per-row checks in
    order (empty-field checks, sidecar-exists check). Returns
    (slice_id, sidecar_path, files_raw_field) tuples. Raises _ContentError on
    the first malformed/invalid row, matching the oracle's fail-fast, no-partial-
    output behavior."""
    rows: List[Tuple[str, str, str]] = []
    row_num = 0
    for raw_line in spec_content.split("\n"):
        line = raw_line
        if line == "":
            continue
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue

        row_num += 1

        tab_count = line.count("\t")
        if tab_count != 2:
            field_count = tab_count + 1
            err.append(
                f"fan-out-integrator.sh: ERROR — malformed row {row_num}: "
                f"expected exactly 3 tab-separated fields, got {field_count}."
            )
            err.append(f"  Row content: {line}")
            err.append("  No output emitted.")
            raise _ContentError()

        slice_id, sidecar_path, files_raw_field = line.split("\t")

        if slice_id == "":
            err.append(
                f"fan-out-integrator.sh: ERROR — malformed row {row_num}: slice-id field is empty."
            )
            err.append("  No output emitted.")
            raise _ContentError()
        # Dead-code parity with the oracle — see module docstring negative-spec.
        if "\n" in slice_id or "\t" in slice_id:
            err.append(
                f"fan-out-integrator.sh: ERROR — row {row_num}: slice-id contains newline or tab character."
            )
            err.append("  No output emitted.")
            raise _ContentError()
        if sidecar_path == "":
            err.append(
                f"fan-out-integrator.sh: ERROR — malformed row {row_num} (slice '{slice_id}'): "
                "reviewer-sidecar-path field is empty."
            )
            err.append("  No output emitted.")
            raise _ContentError()
        if files_raw_field == "":
            err.append(
                f"fan-out-integrator.sh: ERROR — malformed row {row_num} (slice '{slice_id}'): "
                "file-paths field is empty."
            )
            err.append("  No output emitted.")
            raise _ContentError()

        if not os.path.isfile(sidecar_path):
            err.append(
                f"fan-out-integrator.sh: ERROR — reviewer sidecar not found on disk: {sidecar_path}"
            )
            err.append(
                f"  Slice '{slice_id}' requires a code-reviewer output file at the cited path."
            )
            err.append(
                "  Ensure the reviewer has returned and its sidecar is written before running fan-out-integrator."
            )
            err.append("  No output emitted.")
            raise _ContentError()

        rows.append((slice_id, sidecar_path, files_raw_field))

    return rows


def _parse_file_lists(
    rows: Sequence[Tuple[str, str, str]], err: List[str]
) -> List[_Slice]:
    slices: List[_Slice] = []
    for slice_id, sidecar_path, files_raw_field in rows:
        parsed: List[str] = []
        # bash's `IFS=',' read -ra paths_arr <<< "$raw_field"` drops exactly one
        # trailing empty field when raw_field ends with the delimiter (verified
        # empirically: "a,b,," -> 3 elements [a,b,""], not Python's naive
        # str.split(",") 4 elements [a,b,"",""]) — replicate that one-field drop
        # so a trailing comma doesn't spuriously trip the empty-path-entry error
        # relative to the oracle.
        raw_parts = files_raw_field.split(",")
        if raw_parts and raw_parts[-1] == "" and files_raw_field.endswith(","):
            raw_parts.pop()
        for p in raw_parts:
            trimmed = p.strip()
            if trimmed == "":
                err.append(
                    f"fan-out-integrator.sh: ERROR — slice '{slice_id}': empty path entry in "
                    "file list after splitting on commas."
                )
                err.append(f"  Raw file field: {files_raw_field}")
                err.append("  No output emitted.")
                raise _ContentError()
            parsed.append(trimmed)
        slices.append(_Slice(slice_id, sidecar_path, parsed))
    return slices


def _check_overlap(slices: Sequence[_Slice], err: List[str]) -> None:
    overlap_lines: List[str] = []
    for i in range(len(slices)):
        for j in range(i + 1, len(slices)):
            files_j = set(slices[j].files)
            for fi_path in slices[i].files:
                if fi_path in files_j:
                    overlap_lines.append(
                        f"  File '{fi_path}' claimed by both '{slices[i].slice_id}' "
                        f"and '{slices[j].slice_id}'"
                    )
    if overlap_lines:
        err.append("fan-out-integrator.sh: ERROR — file overlap detected between slices:")
        err.extend(overlap_lines)
        err.append(
            "  Action required: merge the overlapping slices into one integrator dispatch OR sequence them"
        )
        err.append("  (serial dispatch with EM verification between). Never silently pick.")
        err.append("  No output emitted.")
        raise _ContentError()


# ---------------------------------------------------------------------------
# Peer-scope template header strip
# ---------------------------------------------------------------------------


def _strip_html_comment_header(template: str) -> str:
    body_lines: List[str] = []
    in_html_comment = False
    leading_blank = True
    for tline in template.split("\n"):
        if tline.startswith("<!--") and not in_html_comment:
            in_html_comment = True
            if "-->" in tline:
                in_html_comment = False
            continue
        if in_html_comment:
            if "-->" in tline:
                in_html_comment = False
            continue
        if leading_blank and tline == "":
            continue
        leading_blank = False
        body_lines.append(tline)
    return "\n".join(body_lines)


# ---------------------------------------------------------------------------
# Dispatch block emission
# ---------------------------------------------------------------------------


def _build_dispatch_block(
    slice_obj: _Slice,
    peers: Sequence[_Slice],
    peer_scope_body: str,
    text_only_preamble: str,
    slice_count: int,
) -> List[str]:
    peer_entries: List[str] = []
    for peer in peers:
        peer_files_display = ", ".join(peer.files)
        peer_entries.append(
            f"- {peer.slice_id} (sidecar: {peer.sidecar_path}, files: {peer_files_display}) — "
            "peer integrator is running RIGHT NOW in parallel; do NOT wait for it, do NOT "
            "collate its findings with yours, your scope is yours alone"
        )
    peer_lines = "\n".join(peer_entries)
    peer_scope_filled = peer_scope_body.replace("{{peer_chunks}}", peer_lines)

    lines: List[str] = []
    lines.append("")
    lines.append(f"# ===== INTEGRATOR DISPATCH BLOCK: {slice_obj.slice_id} =====")
    lines.append("```")
    lines.append(f"## Slice: {slice_obj.slice_id}")
    lines.append("")
    lines.append("### Brief")
    lines.append(
        f"Integrate findings from `{slice_obj.sidecar_path}` into the codebase. "
        f"Slice: {slice_obj.slice_id}."
    )
    lines.append("")
    lines.append("### In-scope")
    lines.append(
        f"Files this integrator owns (same scope as the source reviewer for slice {slice_obj.slice_id}):"
    )
    lines.extend(f"- {f}" for f in slice_obj.files)
    lines.append("")
    lines.append("### Reviewer sidecar")
    lines.append(f"Read findings from: {slice_obj.sidecar_path}")
    lines.append(
        "(This file MUST exist on disk — the integrator reads findings from this path, "
        "not from inline prompt prose.)"
    )
    lines.append("")
    lines.extend(peer_scope_filled.split("\n"))
    lines.append("")
    lines.append("### Parallel-dispatch reminder")
    lines.append(
        f"This integrator is one of {slice_count} running in parallel right now — one per reviewer slice."
    )
    lines.append("Do NOT wait for peer integrators to complete. Do NOT collate peer findings into your pass.")
    lines.append("Your scope is exactly the files listed above and the findings in your sidecar only.")
    lines.append(
        "The partition was applied because one integrator cannot fit the whole surface; the same"
    )
    lines.append("constraint that drove the reviewer partition binds the integrator partition.")
    lines.append("")
    lines.append("### Destructive-action prohibition")
    lines.append("Do NOT delete, force-push, reset --hard, or run any destructive git operation.")
    lines.append(
        "Do NOT stage, push, or commit any files (exception: doctrine files per review-integrator commit discipline)."
    )
    lines.append("Do NOT modify any file outside the In-scope list above.")
    lines.append("")
    lines.append("### Disk-first verification preamble")
    lines.extend(text_only_preamble.split("\n"))
    lines.append("")
    lines.append("```")
    lines.append(f"# ===== END BLOCK: {slice_obj.slice_id} =====")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    out_lines: List[str] = []
    err_lines: List[str] = []

    try:
        spec_file = _parse_args(list(argv), err_lines)
        if spec_file is _HELP_SENTINEL:
            sys.stderr.write(_USAGE_TEXT)
            return 2

        if not _git_is_inside_work_tree():
            err_lines.append("fan-out-integrator.sh: ERROR — not inside a git repository.")
            err_lines.append(
                "  Remediation: run from within your git working tree (e.g., cd /path/to/repo)."
            )
            raise _UsageError()

        expected_branch = _git_current_branch()
        if not expected_branch:
            err_lines.append(
                "fan-out-integrator.sh: ERROR — could not determine current branch (detached HEAD?)."
            )
            err_lines.append(
                "  Remediation: checkout a named branch before running fan-out-integrator."
            )
            raise _UsageError()

        if spec_file:
            if not os.path.isfile(spec_file):
                err_lines.append(f"fan-out-integrator.sh: spec file not found: {spec_file}")
                raise _UsageError()
            with open(spec_file, encoding="utf-8") as fh:
                spec_content = fh.read().rstrip("\n")
        else:
            spec_content = sys.stdin.read().rstrip("\n")

        if spec_content == "":
            err_lines.append("fan-out-integrator.sh: empty spec — no slices to process")
            raise _UsageError()

        plugin_root = _resolve_plugin_root()
        peer_scope_snippet = os.path.join(plugin_root, "snippets", "peer-scope-block.md")
        text_only_snippet = os.path.join(plugin_root, "snippets", "text-only-recovery-preamble.md")

        if not os.path.isfile(peer_scope_snippet):
            err_lines.append(
                f"fan-out-integrator.sh: ERROR — peer-scope snippet not found: {peer_scope_snippet}"
            )
            raise _UsageError()
        with open(peer_scope_snippet, encoding="utf-8") as fh:
            peer_scope_template = fh.read().rstrip("\n")

        if not os.path.isfile(text_only_snippet):
            err_lines.append(
                f"fan-out-integrator.sh: ERROR — text-only-recovery-preamble snippet not found: {text_only_snippet}"
            )
            raise _UsageError()
        with open(text_only_snippet, encoding="utf-8") as fh:
            text_only_preamble = fh.read().rstrip("\n")

        rows = _parse_rows(spec_content, err_lines)
        if len(rows) == 0:
            err_lines.append("fan-out-integrator.sh: no valid slice rows found in spec")
            raise _UsageError()

        slices = _parse_file_lists(rows, err_lines)
        _check_overlap(slices, err_lines)

        slice_count = len(slices)

        # EM reminders → stderr (verbatim heredoc shape from the oracle)
        err_lines.append("")
        err_lines.append(
            "--- fan-out-integrator.sh: EM REMINDERS (not for integrator prompts) ---"
        )
        err_lines.append(
            f"Dispatch ALL {slice_count} integrator blocks in PARALLEL — that is the entire point of this script."
        )
        err_lines.append(
            "Each integrator handles ONE reviewer slice; dispatching them sequentially defeats the"
        )
        err_lines.append(
            "partition that was applied because one integrator couldn't fit the whole surface."
        )
        err_lines.append("")
        err_lines.append(
            "Commit discipline: YOU (the EM) commit serially after the wave completes using plain"
        )
        err_lines.append('scoped git:  git add -- <file1> <file2> ...  &&  git commit -m "<subject>"')
        err_lines.append("Integrators do NOT commit. EM commits serially after integrators return.")
        err_lines.append("")
        err_lines.append(f"Branch captured: {expected_branch}")
        err_lines.append(f"Slices in this wave: {slice_count}")
        err_lines.append("---")
        err_lines.append("")

        peer_scope_body = _strip_html_comment_header(peer_scope_template)

        for i, slice_obj in enumerate(slices):
            peers = [s for j, s in enumerate(slices) if j != i]
            out_lines.extend(
                _build_dispatch_block(
                    slice_obj,
                    peers,
                    peer_scope_body,
                    text_only_preamble,
                    slice_count,
                )
            )

    except _ContentError:
        sys.stderr.write("\n".join(err_lines) + "\n")
        return 1
    except _UsageError:
        sys.stderr.write("\n".join(err_lines) + "\n")
        return 2

    if err_lines:
        sys.stderr.write("\n".join(err_lines) + "\n")
    if out_lines:
        sys.stdout.write("\n".join(out_lines) + "\n")
    return 0


_HELP_SENTINEL = object()


def _parse_args(argv: List[str], err: List[str]):
    """Returns the spec-file path (possibly ""), or `_HELP_SENTINEL` on --help/-h.
    Raises _UsageError (message already appended to `err`) on a bad --spec or an
    unknown argument."""
    spec_file = ""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--help", "-h"):
            return _HELP_SENTINEL
        if arg == "--spec":
            if i + 1 >= len(argv) or argv[i + 1] == "":
                err.append("fan-out-integrator.sh: --spec requires a file argument")
                raise _UsageError()
            spec_file = argv[i + 1]
            i += 2
            continue
        err.append(f"fan-out-integrator.sh: unknown argument: {arg}")
        raise _UsageError()
    return spec_file


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

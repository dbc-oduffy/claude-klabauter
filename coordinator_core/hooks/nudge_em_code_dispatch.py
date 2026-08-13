"""
coordinator_core.hooks.nudge_em_code_dispatch — PreToolUse advisory op.

Purpose: Offers the executor-dispatch path (fan-out wave, /execute-plan) when the
EM (main-agent) writes a code file inline. Design-as-offers: leads with the better
path, names the inline carve-out, never blocks.

Suppression conditions (no_advisory):
  1. agent_id present — subagent writes are always allowed; executors write code.
  2. file_path absent or extension in DOC_DATA_EXTENSIONS denylist.
  3. Session sentinel present, platform-temp-dir-rooted, resolved via
       coordinator_core.session.dispatch_nudge_sentinel.sentinel_path.
  4. Autonomous-run sentinel present, platform-temp-dir-rooted, resolved via
       coordinator_core.session.autonomous_sentinel.sentinel_path.
  5. `op()` ONLY (the async `_handler` above never receives old_string/new_string/
     content — see the MultiEdit negative-spec below): the triggering edit is a
     semantic-bypass shape — whitespace-only, comment/docstring-only (heuristic,
     see `_is_comment_or_docstring_only_diff`), or a single-token rename. This is
     the size-floor mechanism for the EM-code-dispatch nudge (plan
     2026-08-01-advisory-firing-shape-predicate.md C5): a numeric length/diff-size
     threshold derived from a session-local transcript histogram or recent-commit
     diff sizes was REJECTED (one session's datapoint, wrong unit — see
     `_is_semantic_bypass_edit`'s docstring) in favor of this semantic mechanism.

Negative-spec:
  - MultiEdit edits[] are NOT forwarded by the mcp_tool hook (only scalar fields
    are forwarded). The source JS's MultiEdit edits[] walk is not portable here.
    # TODO(pcore-04 C5): if MultiEdit edits[] forwarding is added to the hook
    #   input contract, re-plumb the extension-check to walk edits[].file_path
    #   the same way nudge-em-code-dispatch.js lines 97-105 do.
  - This op is read-only (per ipc.py:28-32 negative-spec). Sentinels are only CHECKED,
    never written — writing the dispatch-nudge-ok sentinel remains the EM's job via bash.

Spec backlink: pln-pcore-04-advisory-hook-ops-mak-b219a8 § C5
Source: coordinator/hooks/scripts/nudge-em-code-dispatch.js
Design: docs/wiki/eager-agent-calibration.md (design-as-offers)
Tripwire: coordinator-tripwires.md § NUDGE-EM-CODE-DISPATCH
"""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import os
import re
import socket
import sys
import tokenize
from datetime import datetime, timezone
from pathlib import PurePosixPath

from coordinator_core._settings_home import settings_home
from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.hooks._payload import field, present
from coordinator_core.ipc import register_op
from coordinator_core.session.autonomous_sentinel import sentinel_path
from coordinator_core.session.dispatch_nudge_sentinel import (
    sentinel_path as dispatch_nudge_sentinel_path,
)

# ---------------------------------------------------------------------------
# Doc / data extension denylist — mirrors nudge-em-code-dispatch.js:30-33.
# Nudge fires on every file whose extension is NOT in this set.  A code-allowlist
# would silently exclude .sh, .rb, .kt, .sql, .swift, and extensionless scripts.
# "Guards match conditions, not containers."
# ---------------------------------------------------------------------------
_DOC_DATA_EXTENSIONS: frozenset[str] = frozenset([
    ".md", ".yaml", ".yml", ".json", ".txt", ".toml",
    ".csv", ".lock", ".cfg", ".ini",
])


def _sanitize_for_hostname(s: str) -> str:
    """Replace non-alphanumeric/hyphen/underscore chars — mirrors JS hostname sanitize."""
    return re.sub(r"[^a-zA-Z0-9_-]", "-", s)


def _resolve_session_id(session_id: str) -> tuple[str, bool]:
    """Return (resolved_sid, is_true_session_id).

    When session_id is non-empty, returns it with True (the authoritative path).
    When absent, falls back to hostname-pid (same shape as the JS fallback),
    with False — the fallback sentinel is invocation-scoped and will not match
    across hook re-invocations for the same session.

    FALLBACK-NOTE (mirrored from JS source lines 46-55): if session_id is absent,
    the sentinel path built from this fallback is pid-scoped. An EM writing a
    suppression sentinel manually must use the payload's session_id, not this
    fallback, or the match will only fire if the engine process happens to share
    the same OS pid.
    """
    if session_id:
        return session_id, True
    hostname = _sanitize_for_hostname(socket.gethostname())
    return f"{hostname}-{os.getpid()}", False


def _ext_of(file_path: str) -> str:
    """Return the lowercase extension of file_path (empty string if none)."""
    # Use PurePosixPath to avoid platform-specific path parsing on Windows
    # for paths that may use forward slashes from the hook payload.
    # Review: code-reviewer (B-F7) — body now matches the comment: PurePosixPath, not Path.
    p = PurePosixPath(file_path)
    return p.suffix.lower()


def _sentinel_exists(path: str) -> bool:
    """Blocking check — call only inside asyncio.to_thread."""
    try:
        os.stat(path)
        return True
    except OSError:
        return False


@register_op("hooks.nudge_em_code_dispatch")
async def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse advisory: nudge the EM toward dispatcher over inline code writes.

    Returns context_only("PreToolUse", msg) when the nudge fires;
    no_advisory() for all suppression conditions.
    """
    # asyncio deferred to first use here (not module scope) — this is the only function
    # in the module touching the asyncio namespace at runtime; a module-scope
    # `import asyncio` dragged asyncio.base_events (~5ms) into every eager op/hook
    # import even for callers that never dispatch this PreToolUse hook. Spec:
    # docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1.
    import asyncio

    # --- Bypass 1: subagent writes are allowed unconditionally ---
    # agent_id present → executor is supposed to write code; allow silently.
    if present(params, "agent_id"):
        return no_advisory()

    # --- Extract file path (scalar only — MultiEdit edits[] not forwarded) ---
    # TODO(pcore-04 C5): if MultiEdit edits[] forwarding lands in the hook input
    #   contract, walk edits[].file_path here the same way JS lines 97-105 do.
    file_path = field(params, "file_path")
    if not file_path:
        return no_advisory()

    # --- Bypass 2: doc/data extension denylist ---
    ext = _ext_of(file_path)
    if ext in _DOC_DATA_EXTENSIONS:
        return no_advisory()

    # --- Resolve session id and sentinel paths ---
    raw_sid = field(params, "session_id")
    session_id, has_true_sid = _resolve_session_id(raw_sid)

    nudge_ok_tmp = str(dispatch_nudge_sentinel_path(session_id))
    autonomous_tmp = str(sentinel_path(session_id))

    # --- Bypass 3: dispatch-nudge suppression sentinel ---
    found = await asyncio.to_thread(_sentinel_exists, nudge_ok_tmp)
    if found:
        return no_advisory()  # sentinel present → suppressed

    # --- Bypass 4: autonomous-run sentinel ---
    found = await asyncio.to_thread(_sentinel_exists, autonomous_tmp)
    if found:
        return no_advisory()  # autonomous mode → suppress nudge

    # --- Emit the offer-shaped nudge ---
    # Leads with the better path (dispatcher), then names the inline carve-out.
    # Never blocks, never denies.

    # When session_id was absent the resolved sentinel path is pid-scoped and
    # will not match across re-invocations — append a warning so the EM is not
    # misled into writing a sentinel that will never be seen again.
    # (Mirrors JS lines 146-156.)
    sentinel_note = "." if has_true_sid else " (this OS pid only — session_id absent)."

    nudge_message = (
        f"EM, not typist. Code-file write — dispatch an executor instead "
        f"(agent-dispatch-economics.md). Suppress by writing {nudge_ok_tmp}"
        f"{sentinel_note}"
    )

    return context_only("PreToolUse", f"[em-code-dispatch nudge] {nudge_message}")


# =============================================================================
# op(payload) — synchronous, in-process, stdin->stdout trampoline entry point.
#
# Purpose: full line-for-line port of nudge-em-code-dispatch.js's `main()`
# orchestration for the coordinator-claude-resident stdin->stdout hook stub
# (coordinator/hooks/scripts/nudge-em-code-dispatch.py), replacing the `node`
# cold-spawn on every Write/Edit/MultiEdit (constraint 7, performant-or-dead).
#
# This is a DELIBERATELY SEPARATE code path from the `_handler`/register_op
# async op above: that op is the pcore-04 mcp_tool IPC-daemon integration
# (flat-scalar `field()`/`present()` payload contract, MultiEdit edits[] NOT
# forwarded) — a different transport wired to a different (currently unused)
# consumer. `op()` below consumes the SAME raw PreToolUse JSON payload the JS
# hook received (nested tool_input, full
# MultiEdit edits[] array) and reproduces every JS branch, including the F7
# bootstrap/out-of-repo carve-out, EXT_EXECUTOR_MAP/COORDINATOR_PATH_MARKERS
# executor-type derivation, and the pending-dispatch artifact write — none of
# which the pcore-04 op implements. Do not conflate the two; do not route the
# coordinator-claude stub through the async op above.
#
# Contract: takes the raw stdin-parsed payload dict, returns a Form-A
# hookSpecificOutput dict (see context_only()) when the nudge fires, or None
# for every silent-allow/bypass path. Never raises on well-formed input;
# callers (the coordinator-claude stub) wrap this in a broad try/except for fail-open ALLOW
# on any resolve/import/run failure per constraint discipline.
#
# Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
# Source: coordinator/hooks/scripts/nudge-em-code-dispatch.js (435 lines, ported whole)
# =============================================================================

# ---------------------------------------------------------------------------
# Executor type derivation — extension -> type mapping. Mirrors JS EXT_EXECUTOR_MAP.
# ---------------------------------------------------------------------------
_EXT_EXECUTOR_MAP: dict[str, str] = {
    ".py": "python-executor",
    ".js": "js-executor",
    ".mjs": "js-executor",
    ".cjs": "js-executor",
    ".ts": "ts-executor",
    ".tsx": "ts-executor",
    ".jsx": "js-executor",
    ".sh": "shell-executor",
    ".bash": "shell-executor",
    ".rb": "ruby-executor",
    ".go": "go-executor",
    ".rs": "rust-executor",
    ".java": "jvm-executor",
    ".kt": "jvm-executor",
    ".c": "cpp-executor",
    ".cpp": "cpp-executor",
    ".cc": "cpp-executor",
    ".h": "cpp-executor",
    ".hpp": "cpp-executor",
    ".swift": "swift-executor",
    ".sql": "sql-executor",
}

# Path markers that signal coordinator-domain files -> coordinator-executor override.
# Mirrors JS COORDINATOR_PATH_MARKERS.
_COORDINATOR_PATH_MARKERS: list[str] = [
    "coordinator/",
    "coordinator_core/",
    "plugins/coordinator",
]

# Extensionless filename patterns -> devops-executor. Mirrors JS main()'s regex bank.
_DEVOPS_FILENAME_RE = re.compile(
    r"^(?:[Mm]akefile|[Dd]ockerfile(?:\..+)?|[Rr]akefile|[Gg]runtfile\.js|[Gg]ulpfile\.js)$"
)


def _bootstrap_dirs() -> list[str]:
    """Machine-config/bootstrap dirs the F7 carve-out skips the nudge for.

    Mirrors JS bootstrapDirs() for the `$HOME`-rooted rungs: HOME must be set
    (matches JS's process.env.HOME guard), and those rungs are omitted when it
    is absent.

    DIVERGENCE FROM THE JS MIRROR — `<settings-home>/bin` is resolved
    independently of the HOME guard, via the canonical
    `coordinator_core._settings_home.settings_home()` (COORDINATOR_SETTINGS_HOME,
    else CLAUDE_HOME, else the platform home, with `.coordinator-claude-settings`
    appended to the latter two). That resolver has a Windows rung `Path.home()`
    that a bare `$HOME` read does not, so gating it behind `HOME` would drop the
    live executable surface's carve-out on exactly the platform where HOME is
    unset. Resolution failure (a cwd-relative COORDINATOR_SETTINGS_HOME, or an
    undeterminable home) degrades to omitting the rung, never to raising — this
    is an advisory hook on the write path.

    `~/.claude/bin` is retained as a legacy rung: its executable surface moved to
    `<settings-home>/bin` on 2026-07-28 (the old dir renamed aside), but a machine
    that has not been reinstalled since still has live bits there, and a stale
    carve-out entry costs nothing but a suppressed nudge on a dir nobody writes.

    Matching is prefix-on-abspath (see `_is_under_bootstrap_dir`), not realpath —
    a symlinked settings home is matched by the path as written, not its target.
    """
    dirs: list[str] = []

    try:
        dirs.append(str(settings_home() / "bin"))
    except (ValueError, RuntimeError, OSError):
        pass

    home = os.environ.get("HOME")
    if home:
        dirs.extend([
            os.path.join(home, ".local", "bin"),
            os.path.join(home, ".claude", "bin"),
            os.path.join(home, ".claude", "shell"),
        ])

    return dirs


def _is_under_bootstrap_dir(file_path: str) -> bool:
    """True when file_path sits under one of the bootstrap dirs. Mirrors JS."""
    abs_file = os.path.abspath(file_path)
    for d in _bootstrap_dirs():
        abs_dir = os.path.abspath(d)
        if abs_file == abs_dir or (abs_file + os.sep).startswith(abs_dir + os.sep):
            return True
    return False


def _is_outside_git_work_tree(file_path: str) -> bool:
    """True when file_path is NOT inside any git work-tree.

    Walks up from the file's containing directory looking for a `.git` entry
    (file or dir — worktrees/submodules use a `.git` file). Mirrors JS
    isOutsideGitWorkTree() exactly, including the "existence, not directory-ness"
    check and the root-of-filesystem termination.
    """
    d = os.path.abspath(os.path.dirname(file_path))
    root = os.path.splitdrive(d)[0] + os.sep if os.name == "nt" else os.sep
    while True:
        try:
            if os.path.exists(os.path.join(d, ".git")):
                return False  # found an enclosing .git -> inside a work-tree
        except OSError as exc:
            print(f"nudge_em_code_dispatch: cannot stat {d}: {exc} (treating as not found)", file=sys.stderr)
        if d == root:
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return True  # no .git found all the way to the filesystem root


def _is_bootstrap_or_out_of_repo(file_path: str) -> bool:
    """F7 carve-out predicate. Mirrors JS isBootstrapOrOutOfRepo()."""
    if _is_under_bootstrap_dir(file_path):
        return True
    if _is_outside_git_work_tree(file_path):
        return True
    return False


def _derive_executor_info(file_path: str) -> tuple[str, bool]:
    """Derive executor type from file extension and path prefix.

    Returns (executor_type, ambiguous). Mirrors JS deriveExecutorInfo().
    """
    ext = os.path.splitext(file_path)[1].lower()
    basename = os.path.basename(file_path)
    norm_path = file_path.replace("\\", "/")

    if any(marker in norm_path for marker in _COORDINATOR_PATH_MARKERS):
        return "coordinator-executor", False

    by_ext = _EXT_EXECUTOR_MAP.get(ext)
    if by_ext:
        return by_ext, False

    if _DEVOPS_FILENAME_RE.match(basename):
        return "devops-executor", False

    return "generic-executor", True


def _describe_edit(payload: dict) -> str:
    """Payload-derived one-line description of the triggering change.

    Fills the dispatch brief's task line from data the hook already has (tool
    name, edit shape) instead of inventing intent the hook cannot know — the
    hook sees WHAT changed, never WHY. Replaces the literal
    "task: [TODO: describe the specific change you want made to this file]"
    placeholder (plan 2026-08-01-advisory-firing-shape-predicate.md C5): a
    brief that hands the agent a TODO names no concrete alternative, which is
    the same Axis-A defect this plan closes elsewhere in the corpus.
    """
    tool_name = payload.get("tool_name") or "Write/Edit"
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    edits = tool_input.get("edits")
    if isinstance(edits, list) and edits:
        # Review: code-reviewer (Finding 6) -- name the shape of the change
        # (string replacement) for parity with the Edit/Write branches below,
        # not merely a count.
        return f"{tool_name}: {len(edits)} string replacement(s) in this call"
    if "old_string" in tool_input:
        return f"{tool_name}: string replacement"
    if "content" in tool_input:
        return f"{tool_name}: full-file content write"
    return f"{tool_name} on this file"


def _build_dispatch_brief(file_path: str, executor_type: str, edit_description: str) -> str:
    """Build a ready-to-paste executor dispatch brief. Mirrors JS buildDispatchBrief(),
    except the task line is filled from `edit_description` (payload-derived, see
    `_describe_edit`) rather than a `[TODO: ...]` placeholder, and the
    acceptance-criteria section is dropped rather than left as a second TODO — the
    hook has no payload-derivable "done condition" to offer in its place.
    """
    return "\n".join(
        [
            "## Pre-assembled dispatch brief",
            f"file:          {file_path}",
            f"executor-type: {executor_type}",
            "commit:        false — EM commits after verification",
            "---",
            f"task:          {edit_description}",
            "",
            "Dispatch: fan-out-dispatch.sh or Agent (docs/wiki/dispatching-parallel-agents.md).",
        ]
    )


def _write_pending_dispatch_artifact(
    session_id: str, file_path: str, executor_type: str, dispatch_brief_text: str
) -> None:
    """Write the pending-dispatch artifact to tempfile.gettempdir().

    Silently no-ops on write failure — the artifact is a convenience, not a
    gate. Mirrors JS writePendingDispatchArtifact().
    """
    import tempfile

    hash8 = hashlib.sha256(file_path.encode("utf-8")).hexdigest()[:8]
    artifact_path = os.path.join(
        tempfile.gettempdir(),
        f"coordinator-pending-dispatch-{session_id}-{hash8}.json",
    )
    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    artifact = {
        "schema": "coordinator-pending-dispatch/v1",
        "generated_at": generated_at,
        "session_id": session_id,
        "file_path": file_path,
        "executor_type": executor_type,
        "dispatch_brief_text": dispatch_brief_text,
        "ambiguous": False,
    }
    try:
        with open(artifact_path, "w", encoding="utf-8") as fh:
            json.dump(artifact, fh, indent=2)
    except OSError:
        pass  # best-effort: artifact write failure does not affect the nudge path


def _resolve_session_id_op(payload: dict) -> str:
    """Resolve session id per JS resolveSessionId() sanitize-then-fallback logic.

    Differs from `_resolve_session_id()` above (the pcore-04 op's helper,
    which never sanitizes a non-empty session_id) — JS strips every char
    outside [A-Za-z0-9_-] from a supplied session_id before using it as a
    filename component, falling through to the hostname-pid fallback only
    when sanitization strips it to empty (e.g. "../../..").
    """
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid.strip():
        safe = re.sub(r"[^A-Za-z0-9_-]", "", sid.strip())
        if safe:
            return safe
    hostname = _sanitize_for_hostname(socket.gethostname())
    return f"{hostname}-{os.getpid()}"


# ---------------------------------------------------------------------------
# Semantic-bypass mechanism (AC6) — the size floor for the nudge.
#
# Purpose: `op()` had zero occurrences of a length/diff-size/changed-line
# threshold of any kind — a one-character Edit nudged identically to a
# full-file rewrite. Plan 2026-08-01-advisory-firing-shape-predicate.md C5
# REJECTS deriving that floor from a session-local transcript histogram or
# from recent-commit diff sizes: one session's datapoint, wrong unit (a
# commit aggregates many Edit calls and excludes reverted ones; the hook
# sees exactly one tool call), and it measures what happened rather than
# what SHOULD have been dispatched. This module ships the PLAN-PREFERRED
# alternative instead of a guessed number: a semantic-bypass mechanism. An
# edit confined to whitespace, to comment/docstring text, or to a single
# identifier rename is defensible without a threshold — it is closer to
# what the nudge is FOR (steering substantive code authorship to a
# dispatched executor) than any character count would be.
#
# Negative-spec: this bypass exists ONLY on `op()`. The async `_handler`
# above (the pcore-04 mcp_tool op) never receives old_string/new_string or
# MultiEdit edits[] — see the module's MultiEdit negative-spec — so it has
# no diff to classify and is unaffected by this mechanism.
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"\w+|\W+")


def _tokenize_for_classification(src: str) -> list | None:
    """Tokenize src, returning (type, string) pairs, or None if unparseable.

    Review: code-reviewer (Findings 1/2) — raw-text/line-prefix classification
    cannot tell a STRING token's content from cosmetic whitespace, or a real
    `#` COMMENT from a string literal's line that happens to start with `#`.
    Tokenizing and comparing by token TYPE closes both gaps at once. Returns
    None on any tokenize failure so the caller can fail toward emitting the
    nudge rather than guessing at intent from unparseable source.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError, ValueError):
        return None
    return [(tok.type, tok.string) for tok in tokens]


def _is_whitespace_only_diff(old: str, new: str) -> bool:
    """True when old and new tokenize identically ignoring whitespace-shaped tokens.

    Tokenize-based (Review: code-reviewer Finding 1) rather than raw-text
    whitespace-stripping: a raw-text strip cannot distinguish cosmetic
    formatting whitespace from whitespace that is itself a STRING token's
    semantic content (e.g. `"hello world"` -> `"helloworld"`). Comparing
    non-NEWLINE/INDENT/DEDENT/NL/COMMENT token streams means a STRING token
    change is never misclassified as whitespace-only. Unparseable input fails
    toward NOT bypassing (nudge still fires).
    """
    old_tokens = _tokenize_for_classification(old)
    new_tokens = _tokenize_for_classification(new)
    if old_tokens is None or new_tokens is None:
        return False
    ignored = {
        tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT,
        tokenize.ENCODING, tokenize.ENDMARKER,
    }
    old_sig = [(t, s) for t, s in old_tokens if t not in ignored]
    new_sig = [(t, s) for t, s in new_tokens if t not in ignored]
    return old_sig == new_sig


def _is_comment_or_docstring_only_diff(old: str, new: str) -> bool:
    """True when the only token-stream differences are COMMENT tokens.

    Tokenize-based (Review: code-reviewer Finding 2) rather than line-prefix
    matching: a `#`-prefixed line inside a triple-quoted STRING is a single
    STRING token, never a COMMENT token, so it is correctly excluded from this
    bypass. Only a genuine Python `#` comment classifies as COMMENT. Compares
    non-comment, non-whitespace-shaped token streams for equality and requires
    the raw token streams (including comments) to actually differ, so a
    genuine no-op diff does not count as "comment-only". Unparseable input
    fails toward NOT bypassing (nudge still fires) — the documented posture
    this predicate has always claimed.
    """
    old_tokens = _tokenize_for_classification(old)
    new_tokens = _tokenize_for_classification(new)
    if old_tokens is None or new_tokens is None:
        return False
    if old_tokens == new_tokens:
        return False
    ignored = {
        tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT,
        tokenize.ENCODING, tokenize.ENDMARKER, tokenize.COMMENT,
    }
    old_sig = [(t, s) for t, s in old_tokens if t not in ignored]
    new_sig = [(t, s) for t, s in new_tokens if t not in ignored]
    return old_sig == new_sig


def _is_single_token_rename(old: str, new: str) -> bool:
    """True when old -> new differs by exactly one `\\w+` token in one place.

    Tokenizes on the word / non-word boundary (`_WORD_RE`) so surrounding
    punctuation and whitespace are preserved as context; the diff must reduce
    to a single `replace` opcode swapping exactly one identifier-shaped token
    for a different identifier-shaped token. A rename that recurs at multiple
    call sites within the SAME old_string/new_string pair is not recognized
    (the diff would show multiple replace regions) — narrower than "rename",
    deliberately: it fails toward nudging, never toward silently bypassing an
    edit this heuristic cannot confirm is rename-shaped.
    """
    old_tokens = _WORD_RE.findall(old)
    new_tokens = _WORD_RE.findall(new)
    matcher = difflib.SequenceMatcher(a=old_tokens, b=new_tokens, autojunk=False)
    replacements = [op for op in matcher.get_opcodes() if op[0] != "equal"]
    if len(replacements) != 1:
        return False
    tag, i1, i2, j1, j2 = replacements[0]
    if tag != "replace":
        return False
    old_slice = old_tokens[i1:i2]
    new_slice = new_tokens[j1:j2]
    if len(old_slice) != 1 or len(new_slice) != 1:
        return False
    old_tok, new_tok = old_slice[0], new_slice[0]
    # Identifier-shaped (leading letter/underscore), not merely `\w+` — a bare
    # `\w+` also matches digit-only tokens, which would misclassify a literal
    # value change ("1" -> "2") as an identifier rename.
    identifier_re = re.compile(r"[A-Za-z_]\w*")
    if not identifier_re.fullmatch(old_tok) or not identifier_re.fullmatch(new_tok):
        return False
    return old_tok != new_tok


def _is_semantic_bypass_edit(old: str, new: str) -> bool:
    """True when old -> new is defensible without a size threshold (AC6).

    Covers: no-op (old == new), whitespace-only, comment/docstring-only
    (heuristic), single-token rename. See the section docstring above for why
    this replaces a numeric floor.
    """
    if old == new:
        return True
    if _is_whitespace_only_diff(old, new):
        return True
    if _is_comment_or_docstring_only_diff(old, new):
        return True
    if _is_single_token_rename(old, new):
        return True
    return False


def _read_pre_edit_content(file_path: str) -> str | None:
    """Best-effort read of file_path's ON-DISK content before a Write executes.

    PreToolUse fires before the tool runs, so the file on disk still holds the
    pre-write content (or does not exist yet, for a brand-new file). Returns
    None on any failure (missing file, permission, decode error) — the caller
    then treats the semantic bypass as inapplicable, which fails TOWARD
    emitting the nudge, never toward silently suppressing it on an unreadable
    baseline.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _semantic_bypass_applies(file_path: str, tool_input: dict) -> bool:
    """Bypass 2.5 predicate for `op()`: does the triggering edit qualify as a
    semantic-bypass shape (AC6)?

    Edit: classifies old_string/new_string directly.
    MultiEdit: ALL edits[] entries must individually qualify — one substantive
      edit among several disqualifies the whole call.
    Write: reads the file's pre-write content off disk and classifies it
      against `content`; a brand-new file (nothing to read) never qualifies.
    Anything else (missing/malformed fields): does not qualify — fails toward
    the nudge firing, per this module's general fail-open-to-nudge posture.
    """
    edits = tool_input.get("edits")
    if isinstance(edits, list) and edits:
        pairs: list[tuple[str, str]] = []
        for edit in edits:
            if not isinstance(edit, dict):
                return False
            old = edit.get("old_string")
            new = edit.get("new_string")
            if not isinstance(old, str) or not isinstance(new, str):
                return False
            pairs.append((old, new))
        return bool(pairs) and all(_is_semantic_bypass_edit(o, n) for o, n in pairs)

    old_string = tool_input.get("old_string")
    new_string = tool_input.get("new_string")
    if isinstance(old_string, str) and isinstance(new_string, str):
        return _is_semantic_bypass_edit(old_string, new_string)

    content = tool_input.get("content")
    if isinstance(content, str):
        old_content = _read_pre_edit_content(file_path)
        if old_content is None:
            return False
        return _is_semantic_bypass_edit(old_content, content)

    return False


def op(payload: dict) -> dict | None:
    """PreToolUse advisory: nudge the EM toward dispatcher over inline code writes.

    Full line-for-line port of nudge-em-code-dispatch.js's `main()`. Returns a
    Form-A hookSpecificOutput dict on nudge-fire, None on every silent-allow path.
    Never raises on well-formed input.
    """
    # --- Bypass 1: subagent writes are allowed unconditionally ---
    if "agent_id" in payload:
        return None

    # --- Extract file path (scalar Write, or MultiEdit edits[] scan) ---
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    file_path = tool_input.get("file_path")
    multiple_code_files = False

    edits = tool_input.get("edits")
    if not file_path and isinstance(edits, list):
        code_file_count = 0
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            edit_path = edit.get("file_path") or ""
            edit_ext = os.path.splitext(edit_path)[1].lower()
            if edit_ext not in _DOC_DATA_EXTENSIONS:
                code_file_count += 1
                if not file_path:
                    file_path = edit_path
        multiple_code_files = code_file_count > 1

    if not file_path:
        return None

    # --- Bypass 1.5: machine-config / bootstrap / out-of-repo writes (F7) ---
    if _is_bootstrap_or_out_of_repo(file_path):
        return None

    # --- Bypass 2: doc/data extension denylist ---
    ext = os.path.splitext(file_path)[1].lower()
    if ext in _DOC_DATA_EXTENSIONS:
        return None

    # --- Bypass 2.5: semantic-bypass edit (AC6 size floor) ---
    if _semantic_bypass_applies(file_path, tool_input):
        return None

    # --- Resolve session id and sentinel paths ---
    session_id = _resolve_session_id_op(payload)

    # --- Bypass 3: dispatch-nudge suppression sentinel ---
    nudge_ok_sentinel = str(dispatch_nudge_sentinel_path(session_id))
    if _sentinel_exists(nudge_ok_sentinel):
        return None

    # --- Bypass 4: autonomous-run sentinel ---
    autonomous_sentinel = str(sentinel_path(session_id))
    if _sentinel_exists(autonomous_sentinel):
        return None

    # --- Derive executor type and build dispatch brief ---
    executor_type, ambiguous = _derive_executor_info(file_path)
    edit_description = _describe_edit(payload)
    dispatch_brief_text = _build_dispatch_brief(file_path, executor_type, edit_description)

    # --- Write pending-dispatch artifact for unambiguous single-file cases ---
    if not ambiguous and not multiple_code_files:
        _write_pending_dispatch_artifact(
            session_id, file_path, executor_type, dispatch_brief_text
        )

    # --- Emit the offer-shaped nudge ---
    raw_sid = payload.get("session_id")
    has_true_session_id = isinstance(raw_sid, str) and raw_sid.strip() != ""

    artifact_note = "" if ambiguous or multiple_code_files else " Artifact written."

    sentinel_suffix = "." if has_true_session_id else " (this OS pid only — session_id absent)."

    nudge_message = (
        f"EM, not typist. Code write: {file_path}. Dispatch an executor instead "
        "(agent-dispatch-economics.md). "
        f"Suppress: write {nudge_ok_sentinel}{sentinel_suffix}\n\n{dispatch_brief_text}"
        f"{artifact_note}"
    )

    return context_only("PreToolUse", f"[em-code-dispatch nudge] {nudge_message}")

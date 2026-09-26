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
  4. Autonomous-run mode resolved via
       coordinator_core.session.mode_resolution.resolve_mode("autonomous", session_id)
       — a session-wins key, so the session's own sentinel always takes precedence
       over any fleet-level value for this key.
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
from coordinator_core.hooks._envelope import context_only, no_advisory, payload_of
from coordinator_core.hooks._payload import field, present
from coordinator_core.ipc import register_op
from coordinator_core.session.dispatch_nudge_sentinel import (
    sentinel_path as dispatch_nudge_sentinel_path,
)
from coordinator_core.session.mode_resolution import resolve_mode

_DOC_DATA_EXTENSIONS: frozenset[str] = frozenset([
    ".md", ".yaml", ".yml", ".json", ".txt", ".toml",
    ".csv", ".lock", ".cfg", ".ini",
])

GENERATES: list = []


def _sanitize_for_hostname(s: str) -> str:
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
    p = PurePosixPath(file_path)
    return p.suffix.lower()


def _sentinel_exists(path: str) -> bool:
    try:
        os.stat(path)
        return True
    except OSError:
        return False


@register_op("hooks.nudge_em_code_dispatch")
async def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    import asyncio

    if present(params, "agent_id"):
        return no_advisory()

    file_path = field(params, "file_path")
    if not file_path:
        return no_advisory()

    ext = _ext_of(file_path)
    if ext in _DOC_DATA_EXTENSIONS:
        return no_advisory()

    raw_sid = field(params, "session_id")
    session_id, has_true_sid = _resolve_session_id(raw_sid)

    nudge_ok_tmp = str(dispatch_nudge_sentinel_path(session_id))

    found = await asyncio.to_thread(_sentinel_exists, nudge_ok_tmp)
    if found:
        return no_advisory()

    autonomous_run = await asyncio.to_thread(resolve_mode, "autonomous", session_id)
    if autonomous_run:
        return no_advisory()


    sentinel_note = "." if has_true_sid else " (this OS pid only — session_id absent)."

    nudge_message = (
        f"EM, not typist. Code-file write — dispatch an executor instead "
        f"(agent-dispatch-economics.md). Suppress by writing {nudge_ok_tmp}"
        f"{sentinel_note}"
    )

    return context_only("PreToolUse", f"[em-code-dispatch nudge] {nudge_message}")


# This is a DELIBERATELY SEPARATE code path from the `_handler`/register_op
# bootstrap/out-of-repo carve-out, EXT_EXECUTOR_MAP/COORDINATOR_PATH_MARKERS

# Executor type derivation — extension -> type mapping. Mirrors JS EXT_EXECUTOR_MAP.
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

# Mirrors JS COORDINATOR_PATH_MARKERS.
_COORDINATOR_PATH_MARKERS: list[str] = [
    "coordinator/",
    "coordinator_core/",
    "plugins/coordinator",
]

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
    abs_file = os.path.abspath(file_path)
    for d in _bootstrap_dirs():
        abs_dir = os.path.abspath(d)
        if abs_file == abs_dir or (abs_file + os.sep).startswith(abs_dir + os.sep):
            return True
    return False


def _is_outside_git_work_tree(file_path: str) -> bool:
    d = os.path.abspath(os.path.dirname(file_path))
    root = os.path.splitdrive(d)[0] + os.sep if os.name == "nt" else os.sep
    while True:
        try:
            if os.path.exists(os.path.join(d, ".git")):
                return False
        except OSError as exc:
            print(f"nudge_em_code_dispatch: cannot stat {d}: {exc} (treating as not found)", file=sys.stderr)
        if d == root:
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return True


def _is_bootstrap_or_out_of_repo(file_path: str) -> bool:
    if _is_under_bootstrap_dir(file_path):
        return True
    if _is_outside_git_work_tree(file_path):
        return True
    return False


def _derive_executor_info(file_path: str) -> tuple[str, bool]:
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
    tool_name = payload.get("tool_name") or "Write/Edit"
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    edits = tool_input.get("edits")
    if isinstance(edits, list) and edits:
        return f"{tool_name}: {len(edits)} string replacement(s) in this call"
    if "old_string" in tool_input:
        return f"{tool_name}: string replacement"
    if "content" in tool_input:
        return f"{tool_name}: full-file content write"
    return f"{tool_name} on this file"


def _build_dispatch_brief(file_path: str, executor_type: str, edit_description: str) -> str:
    del file_path
    return "\n".join(
        [
            f"type: {executor_type}",
            f"task: {edit_description}",
        ]
    )


def _write_pending_dispatch_artifact(
    session_id: str, file_path: str, executor_type: str, dispatch_brief_text: str
) -> None:
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
        with open(artifact_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(artifact, fh, indent=2)
    except OSError:
        pass


def _resolve_session_id_op(payload: dict) -> str:
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid.strip():
        safe = re.sub(r"[^A-Za-z0-9_-]", "", sid.strip())
        if safe:
            return safe
    hostname = _sanitize_for_hostname(socket.gethostname())
    return f"{hostname}-{os.getpid()}"


# what SHOULD have been dispatched. This module ships the PLAN-PREFERRED

_WORD_RE = re.compile(r"\w+|\W+")


def _tokenize_for_classification(src: str) -> list | None:
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError, ValueError):
        return None
    return [(tok.type, tok.string) for tok in tokens]


def _is_whitespace_only_diff(old: str, new: str) -> bool:
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
    identifier_re = re.compile(r"[A-Za-z_]\w*")
    if not identifier_re.fullmatch(old_tok) or not identifier_re.fullmatch(new_tok):
        return False
    return old_tok != new_tok


def _is_semantic_bypass_edit(old: str, new: str) -> bool:
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
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _semantic_bypass_applies(file_path: str, tool_input: dict) -> bool:
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
    if "agent_id" in payload:
        return None

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

    if _is_bootstrap_or_out_of_repo(file_path):
        return None

    ext = os.path.splitext(file_path)[1].lower()
    if ext in _DOC_DATA_EXTENSIONS:
        return None

    if _semantic_bypass_applies(file_path, tool_input):
        return None

    session_id = _resolve_session_id_op(payload)

    nudge_ok_sentinel = str(dispatch_nudge_sentinel_path(session_id))
    if _sentinel_exists(nudge_ok_sentinel):
        return None

    if resolve_mode("autonomous", session_id):
        return None

    executor_type, ambiguous = _derive_executor_info(file_path)
    edit_description = _describe_edit(payload)
    dispatch_brief_text = _build_dispatch_brief(file_path, executor_type, edit_description)

    if not ambiguous and not multiple_code_files:
        _write_pending_dispatch_artifact(
            session_id, file_path, executor_type, dispatch_brief_text
        )

    raw_sid = payload.get("session_id")
    has_true_session_id = isinstance(raw_sid, str) and raw_sid.strip() != ""

    # 216 / write_guards 171 prose bytes (both <= MESSAGE_PROSE_CAP_BYTES ==
    sentinel_suffix = "." if has_true_session_id else " (this OS pid only — session_id absent)."

    nudge_message = (
        f"Write: {file_path}. Suppress: write {nudge_ok_sentinel}{sentinel_suffix}"
        f"\n\n{dispatch_brief_text}"
    )

    return context_only("PreToolUse", f"[em-code-dispatch nudge] {nudge_message}")

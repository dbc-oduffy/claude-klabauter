"""
coordinator_core.ops.check_rag_state — detect example-retrieval-repo freshness, print one token.

Purpose: centralises the RAG-state detection logic shared by update-docs,
enrich-and-review, and the project-orientation hook — each caller invokes this
op and gates repomap generation on the result. Full gating doctrine:
coordinator/docs/wiki/repomap-rag-gating.md (coordinator-claude).

Output (stdout, one token):
    absent   — no example-retrieval-repo MCP tool is registered in this session
    stale    — RAG present but staleness banner was emitted at session start
    fresh    — RAG present and freshness marker is current
    unknown  — could not determine state from the readable signals

Exit codes:
    0 — state is one of: absent, stale, fresh
    1 — state is unknown (callers should treat as stale), OR the coordinator-claude root /
        plugin-root trust preconditions could not be satisfied

Port of: check-rag-state.sh (coordinator-claude b5a4192c, 2026-07-20)
Spec backlink: coordinator/docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Negative-spec:
    - Does NOT perform any MCP tool calls; it only reads the marker file and
      environment variables the bash oracle read — MCP availability cannot be
      probed from here either.
    - Trust-checks plugin_root via the canonical
      `coordinator_core.trusted_root_guard.is_trusted` — see that module for
      the full anchor list.
    - The fail-loud trust-violation stderr message hardcodes the site label
      as `"coordinator/bin/check-rag-state.sh"` rather than reproducing the
      bash oracle's `--site="$0"` (the invoking script path) — the polyglot
      trampoline's `main(argv)` never receives argv[0], and this corner case
      (an untrusted resolved plugin root) is not asserted by any known
      caller/test; the message shape and remediation text are preserved
      verbatim, only the site token is a fixed string instead of dynamic.
"""

from __future__ import annotations

import os
import sys
from typing import Optional, Tuple

from coordinator_core.trusted_root_guard import is_trusted as _is_trusted_root

_VALID_TOKENS = ("absent", "stale", "fresh")
_SITE_LABEL = "coordinator/bin/check-rag-state.sh"


def _claude_home() -> str:
    """Mirror the bash oracle's `${CLAUDE_HOME:-$HOME}/.claude`."""
    base = (
        os.environ.get("CLAUDE_HOME")
        or os.environ.get("HOME")
        or os.environ.get("USERPROFILE")
        or os.path.expanduser("~")
    )
    return os.path.join(base, ".claude")


def _read_doe_root(claude_home: str) -> str:
    """Read `<claude_home>/.doe-root`, stripped. Empty string on any read failure."""
    doe_root_file = os.path.join(claude_home, ".doe-root")
    try:
        with open(doe_root_file, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        print(f"skip: _read_doe_root: with open(doe_root_file, encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""


def check_rag_state() -> Tuple[str, int]:
    """Faithful port of check-rag-state.sh's full body.

    Returns (stdout_text, exit_code). stdout_text is the single token to
    print on stdout ("" when the caller should instead print to stderr, i.e.
    the coordinator-claude-root/trust preconditions failed — that stderr text is the second
    element of the tuple returned by main()'s caller path, not this
    function's return; see main()).
    """
    claude_home = _claude_home()
    doe_root = _read_doe_root(claude_home)
    if not doe_root or not os.path.isdir(os.path.join(doe_root, "coordinator")):
        return ("", 1)

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.join(doe_root, "coordinator")

    if not _is_trusted_root(plugin_root):
        return ("", 1)

    marker_path = os.environ.get("CLAUDE_RAG_STATE_FILE") or os.path.join(
        plugin_root, "tasks", ".rag-state"
    )

    # 1. env var fast-path — a caller or hook may inject RAG_STATE directly.
    rag_state = os.environ.get("RAG_STATE", "")
    if rag_state in _VALID_TOKENS:
        return (rag_state, 0)
    if rag_state == "unknown":
        return ("unknown", 1)

    # 2. marker file written by the W1 hook (example-retrieval-repo-detect.*) at session
    # boot — only the first line is read (mirrors `head -1`), and all
    # whitespace is stripped (mirrors `tr -d '[:space:]'`).
    if os.path.isfile(marker_path):
        try:
            with open(marker_path, encoding="utf-8") as fh:
                first_line = fh.readline()
        except OSError:
            first_line = ""
        state = "".join(first_line.split())
        if state in _VALID_TOKENS:
            return (state, 0)
        if state == "unknown":
            return ("unknown", 1)
        # unrecognised marker content — fall through to unknown, same as bash.

    # 3. fallback: no signal resolved state — conservative "unknown"; callers
    # treat unknown == stale.
    return ("unknown", 1)


def _doe_root_error(doe_root: str) -> Optional[str]:
    """Return the ERROR line for a missing/invalid coordinator-claude root, else None."""
    if doe_root and os.path.isdir(os.path.join(doe_root, "coordinator")):
        return None
    return "ERROR: ~/.claude/.doe-root missing/invalid — re-run coordinator:install"


def _trust_error(plugin_root: str) -> str:
    return (
        f"ERROR: {_SITE_LABEL} '{plugin_root}' outside trusted prefix — refusing to "
        "source; re-run coordinator:install (or set COORDINATOR_PLUGIN_ROOT_TRUSTED=1 "
        "for a sanctioned --plugin-dir spike)"
    )


def main(argv) -> int:  # noqa: ARG001 — takes no arguments, mirrors bash oracle
    """CLI entry — check-rag-state takes no arguments."""
    claude_home = _claude_home()
    doe_root = _read_doe_root(claude_home)

    err = _doe_root_error(doe_root)
    if err is not None:
        print(err, file=sys.stderr)
        return 1

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.join(doe_root, "coordinator")
    if not _is_trusted_root(plugin_root):
        print(_trust_error(plugin_root), file=sys.stderr)
        return 1

    text, rc = check_rag_state()
    print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

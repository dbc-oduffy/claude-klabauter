"""
coordinator_core.ops.ensure_vscode_readonly — editor-side handoff-tracker read-only guard.

Purpose: idempotently, additively merges two `files.readonlyInclude` globs into a
repo's `.vscode/settings.json` so VS Code (and forks honoring the same setting)
opens the generated handoff tracker renders (`state/handoff-tracker.md`,
`state/doe-handoff-tracker.md`) read-only and refuses to save over them. This is
the EDITOR-side guard (layer 1); the AGENT-side complement is the PreToolUse
`block-tracker-edit` hook (layer 2) — see docs/wiki/coordinator-tripwires.md
§ BLOCK-TRACKER-EDIT and docs/wiki/handoff-tracker-system.md.

Offer-shaped, not a hard lock: a user can still override per-file via VS Code's
"Set Active Editor Writeable" command. The next tracker render overwrites any
hand-edit anyway.

Called from repo-setup (Phase 3f.6) at project onboarding time — NOT a hot path.

Exit codes:
  0 — merged / created / already-idempotent / skipped-loudly (unparseable JSON,
      settings not a JSON object). Skips are non-fatal by design — this is a
      best-effort editor convenience, never a gate.
  2 — bad CLI usage (missing --root value, unknown argument, root not a directory).

Negative-spec (bash-oracle parity, do NOT "fix" mid-port):
    - No `node`-availability check: the bash oracle shelled out to `node` for a
      JSONC-tolerant merge because it had no other JSON engine at hand; this
      Python module performs the identical JSONC-strip + merge NATIVELY (json
      stdlib + a ported comment-stripper) — there is no external-interpreter
      dependency to probe for, so the bash oracle's "node not on PATH — skipped
      loudly" branch has no equivalent path here. This is a strict capability
      improvement (fewer external deps), not a parity break: the OBSERVABLE
      merge/create/skip/idempotent behavior for a given settings.json input is
      preserved byte-for-byte against the bash oracle's `node` invocation.
    - Unparseable-even-after-comment-strip settings.json is left BYTE-IDENTICAL
      (skip-loud to stderr, exit 0) — mirrors the oracle's explicit
      "skipped to avoid clobbering" behavior; do not attempt a best-effort parse.
    - JSONC input: comments are normalized away on write (lossy) — the ORIGINAL
      raw bytes are backed up to `settings.json.bak` before the strict-JSON
      rewrite, exactly matching the oracle's backup-then-rewrite sequencing.
    - Idempotent: if both guard keys are already `true`, no write occurs at all
      (no mtime churn) — mirrors the oracle's early `process.exit(0)` before any
      `writeFileSync` call.
    - `files.readonlyInclude` merge is a shallow `dict.update`-style overlay
      (existing keys preserved, our two keys added/overwritten) — never a full
      replace of the pre-existing `files.readonlyInclude` object.

Port of: ensure-vscode-readonly.sh (DoE 894d4bc6, 2026-07-22)
Spec backlink: docs/plans/2026-05-29-handoff-tracker-system.md (edit-resistance follow-up)
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List, Tuple

from coordinator_core.session.declared_writes import declare_write

_PROG = "ensure-vscode-readonly"

# Generator-provenance: merges guard keys into <--root>/.vscode/settings.json
# for whatever repo root the caller passes -- a caller-supplied target repo,
# not a fixed claude-klabauter path.
GENERATES = []

GUARD_KEYS = {
    "**/state/handoff-tracker.md": True,
    "**/state/doe-handoff-tracker.md": True,
}


def _strip_jsonc(text: str) -> str:
    """Strip // and /* */ comments outside string literals, then drop trailing commas.

    Direct line-for-line port of the bash oracle's inline `stripJsonc` JS helper —
    a small hand-rolled scanner (not a general JSON5 parser), tracking single-
    quote/double-quote string state and backslash-escapes to avoid stripping
    comment-like sequences that appear inside string values.
    """
    out: List[str] = []
    i = 0
    n = len(text)
    in_str = False
    quote = ""
    esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                in_str = False
            i += 1
            continue
        if c == '"' or c == "'":
            in_str = True
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    stripped = "".join(out)
    import re

    return re.sub(r",(\s*[}\]])", r"\1", stripped)


def _merge_settings(settings_path: Path) -> Tuple[int, str]:
    """Perform the idempotent additive merge. Returns (exit_code, stdout_or_empty).

    stderr diagnostics are written directly (not returned) to mirror the bash
    oracle's `process.stderr.write` calls at the exact points they fire.
    """
    raw: str | None
    try:
        raw = settings_path.read_text(encoding="utf-8")
    except OSError:
        raw = None

    obj: dict = {}
    had_comments = False
    if raw is not None and raw.strip() != "":
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            had_comments = True
            try:
                obj = json.loads(_strip_jsonc(raw))
            except (json.JSONDecodeError, ValueError):
                print(
                    f"[coordinator] {_PROG}: {settings_path} could not be parsed even "
                    "after comment-strip — skipped to avoid clobbering. Add by hand: "
                    'files.readonlyInclude → "**/state/handoff-tracker.md": true, '
                    '"**/state/doe-handoff-tracker.md": true',
                    file=sys.stderr,
                )
                return (0, "")

    if not isinstance(obj, dict):
        print(
            f"[coordinator] {_PROG}: {settings_path} is not a JSON object — skipped.",
            file=sys.stderr,
        )
        return (0, "")

    cur_val = obj.get("files.readonlyInclude")
    cur = cur_val if isinstance(cur_val, dict) else {}

    if all(cur.get(k) is True for k in GUARD_KEYS):
        return (0, "")

    obj["files.readonlyInclude"] = {**cur, **GUARD_KEYS}

    if had_comments:
        try:
            settings_path.with_name(settings_path.name + ".bak").write_text(raw, encoding="utf-8")
        except OSError:
            print(f"skip: _merge_settings: settings_path.with_name(settings_path.name + \".bak\").write_text(raw, e failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
        print(
            f"[coordinator] {_PROG}: {settings_path} had comments (JSONC) — merged the "
            f"guard and wrote strict JSON. Original backed up to {settings_path}.bak "
            "(re-add comments there if you need them).",
            file=sys.stderr,
        )

    out_str = json.dumps(obj, indent=2) + "\n"
    tmp = settings_path.with_name(f"{settings_path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(out_str, encoding="utf-8")
        tmp.replace(settings_path)
        # DR-276: declared AFTER the write lands, never before — the contract
        # is a report of what was ACTUALLY written, not of an intended surface.
        declare_write(settings_path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            print(f"skip: _merge_settings: tmp.unlink() failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
        print(
            f"[coordinator] {_PROG}: write failed for {settings_path}: {exc}",
            file=sys.stderr,
        )
        return (1, "")

    verb = "created " if raw is None else "merged tracker read-only globs into "
    return (0, f"[coordinator] {_PROG}: {verb}{settings_path}\n")


def main(argv: List[str]) -> int:
    """CLI entry: `ensure-vscode-readonly [--root <repo-root>]` (default: cwd)."""
    root = "."
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--root":
            if i + 1 >= len(argv) or not argv[i + 1]:
                print(f"{_PROG}: --root requires a value", file=sys.stderr)
                return 2
            root = argv[i + 1]
            i += 2
            continue
        print(f"{_PROG}: unknown argument: {arg}", file=sys.stderr)
        return 2

    root_path = Path(root)
    if not root_path.is_dir():
        print(f"{_PROG}: root not a directory: {root}", file=sys.stderr)
        return 2

    vscode_dir = root_path / ".vscode"
    settings_path = vscode_dir / "settings.json"
    vscode_dir.mkdir(parents=True, exist_ok=True)

    rc, stdout_line = _merge_settings(settings_path)
    if stdout_line:
        sys.stdout.write(stdout_line)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

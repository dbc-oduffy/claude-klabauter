"""
coordinator_core.ops.render_template — narrow Mustache-style template renderer.

Purpose: substitute literal {{KEY}} tokens in a template file with
caller-supplied KEY=VALUE pairs. Fails loudly on any unsubstituted
{{KEY}} remaining after render; rejects keys with whitespace inside
braces by treating them as unsubstituted.

Port of: render-template.sh (DoE 290997c7, 2026-07-22)
Spec backlink: docs/plans/2026-05-19-coordinator-installer-redesign-implementation.md § C1 (D3.b)
               docs/plans/2026-06-26-coordinator-install-update-friction-fix-slate.md § C-R3a

CLI contract (unchanged from the bash oracle):
  render-template.sh <template-path> [-o <output-path>] [--guard-sentinel <token>] [KEY=VALUE]...

Exit codes:
  0  All {{KEY}} tokens were substituted; output written successfully.
  1  One or more {{KEY}} tokens remain unsubstituted after render, OR
     template file is not readable, OR output path is not writable,
     OR argument parsing failed.
  3  --guard-sentinel refused to overwrite an existing non-empty output
     file that lacks the sentinel (never-clobber guard) — distinct from
     1 so callers can branch on refusal vs. a generic failure.

Never-clobber guard (opt-in, off by default): pass --guard-sentinel <token>
alongside -o to refuse clobbering a hand-authored output file. Before the
atomic write, if the output path exists and is a non-empty regular file,
its content must contain <token> as a substring or the write is refused
(exit 3, output untouched, no tmp-file residue). An absent or empty
existing file is always overwritable — there is nothing to protect. An
existing file that cannot be read is treated as refuse-worthy (fail-closed,
never fail-open). --guard-sentinel without -o is a usage error (guarding
stdout is meaningless).

Spec backlink: cross-repo/archive/2026-07-22-claude-central-em-render-template-primitive-never-clobber-guard.md

Negative-spec (unchanged from the bash oracle — do NOT "improve" here):
    - NO conditionals, NO loops, NO includes, NO defaults, NO escaping
      of braces, NO whitespace tolerance inside {{ }}.
    - Substitution is a literal string replace (str.replace equivalent),
      NOT a regex — a value containing regex metacharacters or `{{`/`}}`
      substrings is inserted verbatim, mirroring the bash oracle's use of
      Python str.replace (never sed/${var//}) specifically to avoid `&`
      back-reference and backslash-escape hazards.
    - Unsubstituted-key detection matches `{{ANYTHING}}` (non-`}` run)
      remaining in the OUTPUT after all substitutions are applied —
      including keys the caller never supplied AND `{{ SPACE }}`-shaped
      tokens (whitespace inside braces is never eligible for
      substitution, so such tokens always end up reported here).
"""


from __future__ import annotations

GENERATES = []  # writes to a caller-supplied -o <output-path> (or stdout when omitted); no fixed target of its own

import os
import re
import sys
from typing import List, Optional, Tuple

from coordinator_core.session.declared_writes import declare_write

_PROG = "render-template"  # literal program-name prefix, matches bash oracle's stderr prefix
_TOKEN_RE = re.compile(r"\{\{([^}]+)\}\}")
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_args(argv: List[str]) -> Tuple[Optional[str], Optional[str], Optional[List[Tuple[str, str]]], Optional[str], Optional[str]]:
    """Parse CLI args. Returns (template_path, output_path, kv_pairs, guard_sentinel, error_message).

    On error, template_path is None and error_message is set (caller prints
    to stderr and exits 1, matching the bash oracle's `set -euo pipefail` +
    early-exit shape).
    """
    if not argv:
        return None, None, None, None, (
            "usage: render-template.sh <template-path> [-o <output-path>] [KEY=VALUE]..."
        )

    template_path = argv[0]
    rest = argv[1:]

    output_path: Optional[str] = None
    guard_sentinel: Optional[str] = None
    kv_pairs: List[Tuple[str, str]] = []

    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "-o":
            if i + 1 >= len(rest):
                return None, None, None, None, "-o requires an argument"
            output_path = rest[i + 1]
            i += 2
        elif arg == "--guard-sentinel":
            if i + 1 >= len(rest):
                return None, None, None, None, "--guard-sentinel requires an argument"
            guard_sentinel = rest[i + 1]
            i += 2
        elif "=" in arg:
            key, _, value = arg.partition("=")
            kv_pairs.append((key, value))
            i += 1
        else:
            return None, None, None, None, f"unexpected argument: {arg}"

    return template_path, output_path, kv_pairs, guard_sentinel, None


def _apply_substitutions(rendered: str, kv_pairs: List[Tuple[str, str]]) -> Tuple[Optional[str], Optional[str]]:
    """Apply each KEY=VALUE literal substitution in order.

    Returns (result, error_message). On an invalid key, result is None and
    error_message is set.
    """
    for key, value in kv_pairs:
        if not _KEY_RE.match(key):
            return None, f"invalid key: {key} (must be a bare identifier)"
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered, None


def _find_unsubstituted(rendered: str) -> List[str]:
    """Extract every remaining {{...}} token's stripped inner content, deduped+sorted.

    Mirrors the bash oracle: extract all {{[^}]+}} tokens, strip surrounding
    whitespace from the inner content, dedupe, sort.
    """
    tokens = _TOKEN_RE.findall(rendered)
    stripped = {t.strip() for t in tokens}
    return sorted(stripped)


def render(template_path: str, kv_pairs: List[Tuple[str, str]]) -> Tuple[Optional[str], int, Optional[str]]:
    """Render a template file with the given substitutions.

    Returns (rendered_text, exit_code, error_message). rendered_text is None
    on any failure; error_message is the bare (unprefixed) diagnostic.
    """
    if not os.access(template_path, os.R_OK) or not os.path.isfile(template_path):
        return None, 1, f"cannot read template: {template_path}"

    try:
        with open(template_path, "r", encoding="utf-8", errors="surrogateescape") as f:
            rendered = f.read()
    except OSError:
        print(f"skip: render: with open(template_path, \"r\", encoding=\"utf-8\", errors=\"surrogateescap failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None, 1, f"cannot read template: {template_path}"

    rendered, err = _apply_substitutions(rendered, kv_pairs)
    if err is not None:
        return None, 1, err

    unsubstituted = _find_unsubstituted(rendered)
    if unsubstituted:
        joined = ", ".join(unsubstituted)
        return None, 1, f"unsubstituted keys: {joined} in {template_path}"

    return rendered, 0, None


def main(argv: List[str]) -> int:
    """CLI entry: arg parse, render, write (stdout or -o path), return rc."""
    template_path, output_path, kv_pairs, guard_sentinel, err = _parse_args(argv)
    if err is not None:
        if template_path is None and kv_pairs is None and output_path is None and argv == []:
            # missing-args usage message: no program-name prefix, matches bash oracle
            print(err, file=sys.stderr)
        else:
            print(f"{_PROG}: {err}", file=sys.stderr)
        return 1

    assert template_path is not None
    assert kv_pairs is not None

    if guard_sentinel is not None and not output_path:
        print(f"{_PROG}: --guard-sentinel requires -o <output-path>", file=sys.stderr)
        return 1

    rendered, rc, err = render(template_path, kv_pairs)
    if err is not None:
        print(f"{_PROG}: {err}", file=sys.stderr)
        return rc

    assert rendered is not None

    if output_path:
        output_dir = os.path.dirname(output_path) or "."
        if not os.path.isdir(output_dir):
            print(f"{_PROG}: output directory does not exist: {output_dir}", file=sys.stderr)
            return 1

        if guard_sentinel is not None and os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
            try:
                with open(output_path, "r", encoding="utf-8", errors="surrogateescape") as f:
                    existing = f.read()
            except OSError as exc:
                print(
                    f"{_PROG}: refusing to overwrite {output_path}: cannot read existing file "
                    f"to verify guard sentinel ({exc})",
                    file=sys.stderr,
                )
                return 3
            if guard_sentinel not in existing:
                print(
                    f"{_PROG}: refusing to overwrite {output_path}: existing non-empty file "
                    f"lacks guard sentinel '{guard_sentinel}' (never-clobber guard)",
                    file=sys.stderr,
                )
                return 3

        tmp_path = f"{output_path}.render-template.tmp.{os.getpid()}"
        try:
            with open(tmp_path, "w", encoding="utf-8", errors="surrogateescape") as f:
                f.write(rendered)
            os.replace(tmp_path, output_path)
            # DR-276: declared AFTER the atomic replace lands, never before —
            # the contract is a report of what was ACTUALLY written, not of
            # an intended surface.
            declare_write(output_path)
        except OSError as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                print(f"skip: main: os.unlink(tmp_path) failed: {sys.exc_info()[1]}", file=sys.stderr)
                pass
            print(f"{_PROG}: failed to write output: {exc}", file=sys.stderr)
            return 1
    else:
        sys.stdout.write(rendered)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""
coordinator_core.ops.verify_arch_audit_atlas_refresh — pre-commit gate helper
for /architecture-audit Step 6.5.

Purpose: verifies that the targeted-system atlas page is being refreshed
(Branch A) or asserted current (Branch B) as part of the closeout commit.

Spec backlink: docs/plans/2026-06-04-architecture-audit-atlas-refresh-gate.md § C1
Spec backlink: docs/plans/2026-06-08-atlas-attested-clock-split.md (clock split)
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Pre-commit gate: reads STAGED content via `git show :<path>` and `git diff
--cached -- <path>`. Does NOT read HEAD. Reads the intended commit-message
file (argv[2] or .git/COMMIT_EDITMSG).

Two-clock semantics (atlas-attested-clock-split plan):
    Atlas pages carry TWO date clocks in frontmatter:
        last_mapped:   most recent CONTENT-ROTATION (body rewrite). Bumped
                        only by Branch A.
        last_attested: most recent CURRENCY assertion. Bumped by BOTH
                        branches.

Behavior:
    - SKIP   — no atlas page exists for <TARGET_SYSTEM>
    - PASS branch=A — staged body diff AND staged last_mapped == AUDIT_DATE
                      AND staged last_attested == AUDIT_DATE
                      (Branch A evaluated first; B ignored if A satisfies)
    - PASS branch=B — commit-msg contains `atlas-current-as-of:<AUDIT_DATE>`
                      AND staged last_attested == AUDIT_DATE
                      AND zero staged body delta
                      AND last_mapped UNCHANGED (no last_mapped diff)
    - FAIL — neither branch satisfied; verbatim multi-line message emitted

Exit code: always 0 (informational — caller decides whether to abort).
This is a best-effort/never-block-shaped gate: a claude-klabauter-link resolution or
import failure in the CLI trampoline degrades to a stdout `FAIL:` line
(fail-safe — an operator sees a FAIL and re-attempts rather than a broken
gate silently rubber-stamping a commit as PASS), never a distinct nonzero
exit code callers were never wired to handle. See the trampoline's own
docstring for the transport-failure posture.

Negative-spec: does NOT modify any file; does NOT auto-bump last_mapped or
last_attested; does NOT touch the health-ledger; does NOT read or write
`Last full audit`; does NOT read HEAD. The two-clock contract is preserved —
this gate touches only `Last targeted audit` semantics via the atlas page.

Faithful oracle-bug repro: the Branch B commit-message token regex
(`atlas-current-as-of:[[:space:]]*<AUDIT_DATE>`) inserts AUDIT_DATE
UN-escaped into the pattern, exactly as the bash oracle's `grep -E` did — a
caller-supplied AUDIT_DATE containing regex metacharacters (e.g. `.`) would
be interpreted as a regex, not a literal, in both the oracle and this port.
Not "fixed" here; faithfully reproduced.

Portability: bash 3.2 + BSD coreutils + Git-Bash per DR-061 governed the
original; this port targets the same three platforms via `git` subprocess
calls with explicit timeouts (never blocks a caller on a hung child) and
`CREATE_NO_WINDOW` on Windows (no console flash, DR-054).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_GIT_TIMEOUT_SECS = 30

_HUNK_HEADER_RE = re.compile(r"^@@ -[^ ]+ \+([0-9]+)(?:,([0-9]+))? @@")
_LAST_MAPPED_LINE_RE = re.compile(r"^last_mapped:")
_LAST_ATTESTED_LINE_RE = re.compile(r"^last_attested:")
_LAST_MAPPED_DIFF_RE = re.compile(r"^[+-]last_mapped:")
_FM_DELIM_RE = re.compile(r"^---")


def _run_git(args: List[str], cwd: str) -> Tuple[int, str]:
    """Run a git subcommand; returns (returncode, stdout). Never raises on
    a non-git-repo / missing-object condition — mirrors the oracle's
    `2>/dev/null || echo ""` fallback shape.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _run_git: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return (1, "")
    return (result.returncode, result.stdout)


def _repo_root(cwd: str) -> Optional[str]:
    rc, out = _run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    if rc != 0 or out.strip() != "true":
        return None
    rc, out = _run_git(["rev-parse", "--show-toplevel"], cwd)
    if rc != 0:
        return None
    root = out.strip()
    return root or None


def _extract_field(content: str, field_re: re.Pattern) -> str:
    """Mirror `grep -m1 '^<field>:' | sed 's/^<field>:[[:space:]]*//' |
    sed 's/[[:space:]]*$//'` — first matching line only, colon-prefix and
    surrounding whitespace stripped.
    """
    for line in content.splitlines():
        if field_re.match(line):
            value = field_re.sub("", line, count=1)
            return value.strip()
    return ""


def _fm_end_line(content: str) -> Optional[int]:
    """Line number (1-indexed) of the SECOND line starting with '---' —
    mirrors `awk '/^---/{c++; if(c==2){print NR; exit}}'`.
    """
    count = 0
    for idx, line in enumerate(content.splitlines(), start=1):
        if _FM_DELIM_RE.match(line):
            count += 1
            if count == 2:
                return idx
    return None


def _has_body_diff(staged_content: str, staged_diff: str, diff_u0: str) -> bool:
    fm_end = _fm_end_line(staged_content)
    if fm_end is None or fm_end < 2:
        # Malformed frontmatter (no closing ---): conservatively treat any
        # diff as body diff.
        return bool(staged_diff)

    for line in diff_u0.splitlines():
        m = _HUNK_HEADER_RE.match(line)
        if not m:
            continue
        c = int(m.group(1))
        d = int(m.group(2)) if m.group(2) is not None else 1
        if c > fm_end:
            return True
        if d > 0 and (c + d - 1) > fm_end:
            return True
    return False


def _has_last_mapped_diff(diff_u0: str) -> bool:
    for line in diff_u0.splitlines():
        if _LAST_MAPPED_DIFF_RE.match(line):
            return True
    return False


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: verify-arch-audit-atlas-refresh.sh AUDIT_DATE TARGET_SYSTEM [COMMIT_MSG_FILE]",
            file=sys.stderr,
        )
        print("  COMMIT_MSG_FILE defaults to .git/COMMIT_EDITMSG if absent", file=sys.stderr)
        return 0

    audit_date = argv[0]
    target_system = argv[1]
    commit_msg_file_arg = argv[2] if len(argv) > 2 else ""

    cwd = os.getcwd()
    repo_root = _repo_root(cwd)
    if not repo_root:
        print("SKIP — not inside a git work tree")
        return 0

    atlas_rel = f"docs/architecture/systems/{target_system}.md"
    atlas_abs = Path(repo_root) / atlas_rel

    if not atlas_abs.is_file():
        print(f"SKIP — no atlas page for {target_system}")
        return 0

    commit_msg_file = commit_msg_file_arg or str(Path(repo_root) / ".git" / "COMMIT_EDITMSG")

    _, staged_content = _run_git(["show", f":{atlas_rel}"], repo_root)

    staged_last_mapped = _extract_field(staged_content, _LAST_MAPPED_LINE_RE)
    staged_last_attested = _extract_field(staged_content, _LAST_ATTESTED_LINE_RE)

    _, staged_diff = _run_git(["diff", "--cached", "--", atlas_rel], repo_root)
    _, diff_u0 = _run_git(["diff", "--cached", "-U0", "--", atlas_rel], repo_root)

    has_body_diff = _has_body_diff(staged_content, staged_diff, diff_u0)
    has_last_mapped_diff = _has_last_mapped_diff(diff_u0)

    # Branch A — evaluated first: atlas was refreshed this pass.
    if has_body_diff and staged_last_mapped == audit_date:
        if staged_last_attested == audit_date:
            print("PASS branch=A")
            return 0
        print(
            "FAIL: body diff + last_mapped bumped but last_attested not bumped. "
            "A real audit IS an attestation; bump both clocks."
        )
        return 0

    # Branch B — atlas already current, asserted via commit-message token.
    commit_msg_content = ""
    if os.path.isfile(commit_msg_file):
        try:
            with open(commit_msg_file, "r", encoding="utf-8", errors="replace") as fh:
                commit_msg_content = fh.read()
        except OSError:
            commit_msg_content = ""

    # Faithful oracle-bug repro: audit_date is inserted UN-escaped into the
    # regex, exactly as the bash `grep -E` oracle did. See module docstring.
    token_re = re.compile(r"atlas-current-as-of:[ \t]*" + audit_date)
    has_branch_b_token = bool(token_re.search(commit_msg_content))

    if (
        has_branch_b_token
        and staged_last_attested == audit_date
        and not has_body_diff
        and not has_last_mapped_diff
    ):
        print("PASS branch=B")
        return 0

    print(
        f"FAIL: /architecture-audit Step 6.5 atlas-refresh gate not satisfied for {target_system}.\n"
        f"Either (a) refresh docs/architecture/systems/{target_system}.md inline before closing\n"
        f"(body diff + bump both last_mapped and last_attested to {audit_date}),\n"
        f"or (b) declare atlas-current-as-of:{audit_date} in the closeout commit message\n"
        f"(bump last_attested to {audit_date} only; last_mapped and body unchanged)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

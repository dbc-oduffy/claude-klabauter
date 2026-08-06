#!/usr/bin/env python3
"""Scan the tree for accidental secrets before it is published.

Enumerates via the shared repo walker rather than `git ls-files --cached`, so
the scan is meaningful in a zero-commit mirror during bootstrap — see _repo.py.

Suppression:
  inline   ``noqa: secrets`` anywhere on the line
  file     ``.github/.secrets-allowlist``, one ``<path>:<lineno>`` per line

EXIT CONTRACT
  0 — clean
  1 — at least one candidate secret, or the tree enumerated zero files
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.dont_write_bytecode = True  # never litter the published tree with a __pycache__
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _repo import load_allowlist, read_text, repo_files, repo_root  # noqa: E402

PATTERNS = [
    ("API key (sk-style)", re.compile(r"sk-[a-zA-Z0-9]{20,}")),
    ("Anthropic API key", re.compile(r"sk-ant-[a-zA-Z0-9_\-]{20,}")),
    ("AWS access key", re.compile(r"AKIA[A-Z0-9]{16}")),
    ("GitHub personal access token", re.compile(r"gh[pousr]_[a-zA-Z0-9]{36,}")),
    ("Slack token", re.compile(r"xox[abprs]-[a-zA-Z0-9\-]{10,}")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("Hardcoded password", re.compile(r"password\s*[:=]\s*['\"][^'\"]+['\"]", re.IGNORECASE)),
    ("Hardcoded secret", re.compile(r"secret\s*[:=]\s*['\"][^'\"]+['\"]", re.IGNORECASE)),
]

NOQA_RE = re.compile(r"noqa:\s*secrets", re.IGNORECASE)

ALLOWLIST_NAME = ".secrets-allowlist"

SELF_EXEMPT_PATHS = {
    ".github/scripts/check-secrets.py",
    f".github/{ALLOWLIST_NAME}",
}


def check_allowlist_staleness(root: pathlib.Path, allowlist: set[str]) -> list[str]:
    warnings = []
    for entry in sorted(allowlist):
        parts = entry.rsplit(":", 1)
        if len(parts) != 2:
            warnings.append(f"malformed entry '{entry}' (expected '<path>:<lineno>')")
            continue
        fpath, line_str = parts
        if not (root / fpath).exists():
            warnings.append(f"stale entry '{entry}' — file no longer exists")
            continue
        if not line_str.isdigit():
            warnings.append(f"malformed entry '{entry}' — line number must be an integer")
    return warnings


def main() -> int:
    root = repo_root()
    paths = repo_files(root)

    if not paths:
        print("Secrets scan FAILED: enumerated zero files.")
        return 1

    allowlist = load_allowlist(root, ALLOWLIST_NAME)
    warnings = check_allowlist_staleness(root, allowlist)
    errors: list[str] = []
    scanned = 0

    for rel in paths:
        if rel in SELF_EXEMPT_PATHS:
            continue
        text = read_text(root / rel)
        if text is None:
            continue
        scanned += 1
        for line_num, line in enumerate(text.splitlines(), 1):
            if NOQA_RE.search(line):
                continue
            if f"{rel}:{line_num}" in allowlist:
                continue
            for pattern_name, pattern in PATTERNS:
                if pattern.search(line):
                    errors.append(f"{rel}:{line_num}: potential {pattern_name} detected")

    if warnings:
        print("Secrets scan warnings:")
        for w in warnings:
            print(f"  {w}")

    if errors:
        print("Secrets scan FAILED:")
        for err in errors:
            print(f"  {err}")
        print()
        print("  For a false positive: add 'noqa: secrets' to the line, or an entry")
        print(f"  to .github/{ALLOWLIST_NAME}.")
        return 1

    print(f"Secrets scan passed ({scanned} text files scanned).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

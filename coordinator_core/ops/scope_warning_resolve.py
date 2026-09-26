

from __future__ import annotations

GENERATES = []

import os
import re
import sys
import tempfile
from typing import List, Optional, Tuple

from coordinator_core.ops._git_root_util import git_root as _resolve_git_root

VALID_RESOLUTIONS: Tuple[str, ...] = (
    "legitimate-mine",
    "not-mine-unstaged",
    "not-mine-committed-anyway",
    "orphan-claimed",
    "orphan-rejected",
)

_LINE_NUM_RE = re.compile(r"^[1-9][0-9]*$")
_TRAILING_RESOLUTION_RE = re.compile(r"[a-z-]+$")

USAGE = """Usage: scope-warning-resolve <session_id> <line_number> <resolution>

Valid resolutions:
  legitimate-mine            file was correctly mine; warn was a false positive
  not-mine-unstaged          I unstaged it after seeing the warn (true positive)
  not-mine-committed-anyway  I committed despite the warn (true positive — would block in strict mode)
  orphan-claimed             orphan file; I claimed it deliberately
  orphan-rejected            orphan file; I left it for whoever owns it

Example:
  scope-warning-resolve abc-123 3 legitimate-mine
"""


def resolve(
    session_id: str,
    line_number_raw: str,
    resolution: str,
    git_root: Optional[str] = None,
) -> Tuple[str, int]:
    if not _LINE_NUM_RE.match(line_number_raw):
        print(
            f"Error: line_number must be a positive integer, got: '{line_number_raw}'",
            file=sys.stderr,
        )
        return ("", 1)
    line_number = int(line_number_raw)

    if resolution not in VALID_RESOLUTIONS:
        print(f"Error: invalid resolution '{resolution}'", file=sys.stderr)
        print(f"Valid resolutions: {' '.join(VALID_RESOLUTIONS)}", file=sys.stderr)
        return ("", 1)

    if git_root is None:
        git_root = _resolve_git_root()
    if not git_root:
        print(
            "Error: not inside a git repository. Run from within the project repo.",
            file=sys.stderr,
        )
        return ("", 1)

    log_file = os.path.join(
        git_root, ".git", "coordinator-sessions", session_id, "scope-warnings.log"
    )
    if not os.path.isfile(log_file):
        print(f"Error: log file not found: {log_file}", file=sys.stderr)
        print(
            f"  Does session '{session_id}' exist and have scope warnings?",
            file=sys.stderr,
        )
        return ("", 1)

    with open(log_file, encoding="utf-8") as fh:
        content = fh.read()

    # sense — a file with N newline-terminated lines has TOTAL_LINES == N.
    total_lines = content.count("\n")
    if line_number > total_lines:
        print(
            f"Error: line {line_number} does not exist in {log_file} "
            f"(file has {total_lines} lines)",
            file=sys.stderr,
        )
        return ("", 1)

    lines = content.split("\n")
    target_line = lines[line_number - 1]

    if target_line == "":
        print(f"Error: line {line_number} is empty", file=sys.stderr)
        return ("", 1)

    if "pending-resolution" not in target_line:
        m = _TRAILING_RESOLUTION_RE.search(target_line)
        current_res = m.group(0) if m else "unknown"
        print(
            f"Warning: line {line_number} does not contain 'pending-resolution'",
            file=sys.stderr,
        )
        print(f"  Current value: {target_line}", file=sys.stderr)
        print(f"  Current resolution appears to be: {current_res}", file=sys.stderr)
        print(f"  Overwriting anyway with: {resolution}", file=sys.stderr)

    fields = target_line.split("|")
    fields[-1] = " " + resolution
    new_target_line = "|".join(fields)

    new_lines = list(lines)
    new_lines[line_number - 1] = new_target_line
    new_content = "\n".join(new_lines)

    if new_content == content:
        print(
            f"Warning: no change was made to line {line_number}. "
            "Does it contain 'pending-resolution'?",
            file=sys.stderr,
        )
        return ("", 0)

    directory = os.path.dirname(log_file) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=os.path.basename(log_file) + ".tmp.", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new_content)
        os.replace(tmp_path, log_file)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            print(f"skip: resolve: os.unlink(tmp_path) failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
        raise

    text = (
        f"Resolved: line {line_number} in {log_file}\n"
        f"  Resolution: {resolution}\n"
        f"  Entry: {new_target_line}\n"
    )
    return (text, 0)


def main(argv: List[str]) -> int:
    if len(argv) != 3:
        print(f"Error: expected 3 arguments, got {len(argv)}", file=sys.stderr)
        sys.stderr.write(USAGE)
        return 1

    session_id, line_number_raw, resolution = argv
    text, rc = resolve(session_id, line_number_raw, resolution)
    if text:
        sys.stdout.write(text)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

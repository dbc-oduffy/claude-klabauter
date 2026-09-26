
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GENERATES = []

FIXTURES_DIR = Path(__file__).resolve().parent
GATE = FIXTURES_DIR.parent.parent / "check-workstream-complete-deletion-blocks.py"

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from coordinator_core.win_portability import no_console_creationflags  # noqa: E402


def run_gate(args: list[str], cwd: Path) -> int:
    result = subprocess.run(
        [sys.executable, str(GATE), *args],
        cwd=str(cwd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **no_console_creationflags(),
    )
    return result.returncode


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )


def main() -> int:
    if not GATE.is_file():
        print(f"gate script not found: {GATE}", file=sys.stderr)
        return 2

    repo = Path(tempfile.mkdtemp())
    try:
        return _run_smoke(repo)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def _run_smoke(repo: Path) -> int:
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "smoke@test.local")
    git(repo, "config", "user.name", "Smoke Test")

    fixture_dir = repo / "fixture"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    (fixture_dir / "scratch-a.md").write_text("scratch a\n", newline="\n")
    (fixture_dir / "scratch-b.md").write_text("scratch b\n", newline="\n")
    (fixture_dir / "kept-doc.md").write_text("kept doc\n", newline="\n")
    git(repo, "add", "fixture/")
    git(repo, "commit", "-q", "-m", "baseline")

    git(repo, "rm", "-q", "fixture/scratch-a.md", "fixture/scratch-b.md")

    fail_count = 0

    def report(name: str, expected: int, actual: int) -> None:
        nonlocal fail_count
        if expected == actual:
            print(f"  OK  {name:<40} expected={expected} actual={actual}")
        else:
            print(f"  FAIL {name:<40} expected={expected} actual={actual}", file=sys.stderr)
            fail_count += 1

    rc = run_gate([str(FIXTURES_DIR / "msg-ok-deleted-and-kept.txt")], repo)
    report("msg-ok-deleted-and-kept.txt", 0, rc)

    rc = run_gate([str(FIXTURES_DIR / "msg-unstaged-deleted.txt")], repo)
    report("msg-unstaged-deleted.txt", 1, rc)

    rc = run_gate([str(FIXTURES_DIR / "msg-missing-kept.txt")], repo)
    report("msg-missing-kept.txt", 1, rc)

    rc = run_gate([str(FIXTURES_DIR / "msg-with-blank-lines.txt")], repo)
    report("msg-with-blank-lines.txt", 0, rc)

    rc = run_gate([str(FIXTURES_DIR / "msg-no-blocks.txt")], repo)
    report("msg-no-blocks.txt", 1, rc)

    (fixture_dir / "new-doc.md").write_text("new doc\n", newline="\n")
    git(repo, "add", "--", "fixture/new-doc.md")

    rc = run_gate(
        [str(FIXTURES_DIR / "msg-no-blocks.txt"), "--", "fixture/new-doc.md"], repo
    )
    report("pathspec-false-positive-fix (exit 0)", 0, rc)

    rc = run_gate(
        [str(FIXTURES_DIR / "msg-no-blocks.txt"), "--", "fixture/scratch-a.md"], repo
    )
    report("pathspec-protection-preserved (exit 1)", 1, rc)

    rc = run_gate([str(FIXTURES_DIR / "msg-no-blocks.txt")], repo)
    report("no-pathspec-backward-compat (exit 1)", 1, rc)

    if fail_count != 0:
        print(f"\n{fail_count} assertion(s) failed.", file=sys.stderr)
        return 1

    print("\nAll 8 fixtures behaved as expected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

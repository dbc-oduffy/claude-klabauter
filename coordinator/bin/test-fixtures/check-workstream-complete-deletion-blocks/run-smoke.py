"""run-smoke.py — behavior smoke test for check-workstream-complete-deletion-blocks.py

Sets up a throwaway git repo via `tempfile.mkdtemp()`, stages expected changes,
runs the gate against each fixture, asserts exit codes (0 / 1 / 1 / 0 — see
expectations below), cleans up. Exits 0 if all assertions pass, non-zero with
diagnostics otherwise.

Spec: docs/plans/2026-06-15-workstream-complete-self-clean.md (Chunk 6, AC18)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GENERATES = []  # every git/file write happens inside a throwaway tempfile.mkdtemp() repo, cleaned up via shutil.rmtree in a finally block — nothing tracked is touched

FIXTURES_DIR = Path(__file__).resolve().parent
GATE = FIXTURES_DIR.parent.parent / "check-workstream-complete-deletion-blocks.py"

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from coordinator_core.win_portability import no_console_creationflags  # noqa: E402


def run_gate(args: list[str], cwd: Path) -> int:
    """Run the gate script with args, returning its exit code (stdout/stderr discarded).

    Deliberate isolation boundary — do not convert to an in-process
    import. This is crash containment: the smoke fixture asserts the
    gate's EXIT CODE, which only exists as a process, so the gate must
    run as its own process rather than be imported and called in-line.
    Reason recorded in
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
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

    # HEAD-1 baseline — three fixture files exist and are committed
    fixture_dir = repo / "fixture"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    (fixture_dir / "scratch-a.md").write_text("scratch a\n")
    (fixture_dir / "scratch-b.md").write_text("scratch b\n")
    (fixture_dir / "kept-doc.md").write_text("kept doc\n")
    git(repo, "add", "fixture/")
    git(repo, "commit", "-q", "-m", "baseline")

    # Stage the deletions the fixtures CLAIM (scratch-a and scratch-b deleted;
    # kept-doc untouched)
    git(repo, "rm", "-q", "fixture/scratch-a.md", "fixture/scratch-b.md")

    fail_count = 0

    def report(name: str, expected: int, actual: int) -> None:
        nonlocal fail_count
        if expected == actual:
            print(f"  OK  {name:<40} expected={expected} actual={actual}")
        else:
            print(f"  FAIL {name:<40} expected={expected} actual={actual}", file=sys.stderr)
            fail_count += 1

    # Fixture 1: well-formed (Deleted matches staged, Kept exists at HEAD) → expect 0
    rc = run_gate([str(FIXTURES_DIR / "msg-ok-deleted-and-kept.txt")], repo)
    report("msg-ok-deleted-and-kept.txt", 0, rc)

    # Fixture 2: claims a third Deleted path that is NOT staged → expect 1
    rc = run_gate([str(FIXTURES_DIR / "msg-unstaged-deleted.txt")], repo)
    report("msg-unstaged-deleted.txt", 1, rc)

    # Fixture 3: claims a Kept path that does not exist anywhere → expect 1
    rc = run_gate([str(FIXTURES_DIR / "msg-missing-kept.txt")], repo)
    report("msg-missing-kept.txt", 1, rc)

    # Fixture 4: well-formed body with blank lines inside the Deleted block
    # (paragraph grouping) → expect 0
    rc = run_gate([str(FIXTURES_DIR / "msg-with-blank-lines.txt")], repo)
    report("msg-with-blank-lines.txt", 0, rc)

    # Fixture 5: no Step 2.67 blocks at all, but staged deletions exist →
    # expect 1 (inverse check)
    rc = run_gate([str(FIXTURES_DIR / "msg-no-blocks.txt")], repo)
    report("msg-no-blocks.txt", 1, rc)

    # ── Concurrent-EM pathspec assertions (added 2026-07-01) ─────────────────
    # These three fixtures lock in the fix for the concurrent-EM false-positive
    # where sibling-staged deletions on the shared index tripped the whole-index
    # F3 check and blocked an unrelated workstream-complete.
    #
    # State at this point: fixture/scratch-a.md and fixture/scratch-b.md are
    # staged for deletion (the "sibling" deletions from the baseline setup
    # above). We add fixture/new-doc.md as a staged addition (our session's
    # "inside" path).
    (fixture_dir / "new-doc.md").write_text("new doc\n")
    git(repo, "add", "--", "fixture/new-doc.md")

    # Fixture 6: false-positive fix — sibling deletions (scratch-a, scratch-b)
    # are staged but OUTSIDE the pathspec; only fixture/new-doc.md is in scope.
    # Message has no Step 2.67 block; gate must exit 0 (sibling deletions
    # ignored).
    rc = run_gate(
        [str(FIXTURES_DIR / "msg-no-blocks.txt"), "--", "fixture/new-doc.md"], repo
    )
    report("pathspec-false-positive-fix (exit 0)", 0, rc)

    # Fixture 7: protection preserved — fixture/scratch-a.md IS in scope via
    # pathspec; message has no Step 2.67 block. Gate must exit 1 (in-scope
    # deletion not accounted for).
    rc = run_gate(
        [str(FIXTURES_DIR / "msg-no-blocks.txt"), "--", "fixture/scratch-a.md"], repo
    )
    report("pathspec-protection-preserved (exit 1)", 1, rc)

    # Fixture 8: backward-compat — no pathspec passed (standalone invocation);
    # whole-index mode sees both sibling deletions and must exit 1 (existing
    # standalone contract unchanged).
    rc = run_gate([str(FIXTURES_DIR / "msg-no-blocks.txt")], repo)
    report("no-pathspec-backward-compat (exit 1)", 1, rc)

    if fail_count != 0:
        print(f"\n{fail_count} assertion(s) failed.", file=sys.stderr)
        return 1

    print("\nAll 8 fixtures behaved as expected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

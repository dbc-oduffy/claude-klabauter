"""`--reconcile-dest=discard` must clean the residue it says it discarded.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md — found
while publishing the warm engine's operator stop hatch, 2026-08-19.

THE ASYMMETRY THIS PINS. `_dest_dirty_status` probes `git status`, which is
repo-WIDE, so the residue list handed to `_reconcile_dest_discard` routinely
names paths under OTHER rows' dests — a mirror has nine rows sharing one repo.
`reset --hard` is likewise repo-wide. `git clean -fd` is NOT: it removes
untracked files only from its own cwd down, and it was being run at `dest`,
one row's subdirectory. So the function reported "discarded 187 path(s)" while
leaving most of them exactly where they were.

That is not merely a cosmetic over-claim. The surviving files made the NEXT
round classify them as already-present — the mirror sync prints NEW only when
a path is absent from dest — so they never entered the commit pathspec and
stayed untracked on disk indefinitely. `coordinator/bin/warm-engine-stop.py`,
the warm engine's only operator stop hatch, sat in the published clone
present-but-uncommitted for exactly this reason.

NEGATIVE-SPEC:
  - Does NOT assert the audit message's wording or its 20-path cap.
  - Does NOT cover the refusal legs (probe failure, undeterminable ahead
    count) — those are unchanged.
  - Does NOT touch any source repo: the fixture is a self-contained throwaway
    git repo under tmp_path.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _load_round_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_discard_under_test", _BIN_DIR / "percolate-round.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rnd = _load_round_module()


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=True,
        creationflags=_NO_WINDOW,
    )


@pytest.fixture
def mirror(tmp_path: Path) -> Path:
    """A mirror-shaped repo: two row dests under one repository root."""
    root = tmp_path / "mirror"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator" / "bin").mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "discard-test@claude-klabauter.test")
    _git(root, "config", "user.name", "Discard Test")
    (root / "coordinator_core" / "seed.py").write_text("seed\n", encoding="utf-8")
    (root / "coordinator" / "bin" / "seed.py").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    return root


def test_discard_removes_residue_under_a_sibling_rows_dest(mirror: Path):
    """The reported failure: called with the coordinator_core row's dest, the
    discard left every untracked file under coordinator/bin in place."""
    sibling_residue = mirror / "coordinator" / "bin" / "warm-engine-stop.py"
    sibling_residue.write_text("residue\n", encoding="utf-8")
    own_residue = mirror / "coordinator_core" / "orphan.py"
    own_residue.write_text("residue\n", encoding="utf-8")

    dest = str(mirror / "coordinator_core")
    dirty = rnd._dest_dirty_status(dest)
    assert dirty is not None
    assert "coordinator/bin/warm-engine-stop.py" in dirty, (
        "fixture precondition: the repo-wide status probe must see the sibling row's residue"
    )

    ok, message = rnd._reconcile_dest_discard(dest, dirty)

    assert ok, message
    assert not own_residue.exists()
    assert not sibling_residue.exists(), (
        "residue under a sibling row's dest survived a discard that reported it removed"
    )


def test_discard_preserves_committed_content(mirror: Path):
    """Reset-to-HEAD must not be widened into content loss by the wider clean."""
    (mirror / "coordinator_core" / "orphan.py").write_text("residue\n", encoding="utf-8")
    dirty = rnd._dest_dirty_status(str(mirror / "coordinator_core"))
    assert dirty is not None

    ok, _ = rnd._reconcile_dest_discard(str(mirror / "coordinator_core"), dirty)

    assert ok
    assert (mirror / "coordinator_core" / "seed.py").read_text(encoding="utf-8") == "seed\n"
    assert (mirror / "coordinator" / "bin" / "seed.py").read_text(encoding="utf-8") == "seed\n"


def test_the_tree_is_clean_afterwards(mirror: Path):
    """The invariant the caller actually depends on: after a successful
    discard the next round sees a clean dest, so every synced file it writes
    is genuinely NEW and lands in the commit pathspec."""
    (mirror / "coordinator" / "bin" / "a.py").write_text("x\n", encoding="utf-8")
    (mirror / "coordinator_core" / "b.py").write_text("x\n", encoding="utf-8")
    dest = str(mirror / "coordinator_core")

    ok, _ = rnd._reconcile_dest_discard(dest, rnd._dest_dirty_status(dest) or "")

    assert ok
    assert rnd._dest_dirty_status(dest) is None

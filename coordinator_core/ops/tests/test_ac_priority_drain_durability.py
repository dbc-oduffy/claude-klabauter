"""
coordinator_core.ops.tests.test_ac_priority_drain_durability — ACCEPTANCE
battery (part 3 of 3) for priority.drain (coordinator_core.ops.priority_drain):
replay durability, real crash-mid-drain recovery, and hostile-input traversal
rejection at acceptance granularity. Complements (does not duplicate)
test_priority_drain.py's unit coverage — see that file for the fixture/style
precedent this module follows (same _isolated_central_root shape, same
POSIX-lock-backend skip guard).

Coverage maps to DoE-claude docs/plans/2026-07-26-priority-ledger.md
§ Acceptance Criteria:

  AC12 — end-to-end: an intent record dropped in the inbox is drained by
         priority.drain (the SOLE ledger writer stays priority_set.py, this
         op never opens a second write path) and the resulting ledger entry
         carries `source: external-intent` + a `source_repo` stamp.
  AC17 — REPLAY IDEMPOTENCY: the SAME logical intent record, delivered to
         the inbox twice (simulating a redelivering upstream producer, not
         merely calling drain() twice over an already-archived file), still
         yields exactly ONE ledger entry and exactly ONE provenance stamp.
  AC18 — TRAVERSAL REJECTION: several hostile target_id shapes are rejected
         before ever being used as a path component. Asserts, per shape,
         that (a) no file is created ANYWHERE outside the inbox tree —
         the assertion that actually matters, since a check of "no ledger
         entry" alone would pass an implementation that wrote the traversal
         target somewhere else first — (b) no ledger entry, (c) the record
         lands in rejected/ with a reason.
  AC1  — DURABILITY ACROSS A REAL CRASH MID-DRAIN: a genuine subprocess
         kill (SIGKILL, no cleanup handler runs), not a reasoned assertion
         about atomicity. The kill point is synchronized deterministically
         (a monkeypatched hook fires exactly when the drain loop is about to
         archive the record it just applied — after the ledger write lands,
         before the move to drained/), never a sleep race. After restart,
         completing the drain must land exactly ONE ledger entry, exactly
         ONE provenance stamp, and leave NO orphaned intent record in the
         inbox root.

POSIX guard: AC1's kill mechanism is SIGKILL via os.kill, which has no
Windows equivalent (os.kill on Windows cannot deliver an uncatchable kill to
an arbitrary child in the same deterministic way) — that one test is also
skipped when the platform is not POSIX, matching the module's existing
fcntl/msvcrt lock-backend skip precedent below for the same "physics, not
missing coverage" reason.

Spec backlink: DoE-claude DoE-claude:pln-priority-ledger-durable-pm-pri-817d40 § Acceptance Criteria
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

try:
    import fcntl as _fcntl  # noqa: F401
    _FCNTL_AVAILABLE = True
except ImportError:
    _FCNTL_AVAILABLE = False

try:
    import msvcrt as _msvcrt  # noqa: F401
    _MSVCRT_AVAILABLE = True
except ImportError:
    _MSVCRT_AVAILABLE = False

_LOCKING_AVAILABLE = _FCNTL_AVAILABLE or _MSVCRT_AVAILABLE

pytestmark = pytest.mark.skipif(
    not _LOCKING_AVAILABLE,
    reason="locked_rmw needs a file-lock backend (fcntl or msvcrt) — neither available",
)

from coordinator_core.ops import priority_drain, priority_set  # noqa: E402
from coordinator_core.ops.priority_drain import drain  # noqa: E402

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _isolated_central_root(tmp_path, monkeypatch):
    """Mirrors test_priority_drain.py's own fixture exactly — both
    priority_drain's and priority_set's central-state resolution are
    redirected to the SAME per-test tmp dir, since drain() calls
    priority_set.set_priority() internally.
    """
    fake_root = tmp_path / "central-state"
    fake_root.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=str(fake_root),
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    monkeypatch.setattr(
        priority_drain, "coordinator_state_root", lambda central=False: str(fake_root)
    )
    monkeypatch.setattr(
        priority_set, "coordinator_state_root", lambda central=False: str(fake_root)
    )
    return fake_root


def _inbox(central_root: Path) -> Path:
    return central_root / "priority-intent-inbox"


def _drop_record(central_root: Path, filename: str, **fields) -> Path:
    inbox = _inbox(central_root)
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / filename
    path.write_text(yaml.safe_dump(fields), encoding="utf-8")
    return path


def _ledger_file(central_root: Path, target_id: str) -> Path:
    return central_root / "priority-ledger" / f"{target_id}.yaml"


def _all_files(root: Path) -> set:
    if not root.exists():
        return set()
    return {p for p in root.rglob("*") if p.is_file()}


# ---------------------------------------------------------------------------
# AC12 — end-to-end drop-then-drain, provenance stamp on the ledger entry
# ---------------------------------------------------------------------------


def test_ac12_dropped_intent_record_is_drained_with_provenance_stamp(_isolated_central_root):
    _drop_record(
        _isolated_central_root,
        "0001-handoff-e2e.yaml",
        target_id="handoff-e2e001",
        priority="urgent",
        sequence=1,
        requested_by="example-cockpit-repo",
        note="AC12 end-to-end",
    )

    result = drain()

    assert result["failed"] == []
    assert result["rejected"] == []
    assert len(result["drained"]) == 1

    ledger_file = _ledger_file(_isolated_central_root, "handoff-e2e001")
    assert ledger_file.is_file(), (
        "priority.drain must be the path that lands the ledger entry — "
        "priority_set.py remains the SOLE writer it routes through"
    )
    entry = yaml.safe_load(ledger_file.read_text())
    assert entry["source"] == "external-intent"
    assert entry["source_repo"] == "example-cockpit-repo"


# ---------------------------------------------------------------------------
# AC17 — replay idempotency: the SAME logical intent delivered twice
# ---------------------------------------------------------------------------


def test_ac17_same_intent_record_replayed_yields_one_entry_one_stamp(_isolated_central_root):
    record_fields = dict(
        target_id="plan-replay001",
        priority="medium",
        sequence=1,
        requested_by="example-cockpit-repo",
        note="AC17 replay",
    )

    # First delivery.
    _drop_record(_isolated_central_root, "0001-first-delivery.yaml", **record_fields)
    first = drain()
    assert len(first["drained"]) == 1

    # SECOND delivery of the identical logical record under a NEW filename —
    # simulating an upstream producer (example-cockpit-repo) redelivering the same
    # intent, not merely re-calling drain() over a file that already moved
    # away. This is the stronger idempotency claim: applying identical
    # content twice must still converge on one ledger state.
    _drop_record(_isolated_central_root, "0002-redelivered-copy.yaml", **record_fields)
    second = drain()
    assert len(second["drained"]) == 1
    assert second["failed"] == []

    # Exactly one ledger FILE for this target — one-file-per-target design
    # already guarantees this structurally, but assert it explicitly as the
    # acceptance-level claim.
    ledger_dir = _isolated_central_root / "priority-ledger"
    ledger_files = list(ledger_dir.glob("plan-replay001*"))
    assert len(ledger_files) == 1, f"expected exactly one ledger file, found {ledger_files}"

    text = ledger_files[0].read_text()
    assert text.count("source: external-intent") == 1
    assert text.count("source_repo:") == 1
    assert text.count("target_id: plan-replay001") == 1


# ---------------------------------------------------------------------------
# AC18 — traversal rejection: hostile target_id shapes, never escape the inbox
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_target_id",
    [
        "../../foo",
        "../foo",
        "/etc/passwd",
        "foo/../bar",
        ".hidden-leading-dot",
        "foo\\..\\bar",
    ],
)
def test_ac18_hostile_target_id_never_written_outside_inbox(_isolated_central_root, bad_target_id):
    before = _all_files(_isolated_central_root)

    _drop_record(
        _isolated_central_root,
        "0001-hostile.yaml",
        target_id=bad_target_id,
        priority="high",
        sequence=1,
        requested_by="example-cockpit-repo",
    )

    result = drain()

    # (b) no ledger entry.
    assert result["drained"] == []
    assert result["failed"] == []
    assert len(result["rejected"]) == 1
    assert result["rejected"][0]["reason"], "rejection must carry a reason"

    ledger_dir = _isolated_central_root / "priority-ledger"
    assert not ledger_dir.exists() or not list(ledger_dir.iterdir())

    # (c) lands in rejected/ with a reason.
    inbox = _inbox(_isolated_central_root)
    assert (inbox / "rejected" / "0001-hostile.yaml").is_file()

    # (a) — the assertion that matters most: no file was EVER created
    # anywhere outside the inbox tree. A check of "no ledger entry" alone
    # would pass an implementation that wrote the traversal target to some
    # other stray path before rejecting it; walk the WHOLE central-root tree
    # (not just priority-ledger/) and require every newly-created file to
    # live inside the inbox subtree.
    after = _all_files(_isolated_central_root)
    new_files = after - before
    assert new_files, "expected the dropped record itself to count as a new file"
    for f in new_files:
        assert inbox in f.parents, (
            f"file {f} was created outside the inbox tree while rejecting a "
            f"traversal-shaped target_id {bad_target_id!r} — this is exactly "
            f"the write-somewhere-else-first failure mode AC18 exists to catch"
        )


# ---------------------------------------------------------------------------
# AC1 — durability across a REAL crash mid-drain (subprocess SIGKILL)
# ---------------------------------------------------------------------------

_CRASH_SCRIPT = textwrap.dedent(
    """
    import os
    import sys
    from pathlib import Path

    central_root = Path(sys.argv[1])

    from coordinator_core.ops import priority_drain, priority_set

    # Same central-root redirection the test fixture applies in-process --
    # here applied by direct module-attribute assignment since this runs in
    # a fresh subprocess with no monkeypatch fixture available.
    priority_drain.coordinator_state_root = lambda central=False: str(central_root)
    priority_set.coordinator_state_root = lambda central=False: str(central_root)

    _orig_move = priority_drain._move

    def _crash_before_archival_move(path, dest_dir):
        if dest_dir.name == "drained":
            # Deterministic kill point: the ledger write (priority_set.
            # set_priority()) has ALREADY succeeded for this record -- we
            # are about to archive it. Kill here, unconditionally, with no
            # cleanup handler able to run, so the archival move never
            # happens and the record is left in place in the inbox root.
            os.kill(os.getpid(), 9)  # SIGKILL
        return _orig_move(path, dest_dir)

    priority_drain._move = _crash_before_archival_move

    priority_drain.drain()
    print("UNREACHABLE -- should have been SIGKILLed before returning")
    """
)


@pytest.mark.skipif(
    not hasattr(os, "kill") or sys.platform.startswith("win"),
    reason="AC1's kill mechanism (os.kill SIGKILL) has no deterministic POSIX-equivalent on Windows",
)
def test_ac1_durability_across_real_crash_mid_drain(_isolated_central_root):
    central_root = _isolated_central_root

    _drop_record(
        central_root,
        "0001-handoff-crash.yaml",
        target_id="handoff-crash001",
        priority="urgent",
        sequence=1,
        requested_by="example-cockpit-repo",
        note="AC1 crash-mid-drain",
    )

    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{_PROJECT_ROOT}{os.pathsep}{existing_pp}" if existing_pp else str(_PROJECT_ROOT)
    )

    result = subprocess.run(  # popup-intentional-last-resort
        [sys.executable, "-c", _CRASH_SCRIPT, str(central_root)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(_PROJECT_ROOT),
        env=env,
    )

    # A REAL SIGKILL: the subprocess must NOT have exited cleanly, and must
    # NOT have reached the UNREACHABLE print past the kill point.
    assert result.returncode != 0, (
        f"expected the subprocess to be killed, but it exited cleanly "
        f"(stdout={result.stdout!r}, stderr={result.stderr!r})"
    )
    assert "UNREACHABLE" not in result.stdout

    # Sanity on the mid-crash intermediate state: the ledger write landed
    # (it happens before the kill point) but the record is still sitting in
    # the inbox root (the archival move never ran).
    ledger_file = _ledger_file(central_root, "handoff-crash001")
    assert ledger_file.is_file(), "the ledger write happens BEFORE the kill point and must have landed"
    inbox = _inbox(central_root)
    assert (inbox / "0001-handoff-crash.yaml").is_file(), (
        "the record must still be sitting in the inbox root -- the crash "
        "landed before the archival move that would relocate it"
    )
    assert not (inbox / "drained" / "0001-handoff-crash.yaml").exists()

    # RESTART: complete the drain in-process (the fixture already points
    # both modules at this same central_root).
    restart_result = drain()

    assert restart_result["failed"] == []
    assert restart_result["rejected"] == []

    # Exactly ONE ledger entry, exactly ONE provenance stamp.
    ledger_dir = central_root / "priority-ledger"
    ledger_files = list(ledger_dir.glob("handoff-crash001*"))
    assert len(ledger_files) == 1
    text = ledger_files[0].read_text()
    assert text.count("source: external-intent") == 1
    assert text.count("source_repo:") == 1

    # NO orphaned intent record left anywhere in the inbox root.
    assert not (inbox / "0001-handoff-crash.yaml").exists()
    remaining_in_root = [p for p in inbox.iterdir() if p.is_file()]
    assert remaining_in_root == [], f"orphaned record(s) left in inbox root: {remaining_in_root}"
    assert (inbox / "drained" / "0001-handoff-crash.yaml").is_file()

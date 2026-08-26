"""AC6 — the removal side refuses to delete a path that exists on disk.

Condition of assent from claude-central-em (2026-08-26) before the removal side
may be opened against a mirror this repo does not own, in their words "in the
code, not in the procedure".

AC2 fixes the CAUSE of the known false-positive class (`declared_payload`
sourced from the percolation SCAN surface misses a published-but-never-scanned
file, which then reads as undeclared). This pins the RECURRENCE catch. Both
known witnesses are fixtures here, because the whole argument for AC6 is that
the class has members nobody has enumerated yet:

  .github/scripts/check-persona-names.py   both mirrors; excluded from the
                                           transform sweep so the release-CI
                                           identity checker never scrubs itself
  coordinator_core/warm/door/door.exe      a binary in a DECLARED directory,
                                           tracked at HEAD, never scanned

No test here runs a real round or touches a publish mirror.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parents[1] / "percolate-round.py"
_spec = importlib.util.spec_from_file_location("percolate_round_ac6", _MOD_PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_refuses_when_a_candidate_is_still_on_disk(tmp_path):
    (tmp_path / ".github" / "scripts").mkdir(parents=True)
    live = ".github/scripts/check-persona-names.py"
    (tmp_path / live).write_text("BANNED = []\n", encoding="utf-8")

    with pytest.raises(_mod.RemovalCandidateOnDiskError) as excinfo:
        _mod._refuse_removals_present_on_disk(tmp_path, [live, "gone/from/disk.py"])

    msg = str(excinfo.value)
    assert live in msg
    # The message must say what to do, not merely that it refused: a refusal an
    # operator cannot act on is what sends them looking for an override.
    assert "declared_payload" in msg
    # Only the on-disk path is named — a genuine orphan is not a defect.
    assert "gone/from/disk.py" not in msg


def test_binary_in_a_declared_directory_is_caught(tmp_path):
    """`door.exe`'s shape: the class AC2 exists to fix, kept here so a
    regression in AC2 surfaces as a loud refusal rather than a deletion."""
    (tmp_path / "coordinator_core" / "warm" / "door").mkdir(parents=True)
    binary = "coordinator_core/warm/door/door.exe"
    (tmp_path / binary).write_bytes(b"MZ\x90\x00")

    with pytest.raises(_mod.RemovalCandidateOnDiskError):
        _mod._refuse_removals_present_on_disk(tmp_path, [binary])


def test_genuine_orphans_pass_through(tmp_path):
    """The point of the removal side still works: paths at HEAD and absent
    from disk are exactly what it exists to delete."""
    _mod._refuse_removals_present_on_disk(
        tmp_path, ["bin/migrated-away.py", "skills/repo-setup/residue/x.md"]
    )


def test_empty_candidate_set_is_a_noop(tmp_path):
    _mod._refuse_removals_present_on_disk(tmp_path, [])


def test_refusal_is_loud_not_a_silent_skip(tmp_path):
    """The distinction claude-central-em asked for explicitly. A silent-skip
    implementation would drop the live path and return the rest; this must
    raise instead, so a wrong operand set cannot look like a clean round."""
    (tmp_path / "live.py").write_text("x\n", encoding="utf-8")

    with pytest.raises(_mod.RemovalCandidateOnDiskError):
        _mod._refuse_removals_present_on_disk(tmp_path, ["live.py", "orphan.py"])


def test_message_caps_the_list_but_reports_the_true_count(tmp_path):
    """A mis-scope can name thousands. The message stays readable without
    understating how much was refused."""
    names = []
    for i in range(25):
        rel = f"payload-{i:02d}.py"
        (tmp_path / rel).write_text("x\n", encoding="utf-8")
        names.append(rel)

    with pytest.raises(_mod.RemovalCandidateOnDiskError) as excinfo:
        _mod._refuse_removals_present_on_disk(tmp_path, names)

    msg = str(excinfo.value)
    assert "named 25 path(s)" in msg
    assert "... and 5 more" in msg


def test_removal_side_stays_gated_off(tmp_path):
    """AC5: flipping `_REMOVAL_SIDE_ENABLED` is not this chunk, and it deletes
    from mirrors this repo does not own. Pinned so the flag cannot be flipped
    without a test saying so out loud."""
    assert _mod._REMOVAL_SIDE_ENABLED is False

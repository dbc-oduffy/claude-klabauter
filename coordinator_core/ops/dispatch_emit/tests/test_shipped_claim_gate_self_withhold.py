"""The shipped-claim gate does not refuse a run's own diff for a withhold the halt path applied.

Contract pinned for C2: `withheld_paths` is a Mapping[path, reason]; a reason in
`SELF_APPLIED_WITHHOLDS` never yields verdict `withheld`, a content reason still does.

Trace (C1) of where `halt-record-only` comes from:
- Stampers: `halted_record_commit` (every non-ledger-check halt; L664/L673) and `land_run_halt`
  (every non-ledger-check halt, and a ledger-check halt whose targeted-test check is
  `no-verdict`/`not-run`; `withheld.update(... HALT_RECORD_ONLY ...)`). Both stamp the halt's commit
  record AFTER the gate refused, so the field run's `halt_commit.withheld` is a consequence of the
  refusal, not its first-fire cause.
- `land_run.land_run` builds `gate_withheld` from `withhold_reason`, `recorded_reason`, `art_dropped`,
  `art_withheld`, `held_paths`; none of those maps can carry `halt-record-only` (reasons there are
  never-commit, sibling-repo, withheld/review-carried, reverted, outside-row-footprint, peer-held).
  The first-fire withhold on the original run therefore came from one of those, or from the
  `run_withhold`/`em_withheld` records read back on a refire; this unit file does not prove which
  (sizing detent `premise_unproven`; needs the field run's record dir).
- The halt-record-only map is what a refire or `terminal_commit` reads back (`kept_work.kept_chunks`
  keys on it), which is the map the gate must not treat as a refusal ground.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import kept_work, land_run_halt
from coordinator_core.ops.dispatch_emit.shipped_claims import (
    ShippedClaim,
    judge_shipped_claims,
    shipped_gate,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

HALT = "halt-record-only"


def _self_applied():
    from coordinator_core.ops.dispatch_emit import shipped_claims

    return shipped_claims.SELF_APPLIED_WITHHOLDS


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        subprocess.run(
            ["git", *args], cwd=str(root), check=True, capture_output=True, **no_console_creationflags()
        )

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("config", "core.autocrlf", "false")
    (root / "tracked.py").write_text("a\n", encoding="utf-8", newline="\n")
    git("add", ".")
    git("commit", "-q", "-m", "seed")
    (root / "tracked.py").write_text("b\n", encoding="utf-8", newline="\n")
    (root / "arms.py").write_text("n\n", encoding="utf-8", newline="\n")
    (root / "peer.py").write_text("n\n", encoding="utf-8", newline="\n")
    return root


def _judge(root, path, **kw):
    (v,) = judge_shipped_claims(root, [ShippedClaim(path, f"`{path}`")], **kw)
    return v.verdict


def test_the_two_halt_record_only_literals_are_equal():
    assert kept_work.HALT_RECORD_ONLY == land_run_halt.HALT_RECORD_ONLY == HALT


def test_self_applied_set_is_seeded_from_halt_record_only():
    assert kept_work.HALT_RECORD_ONLY in _self_applied()
    assert "peer-held" not in _self_applied()


def test_a_halt_record_only_path_in_the_landing_is_backed(repo):
    assert HALT in _self_applied()
    assert _judge(repo, "arms.py", landing_paths=["arms.py"], withheld_paths={"arms.py": HALT}) == "backed"


def test_a_peer_held_path_still_judges_withheld(repo):
    assert HALT in _self_applied()
    got = _judge(repo, "peer.py", landing_paths=["peer.py"], withheld_paths={"peer.py": "peer-held"})
    assert got == "withheld"


def test_a_halt_record_only_path_outside_the_landing_is_not_landed_never_withheld(repo):
    assert HALT in _self_applied()
    assert _judge(repo, "tracked.py", withheld_paths={"tracked.py": HALT}) == "not-in-commit"
    assert _judge(repo, "arms.py", withheld_paths={"arms.py": HALT}) == "untracked"


def test_a_refire_reading_back_a_halt_stamped_map_is_not_refused(repo):
    """The refire loop: the halt stamped the product halt-record-only, the refire reads it back."""
    owner = {"arms.py": "C1"}
    stamped = {"arms.py": HALT}
    assert "C1" in kept_work.kept_chunks(repo, owner, stamped)
    note = "## What shipped\n- `arms.py`\n"
    refusal, reply = shipped_gate(repo, note, landing_paths=["arms.py"], withheld_paths=stamped)
    assert refusal is None, refusal
    assert reply["unbacked"] == []

"""A roadmap-baton succession must not lose the predecessor edge it was handed.

`baton_assemble`'s succession directive passes `--predecessor=<the baton being
superseded>` on every roadmap-baton handoff. argparse accepted the flag,
`_scaffold_roadmap_baton` took no such parameter, and the successor was written
`predecessor: none` — while the predecessor half of the same supersession
carried `continued_into`. The chain read as broken from one end only, and
nothing warned.

Negative spec — what these tests deliberately do NOT assert:
  * That `predecessor` resolves, exists on disk, or names a real baton. The flag
    is carried VERBATIM by contract; the calling engine decides what it names.
  * That any OTHER frontmatter field is carried on succession. `blocked_by`,
    `sprint` and `wave` are deliberately not inherited (see
    `_scaffold_roadmap_baton`'s docstring); asserting them here would pin the
    opposite of the intended behaviour.
  * The schema's own `predecessor=none` cross-field rule. That binds
    spinoff/goal-seed/roadmap-seed and is enforced in the frontmatter layer;
    these tests cover the SCAFFOLDER's half of the contract only. The refusal
    tests below DO assert a clause from each refusal's own reason, but only to
    keep the two explanations distinguishable -- they assert nothing about
    whether the schema layer agrees.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN = Path(__file__).resolve().parents[1] / "coordinator-doc-new.py"

_PRED = "state/handoffs/2026-01-01_000000_probe-predecessor.md"


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_BIN), *args],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        **no_console_creationflags(),
    )


def _predecessor_line(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("predecessor:"):
            return line
    raise AssertionError(f"no predecessor: line in {path}")


def test_roadmap_baton_carries_a_supplied_predecessor(tmp_path: Path) -> None:
    out = tmp_path / "successor.md"
    proc = _run(
        tmp_path,
        "--type", "roadmap-baton",
        "--title", "probe",
        "--roadmap-id", "rm-probe",
        "--stub-id", "probe-1",
        "--no-sizing-object",
        "--predecessor", _PRED,
        "--out", str(out),
    )
    assert proc.returncode == 0, proc.stderr
    assert _predecessor_line(out) == f"predecessor: {_PRED}"


def test_roadmap_baton_without_the_flag_still_emits_none(tmp_path: Path) -> None:
    """Negative control: the carry must not change the default for every
    existing caller that does not pass the flag."""
    out = tmp_path / "successor.md"
    proc = _run(
        tmp_path,
        "--type", "roadmap-baton",
        "--title", "probe",
        "--roadmap-id", "rm-probe",
        "--stub-id", "probe-2",
        "--no-sizing-object",
        "--out", str(out),
    )
    assert proc.returncode == 0, proc.stderr
    assert _predecessor_line(out) == "predecessor: none"


@pytest.mark.parametrize("doc_type", ["spinoff", "goal-seed", "roadmap-seed"])
def test_predecessor_is_refused_for_the_none_by_design_kinds(
    tmp_path: Path, doc_type: str
) -> None:
    """Refused fail-loud, never silently dropped — dropping it is the defect
    this whole module exists for, one kind over."""
    out = tmp_path / "out.md"
    proc = _run(
        tmp_path,
        "--type", doc_type,
        "--title", "probe",
        "--predecessor", _PRED,
        "--out", str(out),
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "--predecessor is not accepted" in proc.stderr
    # The shared substring alone would not catch an edit that garbles the two
    # DISTINCT reasons behind it -- these kinds are none-by-design under the
    # schema's cross-field rule, which is a different fact from `recovery`'s
    # predecessor meaning a commit SHA. Pin the clause, not just the prefix.
    assert "predecessor:none-by-design" in proc.stderr
    assert "A3a-3" in proc.stderr
    assert not out.exists(), "refusal must scaffold nothing"


def test_predecessor_is_refused_for_recovery(tmp_path: Path) -> None:
    """`recovery`'s own `predecessor:` is a crashed commit SHA, not a baton
    path — a caller passing a path here has a real bug."""
    out = tmp_path / "out.md"
    proc = _run(
        tmp_path,
        "--type", "recovery",
        "--title", "probe",
        "--predecessor", _PRED,
        "--out", str(out),
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "--predecessor is not accepted" in proc.stderr
    # Sibling of the check above: pin recovery's OWN reason, so the two
    # explanations cannot silently collapse into each other.
    assert "crashed commit SHA" in proc.stderr
    assert "--recovers-session" in proc.stderr
    assert not out.exists()

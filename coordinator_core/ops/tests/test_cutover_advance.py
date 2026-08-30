"""
coordinator_core.ops.tests.test_cutover_advance — scoped tests for the
"cutover.advance" op handler (C5): calls cutover.gate internally, refuses
the phase bump on anything short of PASS (naming unconfirmed consumers +
how to confirm them), and writes the phase bump + derivation_history entry
only on a clean PASS.

Does NOT duplicate cutover.gate's own agreement-leg coverage
(test_cutover_gate_handler.py) — these tests exercise cutover.advance's OWN
behavior: the terminal-phase guard, the write-on-PASS-only rule, and the
design-as-offers refusal detail.

Async invocation follows the house convention (test_coverage_gate.py,
test_cutover_gate_handler.py): plain sync test functions wrapping the async
handler in `asyncio.run(...)`.

Spec backlink: DoE-claude:pln-cutover-state-machine-a-phase--96db57 § C5
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.cutover_advance import _cutover_advance
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture()
def git_repo_root(tmp_path: Path) -> Path:
    """A real git-init'd worktree root.

    Review: code-reviewer — locked_rmw (routed through on the PASS/write
    path as of the P2 unlocked-RMW fix) resolves its lock sidecar via
    `git rev-parse --git-common-dir`, which requires a real git repository
    at repo_root; a bare non-existent `.git` path (as several REFUSE/
    INDETERMINATE-path tests here still use, since those never reach
    locked_rmw) raises before the write. Only tests that reach a PASS/write
    need this fixture.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_root), capture_output=True, check=True, **no_console_creationflags())
    subprocess.run(
        ["git", "config", "user.email", "cutover-advance-test@claude-klabauter.test"],
        cwd=str(repo_root), capture_output=True, check=True,
    **no_console_creationflags(),
)
    subprocess.run(
        ["git", "config", "user.name", "Cutover Advance Test"],
        cwd=str(repo_root), capture_output=True, check=True,
    **no_console_creationflags(),
)
    return repo_root


def _common_dir(repo_root: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=str(repo_root), capture_output=True, check=True, encoding="utf-8",
    **no_console_creationflags(),
)
    return Path(result.stdout.strip())


def _write_record(
    tmp_path: Path,
    *,
    phase: str = "dual-write",
    confirmed_consumers: list | None = None,
    gate_source: dict | None = None,
    derivation_history: list | None = None,
) -> Path:
    fm: dict = {
        "surface": "test surface",
        "phase": phase,
        "confirmed_consumers": confirmed_consumers or [],
        "gate_source": gate_source
        or {
            "kind": "value-vocabulary",
            "pattern": "TARGET_TOKEN",
            "paths": ["sub"],
            "repos": [{"repo": "doe-claude", "foreign": False}],
        },
    }
    if derivation_history is not None:
        fm["derivation_history"] = derivation_history
    fm_text = yaml.safe_dump(fm, default_flow_style=False, sort_keys=False)
    record_path = tmp_path / "record.md"
    record_path.write_text(f"---\n{fm_text}---\n\n# Test record\n", encoding="utf-8")
    return record_path


def _write_consumer_writer(tmp_path: Path, filename: str = "producer.py") -> None:
    sub = tmp_path / "sub"
    sub.mkdir(exist_ok=True)
    (sub / filename).write_text('TOKEN = "TARGET_TOKEN"\n', encoding="utf-8")


def _advance(params: dict, repo_root) -> dict:
    return asyncio.run(_cutover_advance(params, repo_root=repo_root))


def _confirmed_producer_entry() -> dict:
    return {
        "id": "doe-claude:sub/producer.py",
        "verified_by": {"kind": "probe-op-key", "ref": "ping"},
        "verified_at": "2026-07-25",
    }


def test_missing_record_param_is_setup_error(tmp_path: Path) -> None:
    result = _advance({}, tmp_path / ".git")
    assert result["exit_code"] == 1
    assert "record" in result["notes"][0]


def test_repo_root_none_is_setup_error() -> None:
    result = _advance({"record": "record.md"}, None)
    assert result["exit_code"] == 1


def test_advance_on_empty_derivation_refuses_and_does_not_write(tmp_path: Path) -> None:
    # No sub/ files -- derive() finds nothing, gate returns INDETERMINATE.
    record_path = _write_record(tmp_path)
    before = record_path.read_text(encoding="utf-8")
    result = _advance({"record": str(record_path)}, tmp_path / ".git")
    after = record_path.read_text(encoding="utf-8")
    assert result["exit_code"] == 2
    assert "VERDICT=INDETERMINATE" in result["verdict_line"]
    assert after == before


def test_advance_names_unconfirmed_consumer_on_indeterminate_signal2(tmp_path: Path) -> None:
    """Two derived consumers, only one confirmed with a re-verifiable ref and
    one confirmed with an unregistered probe-op-key -- cutover.gate REFUSEs
    on the unresolvable-confirmed-id leg first (agreement leg 2 fires before
    signal-2), and this op's own design-as-offers note names the SECOND
    (never-confirmed-at-all) derived id alongside the exact command to
    confirm it -- proving the note names every derived-but-unconfirmed id,
    not just the one that tripped the gate's own refusal.
    """
    _write_consumer_writer(tmp_path, filename="producer.py")
    _write_consumer_writer(tmp_path, filename="other.py")
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "doe-claude:sub/producer.py",
                "verified_by": {"kind": "probe-op-key", "ref": "no.such.op"},
                "verified_at": "2026-07-25",
            }
        ],
    )
    result = _advance({"record": str(record_path)}, tmp_path / ".git")
    assert result["exit_code"] == 2
    assert any("confirm-consumer" in note and "other.py" in note for note in result["notes"])


def test_advance_refuses_and_names_unresolvable_confirmed_id(tmp_path: Path) -> None:
    _write_consumer_writer(tmp_path)
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "doe-claude:sub/nonexistent.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            }
        ],
    )
    before = record_path.read_text(encoding="utf-8")
    result = _advance({"record": str(record_path)}, tmp_path / ".git")
    after = record_path.read_text(encoding="utf-8")
    assert result["exit_code"] == 2
    assert "VERDICT=REFUSE" in result["verdict_line"]
    assert after == before
    assert any("confirm-consumer" in note for note in result["notes"])
    assert any("sub/producer.py" in note for note in result["notes"])


def test_advance_on_pass_writes_next_phase_and_history(git_repo_root: Path) -> None:
    _write_consumer_writer(git_repo_root)
    record_path = _write_record(
        git_repo_root,
        phase="dual-write",
        confirmed_consumers=[_confirmed_producer_entry()],
    )
    result = _advance({"record": str(record_path)}, _common_dir(git_repo_root))
    assert result["exit_code"] == 0
    assert "VERDICT=ADVANCED" in result["verdict_line"]
    assert "phase='retiring'" in result["verdict_line"]

    after_fm = yaml.safe_load(record_path.read_text(encoding="utf-8").split("---")[1])
    assert after_fm["phase"] == "retiring"
    assert len(after_fm["derivation_history"]) == 1
    assert after_fm["derivation_history"][0]["derived_count"] == 1


def test_advance_reader_widen_to_dual_write(git_repo_root: Path) -> None:
    _write_consumer_writer(git_repo_root)
    record_path = _write_record(
        git_repo_root,
        phase="reader-widen",
        confirmed_consumers=[_confirmed_producer_entry()],
    )
    result = _advance({"record": str(record_path)}, _common_dir(git_repo_root))
    assert result["exit_code"] == 0
    after_fm = yaml.safe_load(record_path.read_text(encoding="utf-8").split("---")[1])
    assert after_fm["phase"] == "dual-write"


def test_advance_at_terminal_phase_is_setup_error(tmp_path: Path) -> None:
    _write_consumer_writer(tmp_path)
    record_path = _write_record(
        tmp_path,
        phase="retired",
        confirmed_consumers=[_confirmed_producer_entry()],
    )
    before = record_path.read_text(encoding="utf-8")
    result = _advance({"record": str(record_path)}, tmp_path / ".git")
    after = record_path.read_text(encoding="utf-8")
    assert result["exit_code"] == 1
    assert "terminal phase" in result["notes"][0]
    assert after == before


def test_advance_preserves_existing_derivation_history_entries(git_repo_root: Path) -> None:
    _write_consumer_writer(git_repo_root)
    record_path = _write_record(
        git_repo_root,
        phase="dual-write",
        confirmed_consumers=[_confirmed_producer_entry()],
        derivation_history=[
            {
                "phase": "reader-widen",
                "derived_count": 1,
                "derived_ids": ["doe-claude:sub/producer.py"],
                "at": "2026-07-24T00:00:00Z",
            }
        ],
    )
    result = _advance({"record": str(record_path)}, _common_dir(git_repo_root))
    assert result["exit_code"] == 0
    after_fm = yaml.safe_load(record_path.read_text(encoding="utf-8").split("---")[1])
    assert len(after_fm["derivation_history"]) == 2
    assert after_fm["derivation_history"][0]["phase"] == "reader-widen"
    assert after_fm["derivation_history"][1]["phase"] == "dual-write"


def test_advance_writes_via_locked_rmw(git_repo_root: Path, monkeypatch) -> None:
    """Review: code-reviewer — every sibling lifecycle verb in
    handoff_transition.py routes its write through locked_rmw; this op
    previously wrote via a bare write_text with no lock. Assert the write
    now goes through locked_rmw (not a direct write_text call)."""
    import coordinator_core.ops.cutover_advance as advance_mod

    calls: list[Path] = []
    real_locked_rmw = advance_mod.locked_rmw

    def _spy(target, mutate, *, repo_root, **kwargs):
        calls.append(target)
        return real_locked_rmw(target, mutate, repo_root=repo_root, **kwargs)

    monkeypatch.setattr(advance_mod, "locked_rmw", _spy)

    _write_consumer_writer(git_repo_root)
    record_path = _write_record(
        git_repo_root,
        phase="dual-write",
        confirmed_consumers=[_confirmed_producer_entry()],
    )
    result = _advance({"record": str(record_path)}, _common_dir(git_repo_root))
    assert result["exit_code"] == 0
    assert calls == [record_path.resolve()]

    after_fm = yaml.safe_load(record_path.read_text(encoding="utf-8").split("---")[1])
    assert after_fm["phase"] == "retiring"


def test_advance_aborts_on_phase_changed_since_gate_pass(git_repo_root: Path, monkeypatch) -> None:
    """If the record's on-disk phase differs from the phase the gate PASS
    was computed against (a concurrent advance won the race and already
    bumped it), the locked mutate closure must abort rather than clobber
    the concurrent writer's derivation_history append."""
    import coordinator_core.ops.cutover_advance as advance_mod

    _write_consumer_writer(git_repo_root)
    record_path = _write_record(
        git_repo_root,
        phase="dual-write",
        confirmed_consumers=[_confirmed_producer_entry()],
    )

    real_locked_rmw = advance_mod.locked_rmw

    def _mutate_underfoot(target, mutate, *, repo_root, **kwargs):
        # Simulate a concurrent winner: rewrite the on-disk phase to
        # something other than what the gate PASS (computed against
        # "dual-write") expects, THEN let the real locked_rmw run the
        # mutate closure against that now-stale text.
        text = target.read_text(encoding="utf-8")
        raced_text = text.replace("phase: dual-write", "phase: retiring")
        target.write_text(raced_text, encoding="utf-8")
        return real_locked_rmw(target, mutate, repo_root=repo_root, **kwargs)

    monkeypatch.setattr(advance_mod, "locked_rmw", _mutate_underfoot)

    result = _advance({"record": str(record_path)}, _common_dir(git_repo_root))
    assert result["exit_code"] == 1
    assert "phase changed" in result["notes"][0]

    after_fm = yaml.safe_load(record_path.read_text(encoding="utf-8").split("---")[1])
    # The raced writer's phase bump survives untouched -- this call did not
    # clobber it.
    assert after_fm["phase"] == "retiring"
    assert "derivation_history" not in after_fm or after_fm.get("derivation_history") in (
        None,
        [],
    )


def test_unregistered_gate_op_is_setup_error(tmp_path: Path, monkeypatch) -> None:
    """No ungated advance path exists (D4): if cutover.gate cannot be resolved,
    the advance is refused (setup error), never silently performed."""
    import coordinator_core.ops.cutover_advance as advance_mod

    monkeypatch.setattr(advance_mod, "get_op_handler", lambda name: None)
    _write_consumer_writer(tmp_path)
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[_confirmed_producer_entry()],
    )
    before = record_path.read_text(encoding="utf-8")
    result = _advance({"record": str(record_path)}, tmp_path / ".git")
    after = record_path.read_text(encoding="utf-8")
    assert result["exit_code"] == 1
    assert "not registered" in result["notes"][0]
    assert after == before

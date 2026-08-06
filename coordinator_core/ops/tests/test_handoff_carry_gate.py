"""Characterization + regression tests for coordinator_core.ops.handoff_carry_gate.

The load-bearing cases are the two refusals the gate still owns —
`test_terminal_disposition_without_detail_is_refused_not_a_free_bypass` and
`test_missing_carry_id_fails_loud` — plus
`test_indefinite_carry_is_allowed`, which pins the PM ruling that carry depth
is NOT a defect: no count, no limit, no refusal on depth.

Spec backlink: example-doctrine-repo coordinator/schemas/handoff.schema.json
    `carried_items`; coordinator/skills/handoff/SKILL.md § Cascading
    unresolved items.
"""
from __future__ import annotations

from coordinator_core.ops.handoff_carry_gate import evaluate_gate, main


def _item(carry_id="cf-windows-validation-3f2a1c", disposition="carried", **overrides):
    d = {
        "carry_id": carry_id,
        "description": "Windows validation",
        "disposition": disposition,
    }
    d.update(overrides)
    return d


# ---------------------------------------------------------------------------
# Carries are indefinite
# ---------------------------------------------------------------------------


def test_carried_item_is_allowed():
    result = evaluate_gate([_item()])
    assert result.ok
    assert result.violations == []


def test_indefinite_carry_is_allowed():
    """Depth is not a defect. A legacy carry_count in the frontmatter is inert —
    the gate neither reads it nor refuses on it, at any value."""
    for legacy_count in (1, 3, 8, 40):
        result = evaluate_gate([_item(carry_count=legacy_count)])
        assert result.ok, f"carry_count={legacy_count} must not be refused"


def test_missing_carry_count_is_not_a_violation():
    result = evaluate_gate([{"carry_id": "cf-x-abc123", "description": "x", "disposition": "carried"}])
    assert result.ok


def test_closed_disposition_with_detail_is_ok():
    result = evaluate_gate(
        [_item(disposition="closed", disposition_detail="assessed and deliberately left")]
    )
    assert result.ok


def test_spun_off_disposition_with_detail_is_ok():
    result = evaluate_gate(
        [_item(disposition="spun_off", disposition_detail="state/handoffs/windows-validation.md")]
    )
    assert result.ok


def test_blocked_disposition_with_detail_is_ok():
    result = evaluate_gate(
        [
            _item(
                disposition="blocked",
                disposition_detail="requires a reachable Windows host; none available this cycle",
            )
        ]
    )
    assert result.ok


# ---------------------------------------------------------------------------
# Fail-loud on undeclared state — never fail open
# ---------------------------------------------------------------------------


def test_terminal_disposition_without_detail_is_refused_not_a_free_bypass():
    """A disposition of closed/spun_off/blocked with NO detail is not a rubber
    stamp — it must name the reason, else it is refused."""
    for disposition in ("closed", "spun_off", "blocked"):
        result = evaluate_gate([_item(disposition=disposition)])
        assert not result.ok, f"disposition={disposition} with no detail should be refused"
        assert "disposition_detail" in result.violations[0]


def test_missing_carry_id_fails_loud():
    result = evaluate_gate([{"description": "x", "disposition": "carried"}])
    assert not result.ok
    assert "carry_id" in result.violations[0]


def test_unrecognized_disposition_fails_loud():
    result = evaluate_gate([_item(disposition="ignored")])
    assert not result.ok


def test_missing_disposition_fails_loud():
    result = evaluate_gate([{"carry_id": "cf-x-abc123", "description": "x"}])
    assert not result.ok


# ---------------------------------------------------------------------------
# CLI (main()) — read from disk, exit-code contract
# ---------------------------------------------------------------------------


def _write_handoff(tmp_path, carried_items_yaml_block: str):
    p = tmp_path / "handoff.md"
    p.write_text(
        "---\n"
        "title: test\n"
        "created: 2026-07-25\n"
        "branch: work/test\n"
        "status: open\n"
        "predecessor: null\n"
        f"{carried_items_yaml_block}"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    return p


def test_cli_exit_0_when_no_carried_items(tmp_path):
    p = _write_handoff(tmp_path, "")
    assert main(["check", str(p)]) == 0


def test_cli_exit_0_on_a_long_running_carry(tmp_path):
    p = _write_handoff(
        tmp_path,
        "carried_items:\n"
        "  - carry_id: cf-windows-validation-3f2a1c\n"
        "    description: Windows validation\n"
        "    disposition: carried\n",
    )
    assert main(["check", str(p)]) == 0


def test_cli_exit_1_on_undeclared_disposition(tmp_path, capsys):
    p = _write_handoff(
        tmp_path,
        "carried_items:\n"
        "  - carry_id: cf-windows-validation-3f2a1c\n"
        "    description: Windows validation\n"
        "    disposition: closed\n",
    )
    rc = main(["check", str(p)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "cf-windows-validation-3f2a1c" in err


def test_cli_exit_2_on_missing_path():
    assert main(["check", "/nonexistent/path/handoff.md"]) == 2


def test_cli_exit_2_on_stale_override_reason_trailing_arg(tmp_path):
    """A stale caller still passing --override-reason must fail loud, not be
    silently swallowed and slide through to the ordinary refusal path."""
    p = _write_handoff(
        tmp_path,
        "carried_items:\n"
        "  - carry_id: cf-windows-validation-3f2a1c\n"
        "    description: Windows validation\n"
        "    disposition: carried\n",
    )
    rc = main(["check", str(p), "--override-reason", "stale excuse"])
    assert rc == 2

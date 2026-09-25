"""
coordinator_core.bash_guards.tests.test_bare_skill_gates_are_guard_enforced
— P071-C6's spike: for the four bare, assembler-less skills
(``finishing-a-development-branch``, ``parallel-code-review``, ``validate``,
``plan-delivery-audit``), does an existing guard already discharge the
citation's gate, or does the row need a new mechanism?

Two things live in this file:

1. One case per GUARD-ENFORCED ledger row: drives
   ``check_test_suite_invocation.check`` in-process with the resolved
   Tier-U-shaped command the skill's ``tier-u-grant-cli check`` citation
   gates, and asserts a deny naming the gate without a live grant, plus an
   allow with one — so a guard that always denies cannot pass.
2. The ledger-integrity assertion (E1): parses
   ``docs/reference/grant-and-validation-emit-dispositions.md`` and asserts
   its pinned ref, its row set, and its per-row disposition shape all still
   hold, mechanically rather than by an operator re-reading the table.

NEGATIVE SPEC: proving the guard body refuses this command shape IN-PROCESS,
in THIS repo, does not prove the guard is WIRED for these four skills'
sessions — that wiring is DoE-claude's
`coordinator/hooks/effective-delivery.json` (id `check-test-suite-invocation`,
read at DoE-claude ref `57e1174993d82c2cc0fc867f1b8f65a4a75d8380`,
lines 839-840), which can drift independently of this file staying green.
Every GUARD-ENFORCED case below cites that ref and line pair as a precondition,
never as proven from this repo alone.

Zero process spawns, no git, no subprocess — fast tier, no new tier.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard

# The DoE-claude hook-wiring precondition every GUARD-ENFORCED case below
# depends on but cannot itself verify (see module docstring NEGATIVE SPEC).
_DOE_HOOK_WIRING_REF = "57e1174993d82c2cc0fc867f1b8f65a4a75d8380"
_DOE_HOOK_WIRING_SITE = "coordinator/hooks/effective-delivery.json:839-840 (id check-test-suite-invocation)"

_LEDGER_PATH = Path(__file__).resolve().parents[3] / "docs" / "reference" / "grant-and-validation-emit-dispositions.md"

_CLOSED_SET = {
    "ENTRYPOINT",
    "EMIT",
    "GUARD-ENFORCED",
    "LOAD-BEARING",
    "REDUNDANT",
    "GATED-B0",
    "SPLIT-OUT",
    "ALREADY-MEMOED",
}

_KNOWN_CLIS = ("tier-u-grant-cli", "coordinator-resolve-validation-cmd", "tier-last-run")


def _payload(command: str, cwd: str) -> Dict[str, object]:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": str(cwd),
    }


def _reason(out) -> str:
    assert out is not None, "expected a deny envelope, got allow"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    return hso["permissionDecisionReason"]


def _assert_allowed(verdict) -> None:
    if verdict is None:
        return
    decision = verdict.get("hookSpecificOutput", {}).get("permissionDecision")
    assert decision == "allow", (
        "expected the command to be allowed, got %r: %s"
        % (decision, verdict.get("hookSpecificOutput", {}).get("permissionDecisionReason", ""))
    )


@pytest.fixture
def bare_skill_repo(tmp_path, monkeypatch):
    """A fake repo root, no real git — the identity/mutex legs are held
    constant (free mutex, top-level EM, no ``agent_id``) so each case below
    isolates the grant leg only, the leg these ledger rows actually cite."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\"coordinator_core\"]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
    monkeypatch.setattr(guard, "_mutex_holder", lambda: None)
    return tmp_path


def _ungranted(monkeypatch):
    monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (False, None))


def _granted(monkeypatch):
    monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (True, None))


# ---------------------------------------------------------------------------
# GUARD-ENFORCED cases — one per ledger row.
#
# Each drives the resolved Tier-U-shaped command (unscoped, whole-suite) the
# skill's own `tier-u-grant-cli check` citation gates: ungranted denies
# naming the gate ("Tier-U" + "authorization grant"); granted and wrapped in
# `with-suite-mutex` allows — so a guard wired to always deny cannot pass.
# ---------------------------------------------------------------------------


def test_row_11_finishing_a_development_branch_guard_enforced(bare_skill_repo, monkeypatch):
    """Ledger row 11 — SKILL.md:26 Step 1, gating a resolved fast/full
    validation command. Precondition: %s at DoE ref %s.""" % (
        _DOE_HOOK_WIRING_SITE,
        _DOE_HOOK_WIRING_REF,
    )
    _ungranted(monkeypatch)
    reason = _reason(guard.check(_payload("pytest", bare_skill_repo)))
    assert "Tier-U" in reason
    assert "authorization grant" in reason

    _granted(monkeypatch)
    _assert_allowed(guard.check(_payload("with-suite-mutex -- pytest", bare_skill_repo)))


def test_row_15_plan_delivery_audit_guard_enforced(bare_skill_repo, monkeypatch):
    """Ledger row 15 — SKILL.md:50, Oracle 2(b)'s project test suite gate.
    Precondition: %s at DoE ref %s.""" % (
        _DOE_HOOK_WIRING_SITE,
        _DOE_HOOK_WIRING_REF,
    )
    _ungranted(monkeypatch)
    reason = _reason(guard.check(_payload("pytest", bare_skill_repo)))
    assert "Tier-U" in reason
    assert "authorization grant" in reason

    _granted(monkeypatch)
    _assert_allowed(guard.check(_payload("with-suite-mutex -- pytest", bare_skill_repo)))


def test_row_16_validate_guard_enforced(bare_skill_repo, monkeypatch):
    """Ledger row 16 — SKILL.md:65, validate's pre-invocation tier gate.
    Precondition: %s at DoE ref %s.""" % (
        _DOE_HOOK_WIRING_SITE,
        _DOE_HOOK_WIRING_REF,
    )
    _ungranted(monkeypatch)
    reason = _reason(guard.check(_payload("pytest", bare_skill_repo)))
    assert "Tier-U" in reason
    assert "authorization grant" in reason

    _granted(monkeypatch)
    _assert_allowed(guard.check(_payload("with-suite-mutex -- pytest", bare_skill_repo)))


_GUARD_ENFORCED_CASES = {
    "coordinator/skills/finishing-a-development-branch/SKILL.md:26:tier-u-grant-cli":
        "test_row_11_finishing_a_development_branch_guard_enforced",
    "coordinator/skills/plan-delivery-audit/SKILL.md:50:tier-u-grant-cli":
        "test_row_15_plan_delivery_audit_guard_enforced",
    "coordinator/skills/validate/SKILL.md:65:tier-u-grant-cli":
        "test_row_16_validate_guard_enforced",
}


# ---------------------------------------------------------------------------
# Ledger-integrity assertion (E1) — reads only this repo's ledger and its
# own frozen constant below. No DoE clone, no git or grep spawn.
# ---------------------------------------------------------------------------

# The frozen occurrence set backing the C1 ledger table, at the pinned DoE
# ref — copied verbatim from the ledger's own "Frozen occurrence set" fence
# so a drift in either place, without the other, goes red.
_FROZEN_REF = "57e1174993d82c2cc0fc867f1b8f65a4a75d8380"
_FROZEN_OCCURRENCES: Tuple[str, ...] = (
    "coordinator/commands/bug-blitz.md:54:tier-u-grant-cli",
    "coordinator/commands/bug-blitz.md:55:tier-u-grant-cli",
    "coordinator/commands/bug-blitz.md:60:coordinator-resolve-validation-cmd",
    "coordinator/commands/workday-complete.md:26:tier-u-grant-cli",
    "coordinator/commands/workday-complete.md:31:tier-u-grant-cli",
    "coordinator/commands/install.md:235:coordinator-resolve-validation-cmd",
    "coordinator/commands/install.md:240:coordinator-resolve-validation-cmd",
    "coordinator/skills/bug-sweep/SKILL.md:89:tier-u-grant-cli",
    "coordinator/skills/bug-sweep/SKILL.md:91:tier-u-grant-cli",
    "coordinator/skills/bug-sweep/SKILL.md:163:tier-u-grant-cli",
    "coordinator/skills/finishing-a-development-branch/SKILL.md:26:tier-u-grant-cli",
    "coordinator/skills/finishing-a-development-branch/SKILL.md:26:coordinator-resolve-validation-cmd",
    "coordinator/skills/merging-to-main/SKILL.md:32:tier-u-grant-cli",
    "coordinator/skills/parallel-code-review/SKILL.md:80:tier-u-grant-cli",
    "coordinator/skills/parallel-code-review/SKILL.md:83:coordinator-resolve-validation-cmd",
    "coordinator/skills/plan-delivery-audit/SKILL.md:50:tier-u-grant-cli",
    "coordinator/skills/validate/SKILL.md:65:tier-u-grant-cli",
    "coordinator/skills/validate/SKILL.md:86:tier-last-run",
)

def _ledger_text() -> str:
    return _LEDGER_PATH.read_text(encoding="utf-8")


def _ledger_pinned_ref(text: str) -> str:
    m = re.search(r"pinned_ref:\s*\n\s*DoE-claude:\s*([0-9a-f]+)", text)
    assert m, "ledger frontmatter is missing pinned_ref.DoE-claude"
    return m.group(1)


def _ledger_frozen_occurrences(text: str) -> List[str]:
    m = re.search(r"## Frozen occurrence set.*?```\s*\n(.*?)\n```", text, re.DOTALL)
    assert m, "ledger is missing its Frozen occurrence set fence"
    return [line.strip() for line in m.group(1).splitlines() if line.strip()]


def _cli_in(text: str, previous: str = "") -> str:
    for cli in _KNOWN_CLIS:
        if cli in text:
            return cli
    if text.strip().lower().startswith("same") and previous:
        # e.g. row 7's cited-CLI column reads "same, PowerShell" rather than
        # repeating row 6's full CLI text verbatim.
        return previous
    raise AssertionError("no known CLI name found in cited-CLI column: %r" % text)


def _ledger_rows(text: str) -> List[Dict[str, str]]:
    start = text.index("## Occurrence table")
    end = text.index("## Split-out section")
    body = text[start:end]
    rows: List[Dict[str, str]] = []
    previous_cli = ""
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---"):
            continue
        # Table row: | # | Path:line | Cited CLI + verb | Ceremony | Assembler | Disposition | Note |
        # Split on "|" — none of the cell contents use a literal pipe.
        cells = [c.strip() for c in line.split("|")]
        # A leading/trailing empty cell from the outer "|"s; drop both.
        cells = cells[1:-1] if cells and cells[0] == "" else cells
        if not cells or cells[0] in ("#", "---"):
            continue
        if len(cells) < 6:
            continue
        _idx, path_line_col, cli_col, _ceremony, assembler, disposition = cells[:6]
        path_line = path_line_col.strip("`")
        if not re.match(r"^coordinator/", path_line):
            continue
        cli = _cli_in(cli_col, previous_cli)
        previous_cli = cli
        rows.append(
            {
                "path_line": path_line,
                "cli": cli,
                "assembler": assembler,
                "disposition": disposition.strip(),
                "note": cells[6] if len(cells) > 6 else "",
            }
        )
    return rows


class TestLedgerIntegrity:
    def test_pinned_ref_matches_frozen_constant(self):
        assert _ledger_pinned_ref(_ledger_text()) == _FROZEN_REF

    def test_ledger_occurrences_equal_frozen_set_exactly(self):
        text = _ledger_text()
        ledger_set = set(_ledger_frozen_occurrences(text))
        assert ledger_set == set(_FROZEN_OCCURRENCES)

    def test_row_keys_equal_frozen_occurrences_exactly(self):
        text = _ledger_text()
        rows = _ledger_rows(text)
        row_keys: Set[str] = {"%s:%s" % (r["path_line"], r["cli"]) for r in rows}
        assert row_keys == set(_FROZEN_OCCURRENCES)

    def test_every_row_has_exactly_one_closed_set_disposition(self):
        rows = _ledger_rows(_ledger_text())
        for row in rows:
            assert row["disposition"] in _CLOSED_SET, (
                "row %s carries a non-closed-set disposition %r"
                % (row["path_line"], row["disposition"])
            )

    def test_every_assembler_none_row_converged(self):
        """Every row C1 marked assembler NONE must now carry
        GUARD-ENFORCED, LOAD-BEARING or SPLIT-OUT — never the transitional
        PENDING (C6) placeholder."""
        rows = _ledger_rows(_ledger_text())
        for row in rows:
            if row["assembler"].strip().upper().startswith("NONE"):
                assert row["disposition"] in {"GUARD-ENFORCED", "LOAD-BEARING", "SPLIT-OUT"}, (
                    "assembler-NONE row %s did not converge: %r"
                    % (row["path_line"], row["disposition"])
                )

    def test_every_guard_enforced_row_names_a_case_that_exists(self):
        import sys

        module = sys.modules[__name__]
        rows = _ledger_rows(_ledger_text())
        found_any = False
        for row in rows:
            if row["disposition"] != "GUARD-ENFORCED":
                continue
            found_any = True
            key = "%s:%s" % (row["path_line"], row["cli"])
            case_name = _GUARD_ENFORCED_CASES.get(key)
            assert case_name, "no known pinning-test case registered for GUARD-ENFORCED row %s" % key
            assert hasattr(module, case_name), (
                "ledger names case %r for row %s, but it does not exist in this file"
                % (case_name, key)
            )
        assert found_any, "expected at least one GUARD-ENFORCED row"

    def test_split_out_section_is_empty(self):
        """Converged means the split-out section is empty — this plan's
        spike discharged every assembler-NONE row without a new mechanism."""
        text = _ledger_text()
        start = text.index("## Split-out section")
        end = text.index("## Scope note")
        body = text[start:end]
        # Strip the heading itself; anything left besides whitespace and the
        # explanatory "Empty." sentence means a row was actually split out.
        remainder = body.split("\n", 1)[1].strip()
        assert remainder.startswith("Empty."), "split-out section is non-empty: %r" % remainder

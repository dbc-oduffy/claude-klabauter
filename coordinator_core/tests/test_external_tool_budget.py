"""
Guards for the external-tool carve-out register
(`coordinator_core/external_tool_budget.py`).

WHAT THESE TESTS ARE FOR. `DR-349 § Carve-outs` records the cadence carve-out as
PM-ratified, and makes one word load-bearing: the grant attaches to *when* a
tool runs, never to how long it takes. Its closing condition — *"An author may
not migrate a tool onto a hot path and carry this carve-out along with it"* — is
a claim about the future, so it is only real if a future migration FAILS rather
than passing silently. `test_migrating_a_site_onto_a_hot_path_revokes_the_grant`
and `test_a_granted_row_cannot_declare_a_hot_trigger` are that condition; the
rest guard the register's enumerability, its ceiling, and its fan-out deadline.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core.external_tool_budget import (
    CARVE_OUTS,
    EXTERNAL_TOOL_BUDGET_SECS,
    EXTERNAL_TOOL_SITES,
    EXTERNAL_TOOL_SWEEP_BUDGET_SECS,
    HOT_TRIGGERS,
    REFUSED,
    ExternalToolSite,
    Trigger,
    bound_for,
    spawn_bound,
    sweep_deadline,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REFERENCE_DOC = _REPO_ROOT / "docs" / "reference" / "external-tool-carve-outs.md"


# --------------------------------------------------------------------------
# The ratified condition: the grant attaches to cadence, and cannot be carried
# onto a hot path.
# --------------------------------------------------------------------------


def test_the_trigger_alone_decides_the_grant():
    """DR-349's ratified wording. Not the bound, not the tool, not the runtime."""
    for site, entry in CARVE_OUTS.items():
        assert not entry.trigger.hot, f"{site} is granted while firing at {entry.trigger.name}"
        assert not entry.consumer_owned, f"{site} is granted but spawns consumer-owned code"
    for site, entry in REFUSED.items():
        assert entry.trigger.hot or entry.consumer_owned, f"{site} is refused for no stated reason"
        assert entry.remedy, f"{site} is refused and names no remedy"

    assert set(CARVE_OUTS) | set(REFUSED) == set(EXTERNAL_TOOL_SITES)
    assert not set(CARVE_OUTS) & set(REFUSED)


def test_migrating_a_site_onto_a_hot_path_revokes_the_grant():
    """The future case DR-349 names: an author moves a carved-out linter onto the
    commit path. Restating the trigger is unavoidable, and the new trigger takes
    the grant with it — silently carrying it across is not expressible."""
    granted = next(iter(CARVE_OUTS.values()))
    assert granted.granted()

    for hot in sorted(HOT_TRIGGERS, key=lambda t: t.name):
        migrated = ExternalToolSite(
            site=granted.site,
            tool=granted.tool,
            trigger=hot,
            bound_secs=granted.bound_secs,
            why=granted.why,
            remedy="planted: move it back off the hot path",
        )
        assert not migrated.granted(), f"{hot.name} carried the carve-out onto a hot path"
        assert "NOT carved out" in migrated.rationale()


def test_a_granted_row_cannot_declare_a_hot_trigger():
    """A hot-path row without a named remedy fails at import, so the migration
    cannot land as a one-word diff nobody reads."""
    with pytest.raises(ValueError, match="moved off it, not granted time"):
        ExternalToolSite(
            site="planted/violation.py :: _spawn",
            tool="planted-linter",
            trigger=Trigger.COMMIT,
            bound_secs=1.0,
            why="planted",
        )


def test_the_rationale_a_reader_gets_names_the_trigger_not_the_number():
    """"Because it fires at the weekly gate", never "because it needs 300s"."""
    for site, entry in EXTERNAL_TOOL_SITES.items():
        rationale = entry.rationale()
        assert entry.trigger.label in rationale, f"{site}'s rationale hides its trigger"
        assert str(int(entry.bound_secs)) not in rationale, f"{site}'s rationale argues from duration"


def test_every_trigger_declares_whether_it_is_hot():
    """Adding a way to fire a tool forces its author to answer the only question
    that decides the grant."""
    for trigger in Trigger:
        assert isinstance(trigger.hot, bool)
        assert trigger.label.strip(), f"{trigger.name} gives a reader no answer"
    assert HOT_TRIGGERS, "a register with no hot trigger cannot refuse anything"
    assert Trigger.COMMIT in HOT_TRIGGERS
    assert Trigger.SESSION_START in HOT_TRIGGERS


def test_no_registered_site_is_reachable_from_a_hook_module():
    """The declared trigger is a claim; this checks it against the tree. The hook
    package IS the commit/session path, so a registered module appearing there
    means some row's trigger is wrong."""
    hooks = _REPO_ROOT / "coordinator_core" / "hooks"
    modules = sorted(
        {site.split(" :: ")[0].split("/")[-1][: -len(".py")] for site in EXTERNAL_TOOL_SITES}
    )
    offenders = []
    for path in hooks.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for module in modules:
            if module in text:
                offenders.append(f"{path.relative_to(_REPO_ROOT)} -> {module}")
    assert not offenders, f"external-tool site reachable from a hook: {offenders}"


# --------------------------------------------------------------------------
# Enumerability, the ceiling, and the fan-out deadline.
# --------------------------------------------------------------------------


def test_every_row_sits_at_or_under_the_ceiling():
    over = {
        site: entry.bound_secs
        for site, entry in EXTERNAL_TOOL_SITES.items()
        if entry.bound_secs > EXTERNAL_TOOL_BUDGET_SECS
    }
    assert not over, f"rows above the {EXTERNAL_TOOL_BUDGET_SECS}s ceiling: {over}"


def test_a_row_above_the_ceiling_cannot_be_constructed():
    """The ceiling is enforced at construction, so a future row cannot quietly
    raise itself past it the way the census found 690 sites had."""
    with pytest.raises(ValueError, match="ceiling"):
        ExternalToolSite(
            site="planted/violation.py :: _spawn",
            tool="planted",
            trigger=Trigger.WEEKLY_GATE,
            bound_secs=EXTERNAL_TOOL_BUDGET_SECS + 1.0,
            why="planted",
        )


def test_every_row_carries_a_tool_and_a_rationale():
    for site, entry in EXTERNAL_TOOL_SITES.items():
        assert entry.tool.strip(), f"{site} names no tool"
        assert entry.why.strip(), f"{site} names no rationale"


def test_membership_is_never_implicit():
    """An unnamed site gets a KeyError, never a default bound — a fallback would
    make membership implicit, which is the failure mode the register prevents."""
    with pytest.raises(KeyError, match="not a named external-tool site"):
        bound_for("coordinator_core/ops/some_new_linter.py :: _run")


def test_spawn_bound_derives_from_the_remaining_sweep_deadline():
    """DR-349 § 4: stacking spawns cannot buy time."""
    site = "coordinator_core/ops/run_shellcheck_sweep.py :: _lint_one_file"
    start = 1000.0
    deadline = sweep_deadline(now=start)
    assert deadline == start + EXTERNAL_TOOL_SWEEP_BUDGET_SECS

    assert spawn_bound(site, deadline, now=start) == bound_for(site)
    assert spawn_bound(site, deadline, now=deadline - 5.0) == pytest.approx(5.0)
    assert spawn_bound(site, deadline, now=deadline + 1.0) == 0.0


def test_shellcheck_sweep_stamps_one_deadline_for_the_whole_fan_out(tmp_path, monkeypatch):
    """The sweep's bound belongs to the sweep. Driven through the real op with a
    stubbed spawn so the assertion is about the deadline plumbing, not about
    having shellcheck on PATH."""
    from coordinator_core.ops import run_shellcheck_sweep as mod

    seen: list[float] = []

    def _fake_lint(repo_root, rel_path, deadline):
        seen.append(deadline)
        return []

    monkeypatch.setattr(mod, "tracked_shell_files", lambda root: ["a.sh", "b.sh", "c.sh"])
    monkeypatch.setattr(mod, "_lint_one_file", _fake_lint)

    result = mod.run_shellcheck_sweep(tmp_path)

    assert result == {"findings": [], "files_checked": 3}
    assert len(seen) == 3
    assert len(set(seen)) == 1, "each file got its own deadline — the fan-out is unbounded"


def test_an_exhausted_sweep_fails_loudly_rather_than_returning_short(tmp_path, monkeypatch):
    """A sweep that stopped early reads identical to a clean one; the frozen
    `{findings, files_checked}` contract has no field to say otherwise."""
    from coordinator_core.ops import run_shellcheck_sweep as mod

    monkeypatch.setattr(mod, "tracked_shell_files", lambda root: ["a.sh"])
    monkeypatch.setattr(mod, "sweep_deadline", lambda: 0.0)

    with pytest.raises(RuntimeError, match="aggregate budget"):
        mod.run_shellcheck_sweep(tmp_path)


def test_named_modules_carry_no_loose_timeout_literal():
    """The historical numbers § G9 catalogued (180/300/600/900) are gone from the
    modules the register governs, so none can be copied onward."""
    modules = sorted({site.split(" :: ")[0] for site in EXTERNAL_TOOL_SITES})
    banned = re.compile(
        r"timeout\s*=\s*(180|300|600|900)\b|_SEC(?:ONDS|S)\s*=\s*(180|300|600|900)\b"
    )
    offenders = [rel for rel in modules if banned.search((_REPO_ROOT / rel).read_text(encoding="utf-8"))]
    assert not offenders, f"loose external-tool literals survive in: {offenders}"


def test_reference_doc_names_every_registered_site_and_its_trigger():
    """The prose face cannot drift from the mechanism, and must carry the
    trigger — a doc listing only bounds would reintroduce the framing the
    ratified wording replaced."""
    doc = _REFERENCE_DOC.read_text(encoding="utf-8")
    for site, entry in EXTERNAL_TOOL_SITES.items():
        # The doc writes sites repo-relative from `ops/`, the registry from the
        # package root; compare on the half both spell identically.
        anchor = site.split("coordinator_core/", 1)[-1]
        assert anchor in doc, f"{anchor} is registered but absent from {_REFERENCE_DOC.name}"
        assert entry.trigger.name in doc, f"{entry.trigger.name} is unnamed in {_REFERENCE_DOC.name}"

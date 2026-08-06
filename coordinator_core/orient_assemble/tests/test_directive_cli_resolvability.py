"""
coordinator_core.orient_assemble.tests.test_directive_cli_resolvability —
closes the gap `test_narration_and_constructor_discipline.py` leaves open:
that file asserts the directive *key set* (`id, cli, args, depends_on,
already_satisfied`) but nothing about `cli` being *resolvable*. 13 emission
sites across the four reader families shipped naming CLIs that do not
exist — see
state/subagent-share/conductor/phase-4/orient-assemble-fix-spec.md
(example-doctrine-repo) for the full site-by-site inventory and fix table.

Oracle: `coordinator_core.install.substrate._derive_agent_helper_target_map`
— the SAME derivation `_install_bin_resolvers` uses to generate real
forwarders and `check-forwarder-drift.py` uses to detect install staleness
(`coordinator_core.plugin_health.forwarder_drift._derive_names`). Calling it
against THIS repo's own `coordinator/bin/` directory is a live, in-repo scan
— no settings-home install is consulted, so the test stays hermetic and
environment-independent (unlike the spec's fallback option, a snapshot-diff
against the installed settings-home `bin/`, which would need a documented
skip path for "settings-home absent"). A hand-maintained literal CLI-name
list was explicitly rejected — it would drift exactly the way the live
forwarders already did (the incident this test module exists to prevent
recurring).

Spec backlink: docs/plans/2026-07-24-computed-skills-b2-ceremony-start.md, chunk C3
Spec backlink: state/subagent-share/conductor/phase-4/orient-assemble-fix-spec.md (example-doctrine-repo)

Negative-spec: does NOT hand-maintain a CLI allowlist, does NOT allowlist
ceremony/skill names (`workday-start`, `update-docs`, etc.) to make a
currently-broken directive pass — those must be ABSENT from every emitted
`cli`, not present-and-permitted. Does NOT invoke any reader's real I/O
(git, disk, network, subprocess) — every underlying read is monkeypatched
to a deterministic fixture, mirroring `test_cadence_matrix.py`'s pattern.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.install.substrate import _derive_agent_helper_target_map
from coordinator_core.orient_assemble import CADENCES, brief
from coordinator_core.orient_assemble import (
    readers_branch_reconcile as rbr,
    readers_clean_ops as rco,
    readers_handoff_triage as rht,
    readers_health_reaper as rhr,
)
from coordinator_core.orient_assemble.readers_clean_ops import ReaderResult

#: This module's own directory is coordinator_core/orient_assemble/tests/,
#: so parents[3] is the claude-klabauter repo root (mirrors the parents[2] convention
#: each reader module's own _SOURCE_PATH uses, one level deeper here since
#: this file lives in a tests/ subpackage of orient_assemble).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_AGENT_BIN = _REPO_ROOT / "coordinator" / "bin"

#: Ceremony/skill names that must NEVER appear as a `cli` value — these are
#: names of ceremonies/skills, not bin CLIs, and the bug this test guards
#: against is exactly a directive naming one of these as if it were
#: executable. Asserted absent (never allowlisted) per the spec's item 6 —
#: allowlisting would encode the bug as intended behavior.
_CEREMONY_OR_SKILL_NAMES = frozenset(
    {"workday-start", "workweek-start", "workstream-start", "update-docs"}
)


def _real_cli_names() -> frozenset[str]:
    """The oracle: derive the real, installable CLI name set from a live
    scan of THIS repo's own `coordinator/bin/` — the same derivation
    `_install_bin_resolvers` (forwarder generation) and
    `check-forwarder-drift.py` (staleness probe) both consume. Hermetic:
    reads only this repo's own tree, never a settings-home install."""
    return frozenset(_derive_agent_helper_target_map(_AGENT_BIN))


def _fully_stubbed_brief(monkeypatch, cadence: str) -> dict:
    """Drive `brief(cadence)` with every underlying reader I/O stubbed to a
    deterministic fixture producing at least one directive per emission
    site, reusing the same monkeypatch targets as
    `test_cadence_matrix.py`/`test_narration_and_constructor_discipline.py`.
    Hermetic — no real git/disk/network/subprocess read."""
    # readers_clean_ops: sites 5 (scan-addon-health), 6 (generate-repomap via
    # RAG staleness), 7 (agent-worktree-sweep --reap).
    monkeypatch.setattr(rco, "_read_em_environment", lambda: ReaderResult())
    monkeypatch.setattr(rco, "_scan_addon_health_run", lambda mode: (["RED: x"], 1))
    monkeypatch.setattr(rco, "_read_memo_surface", lambda mode: ReaderResult())
    monkeypatch.setattr(rco, "check_rag_state", lambda: ("stale", 1))

    class _FakeWorktree:
        path = "/tmp/fake-worktree"

    class _FakeClassification:
        state = "empty-clean"
        dirty_count = 0

    monkeypatch.setattr(rco, "_wt_repo_root", lambda: "/tmp/fake-repo")
    monkeypatch.setattr(rco, "_wt_active_branch", lambda root: "main")
    monkeypatch.setattr(rco, "_is_agent_worktree", lambda path: True)
    monkeypatch.setattr(rco, "_list_worktrees", lambda root: [_FakeWorktree()])
    monkeypatch.setattr(rco, "classify_worktree", lambda path, ref: _FakeClassification())

    # readers_handoff_triage: sites 1-3 (stale-plans, ready, awaiting-gate).
    monkeypatch.setattr(rht, "_cmd_stale_plans", lambda args: (print("stale: p1"), 0)[1])
    monkeypatch.setattr(rht, "_cmd_ready", lambda args: (print("ready: h1"), 0)[1])
    monkeypatch.setattr(rht, "_cmd_awaiting_gate", lambda args: (print("gated: h2"), 0)[1])
    # readers_handoff_triage: site 4 (orphaned-plans) — stubbed at the
    # reader module's BOUND NAME (direct-import shape, not the
    # _cmd_*/_load_source_module forwarder seam the other three use).
    monkeypatch.setattr(
        rht,
        "list_orphaned",
        lambda repo_root, threshold_days: {
            "authorized_orphan": [
                {"path": "docs/plans/x.md", "execution_authorized_by": "alice"}
            ],
            "chain_gap": [],
            "parked_count": 0,
            "legacy_unjoinable_count": 0,
            "unrecognized_status": [],
            "population_count": 1,
            "owned_count": 0,
        },
    )

    # readers_branch_reconcile: span-assert directive + auto-reconcile
    # judgment points (no directive from this family; stubbed to a no-op
    # so the family stays cadence-insensitive-but-present in the envelope).
    monkeypatch.setattr(rbr, "_read_span_assert", lambda: ReaderResult())
    monkeypatch.setattr(rbr, "_read_auto_reconcile", lambda: ReaderResult())

    # readers_health_reaper: sites 8-13 (claude-klabauter-bin-sentinel, ceremony-hook,
    # reaper-dry-run, day/session marker-freshness). exec-bit-check was
    # retired 2026-07-28 along with check-all-shebanged-exec-bits.py.
    import io

    def _fake_claude_klabauter_bin_sentinel(argv):
        import sys as _sys

        print("claude-klabauter-bin sentinel missing at X", file=_sys.stderr)
        return 1

    def _fake_ceremony_hook(argv):
        print("post-ceremony hook output")
        return 0

    monkeypatch.setattr(rhr, "_cmd_claude_klabauter_bin_sentinel", _fake_claude_klabauter_bin_sentinel)
    monkeypatch.setattr(rhr, "_cmd_ceremony_hook", _fake_ceremony_hook)

    # Stub the subprocess call itself (the ONE accepted subprocess in this
    # reader family — see readers_health_reaper.py's own
    # _REAP_SUBPROCESS_EXCEPTION) rather than replacing `_read_reaper_dry_run`
    # wholesale: the real function's own dict-construction/regex-parsing
    # logic must run so this test actually exercises (and can catch a bug
    # in) the source's own emitted `cli` value, not a hand-authored fixture
    # dict standing in for it.
    class _FakeCompletedProcess:
        stdout = (
            "1 orphaned in_flight handoffs would be released (dry-run)\n"
        )
        stderr = ""

    monkeypatch.setattr(rhr.subprocess, "run", lambda *a, **kw: _FakeCompletedProcess())

    from coordinator_core.ops import check_weekly_staleness

    monkeypatch.setattr(check_weekly_staleness, "_resolve_state_root", lambda: "/tmp/fake-state-root-does-not-exist")

    return brief(cadence)


def test_every_directives_cli_is_a_member_of_the_derived_real_cli_set(monkeypatch):
    """Exact-match membership, never substring — a value like
    `"workday-start-handoff-triage.py stale-plans"` could slip past a
    substring-based allowlist check but must fail an exact membership
    check (spec item 3)."""
    real_names = _real_cli_names()
    assert real_names, "expected a non-empty derived CLI name set from coordinator/bin/"

    for cadence in CADENCES:
        envelope = _fully_stubbed_brief(monkeypatch, cadence)
        for directive in envelope["directives"]:
            assert directive["cli"] in real_names, (
                f"cadence={cadence!r} directive {directive['id']!r} names "
                f"cli={directive['cli']!r}, which is not in the derived real "
                f"CLI set (exact match required, not substring)"
            )


def test_no_directives_cli_contains_a_space(monkeypatch):
    """A space in `cli` means a subcommand was baked into the string instead
    of populated into `args[]` (spec item 4) — a bare allowlist-membership
    check would not catch this class on its own (a space-joined string is
    simply absent from the allowlist and could coincidentally still look
    like a rejection for the wrong reason); assert directly."""
    for cadence in CADENCES:
        envelope = _fully_stubbed_brief(monkeypatch, cadence)
        for directive in envelope["directives"]:
            assert " " not in directive["cli"], (
                f"cadence={cadence!r} directive {directive['id']!r} cli="
                f"{directive['cli']!r} contains a space — the subcommand "
                "belongs in args[], not concatenated into cli"
            )


def test_no_directives_cli_carries_a_dot_py_suffix(monkeypatch):
    """Settings-home forwarders are extension-less by construction (spec
    item 5) — a `.py` suffix in an emitted directive is wrong by
    construction, never a "the file happens to be missing" issue."""
    for cadence in CADENCES:
        envelope = _fully_stubbed_brief(monkeypatch, cadence)
        for directive in envelope["directives"]:
            assert not directive["cli"].endswith(".py"), (
                f"cadence={cadence!r} directive {directive['id']!r} cli="
                f"{directive['cli']!r} carries a .py suffix — forwarders "
                "are extension-less by construction"
            )


def test_no_directives_cli_is_a_ceremony_or_skill_name(monkeypatch):
    """Sites #12/#13 named `"workday-start"` (a ceremony/skill name, not a
    bin CLI) as if it were executable — the self-referential category-error
    bug the handoff reported (spec item 6). Asserted ABSENT rather than
    allowlisted: allowlisting a ceremony name here would encode the bug as
    intended behaviour instead of fixing the category error."""
    for cadence in CADENCES:
        envelope = _fully_stubbed_brief(monkeypatch, cadence)
        for directive in envelope["directives"]:
            assert directive["cli"] not in _CEREMONY_OR_SKILL_NAMES, (
                f"cadence={cadence!r} directive {directive['id']!r} cli="
                f"{directive['cli']!r} is a ceremony/skill name, not a bin "
                "CLI — this is the self-referential category error, not a "
                "legitimate directive target"
            )

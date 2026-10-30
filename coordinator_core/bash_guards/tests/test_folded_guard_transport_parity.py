"""coordinator_core.bash_guards.tests.test_folded_guard_transport_parity --
warm-chain oracle for the four folded bash guards (doctrine-surface bash
write, repo-setup ~/.claude refusal, host subagent Bash ban, host subagent
spawn shapes).

Each fixture is an opt-in payload known to TRIP its guard (DR-147: guard
registration is not guard coverage). The two host-opt-in guards are inert
unless a `coordinator.local.md` declares their policy key, so every case here
writes its own fixture rather than relying on ambient config, and
`_assert_warm_denies` FAILS LOUDLY when the chain does not deny -- a zero-fire
corpus cannot pass by construction (see
`test_assert_helper_rejects_a_non_firing_case`).

Only the engine chain (`dispatch.evaluate_payload_json`) is exercised. Per-body
behaviour lives in the guards' own test files.

Commit-shaped commands are excluded: a commit-shaped Bash call spawns ~28 git
subprocesses and none of these fixtures is commit-shaped.

Case 1 reads `governed-authoring-surfaces.json` from the coordinator-content-repo sibling
checkout (`resolve_content_root()`) and skips when none is resolved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from coordinator_core.bash_guards.dispatch import evaluate_payload_json
from coordinator_core.bash_guards._shape_classifier import Shape, SHAPE_PRECEDENCE
from coordinator_core.testing.content_root import resolve_content_root

pytestmark = [
    pytest.mark.cadence,
]

_CONTENT_ROOT = resolve_content_root() or None

_SKIP_REASON = (
    "opt-in fixture: no coordinator-content-repo sibling checkout resolved by "
    "resolve_content_root() -- the governed-authoring-surfaces manifest this "
    "case reads lives only in that sibling repo."
)

_DENY_TEXT = {
    "doctrine_surface": "writes a governed doctrine surface",
    "repo_setup": "scaffold cannot target ~/.claude",
    "bash_ban": "denies the Bash tool to dispatched agents",
    "spawn_shapes": "spawns one subprocess per iteration or pipe stage",
}


def _fixture_plugin_root() -> Optional[str]:
    return str(Path(_CONTENT_ROOT) / "coordinator") if _CONTENT_ROOT else None


def _run_warm(payload: Dict[str, Any]) -> Any:
    return evaluate_payload_json(json.dumps(payload))


def _assert_warm_denies(*, case: str, family: str, payload: Dict[str, Any]) -> None:
    envelope = _run_warm(payload)
    assert isinstance(envelope, dict), f"{case}: engine did not deny this fixture (got {envelope!r})"
    hso = envelope.get("hookSpecificOutput") or {}
    # All four folded guards are ADVISORY_BY_RULING (b17ec35251; DR-475, 307ea22403): firing is
    # an allow carrying the guard's text as additionalContext.
    assert hso.get("permissionDecision") == "allow", f"{case}: engine envelope is not an advisory ({envelope!r})"
    reason = hso.get("additionalContext") or ""
    assert _DENY_TEXT[family] in reason, f"{case}: deny text lost its {family} prose: {reason!r}"


@pytest.mark.skipif(_CONTENT_ROOT is None, reason=_SKIP_REASON)
def test_case1_doctrine_surface_bash_write() -> None:
    """Fixture: a plain `>>` redirect targeting the governed `CLAUDE.md`
    surface, run with `cwd` at the coordinator-content-repo repo root -- `CLAUDE.md` is a
    live entry of the manifest-driven `governed-authoring-surfaces.json` the
    engine reads (`<plugin_root>/governed-authoring-surfaces.json`). Not
    identity-gated -- fires for every caller, no coordinator.local.md
    opt-in needed."""
    cwd = str(Path(_CONTENT_ROOT)) if _CONTENT_ROOT else None
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": 'echo "x" >> CLAUDE.md'},
        "cwd": cwd,
        # `plugin_root` rides the payload directly (C1) rather than being
        # left to the ambient ladder (env var / installed-plugin-dir /
        # content-root pointer): a payload whose `cwd` points at the
        # coordinator-content-repo checkout but omits `plugin_root` can still fail that
        # ladder on a host with no `CLAUDE_PLUGIN_ROOT` set and no installed
        # `coordinator-claude` plugin dir -- measured directly (2026-08-28)
        # under this exact fixture. `<content_root>/coordinator` is where
        # `governed-authoring-surfaces.json` actually lives, matching
        # `resolve_plugin_root_loud`'s own contract.
        "plugin_root": _fixture_plugin_root(),
    }
    _assert_warm_denies(
        case="doctrine_surface", family="doctrine_surface", payload=payload
    )


def test_case2_repo_setup_claude_home_refusal() -> None:
    win_home = r"C:\Users\example-operator"
    env = {"HOME": win_home, "USERPROFILE": win_home}
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "python3 -m coordinator_core.install.scaffold_structure --manifest-root x"},
        "cwd": win_home + r"\.claude",
        "env": env,
    }
    _assert_warm_denies(
        case="repo_setup", family="repo_setup", payload=payload
    )


def test_case3_host_subagent_bash_ban_parity(tmp_path: Path) -> None:
    """Opt-in fixture: writes its own `coordinator.local.md` declaring
    `subagent_bash_policy: deny` under `tmp_path`, never ambient config.
    The identity axis is swept in `test_identity_shape_parity`."""
    (tmp_path / "coordinator.local.md").write_text(
        "---\nsubagent_bash_policy: deny\n---\n", encoding="utf-8"
    )
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "cwd": str(tmp_path),
        "agent_id": "abcdef1234567890",
        "session_id": "sess12345678",
        "plugin_root": _fixture_plugin_root(),
    }
    _assert_warm_denies(
        case="bash_ban", family="bash_ban", payload=payload
    )


def test_case4_host_subagent_bash_spawn_shapes_parity(tmp_path: Path) -> None:
    """Opt-in fixture with its own policy key (`subagent_bash_spawn_shapes`)
    so the two cohort guards stay independently switchable. A for-loop is
    outside the in-process-answerable family, so it reaches the deny branch."""
    (tmp_path / "coordinator.local.md").write_text(
        "---\nsubagent_bash_spawn_shapes: deny\n---\n", encoding="utf-8"
    )
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": 'for f in *.md; do wc -l "$f"; done'},
        "cwd": str(tmp_path),
        "agent_id": "abcdef1234567890",
        "session_id": "sess12345678",
        "plugin_root": _fixture_plugin_root(),
    }
    _assert_warm_denies(
        case="spawn_shapes", family="spawn_shapes", payload=payload
    )


def test_case5_spawn_shapes_parity_on_the_powershell_surface(tmp_path: Path) -> None:
    """The only non-Bash fixture here. The guard branches on `tool_name`
    (`_spawn_cost_clause` swaps the cost sentence for PowerShell), so a
    Bash-only set would certify a surface it never exercised."""
    (tmp_path / "coordinator.local.md").write_text(
        "---\nsubagent_bash_spawn_shapes: deny\n---\n", encoding="utf-8"
    )
    payload = {
        "tool_name": "PowerShell",
        "tool_input": {"command": "Get-ChildItem *.md | ForEach-Object { wc -l $_ }"},
        "cwd": str(tmp_path),
        "agent_id": "abcdef1234567890",
        "session_id": "sess12345678",
        "plugin_root": _fixture_plugin_root(),
    }
    _assert_warm_denies(
        case="spawn_shapes_powershell",
        family="spawn_shapes",
        payload=payload,
    )


def _write_local_md(tmp_path: Path, policy_key: str) -> None:
    (tmp_path / "coordinator.local.md").write_text(
        f"---\n{policy_key}: deny\n---\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# AXIS 1 -- IDENTITY SHAPE SWEEP. Cohort membership is any non-empty
# `agent_id` string; these shapes (named teammate + short session_id,
# uppercase hex, dashed UUID) once fell outside a resolver-gated cohort test.
# ---------------------------------------------------------------------------

_IDENTITY_SHAPES: "tuple[tuple[str, str, str], ...]" = (
    ("bare_hex", "abcdef1234567890", "sess12345678"),
    ("named_teammate_short_session", "aexecutor-0123456789abcdef", "short12"),
    ("uppercase_hex", "ABCDEF0123456789", "sess12345678"),
    ("dashed_uuid", "12345678-90ab-cdef-1234-567890abcdef", "sess12345678"),
)

_IDENTITY_GUARDS: "tuple[tuple[str, str, str], ...]" = (
    ("bash_ban", "subagent_bash_policy", "ls"),
    (
        "spawn_shapes",
        "subagent_bash_spawn_shapes",
        'for f in *.md; do wc -l "$f"; done',
    ),
)


@pytest.mark.parametrize(
    "family,policy_key,command", _IDENTITY_GUARDS, ids=[g[0] for g in _IDENTITY_GUARDS]
)
@pytest.mark.parametrize(
    "shape_id,agent_id,session_id", _IDENTITY_SHAPES, ids=[s[0] for s in _IDENTITY_SHAPES]
)
def test_identity_shape_parity(
    tmp_path: Path,
    shape_id: str,
    agent_id: str,
    session_id: str,
    family: str,
    policy_key: str,
    command: str,
) -> None:
    _write_local_md(tmp_path, policy_key)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(tmp_path),
        "agent_id": agent_id,
        "session_id": session_id,
        "plugin_root": _fixture_plugin_root(),
    }
    _assert_warm_denies(
        case=f"identity_{shape_id}__{family}", family=family, payload=payload
    )


def test_identity_axis_can_actually_fail(tmp_path: Path) -> None:
    _write_local_md(tmp_path, "subagent_bash_policy")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "cwd": str(tmp_path),
        "agent_id": "",
        "session_id": "sess12345678",
    }
    with pytest.raises(AssertionError):
        _assert_warm_denies(
            case="identity_empty_agent_id",
            family="bash_ban",
            payload=payload,
        )


# ---------------------------------------------------------------------------
# AXIS 2 -- SHAPE VOCABULARY SWEEP, derived from `_shape_classifier.
# SHAPE_PRECEDENCE` itself (the guard's own shape vocabulary) rather than
# from a hand-copied list of shape names -- so a shape added to the
# classifier later fails this module (via the `_SHAPE_COMMANDS[shape]`
# lookup below, a plain `KeyError`) until someone supplies a payload for it.
# ---------------------------------------------------------------------------

#: One (case suffix, tool_name, command, expectation) tuple per
#: `SHAPE_PRECEDENCE` member -- `expectation` is `"deny"` (must deny on both
#: sides) or `"decline"` (the one INTENDED divergence this axis carries; see
#: `_INTENDED_DIVERGENCES`). Every command here was verified directly
#: against `classify_command` to actually classify as its listed shape
#: before being pinned here (per dispatch brief: "verify each with the
#: classifier, do not assume from the name").
_SHAPE_COMMANDS: "Dict[Shape, list]" = {
    Shape.GREP_VIA_BASH: [
        ("unanswerable_pipe", "Bash", "curl -s foo | grep bar", "deny"),
        # `plan_for` fully answers a bare recursive grep -- the guard's own
        # decline predicate returns None on the warm side. See
        # `_INTENDED_DIVERGENCES` below.
        ("answerable_bare", "Bash", "grep -rn foo .", "decline"),
    ],
    Shape.MULTI_PROBE_BANNER: [
        ("banner", "Bash", 'echo "=== status ==="; git status; pwd', "deny"),
    ],
    Shape.HEAD_TAIL_PLUMBING: [
        ("plumbing", "Bash", "cat foo.txt | head -5", "deny"),
    ],
    Shape.FOR_LOOP: [
        ("for_loop", "Bash", 'for f in *.md; do wc -l "$f"; done', "deny"),
    ],
    Shape.PIPELINE_FOREACH_OBJECT: [
        (
            "pipeline_foreach",
            "PowerShell",
            "Get-ChildItem *.py | ForEach-Object { python3 script.py $_.FullName }",
            "deny",
        ),
    ],
    Shape.WHILE_READ_LOOP: [
        ("while_read", "Bash", 'cat f | while read x; do echo "$x"; done', "deny"),
    ],
    Shape.FIND_EXEC_XARGS: [
        ("find_exec", "Bash", 'find . -name "*.log" -exec wc -l {} ;', "deny"),
    ],
    Shape.LABEL_OR_EXIT_ECHO: [
        ("label_echo", "Bash", 'echo "Session info:" && env', "deny"),
    ],
}

#: Named registry of every INTENDED decline this module
#: asserts AS SUCH (dispatch brief: "never a skip and never a silent
#: exclusion"). `test_shape_vocabulary_parity`'s "decline" branch looks up
#: its case in this registry and FAILS if the entry is missing, so an
#: undocumented divergence cannot slip in as a silent decline; the reverse
#: direction is checked by `test_every_intended_divergence_maps_to_a_
#: decline_case`.
_INTENDED_DIVERGENCES: "tuple[dict, ...]" = (
    {
        "case": "grep_via_bash/answerable_bare",
        "diverges": "shape denies in general / this bare grep is declined",
        "why": (
            "guard_host_subagent_bash_spawn_shapes._declines_for_inprocess_answer: "
            "a bare grep that search.answer.plan_for can fully answer costs the "
            "caller ZERO spawns on the warm path (guard_inprocess_search answers "
            "it in-process), so the guard's own decline predicate returns None. "
                        "Named explicitly in the guard's own module docstring, 'KNOWN, "
            "DELIBERATE COLD-VS-WARM DIVERGENCE' paragraph, and legitimised "
            "under DR-125 (denies the shape minus what the engine already "
            "answers for free)."
        ),
    },
)


@pytest.mark.parametrize("shape", list(SHAPE_PRECEDENCE), ids=lambda s: s.value)
def test_shape_vocabulary_parity(shape: Shape, tmp_path: Path) -> None:
    """Sweep `guard-host-subagent-bash-spawn-shapes` over every shape
    `_shape_classifier.SHAPE_PRECEDENCE` knows about -- not just the single
    for-loop case4 used before this axis existed. `_SHAPE_COMMANDS[shape]`
    (a plain dict index, not `.get`) is what makes this sweep's coverage
    track the classifier's own vocabulary rather than a hand-copied
    snapshot of it: a shape appended to `SHAPE_PRECEDENCE` with no matching
    entry here raises `KeyError` and fails this test, rather than the sweep
    silently iterating over one fewer case (see
    `test_shape_vocabulary_axis_can_actually_fail` for the pinned proof of
    that property).
    """
    entries = _SHAPE_COMMANDS[shape]
    for case_suffix, tool_name, command, expectation in entries:
        case = f"shape_{shape.value}__{case_suffix}"
        _write_local_md(tmp_path, "subagent_bash_spawn_shapes")
        payload = {
            "tool_name": tool_name,
            "tool_input": {"command": command},
            "cwd": str(tmp_path),
            "agent_id": "abcdef1234567890",
            "session_id": "sess12345678",
            "plugin_root": _fixture_plugin_root(),
        }
        if expectation == "deny":
            _assert_warm_denies(
                case=case,
                family="spawn_shapes",
                payload=payload,
            )
        elif expectation == "decline":
            registry_key = f"{shape.value}/{case_suffix}"
            registered = [d for d in _INTENDED_DIVERGENCES if d["case"] == registry_key]
            assert registered, (
                f"{case}: marked 'decline' in _SHAPE_COMMANDS but has no matching "
                f"entry in _INTENDED_DIVERGENCES -- an intended divergence must be "
                f"registered by name, never a silent exclusion."
            )
            warm_envelope = _run_warm(payload)
            warm_hso = (warm_envelope or {}).get("hookSpecificOutput") or {}
            assert warm_hso.get("permissionDecision") != "deny", (
                f"{case}: registered divergence's WARM side no longer declines "
                f"(got {warm_envelope!r}) -- update or remove its "
                f"_INTENDED_DIVERGENCES entry, this is a finding, not a pass."
            )
        else:
            raise AssertionError(f"{case}: unknown expectation {expectation!r}")


def test_every_intended_divergence_maps_to_a_decline_case() -> None:
    """Inverse of the registry check inside `test_shape_vocabulary_parity`:
    every entry in `_INTENDED_DIVERGENCES` must correspond to an actual
    `"decline"` entry in `_SHAPE_COMMANDS`, or the registry is documenting a
    divergence this module no longer exercises at all. No DoE sibling
    checkout needed -- pure structural check over the two module-level
    tables.
    """
    decline_keys = {
        f"{shape.value}/{suffix}"
        for shape, entries in _SHAPE_COMMANDS.items()
        for suffix, _tool, _cmd, expectation in entries
        if expectation == "decline"
    }
    for record in _INTENDED_DIVERGENCES:
        assert record["case"] in decline_keys, (
            f"{record['case']}: registered in _INTENDED_DIVERGENCES but no "
            f"matching 'decline' entry exists in _SHAPE_COMMANDS -- a divergence "
            f"documented but not exercised."
        )


def test_shape_vocabulary_axis_can_actually_fail() -> None:
    """Fail-capability pin for AXIS 2: iterating `SHAPE_PRECEDENCE` and
    directly indexing an incomplete `_SHAPE_COMMANDS`-shaped mapping raises
    `KeyError` rather than silently skipping the missing shape -- the
    property that makes this axis unable to silently under-cover a shape
    added to the classifier later (dispatch brief AXIS 2's own stated
    point). No DoE sibling checkout needed -- pure structural check.
    """
    incomplete = dict(_SHAPE_COMMANDS)
    incomplete.pop(Shape.FIND_EXEC_XARGS)
    with pytest.raises(KeyError):
        for shape in SHAPE_PRECEDENCE:
            _ = incomplete[shape]


def test_assert_helper_rejects_a_non_firing_case(tmp_path: Path) -> None:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "cwd": str(tmp_path),
        "agent_id": "abcdef1234567890",
        "session_id": "sess12345678",
    }
    with pytest.raises(AssertionError):
        _assert_warm_denies(
            case="bash_ban_zero_fire", family="bash_ban", payload=payload
        )

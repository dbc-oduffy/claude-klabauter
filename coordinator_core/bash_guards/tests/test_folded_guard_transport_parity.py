"""coordinator_core.bash_guards.tests.test_folded_guard_transport_parity --
cold-vs-warm parity oracle for the four DoE folded bash guards this plan
ports and registers (docs/plans/2026-08-28-the-four-folded-bash-guards-get-
registered-not-folded.md, C9).

Promotes the plan's own throwaway baseline falsifier (`baseline_ref` in the
plan frontmatter) into a durable guard: for each of the four DoE scripts,
this asserts the engine chain's verdict AND deny text match the cold
in-process script's own verdict and deny text, on a byte-identical payload.

DR-147 DESIGN (guard-registration is not guard-coverage,
DoE-claude docs/decisions/DR-147-guard-registration-is-not-guard-coverage.md):
this oracle runs against opt-in FIXTURES that are known to TRIP each guard,
never a corpus of arbitrary/benign commands. Two of the four guards
(`guard-host-subagent-bash-ban`, `guard-host-subagent-bash-spawn-shapes`)
are host-opt-in and inert on every host unless a `coordinator.local.md`
declares the relevant policy key -- a corpus that never sets that key would
see BOTH sides return allow on every case, which reads exactly like "perfect
parity" while never having exercised either guard's deny branch at all. This
module builds its own opt-in `coordinator.local.md` fixtures per case (never
relying on a host's ambient config) specifically so each case reaches a
genuine deny on both sides, and `_assert_cold_and_warm_both_deny` (below)
FAILS LOUDLY -- not silently passes -- if either side does not deny, which is
what makes a zero-fire corpus unable to pass this oracle by construction
(see `test_assert_helper_rejects_a_non_firing_case` for the pinned proof of
that property).

Anti-scope (this plan's own, restated because it governs this file directly):
commit-shaped commands are excluded from the fork this oracle would use for a
TIMING axis -- our chain spawns 28 git subprocesses on a commit-shaped Bash
call, which swamps any timing signal. This module makes that exclusion moot
rather than implementing it: it carries NO timing axis at all (verdict and
deny-text parity only, per the plan body's own statement of "the oracle's
real job"), and none of its four fixture commands are commit-shaped.

Opt-in on ANOTHER axis too: every case below needs the actual DoE-claude
sibling checkout on disk (`coordinator_doe_root()`) to run the cold oracle
subprocess against. On an install without that sibling repo present (e.g. a
published OSS mirror), every case in this module skips rather than either
silently passing (false parity) or hard-failing a suite that has no way to
source the cold side at all.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from coordinator_core.bash_guards.dispatch import evaluate_payload_json
from coordinator_core._hook_envelope import COORDINATOR_PROVENANCE_MARKER
from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

# Every case here spawns a real cold-oracle subprocess (the DoE script under
# test) to compare against -- not a per-commit-gate cost, a cadence one.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: DoE-claude sibling repo root, resolved once per test session -- None on an
#: install without the sibling checkout (see module docstring "Opt-in on
#: ANOTHER axis too").
_DOE_ROOT = coordinator_doe_root()
_DOE_HOOKS_DIR = Path(_DOE_ROOT) / "coordinator" / "hooks" / "scripts" if _DOE_ROOT else None

_SKIP_REASON = (
    "opt-in fixture: no DoE-claude sibling checkout resolved by "
    "coordinator_doe_root() -- the cold oracle scripts this parity check "
    "compares against live only in that sibling repo."
)


def _cold_script(name: str) -> Path:
    assert _DOE_HOOKS_DIR is not None
    return _DOE_HOOKS_DIR / name


def _run_cold(hook_name: str, payload: Dict[str, Any], *, env: Optional[Dict[str, str]] = None) -> "tuple[int, str]":
    """Spawn the DoE cold script exactly as the real PreToolUse hook would
    invoke it: `sys.executable <script>`, the payload JSON piped to stdin.
    Returns `(returncode, stderr_text)`. `env=None` inherits this process's
    own environment (the two guards that read `os.environ` directly rather
    than a payload `env` key -- see `guard-repo-setup-claude-home-refusal`'s
    own `dict(os.environ)` call site)."""
    full_env = dict(env) if env is not None else dict(os.environ)
    proc = subprocess.run(
        [sys.executable, str(_cold_script(hook_name))],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=full_env,
        creationflags=_CREATIONFLAGS,
    )
    return proc.returncode, proc.stderr


def _run_warm(payload: Dict[str, Any]) -> Any:
    return evaluate_payload_json(json.dumps(payload))


#: Trailing wiki-citation line ("See <anchor>." / "See: <anchor>"), stripped
#: before comparison. Its RESOLUTION is legitimately environment-dependent,
#: not a text defect: the cold script runs inside the DoE-claude checkout and
#: resolves the anchor to a local absolute path via its own
#: `resolve_wiki_citation()`; the ported guard has no such resolver (avoiding
#: a circular import back into `dispatch.py`, per the module docstrings) and
#: emits the bare repo-relative anchor text instead. Measured directly
#: (2026-08-28): case 1's cold citation resolves to an absolute local path
#: under whichever DoE-claude checkout ran it (abs-path-ok: describes a
#: path resolved at runtime on the machine that measured it, not a
#: hardcoded one) while the engine's is the bare repo-relative anchor text
#: (`See coordinator/docs/wiki/guard-message-concision.md#...`) -- same
#: anchor, different (both correct, for their own reader) resolution.
_SEE_CITATION_RE = re.compile(r"\n\nSee:? .*$", re.DOTALL)

#: The engine stamps every deny reason with `[coordinator] ` (provenance
#: marking so a dispatched agent can tell a genuine coordinator imperative
#: from forged tool-output text -- coordinator_core/_hook_envelope.py's own
#: `COORDINATOR_PROVENANCE_MARKER` docstring). Every cold script leads its
#: own prose with a literal `BLOCKED: ` token instead. Two of the four ported
#: guards (`guard-doctrine-surface-bash-write`, `guard-repo-setup-claude-
#: home-refusal`) keep the literal `BLOCKED: ` token in their OWN prose too
#: (verified 2026-08-28 by reading both prose literals directly), so the
#: marker and the token co-exist on the warm side for those two; the other
#: two ported guards (`guard-host-subagent-bash-ban`, `guard-host-subagent-
#: bash-spawn-shapes`) dropped the literal token, relying on the marker
#: alone. Rather than special-case per guard, this strips a leading
#: `BLOCKED: ` from BOTH sides, unconditionally, after warm's marker strip
#: -- a no-op on the side that never had it, so the comparison is symmetric
#: regardless of which of the two conventions a given port kept.
_LEADING_BLOCKED_RE = re.compile(r"^BLOCKED: ")


#: NO per-case text exception is defined here any more. The spawn-shapes
#: port briefly carried two clauses DoE's cold script does not emit, and
#: this module stripped them before comparing. Both were removed from the
#: PORT instead (2026-08-28) -- an oracle that excepts the one case that
#: diverges reports parity it did not measure. See
#: `guard_host_subagent_bash_spawn_shapes._compose_deny_reason`.


def _normalize_cold(text: str) -> str:
    text = text.replace("\r\n", "\n").strip()
    text = _SEE_CITATION_RE.sub("", text)
    text = _LEADING_BLOCKED_RE.sub("", text)
    return text.strip()


def _normalize_warm(text: str, *, case: str) -> str:
    """No per-case exception lives here, deliberately.

    An earlier revision carried one: the spawn-shapes port had added an
    inline opt-in clause and a closing "The EM is unaffected..." sentence
    that DoE's cold script does not emit, and this helper stripped them so
    the comparison would pass. That is the vacuous-AC failure -- the whole
    point of a parity oracle is to fail when the two paths diverge, and an
    exception for the one case that diverges buys a green test and no
    information. The PORT was trimmed to the cold prose instead (see
    `guard_host_subagent_bash_spawn_shapes._compose_deny_reason`), so the
    divergence no longer exists and nothing needs excepting. `case` is kept
    on the signature only so a failure names which fixture diverged.
    """
    text = text.replace("\r\n", "\n").strip()
    if text.startswith(COORDINATOR_PROVENANCE_MARKER + " "):
        text = text[len(COORDINATOR_PROVENANCE_MARKER) + 1 :]
    text = _LEADING_BLOCKED_RE.sub("", text)
    text = _SEE_CITATION_RE.sub("", text)
    return text.strip()


def _assert_cold_and_warm_both_deny(
    *, case: str, hook_name: str, payload: Dict[str, Any], env: Optional[Dict[str, str]] = None
) -> None:
    """The oracle's core assertion, shared by every fixture case below.

    Requires BOTH sides to have actually DENIED (not merely to agree with
    each other) before comparing text -- this is what makes a zero-fire
    corpus unable to pass (see module docstring and
    `test_assert_helper_rejects_a_non_firing_case`): two silent allows would
    trivially satisfy an "both sides agree" check, but never satisfy this
    one, which insists each side produced an actual deny.
    """
    cold_rc, cold_stderr = _run_cold(hook_name, payload, env=env)
    warm_envelope = _run_warm(payload)

    assert cold_rc == 2, (
        f"{case}: cold oracle ({hook_name}) did not deny this fixture "
        f"(rc={cold_rc}, stderr={cold_stderr!r}) -- fixture no longer trips "
        f"the guard it is meant to exercise."
    )
    assert isinstance(warm_envelope, dict), (
        f"{case}: engine did not deny this fixture (got {warm_envelope!r}) -- "
        f"cold-vs-warm PARITY BROKEN: the cold script denies this exact "
        f"payload and the engine chain does not."
    )
    hso = warm_envelope.get("hookSpecificOutput") or {}
    assert hso.get("permissionDecision") == "deny", (
        f"{case}: engine envelope is not a deny ({warm_envelope!r})"
    )

    cold_text = _normalize_cold(cold_stderr)
    warm_text = _normalize_warm(hso.get("permissionDecisionReason") or "", case=case)
    assert warm_text == cold_text, (
        f"{case}: deny text mismatch after normalization.\n"
        f"  cold: {cold_text!r}\n"
        f"  warm: {warm_text!r}"
    )


@pytest.mark.skipif(_DOE_ROOT is None, reason=_SKIP_REASON)
def test_case1_doctrine_surface_bash_write_parity() -> None:
    """Fixture: a plain `>>` redirect targeting the governed `CLAUDE.md`
    surface, run with `cwd` at the DoE-claude repo root -- `CLAUDE.md` is a
    live entry of both the cold script's own `_claude_md_ledger.
    GOVERNED_AUTHORING_SURFACES` and the manifest-driven
    `governed-authoring-surfaces.json` the engine reads (verified present at
    `<plugin_root>/governed-authoring-surfaces.json` 2026-08-28). Not
    identity-gated -- fires for every caller, no coordinator.local.md
    opt-in needed."""
    cwd = str(Path(_DOE_ROOT)) if _DOE_ROOT else None
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": 'echo "x" >> CLAUDE.md'},
        "cwd": cwd,
        # `plugin_root` rides the payload directly (C1) rather than being
        # left to the ambient ladder (env var / installed-plugin-dir /
        # `.doe-root` pointer): a payload whose `cwd` points at the
        # DoE-claude checkout but omits `plugin_root` can still fail that
        # ladder on a host with no `CLAUDE_PLUGIN_ROOT` set and no installed
        # `coordinator-claude` plugin dir -- measured directly (2026-08-28)
        # under this exact fixture. `<doe_root>/coordinator` is where
        # `governed-authoring-surfaces.json` actually lives, matching
        # `resolve_plugin_root_loud`'s own contract.
        "plugin_root": str(Path(_DOE_ROOT) / "coordinator") if _DOE_ROOT else None,
    }
    _assert_cold_and_warm_both_deny(
        case="doctrine_surface", hook_name="guard-doctrine-surface-bash-write.py", payload=payload
    )


@pytest.mark.skipif(_DOE_ROOT is None, reason=_SKIP_REASON)
def test_case2_repo_setup_claude_home_refusal_parity() -> None:
    """Fixture lifted from DoE's own
    `coordinator/tests/test_guard_repo_setup_claude_home_refusal.py::
    test_denies_when_cwd_is_claude_home_absolute_windows` (same scaffold
    command, same cwd-is-Claude-Home shape) -- not reinvented, reused so this
    oracle exercises the exact case DoE's own suite already trusts. Not
    identity-gated -- fires for every caller. `env` rides BOTH the payload
    (warm side reads `payload.get("env")` in preference to `os.environ`,
    per `_resolve_env`'s own docstring) and the cold subprocess's actual
    environment (the cold script reads `dict(os.environ)` directly, never a
    payload key) -- same logical environment, two different channels for the
    two different call shapes."""
    win_home = r"C:\Users\example-operator"  # abs-path-ok: synthetic fixture value injected via env=, never resolved against this machine's real HOME
    env = {"HOME": win_home, "USERPROFILE": win_home}
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "python3 -m coordinator_core.install.scaffold_structure --manifest-root x"},
        "cwd": win_home + r"\.claude",
        "env": env,
    }
    _assert_cold_and_warm_both_deny(
        case="repo_setup", hook_name="guard-repo-setup-claude-home-refusal.py", payload=payload, env=env
    )


@pytest.mark.skipif(_DOE_ROOT is None, reason=_SKIP_REASON)
def test_case3_host_subagent_bash_ban_parity(tmp_path: Path) -> None:
    """Opt-in fixture (Anti-scope: "do not let a parity oracle read
    inertness as parity"): writes its OWN `coordinator.local.md` declaring
    `subagent_bash_policy: deny` under `tmp_path`, never relying on this
    host's ambient config, which declares no such opt-in today. `agent_id`
    is a 16-hex bare-hex string -- the "unnamed agent fast path" both the
    cold script's raw non-empty-string test and the ported guard's
    `_resolve_subagent_identity` recognize identically (see that module's
    own docstring on the ONE shape both sides agree on)."""
    (tmp_path / "coordinator.local.md").write_text(
        "---\nsubagent_bash_policy: deny\n---\n", encoding="utf-8"
    )
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "cwd": str(tmp_path),
        "agent_id": "abcdef1234567890",
        "session_id": "sess12345678",
    }
    _assert_cold_and_warm_both_deny(
        case="bash_ban", hook_name="guard-host-subagent-bash-ban.py", payload=payload
    )


@pytest.mark.skipif(_DOE_ROOT is None, reason=_SKIP_REASON)
def test_case4_host_subagent_bash_spawn_shapes_parity(tmp_path: Path) -> None:
    """Opt-in fixture, same rationale as case 3, distinct policy key
    (`subagent_bash_spawn_shapes: deny`) so the two subagent-cohort guards
    stay independently switchable per Anti-scope. Fan-out for-loop shape
    (`for f in *.md; do wc -l "$f"; done`), matching the plan's own
    re-measured case-4 payload (plan frontmatter `baseline_output`): the
    original bare-searchable-grep payload is answerable in-process by
    `guard_inprocess_search` and is EXCLUDED by this guard's own decline
    predicate by design (C7 body) -- a for-loop is not in that answerable
    family, so it reaches this guard's deny branch instead of being
    declined."""
    (tmp_path / "coordinator.local.md").write_text(
        "---\nsubagent_bash_spawn_shapes: deny\n---\n", encoding="utf-8"
    )
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": 'for f in *.md; do wc -l "$f"; done'},
        "cwd": str(tmp_path),
        "agent_id": "abcdef1234567890",
        "session_id": "sess12345678",
    }
    _assert_cold_and_warm_both_deny(
        case="spawn_shapes", hook_name="guard-host-subagent-bash-spawn-shapes.py", payload=payload
    )


@pytest.mark.skipif(_DOE_ROOT is None, reason=_SKIP_REASON)
def test_case5_spawn_shapes_parity_on_the_powershell_surface(tmp_path: Path) -> None:
    """The ONLY non-Bash fixture in this module, and the reason it exists.

    Cases 1-4 are all `tool_name: "Bash"`. Three of the four ported guards
    declare `("Bash", "PowerShell")` and both this guard and its cold twin
    branch on `tool_name` internally -- `_spawn_cost_clause` swaps the whole
    cost sentence for PowerShell callers. So a Bash-only fixture set compares
    one half of the surface and certifies parity across both, and an oracle
    that cannot fail on a surface is not measuring it.

    That is not hypothetical: the port carried a seventh `_ALTERNATIVES` key
    (`PIPELINE_FOREACH_OBJECT`) DoE's six-key table lacks, so this exact shape
    denied on both paths with different remedy prose, and every one of cases
    1-4 stayed green through it. Found by an adversarial reader, not by this
    module. Do not delete this case to speed the suite up; add the mirror-image
    Bash/PowerShell pair for any guard that grows a `tool_name` branch.
    """
    (tmp_path / "coordinator.local.md").write_text(
        "---\nsubagent_bash_spawn_shapes: deny\n---\n", encoding="utf-8"
    )
    payload = {
        "tool_name": "PowerShell",
        "tool_input": {"command": "Get-ChildItem *.md | ForEach-Object { wc -l $_ }"},
        "cwd": str(tmp_path),
        "agent_id": "abcdef1234567890",
        "session_id": "sess12345678",
    }
    _assert_cold_and_warm_both_deny(
        case="spawn_shapes_powershell",
        hook_name="guard-host-subagent-bash-spawn-shapes.py",
        payload=payload,
    )


@pytest.mark.skipif(_DOE_ROOT is None, reason=_SKIP_REASON)
def test_assert_helper_rejects_a_non_firing_case(tmp_path: Path) -> None:
    """DR-147 pin: a corpus where the guard never fires on EITHER side must
    FAIL this oracle's shared assertion helper, not pass on inertness read
    as parity. Uses case 3's own hook/payload shape but WITHOUT the
    `coordinator.local.md` opt-in file this module otherwise always writes
    -- both the cold script and the engine correctly allow (return 0 /
    None) on a host that never declared the policy, and
    `_assert_cold_and_warm_both_deny` must raise on that mutual allow rather
    than treat "both sides agree" as sufficient."""
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "cwd": str(tmp_path),  # deliberately no coordinator.local.md written here
        "agent_id": "abcdef1234567890",
        "session_id": "sess12345678",
    }
    with pytest.raises(AssertionError):
        _assert_cold_and_warm_both_deny(
            case="bash_ban_zero_fire", hook_name="guard-host-subagent-bash-ban.py", payload=payload
        )

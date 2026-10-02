"""coordinator_core.bash_guards.tests.test_folded_guard_transport_parity --
cold-vs-warm parity oracle for the four DoE folded bash guards this plan
ports and registers (docs/plans/2026-08-28-the-four-folded-bash-guards-get-
registered-not-folded.md, C9).

Promotes the plan's own throwaway baseline falsifier (`baseline_ref` in the
plan frontmatter) into a durable guard: for each of the four DoE scripts,
this asserts the engine chain's verdict AND deny text match the cold
in-process script's own verdict and deny text, on a byte-identical payload.

DR-147 DESIGN (guard-registration is not guard-coverage,
Coordinator-content-repo docs/decisions/DR-147-guard-registration-is-not-guard-coverage.md):
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

Opt-in on ANOTHER axis too: every case below needs the actual coordinator-content-repo
sibling checkout on disk (`resolve_content_root()`) to run the cold oracle
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
from coordinator_core.bash_guards._shape_classifier import Shape, SHAPE_PRECEDENCE
from coordinator_core._hook_envelope import COORDINATOR_PROVENANCE_MARKER
from coordinator_core.testing.content_root import resolve_content_root

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_CONTENT_ROOT = resolve_content_root() or None
_CONTENT_HOOKS_DIR = Path(_CONTENT_ROOT) / "coordinator" / "hooks" / "scripts" if _CONTENT_ROOT else None

_SKIP_REASON = (
    "opt-in fixture: no coordinator-content-repo sibling checkout resolved by "
    "resolve_content_root() -- the cold oracle scripts this parity check "
    "compares against live only in that sibling repo."
)


def _cold_script(name: str) -> Path:
    assert _CONTENT_HOOKS_DIR is not None
    return _CONTENT_HOOKS_DIR / name


def _fixture_plugin_root() -> Optional[str]:
    return str(Path(_CONTENT_ROOT) / "coordinator") if _CONTENT_ROOT else None


def _run_cold(hook_name: str, payload: Dict[str, Any], *, env: Optional[Dict[str, str]] = None) -> "tuple[int, str]":
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


#: Trailing wiki-citation line ("See <anchor>." / "See: <anchor>"), SPLIT OFF
#: the prose before comparison and then compared on its own axis -- no longer
#: discarded (REVERSED 2026-08-29; see `_split_citation` and
#: `_assert_cold_and_warm_both_deny`).
#:
#: The prior revision stripped this line from BOTH sides unconditionally, on
#: the measurement that case 1's cold citation resolved to a local absolute
#: path while warm emitted the bare repo-relative anchor -- recorded then as
#: "same anchor, different (both correct, for their own reader) resolution".
#: That reading was wrong in a way this oracle is specifically supposed to
#: catch. Warm's anchor resolved for NO reader: it named
#: `coordinator/docs/wiki/...` relative to a repo that has no `coordinator/`
#: directory, so a reader in this checkout got a path that does not exist,
#: which is exactly the 404 DoE's own `resolve_wiki_citation()` exists to
#: prevent. The engine now resolves it (`dispatch.
#: resolve_doctrine_surface_wiki_citation`, threaded into the guard's
#: `resolve_wiki_citation` parameter), and both sides emit the same absolute
#: path -- verified 2026-08-29.
#:
#: Stripping the line is what let that divergence live: an oracle that
#: discards the one line a fix changes cannot pin the fix, and cannot fail on
#: a regression in either direction (anchor literal drift, or the resolver
#: being unwired again -- it WAS unwired, `resolve_doctrine_surface_wiki_
#: citation` sat with no caller at all while its own docstring named one).
#: Same failure mode as the Bash-only fixture set that hid the seventh
#: `_ALTERNATIVES` key: a case that cannot fail is not a measurement.
_SEE_CITATION_RE = re.compile(r"\n\nSee:? (?P<anchor>.*)$", re.DOTALL)

_CITATION_PERIOD_RE = re.compile(r"\.\s*$")

#: Warm-only footer (`guard_unlock_sentinel._augment_deny_with_guard_name`,
#: commit 0a1c92c0bb "guard denials name the guard"): "\n\nGuard: `<name>`."
#: appended AFTER the citation line. Cold has no such footer, and it is not
#: part of the citation -- it is a second, independent axis (guard identity,
#: not wiki anchor). Stripped off before citation-splitting so it cannot be
#: swallowed into the anchor capture (`_SEE_CITATION_RE` is DOTALL-to-end-of-
#: string and would otherwise eat it); never stripped from the cold side,
#: which never carries it.
_GUARD_FOOTER_RE = re.compile(r"\n\nGuard: `[^`]+`\.\s*$")

#: NO `_LEADING_BLOCKED_RE` LIVES HERE ANY MORE (REMOVED 2026-08-29). It used
#: to strip a leading `BLOCKED: ` token from BOTH sides unconditionally,
#: which hid a real divergence rather than measuring it: two of the four
#: ported guards (`guard-host-subagent-bash-ban`, `guard-host-subagent-bash-
#: spawn-shapes`) had dropped the literal `BLOCKED: ` token cold's own prose
#: leads with, relying on the `[coordinator] ` provenance marker alone --
#: and an unconditional strip that discards the one token that diverged buys
#: a green test and no information, the same shape as the per-case text
#: exception already removed from this file (see the removed-exception note
#: below). The token is restored in both guards' own deny prose instead
#: (`guard_host_subagent_bash_ban._compose_deny_reason`,
#: `guard_host_subagent_bash_spawn_shapes._compose_deny_reason`), so nothing
#: needs stripping: the token is now compared like the rest of the prose.


def _split_citation(text: str) -> "tuple[str, Optional[str]]":
    """Split `text` into (prose, citation-anchor-or-None).

    The anchor is returned rather than discarded so the caller can compare it
    on its own axis (see `_SEE_CITATION_RE`'s own comment for why discarding
    it was the defect). Only the trailing sentence period is normalized off;
    the anchor text itself is returned verbatim.
    """
    match = _SEE_CITATION_RE.search(text)
    if not match:
        return text, None
    anchor = _CITATION_PERIOD_RE.sub("", match.group("anchor").strip())
    return text[: match.start()], anchor


def _normalize_cold(text: str) -> "tuple[str, Optional[str]]":
    text = text.replace("\r\n", "\n").strip()
    text, anchor = _split_citation(text)
    return text.strip(), anchor


def _normalize_warm(text: str, *, case: str) -> "tuple[str, Optional[str]]":
    text = text.replace("\r\n", "\n").strip()
    if text.startswith(COORDINATOR_PROVENANCE_MARKER + " "):
        text = text[len(COORDINATOR_PROVENANCE_MARKER) + 1 :]
    text = _GUARD_FOOTER_RE.sub("", text).strip()
    text, anchor = _split_citation(text)
    return text.strip(), anchor


#: Cold scripts whose citation resolves into the coordinator-content-repo wiki family; the
#: engine twins omit that `See:` line because message-register rule B7 bans the
#: family unconditionally. Omission is the sanctioned divergence; a warm
#: citation that IS present still has to equal cold's anchor.
_ENGINE_OMITS_DOE_WIKI_CITATION = frozenset(
    {"guard-host-subagent-bash-ban.py", "guard-host-subagent-bash-spawn-shapes.py"}
)


def _assert_cold_and_warm_both_deny(
    *, case: str, hook_name: str, payload: Dict[str, Any], env: Optional[Dict[str, str]] = None
) -> None:
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

    cold_text, cold_anchor = _normalize_cold(cold_stderr)
    warm_text, warm_anchor = _normalize_warm(hso.get("permissionDecisionReason") or "", case=case)
    assert warm_text == cold_text, (
        f"{case}: deny text mismatch after normalization.\n"
        f"  cold: {cold_text!r}\n"
        f"  warm: {warm_text!r}"
    )

    # Citation axis, compared rather than discarded (see `_SEE_CITATION_RE`).
    if hook_name in _ENGINE_OMITS_DOE_WIKI_CITATION and warm_anchor is None:
        assert cold_anchor is not None, (
            f"{case}: cold no longer cites a wiki page, so "
            f"_ENGINE_OMITS_DOE_WIKI_CITATION has gone stale for {hook_name}."
        )
        return
    assert (cold_anchor is None) == (warm_anchor is None), (
        f"{case}: one path emits a wiki citation and the other does not.\n"
        f"  cold: {cold_anchor!r}\n"
        f"  warm: {warm_anchor!r}"
    )
    assert warm_anchor == cold_anchor, (
        f"{case}: wiki-citation anchor mismatch -- the two paths point their "
        f"readers at different targets.\n"
        f"  cold: {cold_anchor!r}\n"
        f"  warm: {warm_anchor!r}"
    )


@pytest.mark.skipif(_CONTENT_ROOT is None, reason=_SKIP_REASON)
def test_case1_doctrine_surface_bash_write_parity() -> None:
    """Fixture: a plain `>>` redirect targeting the governed `CLAUDE.md`
    surface, run with `cwd` at the coordinator-content-repo repo root -- `CLAUDE.md` is a
    live entry of both the cold script's own `_claude_md_ledger.
    GOVERNED_AUTHORING_SURFACES` and the manifest-driven
    `governed-authoring-surfaces.json` the engine reads (verified present at
    `<plugin_root>/governed-authoring-surfaces.json` 2026-08-28). Not
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
    _assert_cold_and_warm_both_deny(
        case="doctrine_surface", hook_name="guard-doctrine-surface-bash-write.py", payload=payload
    )


@pytest.mark.skipif(_CONTENT_ROOT is None, reason=_SKIP_REASON)
def test_case2_repo_setup_claude_home_refusal_parity() -> None:
    win_home = r"C:\Users\example-operator"
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


@pytest.mark.skipif(_CONTENT_ROOT is None, reason=_SKIP_REASON)
def test_case3_host_subagent_bash_ban_parity(tmp_path: Path) -> None:
    """Opt-in fixture (Anti-scope: "do not let a parity oracle read
    inertness as parity"): writes its OWN `coordinator.local.md` declaring
    `subagent_bash_policy: deny` under `tmp_path`, never relying on this
    host's ambient config, which declares no such opt-in today. `agent_id`
    is a 16-hex bare-hex string.

    THE IDENTITY AXIS IS SWEPT SEPARATELY and this case no longer carries it
    (`test_identity_shape_parity` below). This docstring used to argue that
    bare hex was the shape "both sides agree on" because the port gated its
    cohort through `_resolve_subagent_identity` -- which was true, and was
    exactly why this oracle could not see that the port dropped a
    legitimately dispatched named teammate whose `session_id` was merely
    short. One fixture pinned to the one agreeing shape is not an identity
    measurement. The resolver is gone from both ported guards (2026-08-29)
    and the axis is now swept over the shapes that diverged."""
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
    _assert_cold_and_warm_both_deny(
        case="bash_ban", hook_name="guard-host-subagent-bash-ban.py", payload=payload
    )


@pytest.mark.skipif(_CONTENT_ROOT is None, reason=_SKIP_REASON)
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
        "plugin_root": _fixture_plugin_root(),
    }
    _assert_cold_and_warm_both_deny(
        case="spawn_shapes", hook_name="guard-host-subagent-bash-spawn-shapes.py", payload=payload
    )


@pytest.mark.skipif(_CONTENT_ROOT is None, reason=_SKIP_REASON)
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
        "plugin_root": _fixture_plugin_root(),
    }
    _assert_cold_and_warm_both_deny(
        case="spawn_shapes_powershell",
        hook_name="guard-host-subagent-bash-spawn-shapes.py",
        payload=payload,
    )


def _write_local_md(tmp_path: Path, policy_key: str) -> None:
    (tmp_path / "coordinator.local.md").write_text(
        f"---\n{policy_key}: deny\n---\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# AXIS 1 -- IDENTITY SHAPE SWEEP (state/audits/2026-08-29-unverified-parity-
# findings-measured.md FINDING A).
#
# Every fixture above this point hardcodes `agent_id="abcdef1234567890"` --
# the one shape both subagent guards agreed on while they gated cohort
# membership through `_resolve_subagent_identity`. That resolver is now
# GONE from both ported guards (2026-08-29, see each guard's own "IDENTITY
# RESOLUTION" docstring section) -- both now ask cold's own question
# directly, `isinstance(agent_id, str) and agent_id.strip()`, with no
# canonical-id resolution step to disagree with cold about. This sweep
# exercises the shapes that measured cold-DENY/warm-allow BEFORE that
# reversal (named teammate + short session_id, uppercase hex, dashed UUID)
# to prove the reversal actually closed the gap, rather than trusting the
# source diff alone.
# ---------------------------------------------------------------------------

_IDENTITY_SHAPES: "tuple[tuple[str, str, str], ...]" = (
    ("bare_hex", "abcdef1234567890", "sess12345678"),
    ("named_teammate_short_session", "aexecutor-0123456789abcdef", "short12"),
    ("uppercase_hex", "ABCDEF0123456789", "sess12345678"),
    ("dashed_uuid", "12345678-90ab-cdef-1234-567890abcdef", "sess12345678"),
)

_IDENTITY_GUARDS: "tuple[tuple[str, str, str], ...]" = (
    ("guard-host-subagent-bash-ban.py", "subagent_bash_policy", "ls"),
    (
        "guard-host-subagent-bash-spawn-shapes.py",
        "subagent_bash_spawn_shapes",
        'for f in *.md; do wc -l "$f"; done',
    ),
)


@pytest.mark.skipif(_CONTENT_ROOT is None, reason=_SKIP_REASON)
@pytest.mark.parametrize(
    "hook_name,policy_key,command", _IDENTITY_GUARDS, ids=[g[0] for g in _IDENTITY_GUARDS]
)
@pytest.mark.parametrize(
    "shape_id,agent_id,session_id", _IDENTITY_SHAPES, ids=[s[0] for s in _IDENTITY_SHAPES]
)
def test_identity_shape_parity(
    tmp_path: Path,
    shape_id: str,
    agent_id: str,
    session_id: str,
    hook_name: str,
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
    _assert_cold_and_warm_both_deny(
        case=f"identity_{shape_id}__{hook_name}", hook_name=hook_name, payload=payload
    )


@pytest.mark.skipif(_CONTENT_ROOT is None, reason=_SKIP_REASON)
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
        _assert_cold_and_warm_both_deny(
            case="identity_empty_agent_id",
            hook_name="guard-host-subagent-bash-ban.py",
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

#: Named registry of every INTENDED cold-vs-warm divergence this module
#: asserts AS SUCH (dispatch brief: "never a skip and never a silent
#: exclusion"). `test_shape_vocabulary_parity`'s "decline" branch looks up
#: its case in this registry and FAILS if the entry is missing, so an
#: undocumented divergence cannot slip in as a silent decline; the reverse
#: direction is checked by `test_every_intended_divergence_maps_to_a_
#: decline_case`.
_INTENDED_DIVERGENCES: "tuple[dict, ...]" = (
    {
        "case": "grep_via_bash/answerable_bare",
        "diverges": "cold DENY (rc=2) / warm ALLOW (decline)",
        "why": (
            "guard_host_subagent_bash_spawn_shapes._declines_for_inprocess_answer: "
            "a bare grep that search.answer.plan_for can fully answer costs the "
            "caller ZERO spawns on the warm path (guard_inprocess_search answers "
            "it in-process), so the guard's own decline predicate returns None. "
            "Cold has no in-process answer seam and always denies the shape. "
            "Named explicitly in the guard's own module docstring, 'KNOWN, "
            "DELIBERATE COLD-VS-WARM DIVERGENCE' paragraph, and legitimised "
            "under DR-125 (denies exactly what cold denies, minus the shape the "
            "engine already answers for free)."
        ),
    },
)


@pytest.mark.skipif(_CONTENT_ROOT is None, reason=_SKIP_REASON)
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
            _assert_cold_and_warm_both_deny(
                case=case,
                hook_name="guard-host-subagent-bash-spawn-shapes.py",
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
            cold_rc, cold_stderr = _run_cold(
                "guard-host-subagent-bash-spawn-shapes.py", payload
            )
            warm_envelope = _run_warm(payload)
            assert cold_rc == 2, (
                f"{case}: registered divergence's COLD side no longer denies "
                f"(rc={cold_rc}, stderr={cold_stderr!r}) -- update or remove its "
                f"_INTENDED_DIVERGENCES entry, this is a finding, not a pass."
            )
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


@pytest.mark.skipif(_CONTENT_ROOT is None, reason=_SKIP_REASON)
def test_assert_helper_rejects_a_non_firing_case(tmp_path: Path) -> None:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "cwd": str(tmp_path),
        "agent_id": "abcdef1234567890",
        "session_id": "sess12345678",
    }
    with pytest.raises(AssertionError):
        _assert_cold_and_warm_both_deny(
            case="bash_ban_zero_fire", hook_name="guard-host-subagent-bash-ban.py", payload=payload
        )


def test_citation_axis_can_actually_fail() -> None:
    """Red-capability pin for the citation axis added 2026-08-29.

    The axis exists because the previous revision DISCARDED the citation line
    and therefore could not fail on it. Adding a comparison and trusting it
    would repeat that mistake in the other direction -- a comparison whose
    inputs are always equal is as blind as a strip. So this proves the two
    properties the axis rests on, directly, without needing the DoE sibling
    checkout (hence no skipif): the anchor SURVIVES normalization distinct,
    and unequal anchors are actually unequal after it.

    Also pins the one thing that IS normalized: cold's trailing sentence
    period (`_message_envelope.render`'s `f"See {...}."`) must not by itself
    read as a mismatch against a port that omits it.
    """
    resolved = "See C:/coordinator-content-repo/coordinator/docs/wiki/guard-message-concision.md#x."
    unresolved = "See coordinator/docs/wiki/guard-message-concision.md#x"

    cold_prose, cold_anchor = _split_citation("BLOCKED: prose.\n\n" + resolved)
    warm_prose, warm_anchor = _split_citation("BLOCKED: prose.\n\n" + unresolved)

    assert cold_anchor is not None and warm_anchor is not None
    assert cold_prose == warm_prose, "prose must split identically off both"
    assert cold_anchor != warm_anchor, (
        "a resolved absolute anchor and an unresolved repo-relative one must "
        "compare UNEQUAL -- if these normalize to the same string, the axis "
        "cannot catch the exact regression it was added for"
    )

    _, with_period = _split_citation("p\n\nSee coordinator/docs/wiki/a.md.")
    _, without_period = _split_citation("p\n\nSee coordinator/docs/wiki/a.md")
    assert with_period == without_period == "coordinator/docs/wiki/a.md"

    _, none_anchor = _split_citation("BLOCKED: prose with no citation at all.")
    assert none_anchor is None

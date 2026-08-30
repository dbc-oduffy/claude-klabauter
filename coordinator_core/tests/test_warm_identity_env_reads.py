"""Warm-identity env-read ratchet — the residual half of
``state/bug-backlog/2026-08-18-a-warm-server-stamps-every-op-it-serves-eeb801fc6bee.yaml``.

THE DEFECT CLASS. A resident warm server resolves session identity from its own
process environment, which names whoever SPAWNED it rather than the session whose
request it is currently serving. The transport cure exists and works
(``warm.client`` puts the caller's cold-resolved id on the request,
``warm.server._serve_line`` pops it, ``warm.entry_seam.per_request_state`` binds it
through ``session.core.session_identity_override``) — but that binding is a
``ContextVar``, so ONLY a resolver that reads it can see it. A raw
``os.environ`` read steps straight past and gets the spawner.

The cost is never merely a wrong stamp. Where identity feeds a gate it is an input
to an anti-forgery decision, so a session's own correctly-trailered commits read as
foreign: ``review_trail.write`` refused every slice of a fully-reviewed partitioned
close and the close shipped with no trail at all
(cross-repo/inbox/2026-08-19-doe-claude-em-review-trail-write-refuses-own-commits.md).

WHY NO BEHAVIOURAL TEST CATCHES IT, and why this file is an AST test instead.
``cc_invoke`` spawns the COLD subprocess with the caller's own ``os.environ``, so a
raw env read is CORRECT on that path. Every in-process reproduction, unit test and
isolated-settings-home probe therefore passes. The defect is reachable only through
a live warm server. A source-shape ratchet is the artifact that survives that — it
also costs no subprocess, so it stays on the fast tier rather than tripping the
per-item spawn budget.

SCOPE — DELIBERATELY NOT REPO-WIDE, and that is the honest shape.
``coordinator_core`` names these env vars in dozens of places. Most are CLI
entrypoints, hook processes and guard processes that never execute inside a
warm-served dispatch, where a raw env read is correct. A blanket repo-wide assert
would need every one of those triaged; asserting over an untriaged population and
allowlisting whatever happened to fail would encode "currently passing" as
"reviewed", which is the failure mode the backlog entry explicitly warned against.

So this ratchet governs one NAMED cohort: the residual sites that entry enumerated
on 2026-08-19, each carrying its own reachability verdict below. The rest of the
repo is untriaged, not vouched for. Widening this cohort means triaging the new
members first — the verdict is the work, not the allowlist line.

REACHABILITY RULE used for every verdict here: a site is warm-reachable when it is
reachable from a ``@register_op`` handler's call graph, because
``warm.client.try_warm_dispatch`` takes any op message and applies no method
allowlist. Import-presence is NOT the test and gets it wrong in both directions —
``baton_assemble`` is absent from the ops eager-import sweep yet is warm-reachable
via a deliberate call-time import, while ``block_subagent_plan_body_write`` is
imported into the op process yet only ever executes in a hook process.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

#: The identity vars whose direct reading is the defect's shape.
SESSION_ENV_NAMES = frozenset(
    {"COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"}
)

#: The cohort this ratchet governs: every site the 2026-08-18 backlog entry listed as
#: residual, plus the module its one-module predecessor pin already covered. Value is
#: the triage verdict — `False` means "must not read these vars directly".
#:
#: Triaged 2026-08-19. Two of the seven residuals were warm-reachable and are fixed;
#: five are cold-only and keep their raw reads deliberately. Note that BOTH of the
#: backlog entry's own "likely cold-only" guesses were wrong on inspection — one way
#: each — which is why each verdict below names the path that decided it rather than
#: the intuition that suggested it.
COHORT: dict[str, tuple[bool, str]] = {
    "coordinator_core/ops/tracker/push_suggestion.py": (
        False,
        "Warm-reachable: the registered op `tracker.push_suggestion`. Reached from "
        "its `@register_op` handler via `_deliver_envelope` -> `_commit_envelope` -> "
        "`_delivery_commit_message`, which STAMPS the resolved id as a `Session-Id:` "
        "trailer on a commit it lands in a repo THIS session does not own. A "
        "spawner's id there is the plan's own defect shape, one repo further out: "
        "the receiving operators have no other attribution key and cannot tell a "
        "wrong trailer from a genuine one. Triaged and adopted 2026-08-30 (C2/AC4, "
        "docs/plans/2026-08-30-the-c-door-sends-the-callers-session-identity.md). "
        "The read was also single-tier (`CLAUDE_SESSION_ID` alone), so a session "
        "carrying only `COORDINATOR_SESSION_ID` or `CLAUDE_CODE_SESSION_ID` got no "
        "trailer at all -- the ladder-disagreement defect `SESSION_ENV_PRECEDENCE`'s "
        "own docstring records, in a third copy.",
    ),
    "coordinator/bin/wsc-session-disposition.py": (
        False,
        "Warm-reachable, and the first cohort member OUTSIDE coordinator_core — which "
        "is exactly why it escaped: `workstream_complete.compute_session_shape_gate` "
        "loads this bin script BY PATH and calls `resolve_session_id` in-process, so a "
        "registered-op call graph reaches it while no scan of the package could see "
        "it. Its read was also its own fourth copy of the ladder "
        "(em_sid/CLAUDE_SESSION_ID/CLAUDE_CODE_SESSION_ID, missing "
        "COORDINATOR_SESSION_ID) -- the disagreement defect SESSION_ENV_PRECEDENCE's "
        "docstring records. Triaged and adopted 2026-08-30 from a LIVE reproduction, "
        "not from inspection: session 56043240-f71b-447a-bf56-4ee49f92ab33 ran "
        "`workstream-complete-assemble.exe brief` through the warm door and got a "
        "ceremony keyed to live peer 1189eead-f3eb-4c54-a790-236258043b0d, whose six "
        "deliverables and archived baton `apply` would have credited to this session's "
        "close. Cold `.cmd`, same tree, same minute, resolved correctly. "
        "state/bug-backlog/2026-08-30-close-ceremony-clis-resolve-a-live-peer-"
        "b558b27c74e7.yaml. `em_sid` survives as a cold-only legacy tier: this ratchet "
        "does not govern that name, and the warm branch does not read it.",
    ),
    "coordinator_core/baton_assemble/__init__.py": (
        False,
        "Warm-reachable: the registered op `handoff.correct_body` imports "
        "`_resolve_current_session_id` at call time (a deliberate lazy import that "
        "avoids an op-registration cycle — which is why the ops eager-import sweep "
        "does not show this module) and gates authorship on its value. Paired with "
        "`handoff_correct_body._resolve_session_id_with_source`, which cross-checks "
        "the two and REFUSES on mismatch: migrate both or neither.",
    ),
    "coordinator_core/ops/handoff_correct_body.py": (
        False,
        "Warm-reachable: the registered op `handoff.correct_body`. A spawner's id "
        "authorizes the correction against the wrong session AND is stamped into "
        "the AC6 correction note as the session that made it. The AC5 source label "
        "is now derived from the resolved value, not from walking the env ladder.",
    ),
    "coordinator_core/hooks/track_touched_files.py": (
        False,
        "Warm-reachable: `@register_op('hooks.track_touched_files')`. The backlog "
        "guessed 'likely cold-only' and that is wrong — a registered op is warm-"
        "servable by construction. A spawner's id fails the `!= session_id` test and "
        "so gets WRITTEN as the agent dir's owner back-pointer, the exact outcome "
        "that arm's fail-closed conditions exist to prevent.",
    ),
    "coordinator_core/orient_assemble/readers_clean_ops.py": (
        True,
        "Cold-only: no registered op imports `orient_assemble` at module scope or "
        "call time. Entered from the orient/workday-start assemble CLI, in the "
        "operator's own process, where the env read is the correct source.",
    ),
    "coordinator_core/session/claims.py": (
        True,
        "Cold-only at the site that matters: no op calls `self_claim`. The module IS "
        "imported into the op process for other functions, which is why import-"
        "presence is not the reachability test. `self_claim`'s callers are "
        "`baton_assemble.apply` (assemble CLI) and `js_bridge_cli`.",
    ),
    "coordinator_core/ops/check_em_environment.py": (
        True,
        "Cold-only: `main(argv)` CLI entrypoint, no `register_op`, absent from the "
        "ops eager-import sweep. Lives under ops/ by directory, not by dispatch.",
    ),
    "coordinator_core/ops/dispatch_shape_classify.py": (
        True,
        "Cold-only: `main(argv)` CLI entrypoint, no `register_op`, absent from the "
        "ops eager-import sweep. Same shape as check_em_environment.",
    ),
    "coordinator_core/write_guards/block_subagent_plan_body_write.py": (
        True,
        "Cold-only: the read is in `_write_hook_emit_log`, reached only from "
        "`check(payload)`, which `preuse-write-dispatch.py` calls through "
        "`write_guards.engine.evaluate_payload_json` in the hook's own process. That "
        "engine registers no op. The backlog's 'likely cold-only' guess holds here.",
    ),
}

#: Cohort members whose module was DELETED outright, with the commit that did it.
#: `test_cohort_member_exists` exists because an entry naming a file that is not
#: there is an inert ratchet -- but the remedy it names ("re-triage the code at its
#: new home") assumes there IS a new home, and sometimes the answer is that the
#: capability was killed. Moving the entry here rather than dropping it keeps both
#: honesty properties the COHORT map has: the verdict survives as a written record,
#: and `test_gravestoned_members_stay_gone` re-fires the moment the path comes back,
#: so a resurrection re-enters triage instead of landing silently outside the ratchet.
GRAVESTONED: dict[str, str] = {
    "coordinator_core/ops/review_trail_write.py": (
        "DELETED at ae9607e410 (2026-08-29, 'C3: Delete review_trail.write writer "
        "module and all dependent CLIs and tests'). Was warm-reachable as the "
        "registered op `review_trail.write`, where identity fed "
        "`_guard_foreign_session_range`'s anti-forgery decision; fixed f716ee01a "
        "before the module was killed. No successor module carries the read -- "
        "`ops/emit/sections/review_trail.py` is the emit-side reader and names no "
        "session-identity env var, and `ops/ceremony/tail_ops.py` performs no call "
        "against the op. Verdict recorded 2026-08-30 (C2, docs/plans/2026-08-30-"
        "the-c-door-sends-the-callers-session-identity.md); the ratchet had been "
        "red on this entry since the delete."
    ),
}

#: The cohort's frozen floor. Every ratchet test below is
#: `parametrize("rel", sorted(COHORT))`-scoped, so a member deleted from that dict
#: is not reported as missing -- it simply stops being checked, and the file goes on
#: passing. `GRAVESTONED` keeps a KILLED member honest, but nothing made removal go
#: through it: a live, still-warm-reachable entry could be dropped outright and the
#: ratchet would silently shrink. That is the same "untriaged population
#: masquerading as reviewed" failure this module's docstring warns about, reached by
#: subtraction instead of by a false verdict (code-reviewer finding 6, 2026-08-30).
#:
#: A member leaves this floor exactly one way: its module is deleted and it moves to
#: `GRAVESTONED`, which `test_cohort_floor_is_covered` accepts as coverage. Adding
#: members is unconstrained -- the floor is a minimum, never a fixed list.
_COHORT_FLOOR: frozenset = frozenset(
    {
        "coordinator_core/baton_assemble/__init__.py",
        "coordinator_core/ops/handoff_correct_body.py",
        "coordinator_core/hooks/track_touched_files.py",
        "coordinator_core/orient_assemble/readers_clean_ops.py",
        "coordinator_core/session/claims.py",
        "coordinator_core/ops/check_em_environment.py",
        "coordinator_core/ops/dispatch_shape_classify.py",
        "coordinator_core/write_guards/block_subagent_plan_body_write.py",
        "coordinator_core/ops/review_trail_write.py",
        "coordinator_core/ops/tracker/push_suggestion.py",
    }
)

REPO_ROOT = Path(__file__).resolve().parents[2]


#: Names that hold the session-identity precedence ladder. A read indexed by one of
#: these is a ladder walk — `os.environ.get(var)` inside `for var in
#: _SESSION_ENV_PRECEDENCE` reads exactly the vars this ratchet governs while
#: matching no string literal. Found by review, in a cohort member, which is why
#: the literal-only scan below is not sufficient on its own.
LADDER_NAMES = frozenset(
    {"_SESSION_ENV_PRECEDENCE", "SESSION_ENV_PRECEDENCE", "SESSION_ENV_READ_ORDER"}
)

#: (relative path, enclosing function) pairs permitted to walk the ladder, with the
#: reason. A ladder walk is legitimate ONLY for deriving a human-readable label about
#: an identity that has ALREADY been resolved canonically — never for deciding one.
#: Same honesty rule as COHORT: an entry here is a reviewed claim, not a silencer.
LADDER_READ_EXEMPTIONS: dict[tuple[str, str], str] = {
    (
        "coordinator_core/ops/handoff_correct_body.py",
        "_resolve_session_id_with_source",
    ): (
        "Label derivation only. `sid` is resolved by resolve_current_session_id BEFORE "
        "this loop; the loop asks which precedence var happens to hold that value, purely "
        "to name the AC5 source. It never selects the identity. If this loop is ever "
        "changed to PREFER an env value over the resolved one, it stops being a label and "
        "this exemption must be withdrawn."
    ),
}


def _enclosing_function(tree: ast.Module, lineno: int) -> str:
    """Name of the innermost function containing `lineno`, or '' at module level."""
    best, best_start = "", -1
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", None) or node.lineno
            if node.lineno <= lineno <= end and node.lineno > best_start:
                best, best_start = node.name, node.lineno
    return best


def _session_env_reads(path: Path, rel: str = "") -> list[tuple[int, str]]:
    """Return (lineno, env_name) for every direct read of a session-identity env var.

    Matches three shapes: ``os.environ.get("X")`` / ``os.environ["X"]`` on a literal,
    and ``os.environ.get(var)`` where `var` is bound by iteration over one of
    ``LADDER_NAMES`` — the loop-variable form, which reads every governed var while
    containing no literal at all. A module-level constant holding the name is caught
    separately by the string-constant scan below, so aliasing does not launder a read
    past this ratchet either.

    A ladder walk whose (path, enclosing function) appears in
    ``LADDER_READ_EXEMPTIONS`` is not reported — see that map for the standard.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[int, str]] = []

    ladder_vars: set[str] = {
        node.target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and isinstance(node.iter, ast.Name)
        and node.iter.id in LADDER_NAMES
    }

    def _is_environ(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        )

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "pop"}
            and _is_environ(node.func.value)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in SESSION_ENV_NAMES
        ):
            hits.append((node.lineno, node.args[0].value))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "pop"}
            and _is_environ(node.func.value)
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in ladder_vars
        ):
            if (rel, _enclosing_function(tree, node.lineno)) not in LADDER_READ_EXEMPTIONS:
                hits.append((node.lineno, f"<ladder walk via {node.args[0].id}>"))
        elif (
            isinstance(node, ast.Subscript)
            and _is_environ(node.value)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in SESSION_ENV_NAMES
        ):
            hits.append((node.lineno, node.slice.value))
    return hits


@pytest.mark.parametrize("rel", sorted(COHORT))
def test_cohort_member_exists(rel: str) -> None:
    """A cohort entry naming a moved or deleted file is a silently disarmed ratchet."""
    assert (REPO_ROOT / rel).is_file(), (
        f"{rel} is in the warm-identity cohort but does not exist — the ratchet for "
        "it is inert. Re-triage the code at its new home and update COHORT; do not "
        "just delete the entry."
    )


@pytest.mark.parametrize("rel", sorted(rel for rel, (cold, _) in COHORT.items() if not cold))
def test_warm_reachable_modules_read_no_session_env(rel: str) -> None:
    """Warm-reachable modules resolve identity through the canonical resolver only."""
    _cold, reason = COHORT[rel]
    hits = _session_env_reads(REPO_ROOT / rel, rel)
    assert hits == [], (
        f"{rel} reads a session-identity env var directly at "
        f"{', '.join(f'line {ln} ({name})' for ln, name in hits)}.\n\n"
        f"This module is warm-reachable: {reason}\n\n"
        "Inside a warm-served dispatch os.environ names whoever spawned the server, "
        "not the caller. Resolve through "
        "coordinator_core.ops.session_context.resolve_current_session_id, which reads "
        "the per-request identity binding first and falls back to the same env ladder "
        "cold."
    )


@pytest.mark.parametrize("rel", sorted(rel for rel, (cold, _) in COHORT.items() if not cold))
def test_warm_reachable_modules_declare_no_session_env_constant(rel: str) -> None:
    """The name must not survive as a module-local constant either.

    Supersedes the one-module predecessor pin
    (`test_review_trail_write_warm_identity.TestNoLocalSessionEnvConstants`) by
    applying its check to the whole warm-reachable cohort. A constant is how the
    AST read-scan above gets laundered: `os.environ.get(_SESSION_VAR)` matches no
    literal.
    """
    tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
    offenders = [
        (node.lineno, node.value.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        for _t in node.targets
        if isinstance(node.value, ast.Constant) and node.value.value in SESSION_ENV_NAMES
    ]
    assert offenders == [], (
        f"{rel} declares a module-local session-env-var name at "
        f"{offenders} — identity comes from resolve_current_session_id, and a "
        "module-local name is how a direct read returns without tripping the "
        "literal scan."
    )


def test_ladder_read_exemptions_are_live_and_reasoned() -> None:
    """Each ladder exemption must name a real function and say why it is a label.

    An exemption for a function that no longer exists silences nothing today and
    everything tomorrow, once the name is reused. Requiring the target to resolve
    keeps the map honest the same way `test_cohort_member_exists` keeps COHORT honest.
    """
    dead, thin = [], []
    for (rel, func), reason in LADDER_READ_EXEMPTIONS.items():
        path = REPO_ROOT / rel
        if not path.is_file():
            dead.append((rel, func))
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            n.name
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if func not in names:
            dead.append((rel, func))
        if "label" not in reason.lower() or len(reason) < 80:
            thin.append((rel, func))
    assert dead == [], (
        f"ladder exemptions naming a missing file or function: {dead} — remove the entry "
        "or point it at the code that replaced it; a stale exemption is a silent hole."
    )
    assert thin == [], (
        f"ladder exemptions without a substantive label-only rationale: {thin} — a ladder "
        "walk is exempt only when it labels an already-resolved identity, never when it "
        "selects one."
    )


#: C2 (docs/plans/2026-08-30... the-c-door-sends-the-callers-session-identity):
#: sites where `session.core.resolve_session_id` was blending tier-0
#: (warm-carried) and tiers 1-3 (ambient env) behind one anti-forgery
#: self-exclusion check. `resolve_session_id` already tries the bound
#: `session_identity_override` ContextVar first, so this is not a raw-env-read
#: defect the COHORT/`_session_env_reads` scan above would catch — it is a
#: source-shape ratchet that the call site EXPLICITLY prefers
#: `carried_session_id()` (self-documenting the anti-forgery intent, and
#: resilient to a future change to `resolve_session_id`'s own tier order),
#: rather than resting the whole guarantee on that one function's internals.
#: Each entry names the enclosing function and why self-exclusion identity is
#: an anti-forgery input there (mirrors `cs_action_memo`'s own REFUSE-on-
#: mismatch shape — see this module's docstring).
CARRIED_IDENTITY_PREFERENCE_SITES: dict[str, tuple[str, ...]] = {
    "coordinator_core/pickup_assemble/__init__.py": (
        "compute_liveness_signal",
        "_primary_held_disposition",
    ),
}


@pytest.mark.parametrize("rel", sorted(CARRIED_IDENTITY_PREFERENCE_SITES))
def test_self_exclusion_sites_prefer_carried_session_id(rel: str) -> None:
    """Each named self-exclusion function must call `carried_session_id()`
    before it can fall back to `resolve_session_id` — the composed
    prefer-carried-else-resolve shape this ratchet's C2 entry pins. A site
    that regresses to a bare `resolve_session_id(...)` call with no
    `carried_session_id()` preference silently loses the explicit anti-
    forgery preference this test exists to hold in place.
    """
    tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
    func_names = CARRIED_IDENTITY_PREFERENCE_SITES[rel]
    found = {name: False for name in func_names}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in func_names:
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "carried_session_id"
                ):
                    found[node.name] = True
    missing = [name for name, ok in found.items() if not ok]
    assert missing == [], (
        f"{rel}: {missing} no longer call `carried_session_id()` — this "
        "self-exclusion identity check must prefer the warm-carried caller "
        "identity over resolve_session_id's ambient-env degrade (C2, "
        "the-c-door-sends-the-callers-session-identity)."
    )


@pytest.mark.parametrize("rel", sorted(GRAVESTONED))
def test_gravestoned_members_stay_gone(rel: str) -> None:
    """A gravestoned path that exists again is an untriaged warm-identity site.

    The inverse of `test_cohort_member_exists`: that test catches a COHORT entry
    whose file vanished, this one catches a killed module coming back at its old
    path outside the cohort. Either way the ratchet is asserting over something
    other than what its verdict describes, which is the one failure mode this
    file's docstring says the allowlist must never encode.
    """
    assert not (REPO_ROOT / rel).is_file(), (
        f"{rel} exists again but is gravestoned, not triaged: {GRAVESTONED[rel]}"
        "\n\n"
        "Re-triage it and move the entry back into COHORT with a fresh reachability "
        "verdict -- do not leave it here, and do not add it to COHORT without one."
    )


def test_gravestone_verdicts_name_the_deleting_commit() -> None:
    """A gravestone is a claim about a delete and must cite it.

    Same honesty rule as `test_cold_only_verdicts_carry_a_reason`: an entry parked
    here with a placeholder reason retires a ratchet member on nobody's authority.
    """
    thin = [
        rel
        for rel, reason in GRAVESTONED.items()
        if not re.search(r"DELETED at [0-9a-f]{7,40}", reason) or len(reason) < 80
    ]
    assert thin == [], (
        f"gravestones without a cited deleting commit: {thin} -- name the sha that "
        "removed the module and why no successor carries the read."
    )


def test_cohort_floor_is_covered() -> None:
    """No triaged member may leave the ratchet except by being gravestoned.

    The other tests here all iterate `COHORT`, so they cannot see a member that
    is no longer in it. This one names the floor explicitly and asks the
    complementary question: is every path we have ever triaged still accounted
    for, either as a live cohort entry or as a cited gravestone?
    """
    accounted = set(COHORT) | set(GRAVESTONED)
    dropped = sorted(_COHORT_FLOOR - accounted)
    assert dropped == [], (
        f"triaged warm-identity members silently dropped from the ratchet: {dropped}"
        "\n\n"
        "A member leaves COHORT only by being deleted from the repo and moved to "
        "GRAVESTONED with the sha that removed it. Removing the line outright does "
        "not narrow the ratchet's claim, it narrows what the ratchet CHECKS while "
        "the docstring goes on claiming the member is triaged."
    )


def test_cold_only_verdicts_carry_a_reason() -> None:
    """A cold-only verdict is a claim about a call path and must say which one.

    Guards the allowlist's honesty rather than the code: an entry flipped to
    cold-only with a placeholder reason is how an untriaged site enters a cohort
    that is supposed to mean 'triaged'.
    """
    thin = [
        rel
        for rel, (cold, reason) in COHORT.items()
        if cold and ("Cold-only" not in reason or len(reason) < 80)
    ]
    assert thin == [], (
        f"cold-only entries without a substantive call-path reason: {thin} — name the "
        "entrypoint that reaches the code and why no registered op does."
    )

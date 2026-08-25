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
    "coordinator_core/ops/review_trail_write.py": (
        False,
        "Warm-reachable: the registered op `review_trail.write`. Identity is an "
        "input to `_guard_foreign_session_range`'s anti-forgery decision, so a "
        "spawner's id here REFUSES a session's own commits. Fixed f716ee01a.",
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

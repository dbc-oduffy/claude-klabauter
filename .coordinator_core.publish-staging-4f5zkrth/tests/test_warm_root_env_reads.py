"""Warm-root env-read ratchet — the ROOT half of the same defect class
``test_warm_identity_env_reads.py`` governs for session identity.

THE DEFECT CLASS, restated for ROOT. A resident warm server resolves a
repo-root value from its own process environment, which names whoever
SPAWNED it rather than the session whose request it is currently serving.
Two ops carried the transport cure for IDENTITY (``resolve_current_session_id``)
while leaving ROOT resolution as a bare ``os.environ.get(_CLAUDE_KLABAUTER_ROOT_ENV)`` —
the cure was applied to one axis and not the other. Under warm serving a
handler reads the SERVER's environment, so the root names whoever spawned the
server rather than the caller — the write exits 0, prints a normal path, and
lands nowhere the caller can see.

WHY NO BEHAVIOURAL TEST CATCHES IT, and why this file is an AST test instead.
``cc_invoke`` spawns the COLD subprocess with the caller's own ``os.environ``,
so a raw env read is CORRECT on that path. Every in-process reproduction, unit
test and isolated-settings-home probe therefore passes. The defect is
reachable only through a live warm server. A source-shape ratchet is the
artifact that survives that — it also costs no subprocess, so it stays on the
fast tier rather than tripping the per-item spawn budget.

THE FIX SHAPE differs from identity's ContextVar cure, and deliberately so:
a repo root is not a per-request identity that needs binding through the
request envelope, it is a machine-scoped fact (there is exactly one claude-klabauter
clone). The existing precedent in this file's own sibling defect
(``queue_append._output_root_override``, guarding ``QUEUE_APPEND_OUTPUT_ROOT``)
already established the shape: gate the env-var trust behind
``op_latency.execution_route() == op_latency.IN_PROCESS`` and fall through to
the machine-local registry lookup otherwise, which resolves the SAME true
value regardless of which process asks. This ratchet checks for that gate,
not for the identity ContextVar shape — a bare, ungated read is the defect;
a read immediately conditioned on ``execution_route() == IN_PROCESS`` in the
same boolean expression is the cure.

SCOPE — DELIBERATELY NOT REPO-WIDE, same discipline as the identity ratchet.
``coordinator_core`` names these env vars in dozens of places, most of them
CLI entrypoints, hook processes and bootstrap-time resolvers that never
execute inside a warm-served dispatch, where a raw env read is correct.
Asserting over an untriaged population and allowlisting whatever happened to
fail would encode "currently passing" as "reviewed" — the same failure mode
the identity ratchet's own docstring warns against. So this ratchet governs
one NAMED cohort: the sites triaged for the 2026-08-20 ROOT-guard chunk, each
carrying its own reachability verdict below. The rest of the repo is
untriaged, not vouched for. Widening this cohort means triaging the new
members first — the verdict is the work, not the allowlist line.

REACHABILITY RULE, same as the identity ratchet: a site is warm-reachable when
it is reachable from a ``@register_op`` handler's call graph, because
``warm.client.try_warm_dispatch`` takes any op message and applies no method
allowlist. Import-presence is NOT the test — a module can be imported into
the op process for unrelated functions while the one function that reads a
ROOT var is only ever reached from a CLI ``main()``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: The root vars whose direct, ungated reading is the defect's shape.
ROOT_ENV_NAMES = frozenset(
    {"CLAUDE_KLABAUTER_ROOT", "COORDINATOR_ENGINE_ROOT", "COORDINATOR_ROOT", "CLAUDE_PROJECT_DIR"}
)

#: The cohort this ratchet governs. Value is the triage verdict — `False` means
#: "must not read these vars directly without an execution_route() IN_PROCESS
#: gate"; `True` means "cold-only by design, raw read is correct".
#:
#: Triaged 2026-08-20, alongside the C13 chunk that fixed the two named
#: residuals (queue.append, deliverable.rollup) and two more found by walking
#: the same reachability rule the identity ratchet defines. Every entry below
#: was traced by hand from a `@register_op` handler's own body, not from
#: import-presence — see each reason for the exact call path.
COHORT: dict[str, tuple[bool, str]] = {
    "coordinator_core/ops/queue_append.py": (
        False,
        "Warm-reachable: the registered op `queue.append`. `_claude_klabauter_root()` "
        "is called (via `_output_path`/`append_queue_entry`) to resolve the "
        "central-scope write destination. A spawner's root here silently "
        "redirects the write to whichever repo happened to spawn the "
        "server. Guarded via `op_latency.execution_route() == IN_PROCESS` "
        "in `_claude_klabauter_root`, falling through to the machine-local registry "
        "lookup under warm serving.",
    ),
    "coordinator_core/ops/deliverable_rollup.py": (
        False,
        "Warm-reachable: the registered op `deliverable.rollup`. "
        "`_claude_klabauter_root()` feeds the central-initiatives-dir resolution the "
        "op's handler drives. Same hazard and same guard shape as "
        "queue_append's sibling helper.",
    ),
    "coordinator_core/orientation/regenerate_cache.py": (
        False,
        "Warm-reachable: the registered (async) op `orientation.regenerate_cache`. "
        "Its handler calls `build_cache(repo_root=...)`, which calls "
        "`_state_root(repo_root)`, which calls `_claude_klabauter_root()` whenever "
        "`repo_root` equals the meta-repo home (~/.claude) — the branch a "
        "workday-start/handoff invoker for the meta-repo itself takes. "
        "Guarded the same way as queue_append's `_claude_klabauter_root`.",
    ),
    "coordinator_core/ops/_git_root_util.py": (
        False,
        "Warm-reachable: `git_root_zero_spawn()` is called from "
        "`app_session._resolve_repo_root`, itself called by all three "
        "registered ops `app_session.launch`/`census`/`teardown` whenever "
        "neither an explicit `repo_root` param nor an injected one is "
        "supplied. `CLAUDE_PROJECT_DIR` under warm serving would then name "
        "the server's own project dir, not the caller's. Guarded via "
        "`execution_route() == IN_PROCESS` ahead of the env read; the "
        "cwd-walk fallback runs unconditionally either way.",
    ),
    "coordinator_core/install/check_install_singularity.py": (
        True,
        "Warm-reachable (via `plugin_health.sentinel`'s registered op, which "
        "calls `check_install_singularity.main([])` IN-PROCESS through "
        "`_call_native_main` — see that module's docstring) but NOT "
        "APPLICABLE: the `COORDINATOR_ROOT`/`CLAUDE_PLUGIN_ROOT` reads at "
        "CHECK 3/4 diagnose whether the EXECUTING process's own installed "
        "plugin trees are doubled/misaligned — a property of the process's "
        "own environment, not a caller-supplied write destination. Reading "
        "the serving process's own env is the correct source for that "
        "diagnostic; there is no caller-scoped value to prefer instead.",
    ),
}

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Names of module-level accessor functions already established (by hand, at
#: triage time) to gate their ROOT_ENV_NAMES read behind an
#: `execution_route() == IN_PROCESS` check in the SAME boolean expression as
#: the `os.environ` read. A read outside one of these functions in a
#: warm-reachable cohort file is unguarded and must fail.
GATED_FUNCTIONS: dict[tuple[str, str], str] = {
    ("coordinator_core/ops/queue_append.py", "_claude_klabauter_root"): (
        "override and op_latency.execution_route() == op_latency.IN_PROCESS"
    ),
    ("coordinator_core/ops/deliverable_rollup.py", "_claude_klabauter_root"): (
        "override and op_latency.execution_route() == op_latency.IN_PROCESS"
    ),
    ("coordinator_core/orientation/regenerate_cache.py", "_claude_klabauter_root"): (
        "override and op_latency.execution_route() == op_latency.IN_PROCESS"
    ),
    ("coordinator_core/ops/_git_root_util.py", "git_root_zero_spawn"): (
        "project_dir and op_latency.execution_route() == op_latency.IN_PROCESS"
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


def _root_env_reads(path: Path, rel: str) -> list[tuple[int, str, str]]:
    """Return (lineno, env_name, enclosing_function) for every direct read of a
    ROOT env var: ``os.environ.get("X")`` / ``os.environ["X"]`` on a literal.

    A read inside a function named in ``GATED_FUNCTIONS`` for this file is not
    reported — see that map for the standard. A read anywhere else in a
    warm-reachable cohort file is unguarded.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[int, str, str]] = []

    def _is_environ(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        )

    for node in ast.walk(tree):
        name = None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "pop"}
            and _is_environ(node.func.value)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in ROOT_ENV_NAMES
        ):
            name = node.args[0].value
        elif (
            isinstance(node, ast.Subscript)
            and _is_environ(node.value)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in ROOT_ENV_NAMES
        ):
            name = node.slice.value
        if name is not None:
            func = _enclosing_function(tree, node.lineno)
            if (rel, func) not in GATED_FUNCTIONS:
                hits.append((node.lineno, name, func))
    return hits


@pytest.mark.parametrize("rel", sorted(COHORT))
def test_cohort_member_exists(rel: str) -> None:
    """A cohort entry naming a moved or deleted file is a silently disarmed ratchet."""
    assert (REPO_ROOT / rel).is_file(), (
        f"{rel} is in the warm-root cohort but does not exist — the ratchet for "
        "it is inert. Re-triage the code at its new home and update COHORT; do not "
        "just delete the entry."
    )


@pytest.mark.parametrize("rel", sorted(rel for rel, (cold, _) in COHORT.items() if not cold))
def test_warm_reachable_modules_gate_root_env_reads(rel: str) -> None:
    """Warm-reachable modules must not read a ROOT var without the execution_route() gate."""
    _cold, reason = COHORT[rel]
    hits = _root_env_reads(REPO_ROOT / rel, rel)
    assert hits == [], (
        f"{rel} reads a ROOT env var directly, without an execution_route() "
        f"IN_PROCESS gate, at "
        f"{', '.join(f'line {ln} ({name}, in {func or chr(60)}module level{chr(62)})' for ln, name, func in hits)}.\n\n"
        f"This module is warm-reachable: {reason}\n\n"
        "Inside a warm-served dispatch os.environ names whoever spawned the "
        "server, not the caller. Gate the read behind "
        "`op_latency.execution_route() == op_latency.IN_PROCESS` in the same "
        "boolean expression (see queue_append._claude_klabauter_root / "
        "_output_root_override for the established shape) and fall through "
        "to the machine-local registry lookup otherwise."
    )


def test_gated_functions_are_live() -> None:
    """A `GATED_FUNCTIONS` entry naming a missing file or function silences
    nothing today and everything tomorrow, once the name is reused."""
    dead = []
    for rel, func in GATED_FUNCTIONS:
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
    assert dead == [], (
        f"gated-function entries naming a missing file or function: {dead} — "
        "remove the entry or point it at the code that replaced it; a stale "
        "entry is a silent hole."
    )


def test_cold_only_verdicts_carry_a_reason() -> None:
    """A cold-only / not-applicable verdict is a claim about a call path and
    must say which one — guards the cohort's honesty rather than the code."""
    thin = [
        rel
        for rel, (cold, reason) in COHORT.items()
        if cold and len(reason) < 80
    ]
    assert thin == [], (
        f"cold-only/not-applicable entries without a substantive call-path "
        f"reason: {thin} — name the entrypoint that reaches the code and why "
        "the raw read is correct there."
    )

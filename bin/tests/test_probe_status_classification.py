"""
bin.tests.test_probe_status_classification — the fail-token invariant for
bin/claude-klabauter-doctor-probe.py, enforced statically over the source.

Why this file exists. `step-zero-emitter-contract.md` § `status` Enum defines
`fail` as "the probe ran successfully and the checked condition is NOT
satisfied. Expect a non-empty `remediation`." Two probes drifted off that
definition and reported states that are NORMAL (`claude-klabauter.publish.provenance`
on a box that has never published) or SELF-HEALING (`claude-klabauter.warm.generation`,
whose own remediation said the condition "drains on its own ... no direct
action is named here") with the same `fail` token used for real faults. The
consequence was an install that succeeds, exits 0, and prints a wall of
`fail` lines a first-time operator cannot triage
(docs/problems/2026-08-26-what-a-reinstall-on-the-mac-actually-hits.md
§ The frame this reveals, § 5, § 7).

The rule this guard pins is the discriminator that fixes both, and it is
readable off the emitted line itself rather than off a probe author's intent:

    a result that renders as step-zero `fail` must carry a remediation
    that names an action.

`—` is this file's no-action sentinel. A branch that has nothing for the
reader to do is, by that fact, not reporting an unsatisfied condition — it
belongs on `warn` (`_INFO`) if the state is true-and-normal, or on
`inconclusive` (`skipped=True`) if the probe could not measure. The
converse — an actionable remediation on a `fail` — is left alone; that is
the normal, correct shape.

Static by construction: this reads the probe source with `ast` and never
runs a probe, spawns a process, or touches a resident server, so it holds
on every platform and in every tree state.

Run: python -m pytest bin/tests/test_probe_status_classification.py -q
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_BIN_PROBE = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"

# Statuses that `_sz_status` maps to step-zero `fail`.
_FAIL_STATUSES = {"_BROKEN", "_DEGRADED"}

# A remediation naming no action. `_ProbeResult` rows use the em dash; the
# empty string is the contract's own "empty for pass" spelling.
_NO_ACTION_REMEDIATIONS = {"—", "-", "--", ""}

# A remediation can also SAY it names no action while looking non-empty —
# which is how `claude-klabauter.warm.generation` carried a `fail` for a state that
# drains itself ("no direct action is named here"), and how
# `claude-klabauter.publish.provenance` carried one for a step no installer can take
# ("None at install time"). Both spellings are the same defect as `—`, so the
# rule reads the sentence, not just its length. Each phrase below is taken
# verbatim from a remediation that actually shipped or ships now; add to this
# set when a new way of writing "there is nothing to do" appears, never a
# paraphrase of an action.
_NO_ACTION_PHRASES = (
    "no direct action",
    "no action is named",
    "drains on its own",
    "resolves on its own",
    "none at install time",
    "nothing to do",
)


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _probe_result_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_ProbeResult"
    ]


def _renders_as_fail(call: ast.Call) -> bool:
    """True iff this construction reaches step-zero `fail`.

    `_sz_status` short-circuits on `skipped` before consulting `status`, so a
    skipped row renders `inconclusive` whatever status it stores — those rows
    are outside this rule, which is why the sibling probes' documented
    `_DEGRADED`+`skipped=True` "cannot tell" convention does not trip it.
    """
    skipped = _keyword(call, "skipped")
    if isinstance(skipped, ast.Constant) and skipped.value is True:
        return False
    status = _keyword(call, "status")
    return isinstance(status, ast.Name) and status.id in _FAIL_STATUSES


def _static_remediation(call: ast.Call) -> str | None:
    """The remediation text when it is a literal; None when it is computed.

    A computed remediation (f-string, concatenation, a name) cannot be the
    no-action sentinel, so it is not this guard's business.
    """
    node = _keyword(call, "remediation")
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _names_no_action(remediation: str) -> bool:
    """True iff this remediation leaves the reader with nothing to run."""
    stripped = remediation.strip()
    if stripped in _NO_ACTION_REMEDIATIONS:
        return True
    lowered = stripped.lower()
    return any(phrase in lowered for phrase in _NO_ACTION_PHRASES)


@pytest.fixture(scope="module")
def probe_tree() -> ast.AST:
    if not _BIN_PROBE.exists():
        pytest.skip(f"probe source not found at {_BIN_PROBE}")
    return ast.parse(_BIN_PROBE.read_text(encoding="utf-8"))


def test_no_fail_result_names_a_remediation_the_reader_cannot_act_on(
    probe_tree: ast.AST,
) -> None:
    """A step-zero `fail` must name an action; `—` means there is none."""
    offenders: list[str] = []
    for call in _probe_result_calls(probe_tree):
        if not _renders_as_fail(call):
            continue
        remediation = _static_remediation(call)
        if remediation is None:
            continue
        if _names_no_action(remediation):
            probe = _keyword(call, "probe")
            named = (
                probe.id
                if isinstance(probe, ast.Name)
                else ast.unparse(probe)
                if probe is not None
                else "<unnamed>"
            )
            offenders.append(f"{_BIN_PROBE.name}:{call.lineno} ({named})")

    assert not offenders, (
        "step-zero `fail` means the checked condition is NOT satisfied and expects a "
        "remediation the reader can run. These results emit `fail` with no action named:\n  "
        + "\n  ".join(offenders)
        + "\nA state that resolves itself or is normal belongs on _INFO (`warn`); a state "
        "the probe could not measure belongs on skipped=True (`inconclusive`)."
    )


def test_the_guard_can_see_a_violation() -> None:
    """The guard's own detector, exercised against a synthetic offender.

    Without this, a refactor that silently stopped matching `_ProbeResult`
    calls would leave the test above green over zero rows.
    """
    tree = ast.parse(
        'x = _ProbeResult(probe=_SOME_PROBE, status=_DEGRADED, detail="d", remediation="—")'
    )
    calls = _probe_result_calls(tree)
    assert len(calls) == 1
    assert _renders_as_fail(calls[0]) is True
    assert _static_remediation(calls[0]) == "—"


def test_the_guard_reads_the_sentence_not_just_the_length() -> None:
    """The two texts this defect actually shipped under, pinned as offenders.

    Both are non-empty and would pass a bare "is the remediation blank?"
    check; both leave the reader of an install log with nothing to run.
    """
    stale_generation = (
        "A stale generation drains on its own via warm.idle's superseded-generation "
        "arm once a fresh server binds; no direct action is named here."
    )
    never_published = (
        "None at install time. A percolate round publishing from this checkout "
        "records the provenance this probe reads."
    )
    assert _names_no_action(stale_generation) is True
    assert _names_no_action(never_published) is True
    assert _names_no_action("Re-run scripts/setup.py from claude-klabauter.") is False


def test_the_guard_exempts_a_skipped_cannot_tell_row() -> None:
    """`skipped=True` renders `inconclusive`; the rule does not reach it."""
    tree = ast.parse(
        'x = _ProbeResult(probe=_P, status=_DEGRADED, detail="d", remediation="—", '
        "skipped=True)"
    )
    assert _renders_as_fail(_probe_result_calls(tree)[0]) is False


def test_the_guard_sees_every_probe_result_construction(probe_tree: ast.AST) -> None:
    """Floor on the walk itself — a matcher that matches nothing proves nothing."""
    calls = _probe_result_calls(probe_tree)
    assert len(calls) > 50, (
        f"expected the probe module to construct many _ProbeResult rows, found {len(calls)} — "
        "the AST matcher above has probably stopped matching."
    )

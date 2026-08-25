"""Meta-test: every hand-rolled ``_machine_local_impl()`` copy must consult
settings-home BEFORE the retired ``~/.claude/bin`` compat mirror.

The defect class (2026-07-30, six instances found in one sweep): the
``_machine_local.py`` reader path is resolved by ~12 independently hand-rolled
``_machine_local_impl()`` copies scattered across ``coordinator_core/`` — the
same duplication shape that let 456 call sites miss a caller sweep on
2026-07-22. Six of those copies had NO settings-home rung at all: they joined
``<claude_home>/bin/_machine_local.py`` unconditionally, then the caller's
``os.path.exists`` guard turned the miss into a silent ``None``. On this
machine ``~/.claude/bin/`` holds no ``_machine_local.py`` while the
settings-home copy is present, so all six silently resolved
``repos.claude_klabauter`` to ``None`` and degraded — a live, not theoretical,
break of DR-210 Amendment 2026-07-24 ("coordinator resolves nothing through
``~/.claude/bin``").

Why a gate and not just the six fixes: the fixes are two lines each and a
seventh copy can be minted at any time by anyone following the six-of-twelve
majority shape they saw in the tree. Prose in DR-210 did not stop the first
six. This gate is the artifact that discharges the rule.

What it asserts, per ``_machine_local_impl``-shaped function found by AST walk
over every ``.py`` under ``coordinator_core/``: if the body references the
mirror rung (a ``"bin"`` / ``"_machine_local.py"`` join rooted at a
claude-home resolver), it must ALSO reference a settings-home rung, and the
settings-home reference must appear at a strictly lower line number. Order is
the assertion — mere presence of both is what a mirror-first ladder also has.

Negative-spec: this gate does NOT require removing the mirror rung. The
mirror is deliberately retained as a last-resort fallback (DR-210 Amendment
retires it as a RESOLUTION-ORDER concern, not an existence concern); a copy
with settings-home only, and no mirror at all, is equally acceptable here.

Blind spots: matches on the settings-home *name* (``settings_home`` /
``COORDINATOR_SETTINGS_HOME``), so a copy that reaches settings-home by some
future third spelling would read as mirror-only and fail loudly rather than
pass silently — the safe direction. A resolver not named
``_machine_local_impl`` / ``machine_local_impl_path`` is out of scope; extend
``_TARGET_NAMES`` if a new spelling lands.

Spec backlink: DR-210 Amendment 2026-07-24; sibling-repo memo
``cross-repo/inbox/2026-07-30-example-game-repo-em-example-game-repo-claude-bin-rung-retired-in-full.md``
(Correction 2 — the no-hit-returns-a-legacy-path shape that prompted this
sweep); ``coordinator/bin/lib/machine_local_impl_resolve.py`` (the canonical
resolver these copies should ideally delegate to).
"""
from __future__ import annotations

import ast
import warnings
from pathlib import Path

_TARGET_NAMES = {"_machine_local_impl", "machine_local_impl_path"}

_MIRROR_MARKERS = ("_claude_home", "claude_home", "CLAUDE_HOME")
_SETTINGS_MARKERS = ("settings_home", "COORDINATOR_SETTINGS_HOME")

_CORE_ROOT = Path(__file__).resolve().parents[1]


def _first_line_referencing(node: ast.AST, markers: tuple[str, ...]) -> int | None:
    """Return the lowest line number in ``node`` referencing any marker name or
    string constant, or None if none is referenced."""
    hits: list[int] = []
    for sub in ast.walk(node):
        line = getattr(sub, "lineno", None)
        if line is None:
            continue
        if isinstance(sub, ast.Name) and any(m in sub.id for m in markers):
            hits.append(line)
        elif isinstance(sub, ast.Attribute) and any(m in sub.attr for m in markers):
            hits.append(line)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if any(m in sub.value for m in markers):
                hits.append(line)
    return min(hits) if hits else None


def _resolvers() -> list[tuple[Path, ast.FunctionDef]]:
    found: list[tuple[Path, ast.FunctionDef]] = []
    for path in sorted(_CORE_ROOT.rglob("*.py")):
        try:
            # Unrelated modules in the tree carry invalid-escape SyntaxWarnings;
            # they are not this gate's subject and must not surface as its noise.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in _TARGET_NAMES:
                found.append((path, node))
    return found


def test_machine_local_impl_copies_are_settings_home_first() -> None:
    resolvers = _resolvers()
    assert resolvers, (
        "found zero _machine_local_impl-shaped resolvers under coordinator_core/ — "
        "the walk is broken or every copy was renamed; this gate must not pass vacuously"
    )

    offenders: list[str] = []
    for path, node in resolvers:
        mirror_line = _first_line_referencing(node, _MIRROR_MARKERS)
        settings_line = _first_line_referencing(node, _SETTINGS_MARKERS)
        if mirror_line is None:
            continue  # settings-home-only (or neither) — nothing to order
        rel = path.relative_to(_CORE_ROOT.parent)
        if settings_line is None:
            offenders.append(
                f"{rel}:{node.lineno} {node.name}() — mirror-only ladder: references "
                f"~/.claude (line {mirror_line}) with NO settings-home rung"
            )
        elif settings_line > mirror_line:
            offenders.append(
                f"{rel}:{node.lineno} {node.name}() — mirror-first ladder: ~/.claude at "
                f"line {mirror_line} precedes settings-home at line {settings_line}"
            )

    assert not offenders, (
        "DR-210 Amendment 2026-07-24 requires settings-home to be consulted before the "
        "retired ~/.claude/bin compat mirror. Offending resolvers:\n  "
        + "\n  ".join(offenders)
        + "\n\nFix: insert the settings-home probe before the mirror join —\n"
        '    settings_home_impl = os.path.join(str(settings_home()), "bin", "_machine_local.py")\n'
        "    if os.path.exists(settings_home_impl):\n"
        "        return settings_home_impl\n"
        "Keep the mirror return as the last-resort rung; do not delete it."
    )

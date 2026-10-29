"""Ratchet: no guard or refusal in coordinator_core unlocks only by a PM act.

Walks every non-test module under coordinator_core (so a new guard is covered
without editing this file) and fails on a string literal, outside docstrings
and comments, that tells the reader to get PM approval, a waiver or a sentinel,
or that names a PM-created unlock file. Seeded from docs/decisions/DR-NO-PM-UNLOCKS.md.
A message routing to the G-EM for an irreversible class passes.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

# Remedy phrasings that send the reader to a PM (DR-NO-PM-UNLOCKS seed corpus).
_PM_REMEDY = re.compile(
    r"[Aa]sk (?:the )?PM|ask them to approve|[Oo]nly (?:the )?PM|PM creates|PM-created"
    r"|PM approval|PM assent|PM grant|PM stamp|PM-vouch|PM grantor"
    r"|[Dd]escribe the proposed change to the PM|\bwaiver\b[^.\n]{0,40}\bPM\b"
    r"|\bPM\b[^.\n]{0,40}\bwaiver\b|PM sentinel|approval sentinel|tier-u-grant-cli grant pm",
)

# Files whose existence is itself a PM-only unlock.
_PM_SENTINEL_NAMES = (
    ".coordinator-doctrine-edit-approved",
    ".coordinator-override-worktree-guard",
    ".coordinator-bash-guards-disarmed",
)

_PRE_FILTER = re.compile(r"\bPM\b|sentinel|\.coordinator-", re.IGNORECASE)

_SELF = Path(__file__).resolve()


def _module_files() -> list[Path]:
    out = []
    for p in _ROOT.rglob("*.py"):
        parts = p.relative_to(_ROOT).parts
        if "tests" in parts or "__pycache__" in parts or "fixtures" in parts:
            continue
        if p.name.startswith("test_") or p.name == "conftest.py" or p.resolve() == _SELF:
            continue
        out.append(p)
    return sorted(out)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _violations(text: str) -> list[tuple[int, str, str]]:
    """Return (lineno, kind, snippet) for each offending non-docstring literal."""
    if not _PRE_FILTER.search(text):
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    skip = _docstring_nodes(tree)
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in skip:
            continue
        s = node.value
        m = _PM_REMEDY.search(s)
        if m:
            hits.append((node.lineno, "pm-remedy", m.group(0)))
        for name in _PM_SENTINEL_NAMES:
            if name in s:
                hits.append((node.lineno, "pm-sentinel-name", name))
    return hits


def test_no_module_unlocks_or_advises_only_by_a_pm_act():
    offenders = []
    for path in _module_files():
        for lineno, kind, snippet in _violations(path.read_text(encoding="utf-8", errors="replace")):
            offenders.append(f"{path.relative_to(_ROOT).as_posix()}:{lineno} [{kind}] {snippet!r}")
    assert not offenders, (
        "PM-only unlock or PM-approval remedy found; convert to an advisory, or route an "
        "irreversible class to the G-EM (docs/decisions/DR-NO-PM-UNLOCKS.md):\n  "
        + "\n  ".join(offenders)
    )


def test_walk_reaches_every_registered_guard_module():
    from coordinator_core.bash_guards import dispatch

    walked = {p.stem for p in _module_files()}
    src = Path(dispatch.__file__).read_text(encoding="utf-8")
    registered = set(re.findall(r"\b((?:block|check|guard|nudge|bump)_[a-z0-9_]+)\b", src))
    names = {n for n in registered if (_ROOT / "bash_guards" / f"{n}.py").exists()}
    assert names, "dispatch registry yielded no guard modules"
    assert names <= walked, sorted(names - walked)
    assert {p.stem for p in (_ROOT / "write_guards").glob("*.py")} - {"__init__"} <= walked


@pytest.mark.parametrize(
    "message",
    [
        "Describe the proposed change to the PM and ask them to approve it before editing.",
        "BLOCKED: this edit grows the doc. Ask the PM to change it.",
        "Only the PM can grant a venv mid-session.",
        "Ask the PM for a Tier-U authorization grant",
        "the PM creates the marker",
        "needs per-session PM assent",
        "the EM obtains PM approval, then runs claude_md_grant",
        "re-assemble and get a fresh PM stamp",
        'tier-u-grant-cli grant pm "quote"',
    ],
)
def test_matcher_flags_known_bad_phrasings(message):
    assert _violations(f"X = {message!r}\n")


@pytest.mark.parametrize(
    "message",
    [
        "Force-push rewrites published history; the G-EM unlocks it.",
        "advisory: stay within the always-loaded byte budget; re-read the diff for unprompted rewrites.",
    ],
)
def test_matcher_passes_gem_routed_and_advisory_text(message):
    assert not _violations(f"X = {message!r}\n")


def test_docstring_and_comment_mentions_are_not_deny_paths():
    src = '"""Ask the PM."""\n# ask the PM\ndef f():\n    """only the PM"""\n    return 1\n'
    assert not _violations(src)

"""Ratchet: every engine leg spawn site passes `**tenant_priority.popen_kwargs(argv)` to its Popen.

A leg is a batch child the engine starts for a codex, agy or grok turn. On a prime-tenant box the
kwargs lower it; off-prime or for a serve-lineage argv they are empty. A new Popen under codex_runner/ or agent_pipe/ must join SITES,
or EXEMPT with the reason it needs no kwargs.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1]

SITES = (
    "ops/workflow_fire/headless_agents.py",
    "codex_runner/workflow.py",
    "codex_runner/execute.py",
    "codex_runner/review.py",
    "codex_runner/agy.py",
    "codex_runner/grok.py",
    "agent_pipe/reviewer_run.py",
)

EXEMPT = {
    "codex_runner/sandbox_preflight.py": "short `codex sandbox` probe, not a leg",
    "agent_pipe/acp.py": "engine-class bridge child; runs at engine priority",
    "agent_pipe/codex_app_server.py": "engine-class bridge child; runs at engine priority",
    "agent_pipe/bridge.py": "engine-class bridge child; runs at engine priority",
    "agent_pipe/peer.py": "engine-class peer; runs at engine priority",
    "agent_pipe/__main__.py": "engine-class bridge entry; runs at engine priority",
}

_SCANNED_DIRS = ("codex_runner", "agent_pipe")


def _popen_calls(tree: ast.AST) -> list[ast.Call]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if (isinstance(fn, ast.Name) and fn.id == "Popen") or (isinstance(fn, ast.Attribute) and fn.attr == "Popen"):
                out.append(node)
    return out


def _carries_popen_kwargs(call: ast.Call) -> bool:
    """A `**tenant_priority.popen_kwargs(argv)` splat whose argument is the Popen's own first argument.

    The serve-lineage skip reads that argv, so a different expression would defeat it.
    """
    first = ast.dump(call.args[0]) if call.args else None
    if first is None:
        argv_kw = next((k for k in call.keywords if k.arg == "args"), None)
        first = ast.dump(argv_kw.value) if argv_kw is not None else None
    for kw in call.keywords:
        v = kw.value
        if (
            kw.arg is None
            and isinstance(v, ast.Call)
            and isinstance(v.func, ast.Attribute)
            and v.func.attr == "popen_kwargs"
            and isinstance(v.func.value, ast.Name)
            and v.func.value.id == "tenant_priority"
            and len(v.args) == 1
            and first is not None
            and ast.dump(v.args[0]) == first
        ):
            return True
    return False


def _unlowered(tree: ast.AST) -> list[int]:
    return sorted(c.lineno for c in _popen_calls(tree) if not _carries_popen_kwargs(c))


def _is_production(path: Path) -> bool:
    rel = path.relative_to(CORE).parts
    return not ("tests" in rel[:-1] or "fixtures" in rel[:-1] or "__pycache__" in rel or path.name.startswith("test_"))


def _tree(rel: str) -> ast.AST:
    path = CORE / rel
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.mark.parametrize("site", SITES)
def test_site_has_a_popen_and_every_one_carries_popen_kwargs(site: str) -> None:
    tree = _tree(site)
    assert _popen_calls(tree), f"{site} is in SITES but spawns no Popen; drop it"
    assert not _unlowered(tree), f"{site} Popen without **tenant_priority.popen_kwargs(argv) at lines {_unlowered(tree)}"


def test_no_unlisted_leg_spawn_site() -> None:
    known = {*SITES, *EXEMPT}
    unlisted = sorted(
        p.relative_to(CORE).as_posix()
        for d in _SCANNED_DIRS
        for p in (CORE / d).rglob("*.py")
        if _is_production(p)
        and p.relative_to(CORE).as_posix() not in known
        and _popen_calls(ast.parse(p.read_text(encoding="utf-8")))
    )
    assert not unlisted, f"Popen sites missing from SITES or EXEMPT: {unlisted}"


@pytest.mark.parametrize("exempt", sorted(EXEMPT))
def test_exemptions_are_live(exempt: str) -> None:
    assert _popen_calls(_tree(exempt)), f"{exempt} no longer spawns; drop its EXEMPT entry"


@pytest.mark.parametrize(
    "src,expected",
    [
        ("import subprocess\nsubprocess.Popen(a, **tenant_priority.popen_kwargs(a))\n", []),
        ("import subprocess\nsubprocess.Popen(a, env=e, **tenant_priority.popen_kwargs(a))\n", []),
        ("import subprocess\nsubprocess.Popen(a, env=e)\n", [2]),
        ("import subprocess\nsubprocess.Popen(a, **other.popen_kwargs(a))\n", [2]),
        ("import subprocess\nsubprocess.Popen(a, **{'preexec_fn': f})\n", [2]),
        ("import subprocess\nsubprocess.Popen(a, **tenant_priority.popen_kwargs(b))\n", [2]),
        ("import subprocess\nsubprocess.Popen(args=a, **tenant_priority.popen_kwargs(a))\n", []),
    ],
)
def test_scan_accepts_lowered_and_rejects_bare_spawns(src: str, expected: list[int]) -> None:
    assert _unlowered(ast.parse(src)) == expected

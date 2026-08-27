"""AST ratchet: the eol op family never resolves `target_root` from the environment.

Clone of `coordinator_core/tests/test_warm_identity_env_reads.py`, scoped to the
`eol` op family and to the ROOT env names a warm-served op could be tempted to
fall back on: `CLAUDE_KLABAUTER_ROOT`, `COORDINATOR_ENGINE_ROOT`, `COORDINATOR_ROOT`,
`CLAUDE_PROJECT_DIR`.

WHY AST, NOT BEHAVIOURAL -- same reason as the model test, verbatim: `cc_invoke`
spawns the COLD subprocess with the caller's own `os.environ`, so a raw env read
is CORRECT on that path. Every in-process reproduction, unit test and isolated-
settings-home probe therefore passes. The defect is reachable only through a
live warm server, so a source-shape ratchet is the artifact that survives it --
and it costs no subprocess, staying on the fast tier.

TWO LEGS, BOTH NEEDED -- the scanner's two legs, not two ops. (Written when the
family was three ops; `eol.repair` is now the only registered id and `census.py`
is its library, K-062. The AC and both legs are unchanged by that collapse --
`census()` still takes the root as an argument, so the shape this ratchet
refuses is still reachable.) The family resolves `target_root` exclusively from
the wire param, by design (this plan's AC7, "Do not resolve the target root from
the environment"). The second leg is the
one that bites: the laundering shape seen elsewhere in this repo is a module
constant (e.g. `_CLAUDE_KLABAUTER_ROOT_ENV = "CLAUDE_KLABAUTER_ROOT"`) plus a bare read elsewhere --
scanning for `os.environ` calls alone misses a constant-name read entirely,
since the literal never appears at the read site.

PARAMETERIZED OVER A FAMILY-PREFIX LIST, not hard-coded to `eol.` -- C13's
deferred fleet-wide guard widens this scanner by passing a longer cohort of
prefixes, instead of duplicating the AST walk.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: The root-resolution vars a warm-served eol op must never read directly.
ROOT_ENV_NAMES = frozenset(
    {"CLAUDE_KLABAUTER_ROOT", "COORDINATOR_ENGINE_ROOT", "COORDINATOR_ROOT", "CLAUDE_PROJECT_DIR"}
)

#: Family prefixes this ratchet governs today. A prefix is a repo-relative,
#: forward-slash directory prefix under `coordinator_core/`. C13 widens this
#: scanner for a fleet-wide cohort by passing a longer tuple here (or an
#: equivalent one built at its own call site) rather than re-implementing the
#: walk below.
EOL_FAMILY_PREFIXES = ("coordinator_core/ops/eol/",)

REPO_ROOT = Path(__file__).resolve().parents[4]


def _family_modules(repo_root: Path, prefixes: tuple[str, ...]) -> list[Path]:
    """Every `.py` file under `repo_root` whose repo-relative path (forward
    slashes) starts with one of `prefixes`, excluding `tests/` and caches.
    """
    out: list[Path] = []
    for prefix in prefixes:
        base = repo_root / prefix
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            rel = path.relative_to(repo_root).as_posix()
            if "/tests/" in f"/{rel}" or "__pycache__" in rel:
                continue
            out.append(path)
    return sorted(out)


def _is_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _env_reads(path: Path, names: frozenset[str]) -> list[tuple[int, str]]:
    """(lineno, env_name) for every direct `os.environ.get/pop/[...]` read of a
    literal in `names`. Leg 1 -- the bare read.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "pop"}
            and _is_environ(node.func.value)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in names
        ):
            hits.append((node.lineno, node.args[0].value))
        elif (
            isinstance(node, ast.Subscript)
            and _is_environ(node.value)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in names
        ):
            hits.append((node.lineno, node.slice.value))
    return hits


def _env_name_constants(path: Path, names: frozenset[str]) -> list[tuple[int, str]]:
    """(lineno, env_name) for every assignment of a literal in `names` to a
    bare name, at any scope. Leg 2 -- the laundering shape: a module constant
    like `_CLAUDE_KLABAUTER_ROOT_ENV = "CLAUDE_KLABAUTER_ROOT"` (or any local alias of the same
    shape) that a later `os.environ.get(<the name>)` reads without the literal
    ever appearing at the read site, so leg 1 alone would miss it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.Constant) and node.value.value in names):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                hits.append((node.lineno, node.value.value))
    return hits


@pytest.mark.parametrize("prefixes", [EOL_FAMILY_PREFIXES], ids=["eol"])
def test_family_has_modules(prefixes: tuple[str, ...]) -> None:
    """A prefix matching nothing is a silently disarmed ratchet."""
    assert _family_modules(REPO_ROOT, prefixes), (
        f"no modules found under {prefixes} -- the eol root-env ratchet is inert. "
        "The family moved or was renamed; update EOL_FAMILY_PREFIXES."
    )


@pytest.mark.parametrize("prefixes", [EOL_FAMILY_PREFIXES], ids=["eol"])
def test_family_reads_no_root_env_directly(prefixes: tuple[str, ...]) -> None:
    """No module in the family reads a root-resolution env var via `os.environ`."""
    offenders: list[str] = []
    for path in _family_modules(REPO_ROOT, prefixes):
        hits = _env_reads(path, ROOT_ENV_NAMES)
        if hits:
            rel = path.relative_to(REPO_ROOT).as_posix()
            offenders.append(
                f"{rel}: " + ", ".join(f"line {ln} ({name})" for ln, name in hits)
            )
    assert offenders == [], (
        "eol op(s) resolve target_root from the environment directly:\n"
        + "\n".join(offenders)
        + "\n\ntarget_root must come from the wire param only (AC7) -- inside a "
        "warm-served dispatch os.environ names whoever spawned the server, not "
        "the caller."
    )


@pytest.mark.parametrize("prefixes", [EOL_FAMILY_PREFIXES], ids=["eol"])
def test_family_declares_no_root_env_name_constant(prefixes: tuple[str, ...]) -> None:
    """No module in the family launders a root env-var name through a constant.

    This is the shape that bites: `_CLAUDE_KLABAUTER_ROOT_ENV = "CLAUDE_KLABAUTER_ROOT"` plus a bare
    read elsewhere passes leg 1 (no literal at the read site) but is caught here.
    """
    offenders: list[str] = []
    for path in _family_modules(REPO_ROOT, prefixes):
        hits = _env_name_constants(path, ROOT_ENV_NAMES)
        if hits:
            rel = path.relative_to(REPO_ROOT).as_posix()
            offenders.append(
                f"{rel}: " + ", ".join(f"line {ln} ({name})" for ln, name in hits)
            )
    assert offenders == [], (
        "eol op(s) declare a root env-var name as a local/module constant:\n"
        + "\n".join(offenders)
        + "\n\nthis is the laundering shape a bare os.environ scan misses -- "
        "target_root must come from the wire param only (AC7)."
    )

"""AST ratchet: no module under `coordinator_core/` or `coordinator/bin/`
resolves the retired `CLAUDE_KLABAUTER_ROOT` engine-root env var directly -- it must
route through `coordinator_core.engine_root.coordinator_engine_root_env()`.

Clone of `coordinator_core/ops/eol/tests/test_eol_root_env_reads.py`, whose
own docstring names why AST and not behaviour, and why scanning for
`os.environ` calls alone misses a constant-name read -- both apply here
verbatim, so legs 1 and 2 below are copied rather than re-derived:

WHY AST, NOT BEHAVIOURAL. A raw env read is CORRECT wherever the caller
genuinely owns the process environment (e.g. a cold subprocess spawn
inheriting the caller's own `os.environ`); the defect this ratchet guards
against is a warm-served op reading a value that names whoever spawned the
long-lived server, not the current caller. Every in-process reproduction and
isolated-settings-home probe passes regardless -- a source-shape ratchet is
the artifact that survives that, and it costs no subprocess, staying on the
fast tier.

THREE LEGS. The laundering shapes seen across this repo:
  Leg 1 -- a bare `os.environ.get/pop/[...]("CLAUDE_KLABAUTER_ROOT")` read.
  Leg 2 -- a module constant (`_CLAUDE_KLABAUTER_ROOT_ENV = "CLAUDE_KLABAUTER_ROOT"`) plus a bare
    `os.environ.get(_CLAUDE_KLABAUTER_ROOT_ENV, ...)` read elsewhere -- leg 1 alone
    misses this because the literal never appears at the read site.
  Leg 3 -- the retired literal appearing ANYWHERE else in executable code
    (e.g. `for _var in ("COORDINATOR_ENGINE_ROOT", "CLAUDE_KLABAUTER_ROOT"): ... =
    os.environ.get(_var, "")` -- a tuple element read through a loop
    variable, so neither leg 1 (no literal at the read site) nor leg 2 (no
    assignment of the literal to a bare Name) sees it).
Leg 3 strictly subsumes legs 1 and 2 as a PRESENCE check. Legs 1 and 2 stay
because they produce a specific, actionable "this is a read" / "this is a
name-constant" message; leg 3 alone can only say "the literal is here
somewhere" and still needs a human to classify why. That classification is
exactly what `EXCLUDED_PATHS` below records once, per file, so leg 3 does
not have to re-litigate it on every run.

WORD BOUNDARIES ARE EXACT-LITERAL, not substring, on ALL THREE legs: each
leg matches a Python string/name constant EQUAL to `"CLAUDE_KLABAUTER_ROOT"`, never a
substring search. `CLAUDE_KLABAUTER_ROOT_SKEW_QUIET`/`CLAUDE_KLABAUTER_ROOT_SKEW_VERBOSE`
(advisory kill-switches in `coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py`)
are DIFFERENT string constants and never trip this test -- an earlier census
wrongly condemned that whole file over exactly this conflation. The same
exact-equality property is why prose sentences, docstrings, and error
messages that merely MENTION the name (e.g. an f-string ending
"...verify CLAUDE_KLABAUTER_ROOT") do not trip leg 3 either: implicit adjacent-string
concatenation (including an f-string concatenated with plain string
literals) folds into ONE ast.Constant holding the full joined text at parse
time, which is never equal to the bare four-character-shorter literal —
verified empirically against this repo's own handoff-*.py remediation
strings, not merely asserted.

Spec backlink: docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md § C23.
C23 first landed this ratchet scoped to `coordinator_core/ops/` (the eight
modules it swept: two sites in `queue_append`, one site each in
`deliverable_rollup`, `check_weekly_staleness`, `check_arch_audit_staleness`,
`list_review_trail_records`, `list_week_changelog`,
`setup_seed_health_ledger`, `workday_complete_backfill_scan`), then widened
it to full `coordinator_core/` + `coordinator/bin/` scope in the same chunk
after sweeping/carving the remaining candidates leg 3 uncovered: two more
read sites (`coordinator_core/orientation/regenerate_cache.py` -- two
sites, `coordinator_core/roadmap/audit.py`), three more read sites in
`coordinator/bin/` (`reap-integrated-review-findings.py`'s two-name
tuple-loop, `coordinator-lesson-add.py`, `repo-setup-args-and-register.py`'s
previously-dark exec-summary fallback which had NO new-name rung at all),
and one AC13-style bootstrap carve-out (`coordinator/bin/lib/cli_shared.py`
-- see `EXCLUDED_PATHS`).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: The one retired root-resolution var name this ratchet governs.
ROOT_ENV_NAMES = frozenset({"CLAUDE_KLABAUTER_ROOT"})

#: Family prefixes this ratchet governs -- widened from `coordinator_core/ops/`
#: (C23's first landing) to full engine + CLI scope in the same chunk, once
#: everything leg 3 turned up under it was swept or carved out (see module
#: docstring). A later widening pass (e.g. over `coordinator/lib/`) extends
#: this tuple, or builds an equivalent one at its own call site, rather than
#: re-implementing the walk below.
FAMILY_PREFIXES = ("coordinator_core/", "coordinator/bin/", "bin/", "scripts/")

#: Named, by-path exceptions -- repo-relative, forward-slash paths, each with
#: the one-line reason it does not need to route through the accessor.
EXCLUDED_PATHS: dict[str, str] = {
    "coordinator_core/engine_root.py": (
        "the accessor itself -- reads the retired name only to report it as "
        "retired (coordinator_engine_root_env), never to answer with it."
    ),
    "coordinator/bin/lib/cc_invoke.py": (
        "AC13 bootstrap carve-out (named in the plan) -- resolving the engine "
        "root is what this module does, so it cannot import the accessor to "
        "find it; the precedence is duplicated by hand instead."
    ),
    "coordinator/bin/lib/cli_shared.py": (
        "C23 AC13-style bootstrap carve-out -- DoE-resident CLI plumbing "
        "consumed by the legacy State-1 CLIs (coordinator-queue-append, "
        "coordinator-lesson-promote, coordinator-harvest-deferrals, "
        "regen-cockpit-schema, klabauter-channel) that must keep working "
        "before coordinator_core is importable; claude_klabauter_root() IS the "
        "primitive those callers use to find where coordinator_core lives, "
        "so importing the accessor here is the same chicken-and-egg cc_invoke's "
        "own AC13 rung exists to avoid. Precedence duplicated by hand."
    ),
    "coordinator/bin/classify-engine-root-residue.py": (
        "inventory/classification tool -- the literal is a SCAN TARGET "
        "(data), never a value-read; this script never resolves an engine "
        "root for its own use."
    ),
    "coordinator/bin/classify-env-var-callers.py": (
        "bucket-enumerator tool -- the literal is a SCAN TARGET (data, used "
        "to build regex patterns for a report), never a value-read."
    ),
    "coordinator_core/message_register/_codename_classes.py": (
        "publish-transform codename/redaction classification table -- the "
        "literal is a dict KEY (data), never a value-read."
    ),
    "coordinator_core/percolate/codename_provenance_seed.py": (
        "publish-transform name-mapping seed table -- the literal is a dict "
        "KEY/VALUE (data), never a value-read."
    ),
    "coordinator/bin/regen-cockpit-schema.py": (
        "deliberately EXPORTS both COORDINATOR_ENGINE_ROOT and CLAUDE_KLABAUTER_ROOT to "
        "a child process's environment during the rename window (C11's "
        "dual-write shape) -- a write site, not a read site."
    ),
    "coordinator/bin/append-goal-event.py": (
        "deliberately EXPORTS both COORDINATOR_ENGINE_ROOT and CLAUDE_KLABAUTER_ROOT to "
        "a child process's environment during the rename window -- a write "
        "site, not a read site."
    ),
    "coordinator/bin/coordinator-compute-layer-scaffold.py": (
        "error-string marker -- `\"CLAUDE_KLABAUTER_ROOT\" in msg` classifies a "
        "transport failure message a downstream engine might still emit; "
        "not a value-read of the caller's own environment."
    ),
    "coordinator/bin/coordinator-workflow-scaffold.py": (
        "error-string marker -- `\"CLAUDE_KLABAUTER_ROOT\" in msg` classifies a "
        "transport failure message a downstream engine might still emit; "
        "not a value-read of the caller's own environment."
    ),
    "bin/claude-klabauter-doctor-probe.py": (
        "C23 second-rung carve-out. Rung 1 reads COORDINATOR_ENGINE_ROOT "
        "first; the retired name is kept as an explicitly-labelled second "
        "rung because this is a DIAGNOSTIC run against boxes in unknown "
        "states, and a probe that cannot see a stale pin cannot report one. "
        "Before C23 this rung read the retired name and NOTHING else, so it "
        "went dark at C14 -- the tool for diagnosing a misresolved root was "
        "itself misresolving it."
    ),
    "scripts/setup.py": (
        "C23 second-rung carve-out, plus two deliberate dual-EXPORT sites. "
        "resolve_claude_klabauter_root() reads COORDINATOR_ENGINE_ROOT first and keeps "
        "the retired name as a labelled second rung because an INSTALLER runs "
        "against un-migrated boxes -- precisely the population still "
        "exporting the old spelling. install_bin_forwarders and "
        "install_claude_doe_launcher_chain additionally EXPORT both names to "
        "child environments (requested by doe-claude-em so their own fallback "
        "removal could land safely) -- write sites, not read sites."
    ),
}

REPO_ROOT = Path(__file__).resolve().parents[2]


def _family_modules(repo_root: Path, prefixes: tuple[str, ...]) -> list[Path]:
    """Every `.py` file under `repo_root` whose repo-relative path (forward
    slashes) starts with one of `prefixes`, excluding test modules
    (`tests/` directories and `test_*.py`/`*_test.py` files -- `ops/` keeps
    several `test_*.py` siblings alongside its production modules rather
    than in a `tests/` subdirectory), caches, and `EXCLUDED_PATHS`.
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
            if path.name.startswith("test_") or path.name.endswith("_test.py"):
                continue
            if rel in EXCLUDED_PATHS:
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


def _bare_literal_hits(path: Path, names: frozenset[str]) -> list[tuple[int, str]]:
    """(lineno, value) for every `ast.Constant` string literal ANYWHERE in the
    module whose value exactly equals one of `names`. Leg 3 -- catches any
    other laundering shape legs 1/2 miss, e.g. a retired name buried inside a
    tuple/list literal that is iterated at a read site (never itself
    assigned to a bare Name, so leg 2 misses it; no literal at the actual
    `os.environ.get(...)` call, so leg 1 misses it too).

    Exact-equality (not substring) means prose, docstrings, and error
    messages that merely mention the name do not trip this -- see module
    docstring § "WORD BOUNDARIES".
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in names:
            hits.append((node.lineno, node.value))
    return hits


@pytest.mark.parametrize("prefixes", [FAMILY_PREFIXES], ids=["engine+bin"])
def test_family_has_modules(prefixes: tuple[str, ...]) -> None:
    """A prefix matching nothing is a silently disarmed ratchet."""
    assert _family_modules(REPO_ROOT, prefixes), (
        f"no modules found under {prefixes} -- the retired-root-env ratchet is "
        "inert. The family moved or was renamed; update FAMILY_PREFIXES."
    )


@pytest.mark.parametrize("prefixes", [FAMILY_PREFIXES], ids=["engine+bin"])
def test_family_reads_no_root_env_directly(prefixes: tuple[str, ...]) -> None:
    """No module in the family reads the retired `CLAUDE_KLABAUTER_ROOT` var via `os.environ`.

    A hit here means the retired name has crept back into a value-read path:
    a caller is trusting `os.environ.get("CLAUDE_KLABAUTER_ROOT", ...)` for the answer
    instead of going through `coordinator_core.engine_root.coordinator_engine_root_env`,
    the one seam the C10/C14/C23 rename converged on. Fix it by routing the
    call site through that accessor (pass a short stable `site` tag, e.g. the
    module's `__name__`) -- never by re-adding a raw `os.environ` read of the
    old name.
    """
    offenders: list[str] = []
    for path in _family_modules(REPO_ROOT, prefixes):
        hits = _env_reads(path, ROOT_ENV_NAMES)
        if hits:
            rel = path.relative_to(REPO_ROOT).as_posix()
            offenders.append(
                f"{rel}: " + ", ".join(f"line {ln} ({name})" for ln, name in hits)
            )
    assert offenders == [], (
        "module(s) read the retired CLAUDE_KLABAUTER_ROOT env var directly instead of "
        "through coordinator_core.engine_root.coordinator_engine_root_env:\n"
        + "\n".join(offenders)
        + "\n\nRoute the read through the accessor -- it is the one seam every "
        "reader/writer of the engine-root var is required to use."
    )


@pytest.mark.parametrize("prefixes", [FAMILY_PREFIXES], ids=["engine+bin"])
def test_family_declares_no_root_env_name_constant(prefixes: tuple[str, ...]) -> None:
    """No module in the family launders the retired root env-var name through
    a constant.

    This is the shape that bites: `_CLAUDE_KLABAUTER_ROOT_ENV = "CLAUDE_KLABAUTER_ROOT"` plus a
    bare read elsewhere passes leg 1 (no literal at the read site) but is
    caught here. A hit means the same thing as the leg-1 failure above --
    the retired name has crept back into a value-read path -- fix it the
    same way, by routing through `coordinator_engine_root_env` and deleting
    the constant.
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
        "module(s) declare the retired CLAUDE_KLABAUTER_ROOT env-var name as a "
        "local/module constant:\n"
        + "\n".join(offenders)
        + "\n\nthis is the laundering shape a bare os.environ scan misses -- "
        "route through coordinator_core.engine_root.coordinator_engine_root_env "
        "and delete the constant."
    )


@pytest.mark.parametrize("prefixes", [FAMILY_PREFIXES], ids=["engine+bin"])
def test_family_contains_no_bare_root_env_literal(prefixes: tuple[str, ...]) -> None:
    """No module in the family contains the retired CLAUDE_KLABAUTER_ROOT literal
    anywhere in executable code, outside a named `EXCLUDED_PATHS` exception.

    Leg 3 -- subsumes legs 1 and 2 as a presence check; it exists for the
    laundering shape neither of them sees, e.g.
    `for _var in ("COORDINATOR_ENGINE_ROOT", "CLAUDE_KLABAUTER_ROOT"): os.environ.get(_var)`
    (a tuple element read through a loop variable). A hit here means the
    retired name has crept back into this file in SOME form that reads as a
    value-read, a laundered constant, or an unclassified new shape -- the
    fix is either to route the read through
    `coordinator_core.engine_root.coordinator_engine_root_env`, or, if the
    literal is genuinely data (a classification table, an error-string
    marker) or a write/export site or a bootstrap carve-out, to add the file
    to `EXCLUDED_PATHS` above with a one-line reason -- never to leave it
    unclassified.
    """
    offenders: list[str] = []
    for path in _family_modules(REPO_ROOT, prefixes):
        hits = _bare_literal_hits(path, ROOT_ENV_NAMES)
        if hits:
            rel = path.relative_to(REPO_ROOT).as_posix()
            offenders.append(
                f"{rel}: " + ", ".join(f"line {ln} ({name})" for ln, name in hits)
            )
    assert offenders == [], (
        "module(s) contain the retired CLAUDE_KLABAUTER_ROOT literal in executable code, "
        "outside a named EXCLUDED_PATHS exception:\n"
        + "\n".join(offenders)
        + "\n\nRoute the read through "
        "coordinator_core.engine_root.coordinator_engine_root_env, or add a "
        "named EXCLUDED_PATHS entry with a one-line reason if it is genuinely "
        "data, an export site, or a bootstrap carve-out."
    )


def test_leg3_catches_the_tuple_loop_laundering_shape_that_legs_1_and_2_miss(
    tmp_path: Path,
) -> None:
    """Self-test: a scanner nobody has watched fail is not yet a scanner.

    Plants the real defect this leg was added for --
    `coordinator/bin/reap-integrated-review-findings.py`'s
    `for _var in ("COORDINATOR_ENGINE_ROOT", "CLAUDE_KLABAUTER_ROOT"): os.environ.get(_var, "")`
    -- into a synthetic file and asserts leg 3 reports a hit on it, while
    confirming legs 1 and 2 do NOT (that gap is exactly why leg 3 exists).
    """
    synthetic = tmp_path / "tuple_loop_laundering.py"
    synthetic.write_text(
        "import os\n"
        "\n"
        "def _reap_seam_present():\n"
        "    for _var in (\"COORDINATOR_ENGINE_ROOT\", \"CLAUDE_KLABAUTER_ROOT\"):\n"
        "        _override = os.environ.get(_var, \"\")\n"
        "        if _override:\n"
        "            return _override\n"
        "    return None\n",
        encoding="utf-8",
    )

    assert _env_reads(synthetic, ROOT_ENV_NAMES) == [], (
        "leg 1 must NOT catch the tuple-loop shape -- no literal appears at "
        "the os.environ.get(...) call site itself (it reads a loop variable). "
        "If this now fails, the fixture no longer represents leg 3's reason "
        "to exist and needs updating alongside it."
    )
    assert _env_name_constants(synthetic, ROOT_ENV_NAMES) == [], (
        "leg 2 must NOT catch the tuple-loop shape -- the literal is a tuple "
        "ELEMENT, never assigned to a bare Name. If this now fails, the "
        "fixture no longer represents leg 3's reason to exist and needs "
        "updating alongside it."
    )
    leg3_hits = _bare_literal_hits(synthetic, ROOT_ENV_NAMES)
    assert leg3_hits, (
        "leg 3 failed to catch the tuple-loop laundering shape it exists "
        "for -- this scanner has gone inert. It was verified live against "
        "coordinator/bin/reap-integrated-review-findings.py before this "
        "test existed; do not remove this test without another equally "
        "concrete live-shape check replacing it."
    )
    assert leg3_hits[0][1] == "CLAUDE_KLABAUTER_ROOT"

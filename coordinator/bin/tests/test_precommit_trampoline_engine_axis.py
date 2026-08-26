"""The pre-commit installer trampoline resolves the engine it can actually import.

Regression for a break-class defect measured 2026-08-26 on a dogfooded
`scripts/setup.py --i-am-agent` reinstall: `install-claude-klabauter-precommit-hook.py`
resolved its engine on the DISPATCH axis (`require_dispatch_engine_on_path`),
which on any box with a published mirror installed answers
`/…/claude-klabauter`, not this working tree. The publish transform renames the
op it dispatches — `install_claude_klabauter_precommit_hook.py` ->
`install_claude_klabauter_precommit_hook.py`, per the `basename_rename` table in
`setup/percolate-hooks/percolate-store.yaml` — so the module name the trampoline
spells exists ONLY in the tree the trampoline itself ships in. Every run on such
a box died at `run_op_main` with

    coordinator_core.ops.install_claude_klabauter_precommit_hook not importable

and the pre-commit gate silently never installed; setup.py degraded it to one
`[ADVISORY]` line.

WHAT THIS FILE PINS, in the order the defect has to be caught:

  AC-axis   The general rule, hermetically: a `coordinator/bin` trampoline whose
            dispatched op module is renamed by the publish store must not be on
            the dispatch axis. Box-independent — it reads the store, not the
            machine's engine-root registry — so it fails on a publisher box and
            a stranger's box alike, and it catches the NEXT trampoline written
            by copying the nearest sibling, not just this one.

  AC-import The specific claim, against the real ladder: the engine root this
            trampoline actually resolves contains the op module it actually
            names. This one is box-SENSITIVE by construction (it exercises the
            resolver, which is a property of the box) — that is the point; it is
            the assertion that was false on the machine where the defect was
            found.

WHAT THIS DOES NOT ASSERT. Not "no trampoline may use the dispatch axis" — the
sibling installers (`install-doe-claude-`, `install-meta-repo-`,
`install-publish-repo-precommit-hook`) install into OTHER repos, take their
identity from the target rather than from `__file__`, and carry no publish-time
rename. AC-axis passes them unchanged, and a change that moved them onto the
locator axis would repoint them at the working tree — a fleet behaviour change
this file has no opinion on and must not be read as endorsing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"
_REPO_ROOT = _TESTS_DIR.parents[2]

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke  # noqa: E402  (import after path setup)

_STORE = _REPO_ROOT / "setup" / "percolate-hooks" / "percolate-store.yaml"

#: The population, discovered — not one hardcoded path. The original subject
#: (`install-claude-klabauter-precommit-hook.py`) was deleted at 65508c924 ("C3: delete
#: staged-rollback and precommit-hook ops"), after which this file's AC-import
#: test raised FileNotFoundError on every run instead of asserting anything: a
#: guard pointed at a corpse. The JOB survives the subject, so it is the job
#: that is re-anchored here — every bin CLI dispatching a publish-renamed op,
#: which is the same population AC-axis above already sweeps.

#: `run_op_main("coordinator_core.ops.<name>", ...)` — the dispatched op module,
#: read out of the trampoline source rather than hardcoded, so this file tracks a
#: rename of the op instead of going quietly vacuous after one.
_OP_MODULE_RE = re.compile(r"[\"']coordinator_core\.ops\.(?P<name>\w+)[\"']")

_DISPATCH_SEAM = "require_dispatch_engine_on_path"
_COLOCATED_SEAM = "require_colocated_engine_on_path"

#: A CALL of a seam, never a mention of one. Both fixed trampolines name the
#: dispatch seam in prose — in the negative-spec paragraph explaining why they
#: are off it — and a bare substring test would read its own remediation text as
#: the violation and never go green.
_DISPATCH_CALL_RE = re.compile(rf"\b{_DISPATCH_SEAM}\s*\(")
_COLOCATED_CALL_RE = re.compile(rf"\b{_COLOCATED_SEAM}\s*\(")

#: `- src: "<basename>"` inside the store's `basename_rename` sections. Parsed by
#: regex, not by yaml.safe_load: the store is a large publisher-side document
#: whose OTHER sections are none of this test's business, and the `src:` keys are
#: the only thing being read. Deliberately over-broad — it matches every `src:`
#: in the file, so a name that is renamed by ANY row counts as renamed, which is
#: the conservative direction for this guard.
_RENAME_SRC_RE = re.compile(r"^\s*-\s*src:\s*\"(?P<src>[^\"]+)\"", re.MULTILINE)


def _publish_renamed_basenames() -> set[str]:
    """Every basename the publish store renames on its way to the mirror.

    Returns an empty set when the store is absent — the published mirror does
    not carry the publisher-side store, and a test that ships to the mirror must
    degrade to a skip there rather than erroring on a missing file.
    """
    if not _STORE.is_file():
        return set()
    return {m.group("src") for m in _RENAME_SRC_RE.finditer(_STORE.read_text(encoding="utf-8"))}


def _dispatched_op_modules(source: str) -> set[str]:
    return {m.group("name") for m in _OP_MODULE_RE.finditer(source)}


def test_publish_renamed_op_trampolines_are_off_the_dispatch_axis() -> None:
    """AC-axis: a trampoline whose op is renamed by publish must self-locate.

    The dispatch axis answers "which engine executes on this box"; a renamed op
    module is spelled differently in that engine, so the two can only agree by
    accident (a box with no published mirror, where both axes return the same
    root). That accident is what made this defect invisible until a reinstall.
    """
    renamed = _publish_renamed_basenames()
    if not renamed:
        pytest.skip("publish store absent (published mirror) — nothing to cross-check")

    offenders: list[str] = []
    for script in sorted(_BIN_DIR.glob("*.py")):
        source = script.read_text(encoding="utf-8")
        if not _DISPATCH_CALL_RE.search(source):
            continue
        for op_name in _dispatched_op_modules(source):
            if f"{op_name}.py" in renamed:
                offenders.append(f"{script.name} -> coordinator_core.ops.{op_name}")

    assert not offenders, (
        "these trampolines dispatch an op module the publish store RENAMES, while "
        "resolving their engine on the dispatch axis — on any box whose dispatch "
        "root is the published mirror the module is there under its other name and "
        "the import fails unconditionally: "
        + "; ".join(sorted(offenders))
        + f". Route them onto the locator axis ({_COLOCATED_SEAM}) with the reason "
        "named at the call site, as install-claude-klabauter-precommit-hook.py does."
    )


def _renamed_op_trampolines() -> list[Path]:
    """Every bin CLI that dispatches an op the publish store renames."""
    renamed = _publish_renamed_basenames()
    if not renamed:
        return []
    return [
        script
        for script in sorted(_BIN_DIR.glob("*.py"))
        if any(f"{op}.py" in renamed for op in _dispatched_op_modules(script.read_text(
            encoding="utf-8", errors="replace")))
    ]


def test_renamed_op_trampolines_resolve_a_root_holding_their_own_op() -> None:
    """AC-import: the resolved engine root actually contains the dispatched op.

    Exercises the SAME resolver each trampoline names, discovered from its source
    rather than assumed, so the test cannot pass by checking an axis the file no
    longer uses.
    """
    if not _publish_renamed_basenames():
        pytest.skip("publish store absent (published mirror) — nothing to cross-check")

    trampolines = _renamed_op_trampolines()
    assert trampolines, (
        "no bin CLI dispatches a publish-renamed op any more. If that is real, this "
        "guard's subject is gone and the file should be retired deliberately — not "
        "left passing vacuously over an empty population."
    )

    for trampoline in trampolines:
        source = trampoline.read_text(encoding="utf-8", errors="replace")

        if _COLOCATED_CALL_RE.search(source):
            root = Path(cc_invoke.resolve_colocated_claude_klabauter_root(str(trampoline)))
        elif _DISPATCH_CALL_RE.search(source):
            root = Path(cc_invoke._resolve_claude_klabauter_root())
        else:  # pragma: no cover - a third axis would need its own reasoning here
            pytest.fail(
                f"{trampoline.name} resolves its engine root through neither known "
                "seam; this test must be taught the new one rather than deleted."
            )

        op_modules = _dispatched_op_modules(source)
        assert op_modules, f"{trampoline.name} dispatches no coordinator_core.ops module"

        for op_name in sorted(op_modules):
            module_file = root / "coordinator_core" / "ops" / f"{op_name}.py"
            assert module_file.is_file(), (
                f"{trampoline.name} dispatches coordinator_core.ops.{op_name}, but the "
                f"engine root it resolves ({root}) has no {module_file.relative_to(root)} "
                "— the import fails at run time and the pre-commit gate never installs. "
                "This is the published-mirror-vs-working-tree axis defect: the op is "
                "renamed by the publish transform, so it is only importable from the "
                "checkout this trampoline itself ships in."
            )

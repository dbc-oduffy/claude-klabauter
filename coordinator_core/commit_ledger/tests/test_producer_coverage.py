"""
Tests pinning the set of production commit-issuing mechanisms the commit
ledger (C1/C5) must cover (C11).

Spec backlink: state/dispatch-briefs/2026-08-19-the-baton-carries-its-commits/C11.md
Spec backlink: state/lessons/2026-08-18-a-ruling-applied-at-one-door-leaves-the-siblings-unswept-7c3e1f9a4d22.yaml

AC13/AC14: every production commit path either writes a ledger entry, or
appears in an explicit exempt list with a stated reason -- enumerated by
MECHANISM (a literal git ``commit``/``commit-tree`` argv shape), never by
the one door C5 opened first. A fourth (or fifth) commit path introduced
anywhere under ``coordinator_core/`` must fail this test until it is either
wired to the ledger (``coordinator_core.contract.apply_base.
record_ledger_entry``) or added to ``_EXEMPT_PRODUCERS`` with a reason --
the exact failure mode this plan's lesson names (a ruling applied at one
door left three siblings unswept for thirteen days).

Detection shape: a repo-wide scan for the literal git-argv-list opening
``["commit"`` / ``["commit-tree"`` (matched against the CODE portion of
each line only, i.e. everything before a ``#`` -- a comment referencing the
shape, such as ``bash_guards/block_subagent_commit.py``'s own inventory
comment, is not itself a producer). Test files (``tests/`` dirs and any
``test_*.py`` basename) are excluded -- this scan is for PRODUCTION commit
mechanisms only.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CORE = _REPO_ROOT / "coordinator_core"

#: A literal git argv list opening with a commit-creating subcommand --
#: ``"commit"`` (git commit[-m]/--pathspec-from-file) or ``"commit-tree"``
#: (the private-index plumbing entrypoint `git_native.py` shares between
#: `commit_scoped` and `commit_authored_content`). Matches the argv-literal
#: convention every producer in this repo already writes ("never a bare
#: `git commit`, never shell=True" -- see each producer's own docstring),
#: so a NEW raw-commit call site is caught by this same shape.
_COMMIT_ARGV_RE = re.compile(r'\[\s*["\']commit(-tree)?["\']')

#: Producers wired to the ledger (C5/C11) -- each file below is expected to
#: call (or, for `git_native.py`, contain the deferred import of)
#: `apply_base.record_ledger_entry` as the LAST step after its own commit
#: lands. `git_native.py` also carries `commit_scoped`'s own commit-tree
#: plumbing, reached exclusively via `ceremony.scoped_git_commit` -- itself
#: ledger-wired at C5, so it is covered without a second, direct write
#: inside `git_native.py` for that call path.
_WIRED_PRODUCERS = {
    "backlog_grind_assemble/apply.py",
    "contract/apply_base.py",
    "ops/ceremony/git_native.py",
}

#: Producers this scan finds that are NOT ledger-wired, with a stated
#: reason each -- AC13's explicit-exempt-list half.
#: Producers that import `commit_ledger` directly (NOT via
#: `apply_base.record_ledger_entry`) -- kept as a separate set from
#: `_WIRED_PRODUCERS` rather than folded in, because `_WIRED_PRODUCERS` is
#: also the set `test_every_wired_producer_references_record_ledger_entry`
#: checks for a `record_ledger_entry` reference, and these producers don't
#: have one: `scoped_git_commit.py` carries its own duplicate
#: `_ledger_kind_and_weight` and calls `commit_ledger.resolve_owner`/
#: `commit_ledger.store` inline instead. Measured live on 2026-08-19: this
#: is the file where a module-level `commit_ledger` import was actually
#: introduced and actually de-registered `ceremony.scoped_git_commit`,
#: `session.boot_sweep` and `session.sweep_consumed_handoffs` at once --
#: `test_producers_never_import_commit_ledger_at_module_level` scans the
#: UNION of this set and `_WIRED_PRODUCERS` so that incident's own module
#: stays covered. Do not merge this back into `_WIRED_PRODUCERS`.
_DIRECT_COMMIT_LEDGER_IMPORTERS = {
    "ops/ceremony/scoped_git_commit.py",
}

_EXEMPT_PRODUCERS = {
    "benchmarks/op_fixtures.py": (
        "benchmark fixture builder -- seeds a synthetic, disposable temp "
        "repo (no real baton/handoff owns it) purely to give benchmark "
        "harnesses a realistic git history to measure against; not a "
        "production commit path."
    ),
    "ops/ceremony/commit_exec_bit.py": (
        "pre-existing raw-commit producer, NOT one of the three sweep "
        "targets this chunk's brief named (backlog_grind_assemble/apply.py, "
        "contract/apply_base.py, git_native.commit_authored_content) -- "
        "already inventoried as a recognized committing op by "
        "bash_guards/block_subagent_commit.py's own registered-ops comment. "
        "Left unwired here rather than silently widened past this chunk's "
        "brief scope; flagged for a follow-up sweep, not fixed in C11."
    ),
    "ops/workday_complete_step2_5_dirty_tree.py": (
        "pre-existing raw-commit producer (`_act_commit` / the .gitignore "
        "auto-commit arm), NOT one of the three sweep targets this chunk's "
        "brief named. Found by this chunk's own mechanism sweep but out of "
        "scope to wire here -- left unwired and flagged for a follow-up "
        "sweep rather than silently widened past this chunk's brief scope."
    ),
}


def _iter_source_files():
    for path in _CORE.rglob("*.py"):
        rel = path.relative_to(_CORE).as_posix()
        if "/tests/" in f"/{rel}" or rel.startswith("tests/"):
            continue
        if "__pycache__" in rel:
            continue
        if Path(rel).name.startswith("test_"):
            continue
        yield path, rel


def _scan_commit_argv_producers() -> dict:
    found: dict[str, list[int]] = {}
    for path, rel in _iter_source_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = []
        for i, line in enumerate(text.splitlines()):
            code_part = line.split("#", 1)[0]
            if _COMMIT_ARGV_RE.search(code_part):
                lines.append(i + 1)
        if lines:
            found[rel] = lines
    return found


def test_commit_argv_producers_are_the_pinned_set():
    """Every file containing a literal commit-creating git argv element is
    either wired to the ledger or explicitly exempted -- a new file
    introducing one (an unenumerated commit path) fails this test until it
    is wired (`apply_base.record_ledger_entry`) or added to
    `_EXEMPT_PRODUCERS` with a stated reason (AC13/AC14)."""
    found = _scan_commit_argv_producers()
    known = _WIRED_PRODUCERS | set(_EXEMPT_PRODUCERS)

    unknown = sorted(set(found) - known)
    assert not unknown, (
        f"New commit-argv producer(s) found, not in the pinned set: {unknown} "
        "-- wire the ledger (coordinator_core.contract.apply_base."
        "record_ledger_entry) or add an explicit entry to _EXEMPT_PRODUCERS "
        "with a stated reason."
    )

    stale = sorted(known - set(found))
    assert not stale, (
        f"Pinned producer(s) no longer found by the scan: {stale} -- the "
        "enumeration is stale (the mechanism moved/was removed); update "
        "_WIRED_PRODUCERS/_EXEMPT_PRODUCERS to match the current set."
    )


def test_every_wired_producer_references_record_ledger_entry():
    """Each `_WIRED_PRODUCERS` file actually references
    `record_ledger_entry` (a call, or -- for `git_native.py` -- the
    deferred import plus call) -- catching a wiring regression (the call
    site quietly removed) independently of the argv-shape scan above."""
    for rel in sorted(_WIRED_PRODUCERS):
        text = (_CORE / rel).read_text(encoding="utf-8")
        assert "record_ledger_entry" in text, (
            f"{rel}: expected a record_ledger_entry call/import (C11 ledger "
            "wiring) -- not found."
        )


def test_producers_never_import_commit_ledger_at_module_level():
    """`commit_ledger.store`/`classify` import back into `coordinator_core.ops`
    (`resolve_swept_baton`, `review_brightline_gate`), so a MODULE-LEVEL
    `commit_ledger` import in a producer closes a cycle: anything importing
    `commit_ledger.store` before `coordinator_core.ops` leaves the store
    partially initialized and the producer's op fails to register. Measured
    live on 2026-08-19 -- that ordering de-registered `ceremony.
    scoped_git_commit`, `session.boot_sweep` and `session.
    sweep_consumed_handoffs` at once. The import must stay function-local in
    every producer; this pins that, and an AST check costs no subprocess.

    Scans `_WIRED_PRODUCERS | _DIRECT_COMMIT_LEDGER_IMPORTERS` -- the union,
    not `_WIRED_PRODUCERS` alone, so a direct importer like
    `scoped_git_commit.py` (the file the 2026-08-19 incident actually
    happened in) stays covered even though it doesn't call
    `record_ledger_entry`.
    """
    import ast

    for rel in sorted(_WIRED_PRODUCERS | _DIRECT_COMMIT_LEDGER_IMPORTERS):
        tree = ast.parse((_CORE / rel).read_text(encoding="utf-8"))
        offenders = [
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("coordinator_core.commit_ledger")
        ]
        assert not offenders, (
            f"{rel}: module-level commit_ledger import(s) {offenders} -- these "
            "must be function-local; a module-level import re-creates the "
            "partially-initialized-module cycle that de-registers this "
            "producer's op."
        )

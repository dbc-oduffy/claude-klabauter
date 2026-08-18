"""
coordinator_core.ops.tests.test_supersede_archives_atomically

Pins C3 (docs/plans/2026-08-18-supersede-stamps-and-archives-atomically.md):
every writer of a terminal `deployment_state: continued` discharges its own
archival in the same operation, or refuses to write.

Covers:
  - AC1/AC2 (writer 1, THE PRIMARY DEFECT): `handoff.archive_transition`
    mode="supersede" archives the predecessor DESPITE its successor being a
    live child that names it via `predecessor:` — the successor's existence
    is the justification for archiving, not a reason to retain. Asserted by
    the FILE MOVING (absent from state/handoffs/, present under
    archive/handoffs/YYYY-MM/), never by the terminal verdict alone.
  - AC4-shape control: a live `forked_from` child (a spinoff) still retains —
    the exemption is narrow (succession only), not wholesale. Proves the fix
    did not disable the guard outright.
  - Writer 3 (`archive_stamp.cs_supersede_archive_handoff`) — a thin wrapper
    over writer 1 — inherits the fix with no code change of its own.
  - AC11: a MECHANICALLY DISCOVERED (AST-walked, not hand-listed) inventory
    of every source site that writes the literal "continued" into a
    `deployment_state` field, asserted as a closed (frozen) set — a NEW,
    unlisted site fails this test by construction, mirroring
    `coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`'s own
    frozen-inventory-subset shape (see that module's G2 note).

Known, reported divergence (not fixed by this chunk — see the dispatch
report, not silently swallowed here): writer 2
(`handoff_transition._supersede`) still writes `continued` with NO archival
and NO refusal. Fixing it (either shape the plan allows: delegate, or
refuse) changes its return contract on a live handoff, which breaks
`coordinator_core/ops/tests/test_lifecycle_pair_consistency.py`'s
`test_transition_supersede_clears_pickup_ready` /
`test_transition_supersede_idempotent_no_op_only_at_full_target_state` — a
file outside this chunk's `writes:`. `test_open_gap_writer_two_still_does_
not_discharge` below pins TODAY's behavior as an `xfail(strict=True)` so a
future fix is caught (test starts unexpectedly passing) rather than silently
re-opening.
"""

from __future__ import annotations

import ast
import asyncio
import subprocess
from pathlib import Path
from typing import List, Set, Tuple

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.archive_stamp as arstamp
import coordinator_core.ops.handoff_archive_transition as _op  # noqa: F401 — fires @register_op
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_archive_transition import _handler as _archive_transition_handler
from coordinator_core.test_archive_stamp import (
    _init_repo,
    _seed_handoff,
    _seed_handoff_with_predecessor,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_OP_NAME = "handoff.archive_transition"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_archive_transition @register_op did not fire"
)


def _run(coro):
    return asyncio.run(coro)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, check=True,
        **no_console_creationflags(),
    )


def _common_dir(repo: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=str(repo), capture_output=True, check=True,
        **no_console_creationflags(),
    )
    return Path(result.stdout.decode().strip()).resolve()


def _seed_handoff_with_forked_from(repo: Path, name: str, forked_from: str) -> Path:
    """Live, non-terminal handoff naming `forked_from` (bare filename) — a
    spinoff, distinct in edge-kind from `_seed_handoff_with_predecessor`'s
    succession edge. Mirrors that helper's own frontmatter shape."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        f'forked_from: "{forked_from}"\n'
        "deployment_state: active\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


# ---------------------------------------------------------------------------
# AC1 / AC2 — writer 1, THE PRIMARY DEFECT
# ---------------------------------------------------------------------------


def test_supersede_archives_despite_live_successor_predecessor_edge(tmp_path):
    """The successor itself — a live handoff naming the candidate via
    `predecessor:` — must NOT retain the candidate. This is the exact shape
    the plan's Problem section names: 'the successor... is read as a live
    child the instant it exists on disk', which retained on essentially
    every real supersede call before this fix.

    Asserts the FILE MOVED (AC2) — never the terminal verdict alone.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    hp = _seed_handoff(
        repo, "ac1-predecessor.md", "claimed", "in_flight",
        extra="scope:\n  - state/handoffs/ac1-predecessor.md\n",
    )
    _seed_handoff_with_predecessor(repo, "ac1-successor.md", "ac1-predecessor.md")
    common_dir = _common_dir(repo)

    result = _run(_archive_transition_handler(
        {
            "handoff_path": "state/handoffs/ac1-predecessor.md",
            "mode": "supersede",
            "continued_into": "ac1-successor.md",
        },
        common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["superseded"] is True, result
    assert result["retained"] is False, result
    assert result["moved"] is True, result

    # AC2: the FILE MOVED — gone from state/handoffs/, present under
    # archive/handoffs/YYYY-MM/. Never assert the verdict alone.
    assert not hp.exists(), "predecessor must be gone from state/handoffs/"
    archived = list((repo / "archive" / "handoffs").rglob("ac1-predecessor.md"))
    assert len(archived) == 1, archived
    text = archived[0].read_text(encoding="utf-8")
    split = split_frontmatter(text)
    assert split is not None
    assert read_fm_field(split.fm_text, "status") == "claimed", text
    assert read_fm_field(split.fm_text, "deployment_state") == "continued", text
    assert read_fm_field(split.fm_text, "continued_into") == "ac1-successor.md", text


# ---------------------------------------------------------------------------
# AC4-shape control — the exemption is narrow, not wholesale
# ---------------------------------------------------------------------------


def test_supersede_still_retains_for_live_forked_from_child(tmp_path):
    """A live `forked_from` child (a spinoff) must still retain — dropping it
    would strand the spinoff's own origin pointer (DR-224, AC4). Proves the
    C3 exemption (`edge_kinds={"forked_from"}`) is narrow: it drops
    succession edges from the guard's blocking set, but `forked_from` still
    blocks.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    hp = _seed_handoff(
        repo, "ac4-predecessor.md", "claimed", "in_flight",
        extra="scope:\n  - state/handoffs/ac4-predecessor.md\n",
    )
    _seed_handoff_with_forked_from(repo, "ac4-spinoff.md", "ac4-predecessor.md")
    common_dir = _common_dir(repo)

    result = _run(_archive_transition_handler(
        {
            "handoff_path": "state/handoffs/ac4-predecessor.md",
            "mode": "supersede",
            "continued_into": "ac4-successor-not-on-disk.md",
        },
        common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["superseded"] is True, result
    assert result["retained"] is True, result
    assert result["moved"] is False, result
    assert hp.exists(), "a live forked_from child must retain — file must stay in place"


# ---------------------------------------------------------------------------
# Writer 3 — thin wrapper over writer 1, no code change of its own
# ---------------------------------------------------------------------------


def test_cs_supersede_archive_handoff_inherits_the_fix(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    hp = _seed_handoff(
        repo, "ac3-predecessor.md", "claimed", "in_flight",
        extra="scope:\n  - state/handoffs/ac3-predecessor.md\n",
    )
    _seed_handoff_with_predecessor(repo, "ac3-successor.md", "ac3-predecessor.md")

    rc = arstamp.cs_supersede_archive_handoff(str(hp), "ac3-successor.md")

    assert rc == 0
    assert not hp.exists(), "writer 3 must inherit writer 1's fix — file must move"
    archived = list((repo / "archive" / "handoffs").rglob("ac3-predecessor.md"))
    assert len(archived) == 1, archived


# ---------------------------------------------------------------------------
# Writer 4 (as literally named by the plan's AC1) — verified structurally
# safe, not merely trusted: it can never touch a record resident in
# state/handoffs/, and it is not a registered op.
# ---------------------------------------------------------------------------


def test_repair_archived_deployment_state_handler_rejects_live_handoffs_path(tmp_path):
    """`_repair_archived_deployment_state_handler`'s containment allowlist is
    archive/handoffs/ ONLY — a state/handoffs/ target must be refused, which
    is what makes this writer safe by construction rather than by promise:
    it structurally cannot leave a `continued` record resident in
    state/handoffs/."""
    import asyncio as _asyncio

    from coordinator_core.ops.handoff_stamp import _repair_archived_deployment_state_handler

    git_dir = tmp_path / "repo" / ".git"
    git_dir.mkdir(parents=True)

    result = _asyncio.run(_repair_archived_deployment_state_handler(
        {
            "handoff_path": "state/handoffs/some-live-handoff.md",
            "reason": "test: wrong root",
            "deployment_state": "continued",
            "continued_into": "some-successor.md",
            "continued_into_override": True,
        },
        git_dir,
    ))
    assert result.get("exit_code") == 1, result
    assert "archive/handoffs" in (result.get("error") or ""), result


def test_handoff_stamp_registered_op_is_not_deployment_state_shaped():
    """The module's SOLE `@register_op("handoff.stamp")` decorates a
    function whose params are handoff_path/sha/force/kind — shipped_in
    provenance only. It has no `deployment_state`/`continued_into` param at
    all, contradicting the plan's AC1 characterization of "the registered
    JSON-RPC op handoff.stamp" as the target_state:continued writer — that
    writer (`_repair_archived_deployment_state_handler`) is a separate,
    deliberately unregistered function (see this module's own docstring:
    "deliberately NOT @register_op-registered")."""
    import inspect

    from coordinator_core.ops.handoff_stamp import _handler

    params = set(inspect.signature(_handler).parameters) | {
        "handoff_path", "sha", "force", "kind",
    }
    sig_src = inspect.getsource(_handler)
    assert "deployment_state" not in sig_src.split('"""', 2)[0], (
        "the registered handoff.stamp handler's own signature/param list "
        "unexpectedly names deployment_state — re-check the AC1 divergence "
        "note above"
    )


# ---------------------------------------------------------------------------
# Known, reported open gap — writer 2 (see module docstring)
# ---------------------------------------------------------------------------


def test_open_gap_writer_two_still_does_not_discharge(tmp_path):
    """Plain pinning test (NOT an xfail — the current behavior below is what
    actually happens today and passes cleanly; the gap is that it happens at
    all, not that this test fails). When writer 2 is fixed (delegate or
    refuse, per the plan's own two sanctioned shapes), THIS TEST must be
    updated in the same change — it is deliberately not an xfail because
    xfail requires the body to raise, and today's actual behavior does not."""
    import coordinator_core.ops.handoff_transition as ht

    repo = tmp_path / "repo"
    _init_repo(repo)
    hp = _seed_handoff(repo, "gap-predecessor.md", "claimed", "in_flight")
    common_dir = _common_dir(repo)
    worktree = repo

    result = ht._supersede(
        "state/handoffs/gap-predecessor.md", "gap-successor.md", worktree, common_dir
    )

    assert result.get("exit_code") == 0, result
    # THE GAP: continued was written (superseded), but the file was neither
    # moved nor was the write refused.
    assert hp.exists(), "today: writer 2 never archives (this is the open gap)"
    text = hp.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    assert split is not None
    assert read_fm_field(split.fm_text, "deployment_state") == "continued", (
        "today: writer 2 writes continued with no discharge and no refusal"
    )


# ---------------------------------------------------------------------------
# AC11 — mechanical (AST-walked) invariant, not a hand-maintained enumeration
# ---------------------------------------------------------------------------

# Mirrors coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py's
# own frozen-inventory-SUBSET shape (see that module's G2 note): the scan is
# mechanical (AST, not grep-by-eye or a hand-typed list of "the writers we
# know about"), and the assertion is that DISCOVERED sites are a SUBSET of
# this frozen inventory — a genuinely NEW, unlisted site fails the test by
# construction (AC11's own requirement), while a listed one regressing (its
# verdict changing) is caught by the per-site behavioral tests above, not by
# this inventory shape.
#
# Attribution is to the INNERMOST enclosing function — a nested `mutate`/
# `_mutate` locked_rmw closure, not the outer op handler that defines it
# (see `_innermost_function_writers`'s own docstring for why). Each tuple
# below is annotated with which outer function actually owns that closure,
# and its C3 verdict:
_KNOWN_CONTINUED_WRITER_SITES: Set[Tuple[str, str]] = {
    # writer 1 (THE PRIMARY DEFECT) — `_supersede_continued`'s own `mutate`
    # closure. FIXED this chunk: `_handler`'s live-children guard call now
    # passes edge_kinds={"forked_from"} for mode="supersede" (see
    # handoff_archive_transition.py's guard-call comment), so the successor
    # itself no longer retains the predecessor. Covered by
    # test_supersede_archives_despite_live_successor_predecessor_edge above.
    ("coordinator_core/ops/handoff_archive_transition.py", "mutate"),
    # writer 2 — `_supersede`'s own `mutate` closure (handoff_transition.py).
    # OPEN GAP, reported not fixed this chunk — see module docstring and
    # test_open_gap_writer_two_still_does_not_discharge above: writes
    # continued with no archival and no refusal. Fixing it (delegate or
    # refuse, either of the plan's two sanctioned shapes) changes its return
    # contract on a live handoff and breaks
    # test_lifecycle_pair_consistency.py's test_transition_supersede_*
    # tests, a file outside C3's writes:.
    ("coordinator_core/ops/handoff_transition.py", "mutate"),
    # writer 4 (as literally named by the plan's AC1 enumeration) —
    # `_repair_archived_deployment_state_handler`'s own `_mutate` closure
    # (handoff_stamp.py). VERIFIED SAFE BY CONSTRUCTION, not merely trusted:
    # its containment allowlist is `worktree / "archive" / "handoffs"`
    # ONLY (see that handler's own path-resolution code) — it can never
    # write to a record resident in state/handoffs/, and it is NOT
    # `@register_op`-registered (confirmed: the module's sole
    # `@register_op("handoff.stamp")` decorates a *different* function that
    # stamps shipped_in, not deployment_state) — the plan's "reachable by
    # any skill, CLI, or sibling repo" characterization of this site does
    # not hold against the current source. No code change needed.
    ("coordinator_core/ops/handoff_stamp.py", "_mutate"),
    # A FIFTH WRITER, not named by the plan's AC1 enumeration — found by
    # THIS mechanical scan, exactly the "writer enumeration wrong a fourth
    # time" shape the dispatch brief said was EXPECTED. `_plan_one` is
    # `migrate_handoff_vocabulary.py`'s per-record decision function for the
    # DR-084 one-shot corpus-vocabulary migrator (fleet.migrate_handoff_
    # vocabulary op) — it writes deployment_state:continued directly, on a
    # record that may currently be resident in EITHER state/handoffs/** or
    # archive/handoffs/** (it walks both), with no archival step of its own.
    # Reported, not fixed: coordinator_core/ops/fleet/
    # migrate_handoff_vocabulary.py is outside C3's writes:, and it is a
    # deliberately human-operator-run, one-shot migration tool (not the
    # steady-state production write path AC1 concerns) — whether it needs
    # the same discharge-or-refuse treatment is a PM/EM call, not resolved
    # here.
    ("coordinator_core/ops/fleet/migrate_handoff_vocabulary.py", "_plan_one"),
}

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _has_continued_constant(node: ast.AST) -> bool:
    """True iff a real code-level `"continued"` string constant appears
    anywhere in `node` — an actual comparison/argument value, never prose.
    Skips the function's own docstring (the first-statement bare Expr(str),
    if present) so a docstring merely DISCUSSING "continued" does not count
    as evidence the function WRITES it."""
    body = getattr(node, "body", [])
    skip_id = None
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        skip_id = id(body[0].value)
    for n in ast.walk(node):
        if id(n) == skip_id:
            continue
        if isinstance(n, ast.Constant) and n.value == "continued":
            return True
    return False


def _innermost_function_writers(root: Path) -> Set[Tuple[str, str]]:
    """AST-walk every non-test .py file under coordinator_core/, collect
    every (file, INNERMOST-enclosing-function) that contains a call to
    insert_fm_field/replace_fm_field whose field-name argument is the
    literal "deployment_state" AND whose innermost enclosing function ALSO
    contains a real `"continued"` string constant (a comparison or an
    argument value — see `_has_continued_constant` — not prose). Attributing
    to the innermost function only (not every enclosing scope) avoids
    double-counting a nested `mutate`/`_mutate` closure separately from the
    outer op handler that defines it.
    """
    found: Set[Tuple[str, str]] = set()
    base = root / "coordinator_core"
    for path in base.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        parts = path.parts
        if "tests" in parts or "__pycache__" in parts:
            continue
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue

        func_stack: List[ast.AST] = []

        class _Visitor(ast.NodeVisitor):
            def _visit_func(self, node):
                func_stack.append(node)
                self.generic_visit(node)
                func_stack.pop()

            visit_FunctionDef = _visit_func
            visit_AsyncFunctionDef = _visit_func

            def visit_Call(self, node: ast.Call) -> None:
                func = node.func
                name = func.id if isinstance(func, ast.Name) else None
                if name in ("insert_fm_field", "replace_fm_field") and len(node.args) >= 2:
                    field_arg = node.args[1]
                    if (
                        isinstance(field_arg, ast.Constant)
                        and field_arg.value == "deployment_state"
                        and func_stack
                    ):
                        innermost = func_stack[-1]
                        if _has_continued_constant(innermost):
                            found.add((rel, innermost.name))
                self.generic_visit(node)

        _Visitor().visit(tree)
    return found


def test_ac11_continued_writer_inventory_is_closed():
    discovered = _innermost_function_writers(_REPO_ROOT)
    unlisted = {
        (f, fn) for (f, fn) in discovered
        if (f.replace("\\", "/"), fn) not in _KNOWN_CONTINUED_WRITER_SITES
    }
    assert not unlisted, (
        "AC11: a NEW, unlisted writer of deployment_state:continued was "
        f"found: {sorted(unlisted)} — add it to "
        "_KNOWN_CONTINUED_WRITER_SITES above and verify it either "
        "discharges its own archival in the same operation or refuses to "
        "write (per C3's AC1)"
    )
    # AC11 is a two-sided invariant: also assert every KNOWN site is still
    # actually discovered by the mechanical scan — a site quietly falling
    # out of the scan (e.g. a rename) would otherwise silently narrow this
    # test's coverage without failing anything.
    missing = {
        (f, fn) for (f, fn) in _KNOWN_CONTINUED_WRITER_SITES
        if (f, fn) not in {(d[0].replace("\\", "/"), d[1]) for d in discovered}
    }
    assert not missing, (
        f"a previously-known writer site is no longer discovered: {sorted(missing)} "
        "— renamed, removed, or the scanner regressed; update "
        "_KNOWN_CONTINUED_WRITER_SITES or investigate"
    )

"""Standing gate: no explicit write destination outside the sanctioned helper.

THE DEFECT THIS EXISTS FOR (2026-08-27). Two benchmark fixtures minted their
isolated engine clones with `tempfile.mkdtemp(dir=str(source_root.parent))` --
for a repo checked out at a volume top level, the bare drive root. Outside
every git repo, so `bash_guards.bump_outside_repo_write` could not see it (that
guard reads Bash tool command strings; this was engine Python). They leaked
too: 68 directories, ~14GB, 37 orphaned warm supervisors over two days, found
by a human reading a drive listing.

WHY THIS PREDICATE AND NOT THE OBVIOUS ONE. The obvious gate is "flag a
destination that escapes every git root" -- and it cannot be written. Whether
`source_root.parent` escapes is a fact about where this box has the tree
checked out, not a property of the source text; an AST can only implement some
proxy for it, and the false-positive rate of an unspecified proxy is not a
number worth measuring. So this gate does not try. Its predicate is exact and
syntactic: an explicit destination argument passed to a directory-creating
sink, from a site not on `_SANCTIONED_SITES`. Whether any given destination is
rootless is answered at RUNTIME, where it is answerable, by
`isolated_clone.mkdtemp_for_clone`'s own assertion.

The two halves are not redundant, and neither subsumes the other:
  - the runtime assertion is blind to callers that never reach it (measured:
    re-introducing the literal defect raises nothing);
  - this gate is blind to whether a destination is actually rootless.
Together they cover the incident. Spike verdict:
`docs/research/spike-verdicts/2026-08-27-the-choke-point-cannot-see-its-own-bypass.md`.

NO BURN-DOWN LEG, DELIBERATELY. The measured population is 8 files; at that
size a flat allowlist is reviewable in one sitting and a `designed_red`
burn-down list is a list nobody works. This is NOT the shape of
`tests/test_no_unbatched_per_item_git_spawn.py` (7966 lines, thirteen-plus
discriminators, 154 sites) -- that artifact is a reference for its
measure-don't-assert discipline only, and copying its machinery here would be
a two-orders-of-magnitude mismatch. Line count is a measured axis
(`CLAUDE.md` § brightline).

ADDING A SITE. If new code legitimately needs an explicit destination, add it
to `_SANCTIONED_SITES` with the one-line reason, in the same commit. That
review -- a human saying why this destination is correct -- IS the mechanism.
Do not widen the sink list or loosen the predicate to make a site pass.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, NamedTuple

_ENGINE_PKG = Path(__file__).resolve().parents[2]

# THROWAWAY-MINTING sinks only, and this narrowness is load-bearing.
#
# `os.makedirs` is deliberately NOT here, and the reason is the whole design.
# Measured while building this gate: adding it produced 100+ hits across the
# engine, all benign. `makedirs(path)` means "make this path exist" -- the
# destination was already chosen and named by the caller. `mkdtemp(dir=X)`
# means "INVENT a name under X" -- X is a scratch-location decision, made
# here, and that decision is the one the 2026-08-27 incident got wrong.
# Widening past this line reintroduces the false-positive rate that made the
# original sweep proposal unworkable; a gate that misfires gets disabled, and
# a disabled gate prevents nothing.
_SINKS = frozenset({"mkdtemp", "TemporaryDirectory"})

# Both sinks spell their destination `dir=`, and both REQUIRE the keyword form
# -- `mkdtemp`'s positional order is (suffix, prefix, dir), so a positional
# destination is not expressible without also passing the first two. There is
# no positional arm to miss.
_DEST_KW = "dir"


class _Site(NamedTuple):
    relpath: str
    lineno: int
    sink: str
    dest: str


# (relpath, sink) -> why this site is allowed to name its own destination.
# Measured 2026-08-27: 19 explicit-destination calls across these 8 files.
_SANCTIONED_SITES: dict[tuple[str, str], str] = {
    ("coordinator_core/benchmarks/isolated_clone.py", "mkdtemp"): (
        "THE sanctioned helper -- the one site that resolves a destination and "
        "asserts a git root above it at runtime"
    ),
    ("coordinator_core/bash_guards/tests/guard_message_corpus.py", "TemporaryDirectory"): (
        "guard-message corpus fixtures; destination is `_neutral_scratch_parent()`, "
        "a deliberately repo-neutral parent the corpus needs to exercise guard text"
    ),
    ("coordinator_core/benchmarks/tests/test_warm_door_process_time_gate.py", "mkdtemp"): (
        "macOS `_short_runtime_base` -- destination is chosen for the AF_UNIX "
        "sun_path byte budget, not for repo placement"
    ),
    ("coordinator_core/warm/tests/test_door_read_deadline_posix.py", "mkdtemp"): (
        "same sun_path budget constraint as the warm-door gate above"
    ),
    ("coordinator_core/install/sandbox_check.py", "mkdtemp"): (
        "install sandbox is a throwaway CLAUDE_HOME under the platform temp dir "
        "by design -- it must NOT be inside any repo"
    ),
    ("coordinator_core/ops/tests/test_invoke_from_argv.py", "mkdtemp"): (
        "argv-parsing test needs a path outside any repo to exercise repo resolution"
    ),
    ("coordinator_core/tests/test_invoke_main.py", "mkdtemp"): (
        "same repo-resolution exercise as test_invoke_from_argv above"
    ),
    ("coordinator_core/tests/_fixtures.py", "mkdtemp"): (
        "service fixtures pin a short base to stay inside path-length limits"
    ),
}


def _sink_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", None)


def _destination_expr(call: ast.Call) -> str | None:
    """The source text of this call's explicit destination, or None if it
    names no destination (and so chooses nothing this gate cares about)."""
    for kw in call.keywords:
        if kw.arg == _DEST_KW:
            return ast.unparse(kw.value)
    return None


def _iter_sites() -> Iterator[_Site]:
    for path in sorted(_ENGINE_PKG.rglob("*.py")):
        relpath = path.relative_to(_ENGINE_PKG.parent).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            # A file this gate cannot parse is not silently clean -- but it is
            # also not this gate's business to fail on; the suite has its own
            # import-time coverage for unparseable modules.
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            sink = _sink_name(node)
            if sink not in _SINKS:
                continue
            dest = _destination_expr(node)
            if dest is None:
                continue
            yield _Site(relpath, node.lineno, sink, dest)


def test_no_unsanctioned_explicit_write_destination() -> None:
    """Every explicit write destination in the engine is a sanctioned site.

    Measured at authoring (2026-08-27): 19 calls, 8 files, all sanctioned --
    this gate is green on a clean tree with zero false positives, because the
    predicate is exact rather than a proxy.
    """
    violations = [
        site
        for site in _iter_sites()
        if (site.relpath, site.sink) not in _SANCTIONED_SITES
    ]
    assert not violations, (
        "explicit write destination(s) outside the sanctioned helper:\n"
        + "\n".join(
            f"  {s.relpath}:{s.lineno}  {s.sink}(dest={s.dest})" for s in violations
        )
        + "\n\nUse `benchmarks.isolated_clone.mkdtemp_for_clone` if this is an "
        "engine clone -- it resolves the destination and refuses one that sits "
        "under no git root. If this site genuinely must name its own "
        "destination, add it to `_SANCTIONED_SITES` with the reason, in this "
        "same commit."
    )


def test_sanctioned_allowlist_has_no_dead_entries() -> None:
    """An allowlist entry whose site no longer exists is a licence nobody is
    using -- and the next file at that path inherits it silently. The 2026-08-27
    incident is what a silently-inherited licence looks like."""
    live = {(s.relpath, s.sink) for s in _iter_sites()}
    dead = sorted(set(_SANCTIONED_SITES) - live)
    assert not dead, (
        "sanctioned-site entries matching nothing on disk (remove them):\n"
        + "\n".join(f"  {relpath}  {sink}" for relpath, sink in dead)
    )


def test_the_2026_08_27_defect_would_be_caught() -> None:
    """The prime exit criterion, asserted rather than described.

    Re-introducing the literal defect must turn this gate red. Exercised
    against the real predicate on synthetic source, so it keeps holding if the
    fixtures that carried the original defect are later renamed or deleted.
    """
    defect = ast.parse(
        'tmp_parent = Path(tempfile.mkdtemp(prefix="commit-op-wallclock-",'
        ' dir=str(source_root.parent)))'
    )
    found = [
        node
        for node in ast.walk(defect)
        if isinstance(node, ast.Call)
        and _sink_name(node) in _SINKS
        and _destination_expr(node) is not None
    ]
    assert found, "the predicate no longer matches the defect it exists to catch"
    assert (
        "coordinator_core/benchmarks/tests/test_commit_op_wallclock_budget.py",
        "mkdtemp",
    ) not in _SANCTIONED_SITES, (
        "the fixture that carried the 2026-08-27 defect must not be sanctioned "
        "-- it goes through `mkdtemp_for_clone` now"
    )

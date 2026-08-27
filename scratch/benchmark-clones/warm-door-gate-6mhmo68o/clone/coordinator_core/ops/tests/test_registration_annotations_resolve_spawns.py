"""coordinator_core.ops.tests.test_registration_annotations_resolve_spawns
-- the leg that reads the eager table at HEAD via real git.

SPLIT OUT 2026-08-27. `_git`/`_head_eager_table` spawn, and a spawn site in a
non-test function forces the module-level tier form (spawn ratchet Rule 4 --
a marker on a helper is inert). The remaining assertions in the sibling file
resolve annotations in process and stay on the fast tier.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ipc import get_op_handler

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

#: Shared with the in-process half of this suite. Imported rather than
#: duplicated so the two files cannot drift apart.
from coordinator_core.ops.tests.test_registration_annotations_resolve import (  # noqa: E402
    _advertised_at_head,
    _REGISTRY_MAP_PATH,
    _head_annotation_failures,
    _module_candidate_paths,
    _parse_cat_file_batch,
    _repo_root,
)


def _git(args, cwd, stdin=None):
    """Run one git command, returning stdout, or None when git/HEAD is unusable."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            input=stdin,
            capture_output=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _head_eager_table(root):
    """`_EAGER_OP_MODULES` as committed at HEAD, or None when unreadable.

    Parsed out of HEAD's source text with `ast` rather than imported: importing
    it would re-read the worktree and reintroduce the very blind spot this leg
    exists to close.
    """
    blob = _git(["show", "HEAD:coordinator_core/ops/__init__.py"], root)
    if blob is None:
        return None
    try:
        tree = ast.parse(blob.decode("utf-8", errors="replace"))
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "_EAGER_OP_MODULES":
                if node.value is None:  # a bare annotation, no value to read
                    return None
                try:
                    return ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return None
    return None


@pytest.mark.spawns_process
def test_every_advertised_op_is_served_at_head():
    """HEAD's annotations may not name an op HEAD does not implement.

    A failure here means a commit advertised something whose implementation is
    still uncommitted -- most often a peer's in-flight work on a shared branch.
    The remedy is the one the worktree leg already prescribes: strike the name.
    It goes back in the commit that lands the op, where it is true.
    """
    root = _repo_root()
    table = _head_eager_table(root)
    if table is None:
        pytest.skip("HEAD's coordinator_core/ops/__init__.py is not readable via git")

    advertised = _advertised_at_head(table)
    assert len(advertised) > 20, (
        f"parsed only {len(advertised)} advertised ops out of HEAD -- parser drift?"
    )

    listing = _git(["ls-tree", "-r", "HEAD", "--name-only"], root)
    if listing is None:
        pytest.skip("HEAD is not readable via git ls-tree")
    tracked = set(listing.decode("utf-8", errors="replace").splitlines())

    wanted = {_REGISTRY_MAP_PATH}
    for module_path, _name in advertised:
        wanted.update(p for p in _module_candidate_paths(module_path) if p in tracked)
    ordered = sorted(wanted)

    stdin = "".join(f"HEAD:{path}\n" for path in ordered).encode()
    batch = _git(["cat-file", "--batch"], root, stdin=stdin)
    if batch is None:
        pytest.skip("git cat-file --batch failed against HEAD")

    contents = _parse_cat_file_batch(batch, ordered)
    failures = _head_annotation_failures(advertised, contents)
    assert not failures, "\n".join(failures)

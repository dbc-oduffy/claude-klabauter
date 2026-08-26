"""test_publish_declared_payload_walk_prunes — pins the three prunes on
`publish.py :: _walk_published_payload`, the widening that carries
`declared_payload` past the percolation scan surface.

Why this file exists rather than trusting the walk's own docstring: the
widening shipped UNPRUNED first, and nothing under test caught it. A
flat-mirror row's `dest_dir` IS the mirror root, so the unpruned walk
descended into `.git` and named thousands of git objects as declared payload;
`declared_payload` drives the ADD side of `percolate-round.py ::
_pathspec_from_manifest`, so those paths would have reached the commit leg,
been declined, and refused the round's push outright (§
`_round_refusal_reason`). The failure mode is "every future round stops
publishing", not "the walk is slow" — which is why each prune gets a test
rather than a comment.

Fixtures only — no test here runs a real round or touches a live publish
mirror.

Run: python -m pytest coordinator/bin/tests/test_publish_declared_payload_walk_prunes.py -q
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish():
    spec = importlib.util.spec_from_file_location(
        "publish_declared_payload_walk_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: publish.py defines dataclasses, and
    # `dataclasses._is_type` resolves a field's annotation through
    # `sys.modules[cls.__module__]` — absent that entry the import dies at
    # collection. Same idiom as every other publish.py test in this directory.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_publish()


def _mirror(tmp_path: Path) -> Path:
    """A dest dir shaped like a real flat-mirror row's: payload at the root,
    a `.git` beside it, plus the two scratch classes a destination
    accumulates on its own."""
    root = tmp_path / "mirror"
    (root / ".git" / "objects" / "ab").mkdir(parents=True)
    (root / ".git" / "objects" / "ab" / "cdef123").write_text("object")
    (root / ".git" / "config").write_text("[core]\n")
    (root / "coordinator_core" / "warm" / "door").mkdir(parents=True)
    (root / "coordinator_core" / "warm" / "door" / "door.exe").write_bytes(b"MZ\x00")
    (root / "coordinator_core" / "__pycache__").mkdir()
    (root / "coordinator_core" / "__pycache__" / "mod.cpython-311.pyc").write_bytes(b"\x00")
    (root / "coordinator_core" / "stray.pyc").write_bytes(b"\x00")
    staging = root / ".bin.publish-staging-dsnce3r6.prior"
    (staging / "nested").mkdir(parents=True)
    (staging / "nested" / "leftover.py").write_text("# crashed round scratch\n")
    (root / "LICENSE").write_text("license\n")
    (root / "payload.py").write_text("x = 1\n")
    return root


def test_walk_names_the_binary_the_scan_surface_misses(tmp_path):
    """The whole reason the widening exists: a non-transform-eligible file
    the row genuinely published is tracked at dest HEAD and absent from every
    scan set. `door.exe` is the measured witness from the klabauter mirror.
    """
    root = _mirror(tmp_path)

    found = _mod._walk_published_payload([root])

    assert root / "coordinator_core" / "warm" / "door" / "door.exe" in found
    assert root / "LICENSE" in found
    assert root / "payload.py" in found


def test_walk_never_descends_into_dot_git(tmp_path):
    """The defect that would have refused every future round."""
    root = _mirror(tmp_path)

    found = _mod._walk_published_payload([root])

    assert not [p for p in found if ".git" in p.parts], (
        "a .git path reached declared_payload — it drives the ADD side, so the "
        "commit leg declines it and _round_refusal_reason refuses the push"
    )


def test_walk_never_names_a_publish_staging_directory(tmp_path):
    """Round `eebf1c67` put 1,028 files of a stranded staging directory into
    the public mirror. Re-declaring one here would reopen that path."""
    root = _mirror(tmp_path)

    found = _mod._walk_published_payload([root])

    assert not [p for p in found if any("publish-staging" in part for part in p.parts)]


def test_walk_never_names_locally_generated_bytecode(tmp_path):
    """`__pycache__`/`.pyc`/`.pyo` are created AT the destination by anything
    that runs Python there. No row publishes them."""
    root = _mirror(tmp_path)

    found = _mod._walk_published_payload([root])

    assert not [p for p in found if p.suffix in (".pyc", ".pyo")]
    assert not [p for p in found if "__pycache__" in p.parts]


def test_no_published_dirs_yields_nothing():
    """A round that published nowhere declares nothing extra — the same
    fail-direction the empty row scope gets on the removal side."""
    assert _mod._walk_published_payload([]) == set()


def test_walk_is_bounded_by_the_published_dirs_and_declares_nothing_else(tmp_path):
    """The composition invariant, pinned because it is not obvious and it
    holds by ORDERING rather than by design.

    `declared_payload` is `scan_set | _walk_published_payload(published_dest_dirs)`.
    The obvious reading of that widening — "it re-declares everything on
    disk at dest" — is what this session predicted, and it is wrong only
    because the walk is bounded by the dirs THIS round published. A
    directory sitting on disk from a retired row is therefore NOT declared,
    which is what lets the removal side see it at all.

    Measured 2026-08-26 on the `coordinator-claude` mirror: `declared_payload`
    went DOWN 1498 -> 1477 across this change, and `whoami/` — a ratified
    retirement whose bytes were still on disk — became reachable by
    `head_tree - declared_payload` for the first time (0/23 -> 23/23).

    NEGATIVE SPEC. Two independent changes would silently reverse that and
    make every retired-but-still-on-disk path permanently undeletable:

    - widening this walk's input beyond the round's own published dest dirs
      (to the repo root, to `_dest_head_tree`'s output, to a `git status`
      survey), or
    - dropping the row-scoping that narrows those dirs in the first place.

    Either one re-declares a retired row's bytes, and nothing else in the
    pipeline would notice — the round would still report PASS, the pathspec
    would simply stop naming the paths anyone wanted gone. Hence a test
    rather than a comment.
    """
    published = tmp_path / "published"
    (published / "pkg").mkdir(parents=True)
    (published / "pkg" / "live.py").write_text("x = 1\n")

    retired = tmp_path / "whoami"
    (retired / "coordinator_whoami").mkdir(parents=True)
    (retired / "coordinator_whoami" / "cli.py").write_text("# retired row, still on disk\n")
    (retired / "pyproject.toml").write_text("[project]\n")

    found = _mod._walk_published_payload([published])

    assert published / "pkg" / "live.py" in found
    assert not [p for p in found if "whoami" in p.parts], (
        "a retired row's on-disk bytes entered declared_payload — the removal "
        "side can no longer name them, and nothing downstream reports it"
    )


def test_over_declaring_silently_disables_removals(tmp_path):
    """The failure mode is INERTNESS, and it is silent — which is why it gets
    a test naming the consequence rather than only the prune.

    MEASURED 2026-08-26 on `claude-klabauter`, from this session's own
    defect: the walk shipped pruning only `.git`/`__pycache__`/staging, so a
    flat-mirror row whose `dest_dir` is the mirror root descended into
    `.fleet-env/` — the fleet virtualenv, 2.7GB and 26,405 files.
    `declared_payload` reached **48,929** against a tracked tree of **8,717**,
    which made `head_tree - declared_payload` **empty**. The removal side went
    completely inert on that mirror and the round still reported PASS.

    Note the direction. An over-wide `declared_payload` never deletes anything
    WRONG — it quietly stops deleting anything at all. No verdict changes, no
    warning fires, no count moves; the only symptom is that paths everyone
    wants gone stay. A prune bug that over-deletes announces itself. This one
    cannot, so the invariant it violates is asserted here directly:
    **declared_payload must not exceed what dest actually tracks.**

    Fixed by a concurrent session wiring the walk to
    `surface.STRUCTURAL_NEVER_PUBLISHED_PREFIXES`, the SSOT that already
    carried `.fleet-env` (and `.git`, `.percolate`) — the right fix, and the
    one this session should have found instead of hardcoding three prunes.
    """
    root = tmp_path / "mirror"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator_core" / "real.py").write_text("x = 1\n")

    # The shape that caused it: a large gitignored tree sitting in the row's
    # own published dir, published by nothing.
    venv = root / ".fleet-env" / "lib" / "python3.11" / "site-packages"
    venv.mkdir(parents=True)
    for i in range(50):
        (venv / f"dep{i}.py").write_text("# vendored dependency\n")
    (root / ".fleet-env" / "pyvenv.cfg").write_text("home = /usr\n")

    found = _mod._walk_published_payload([root])

    assert root / "coordinator_core" / "real.py" in found
    assert not [p for p in found if ".fleet-env" in p.parts], (
        "the environment directory entered declared_payload — head_tree minus "
        "declared_payload then empties and the removal side goes inert, "
        "silently, with the round still reporting PASS"
    )
    # The invariant, stated as the thing that actually matters: what the walk
    # declares stays proportionate to real payload, never swamped by a
    # destination's own local scratch.
    assert len(found) == 1

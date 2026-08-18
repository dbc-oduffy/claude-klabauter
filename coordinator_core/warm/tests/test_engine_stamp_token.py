"""
coordinator_core.warm.tests.test_engine_stamp_token

Regression pin for the defect that made the warm engine useless on a shared
branch, measured on this box 2026-08-18.

The generation token is embedded in the pipe name (`election.pipe_name`), so
rotating it strands the running server and makes the next client spawn a
successor. Keyed on the git ref, the token rotated **every ~32 seconds** here,
because any of the 50-70 concurrent sessions committing ANYTHING -- a doc, a
`state/` artifact, an unrelated peer's code -- moves the branch ref. A server
takes ~1s to boot and was stale before a client could reach it: with the
feature fully enabled and correctly wired, warm served 0/6. Engine code had
not changed once in that window.

The token was therefore COARSER than engine-source change in the direction
that matters: it fired on commits touching no engine code at all.

A published engine tree now carries `coordinator_core/_engine_stamp`, so its
generation changes exactly when a publish ships new engine code. A live
working tree has no stamp and keeps the original ref-based behaviour, which is
over-sensitive but never stale.

NOT a staleness hole: axis 2 (`ServerVersionState`) hashes the engine package
server-side and evicts on any real code change, stamp or no stamp. Axis 1's
job is generation binding, not staleness -- see `compute_client_token`'s own
negative spec.
"""

import time
from pathlib import Path

from coordinator_core.warm import skew


def _make_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A minimal tree shaped like an engine clone: a `coordinator_core`
    package dir plus the `.git` files the ref-based fallback reads."""
    root = tmp_path / "clone"
    (root / "coordinator_core").mkdir(parents=True)
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/shared\n")
    ref = git / "refs" / "heads" / "shared"
    ref.write_text("a" * 40 + "\n")
    return root, ref


def _peer_commit(ref: Path, sha_char: str) -> None:
    """Someone else's commit on the shared branch. Touches no engine code."""
    time.sleep(0.02)  # ensure a distinct st_mtime_ns
    ref.write_text(sha_char * 40 + "\n")


def test_published_tree_token_survives_an_unrelated_peer_commit(tmp_path):
    """THE defect. A stamped tree must not rotate because a peer committed."""
    root, ref = _make_clone(tmp_path)
    skew.write_engine_stamp(root, "sha:" + "1" * 40)

    before = skew.compute_client_token(root)
    _peer_commit(ref, "b")
    after = skew.compute_client_token(root)

    assert before == after, (
        "a published engine's generation token rotated because an unrelated "
        "peer commit moved the branch ref -- this is the ~32s rotation that "
        "made warm serve 0/6"
    )


def test_published_tree_token_rotates_when_a_publish_ships_new_engine_code(tmp_path):
    """The other half: stability must not cost eviction. A new build must
    still bind a new generation, or a successor could not take over."""
    root, _ref = _make_clone(tmp_path)
    skew.write_engine_stamp(root, "sha:" + "1" * 40)
    before = skew.compute_client_token(root)

    skew.write_engine_stamp(root, "sha:" + "2" * 40)
    after = skew.compute_client_token(root)

    assert before != after, "a publish shipping new engine code did not rotate the generation"


def test_working_tree_without_a_stamp_keeps_the_ref_based_fallback(tmp_path):
    """A live working tree has no stamp and must keep the original
    behaviour -- over-sensitive, never stale. Pinned so a later change cannot
    quietly extend stamp semantics to unstamped trees, which would pin a
    generation while the code moved underneath it."""
    root, ref = _make_clone(tmp_path)
    assert not (root / "coordinator_core" / skew.ENGINE_STAMP_FILENAME).exists()

    before = skew.compute_client_token(root)
    _peer_commit(ref, "c")
    after = skew.compute_client_token(root)

    assert before != after, "an unstamped working tree stopped tracking its ref"


def test_stamp_identity_is_recorded_verbatim_for_debugging(tmp_path):
    """Only the stamp's bytes matter to the token, but the identity is kept
    human-readable so an operator can tell which build a resident server is
    serving."""
    root, _ref = _make_clone(tmp_path)
    written = skew.write_engine_stamp(root, "sha:" + "9" * 40)

    assert written.read_text(encoding="utf-8").strip() == "sha:" + "9" * 40


def test_stamp_lands_where_a_published_mirror_puts_it(tmp_path):
    """The path is contract with the publish/install seam: a mirror whose
    root is the repo gets `<root>/coordinator_core/_engine_stamp`.

    The name must NOT be dot-prefixed -- the publish sync skips dotfiles, so a
    dot-named stamp ships nowhere and the engine silently reverts to ref-based
    rotation."""
    assert not skew.ENGINE_STAMP_FILENAME.startswith(".")
    root, _ref = _make_clone(tmp_path)
    written = skew.write_engine_stamp(root, "sha:abc")

    assert written == root / "coordinator_core" / "_engine_stamp"

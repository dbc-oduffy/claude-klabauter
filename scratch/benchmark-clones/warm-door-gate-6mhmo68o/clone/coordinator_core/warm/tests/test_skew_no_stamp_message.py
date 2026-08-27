"""
coordinator_core.warm.tests.test_skew_no_stamp_message

Spec backlink: docs/plans/2026-08-22-warm-engine-and-door-install-from-published-root.md § C10

Pins `skew._no_stamp_message`'s TWO-CONDITION shape against silent drift.
`README-posix.md` § "The peer now exists" used to describe a single message
("an error message about the clone that reads like a complaint about the
engine root you passed") that does not match either branch this function
actually renders -- this test is the guard the corrected README account
leans on, so the string cannot drift back without a red here first.

Branch 1 (root does not exist): names the absent path and points at
`root_channel_reconcile`, a cold-path-safe remediation with no session
dependency.
Branch 2 (root exists, unstamped): the ruling-shaped message, resolved and
naming `scripts/setup.py` under the (resolved) root.

Negative-spec: this file does not assert anything about generation-token
stability -- `compute_client_token` binds `root` UNRESOLVED and only calls
`_no_stamp_message(root)` on the failure branch, so `.resolve()` here
affects the MESSAGE text only, never the token comparison. Out of scope
for this chunk.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.warm import skew


def test_absent_root_names_the_path_and_routes_to_root_channel_reconcile(tmp_path):
    absent = tmp_path / "does-not-exist"

    message = skew._no_stamp_message(absent)

    assert str(absent) in message
    assert "Remediation: python3 -m coordinator_core.root_channel_reconcile" in message
    assert "setup.py" not in message


def test_present_unstamped_root_names_resolved_path_and_routes_to_setup_py(tmp_path):
    present = tmp_path / "unstamped-clone"
    present.mkdir()

    message = skew._no_stamp_message(present)

    resolved = present.resolve()
    assert str(resolved) in message
    assert f"{resolved} is not a published engine." in message
    assert f"python3 {resolved / 'scripts' / 'setup.py'}" in message
    assert "root_channel_reconcile" not in message


def test_compute_client_token_raises_with_root_and_root_exists_paired(tmp_path, monkeypatch):
    """`UnstampedEngineRootError`'s `root_exists` is UNKNOWN by default
    (`None`), never fabricated as present -- `compute_client_token` must
    pass both `root` and `root_exists` together, never one alone, per this
    chunk's brief."""
    absent = tmp_path / "does-not-exist"

    try:
        skew.compute_client_token(repo_root=absent)
    except skew.UnstampedEngineRootError as exc:
        assert exc.root == absent
        assert exc.root_exists is False
    else:
        raise AssertionError("expected UnstampedEngineRootError")


def test_bare_construction_leaves_root_exists_unknown_not_present():
    """A caller constructing the error with a bare message (a test double,
    an older call site) must not have presence fabricated on its behalf --
    `root_exists` defaults to `None` (UNKNOWN), not `True`."""
    exc = skew.UnstampedEngineRootError("some message")

    assert exc.root is None
    assert exc.root_exists is None

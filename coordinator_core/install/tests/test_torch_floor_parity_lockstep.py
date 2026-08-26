"""Cross-repo torch/torchvision floor lockstep is enforced mechanically.

Two sibling repos declare in prose file headers that their torch floors MUST
move together. A prose rule has no artifact discharging it: a desync resolves
green on both sides and surfaces much later as unexplained per-repo variance.
``fleet_env_lock.check_parity_lockstep`` is that artifact; these tests prove it
actually goes red, since a check that has only ever been green proves nothing.

Negative spec: example-retrieval-repo's torchvision pins deliberately diverge between its
uv overrides file (0.25-floor, 2-ceiling) and constraints.txt
    (``~=0.27.0``, a pip
resolver-backtracking bound narrowed per AC-A.10). ``test_declared_divergence_
is_not_flagged`` exists so a later reader does not "repair" that into lockstep.

Backlink: state/improvement-queue/2026-08-16-two-sibling-repos-keep-their-torch-floor-d0c319ea81ac.yaml
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.install import fleet_env_lock
from coordinator_core.install.fleet_env_lock import (
    FleetEnvLockError,
    PARITY_LOCKSTEP_GROUPS,
    check_parity_lockstep,
)


def _stub_registry(monkeypatch, mapping):
    def fake(key):
        return mapping.get(key.removeprefix("repos."))

    monkeypatch.setattr(fleet_env_lock, "registry_get", fake)


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_declared_groups_are_wellformed():
    assert PARITY_LOCKSTEP_GROUPS, "the lockstep groups must not be emptied silently"
    for group in PARITY_LOCKSTEP_GROUPS:
        assert group["packages"], "a group with no packages enforces nothing"
        assert len(tuple(group["members"])) >= 2, (  # type: ignore[arg-type]
            "a lockstep needs at least two members"
        )
        assert str(group["rule"]).strip(), (
            "every group must quote the peer file's own header — the rule's "
            "authority lives in the sibling repo, not in this module"
        )


def test_lockstep_holds_on_disk():
    """Regression canary against the real sibling trees. Skips rather than
    fails where a sibling is not cloned — a fleet check must not fail on a
    machine that simply does not carry every repo."""
    violations = check_parity_lockstep()
    assert violations == [], "\n".join(violations)


def test_desynced_floor_is_caught(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, "overrides.txt", "torch>=2.10.0,<3\n")
    _write(b, "overrides.txt", "torch>=2.11.0,<3\n")
    _stub_registry(monkeypatch, {"repo_a": str(a), "repo_b": str(b)})

    violations = check_parity_lockstep(
        [
            {
                "packages": ("torch",),
                "members": (("repo_a", "overrides.txt"), ("repo_b", "overrides.txt")),
                "rule": "test rule",
            }
        ]
    )

    assert len(violations) == 1
    message = violations[0]
    assert "torch" in message
    # The message must carry BOTH sides and the rule — a desync report naming
    # only one file sends the reader to the wrong repo.
    assert "2.10.0" in message and "2.11.0" in message
    assert "test rule" in message


def test_declared_divergence_is_not_flagged(tmp_path, monkeypatch):
    """The real shape of group 2: torch in lockstep, torchvision intentionally
    divergent. Flagging torchvision here would be a false positive that pushes
    someone to undo a deliberate, documented narrowing."""
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, "overrides.txt", "torch>=2.10.0,<3\ntorchvision>=0.25,<2\n")
    _write(b, "constraints.txt", "torch>=2.10.0,<3\ntorchvision~=0.27.0\n")
    _stub_registry(monkeypatch, {"repo_a": str(a), "repo_b": str(b)})

    violations = check_parity_lockstep(
        [
            {
                "packages": ("torch",),
                "diverges": ("torchvision",),
                "members": (("repo_a", "overrides.txt"), ("repo_b", "constraints.txt")),
                "rule": "torch only",
            }
        ]
    )

    assert violations == []


def test_comment_only_mention_is_not_a_declaration(tmp_path, monkeypatch):
    """example-game-repo's gpu_sidecar/requirements.txt names torchvision only inside a
    `# pip install ...` comment. A commented mention must not count as a
    declaration, or the check reports a phantom desync against real pins."""
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, "overrides.txt", "torch>=2.10.0,<3\n")
    _write(b, "overrides.txt", "#   pip install torch torchvision --index-url x\ntorch>=2.10.0,<3\n")
    _stub_registry(monkeypatch, {"repo_a": str(a), "repo_b": str(b)})

    violations = check_parity_lockstep(
        [
            {
                "packages": ("torch", "torchvision"),
                "members": (("repo_a", "overrides.txt"), ("repo_b", "overrides.txt")),
                "rule": "comment handling",
            }
        ]
    )

    assert violations == []


def test_partial_declaration_is_caught(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, "overrides.txt", "torch>=2.10.0,<3\ntorchvision>=0.25,<2\n")
    _write(b, "overrides.txt", "torch>=2.10.0,<3\n")
    _stub_registry(monkeypatch, {"repo_a": str(a), "repo_b": str(b)})

    violations = check_parity_lockstep(
        [
            {
                "packages": ("torch", "torchvision"),
                "members": (("repo_a", "overrides.txt"), ("repo_b", "overrides.txt")),
                "rule": "partial",
            }
        ]
    )

    assert len(violations) == 1
    assert "torchvision" in violations[0]


def test_uncloned_sibling_skips_rather_than_fails(tmp_path, monkeypatch):
    a = tmp_path / "a"
    _write(a, "overrides.txt", "torch>=2.10.0,<3\n")
    _stub_registry(monkeypatch, {"repo_a": str(a)})

    violations = check_parity_lockstep(
        [
            {
                "packages": ("torch",),
                "members": (("repo_a", "overrides.txt"), ("not_cloned", "overrides.txt")),
                "rule": "skip",
            }
        ]
    )

    assert violations == []


def test_moved_peer_file_fails_loud(tmp_path, monkeypatch):
    """The rot this check exists to catch. Example-retrieval-repo's own header already
    points at `scripts/constraints.txt` while the file lives at
    `example_retrieval_repo_scripts/constraints.txt` — a present repo with the declared
    file missing must stop the world, not degrade to a skip."""
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, "overrides.txt", "torch>=2.10.0,<3\n")
    b.mkdir(parents=True, exist_ok=True)
    _stub_registry(monkeypatch, {"repo_a": str(a), "repo_b": str(b)})

    with pytest.raises(FleetEnvLockError) as excinfo:
        check_parity_lockstep(
            [
                {
                    "packages": ("torch",),
                    "members": (("repo_a", "overrides.txt"), ("repo_b", "gone.txt")),
                    "rule": "moved",
                }
            ]
        )

    assert "not on disk" in str(excinfo.value)

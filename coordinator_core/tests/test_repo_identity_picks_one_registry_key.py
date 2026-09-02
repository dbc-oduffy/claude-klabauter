"""A repo registered under several registry keys still has ONE identity.

A repo carries its canonical `repos.*` key plus any receive-only alias a
sibling may address it by — `repos.claude_klabauter` alongside
`repos.project_example_orchestration_hub`, `repos.example-sim-repo` alongside
`repos.example_sim_repo_md`, four such pairs live on the machine-b box alone. Both
reverse mappings (path -> EM id) used to take the first key that path-matched,
so the answer was whatever order the caller happened to enumerate the registry
in: `coordinator/bin/cross-repo-memo.py` sorts keys alphabetically and sent
every memo from this repo as `project-example-orchestration-hub-em`, while
`resolve_self_em_id` enumerates in file order and answered `claude-klabauter-em`
for the same repo in the same process. Six memos went out to sibling inboxes
under the wrong sender before this was caught by hand.

The failure is fleet-wide and silent in both directions: a receiver's
addressee gate, the DR-026 sender-namespaced filename, and
`compute_reply_closure`'s sender match all key on that string.

Negative-spec: an alias must stay a valid address a sibling can send TO. This
fixes which key wins the REVERSE mapping only — it does not dedupe the
registry and does not narrow what resolves as a receiver.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

from coordinator_core.machine_resolver import canonical_repo_key_for_root

try:
    from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

    _DOE_ROOT = coordinator_doe_root()
except Exception:  # noqa: BLE001 — no DoE checkout is a skip, never a suite error
    _DOE_ROOT = None

_CANONICAL_KEY = "repos.claude_klabauter"
_ALIAS_KEY = "repos.project_example_orchestration_hub"


def _registry(tmp_path: Path, declared: tuple[str, ...], live: dict[str, Path]) -> Path:
    """Write a two-file machine-local registry and return its settings home.

    `declared` names the keys the seeded `registry.toml` roster carries (value
    empty, exactly as the shipped template declares them); `live` is what this
    machine has actually pointed at a path, in `registry.local.toml`.
    """
    claude_home = tmp_path / "claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True, exist_ok=True)
    roster = "schema = 1\n" + "".join(f'"{key}" = ""\n' for key in declared)
    (machine_local / "registry.toml").write_text(roster, encoding="utf-8")
    body = "".join(
        f'"{key}" = "{str(path).replace(chr(92), chr(92) * 2)}"\n'
        for key, path in live.items()
    )
    (machine_local / "registry.local.toml").write_text(body, encoding="utf-8")
    return claude_home


@pytest.fixture
def collided_registry(tmp_path, monkeypatch):
    """One repo path, two registry keys, only one of them on the roster."""
    repo = tmp_path / "claude-klabauter"
    repo.mkdir()
    claude_home = _registry(
        tmp_path,
        declared=(_CANONICAL_KEY,),
        live={_ALIAS_KEY: repo, _CANONICAL_KEY: repo},
    )
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(machine_local))
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    return repo


@pytest.mark.parametrize("alias_first", [True, False])
def test_declared_roster_key_wins_whatever_the_enumeration_order(
    collided_registry, alias_first
):
    """The roster key wins from either enumeration order — that is the whole fix.

    Both orders are live today: the CLI passes alphabetically sorted keys (the
    alias first), `read_registry_repos` passes file order (the canonical key
    first, by accident of having been registered earlier).
    """
    repo = collided_registry
    keys = [_ALIAS_KEY, _CANONICAL_KEY] if alias_first else [_CANONICAL_KEY, _ALIAS_KEY]
    paths = {key: str(repo) for key in keys}
    assert canonical_repo_key_for_root(str(repo), paths) == _CANONICAL_KEY


def test_self_identity_resolves_to_the_canonical_em_id(collided_registry):
    """`resolve_self_em_id` — the in-process half, used for the addressee gate's
    self line and `compute_reply_closure`'s sender match."""
    from coordinator_core.ops.fleet import _memo_resolver

    importlib.reload(_memo_resolver)
    assert _memo_resolver.resolve_self_em_id(collided_registry) == "claude-klabauter-em"


def test_cli_sender_identity_resolves_to_the_canonical_em_id(
    collided_registry, monkeypatch
):
    """`em_id_for_root` — the CLI half that writes a memo's `from:` line."""
    if not _DOE_ROOT:
        pytest.skip("no DoE-claude checkout — coordinator_registry cannot import")
    # The fixture redirects CLAUDE_HOME, which is one rung of the ladder this
    # module reads its manifest through at IMPORT time. Name the real root on
    # the documented override rung so the redirect costs a registry, not an
    # install-integrity failure.
    monkeypatch.setenv("REPO_DOE_CLAUDE", _DOE_ROOT)
    lib_dir = str(Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import coordinator_registry

    repo = collided_registry
    paths = {key: str(repo) for key in sorted((_ALIAS_KEY, _CANONICAL_KEY))}
    assert coordinator_registry.em_id_for_root(str(repo), paths) == "claude-klabauter-em"


@pytest.mark.parametrize("reverse", [True, False])
def test_undeclared_collision_is_stable_rather_than_arbitrary(
    tmp_path, monkeypatch, reverse
):
    """Neither key on the roster: still one answer, the same one every time.

    A machine can register a repo under two keys the shipped roster never
    declares. There is no principled winner there, so the tie-break is
    lexicographic and logged — stable is the property under test, not which
    of the two it picks.
    """
    repo = tmp_path / "some-repo"
    repo.mkdir()
    keys = ("repos.zulu_name", "repos.alpha_name")
    claude_home = _registry(
        tmp_path, declared=(), live={key: repo for key in keys}
    )
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(machine_local))
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    paths = {key: str(repo) for key in (reversed(keys) if reverse else keys)}
    assert canonical_repo_key_for_root(str(repo), paths) == "repos.alpha_name"


def test_alias_still_resolves_as_a_receiver(collided_registry):
    """The negative spec: a sibling addressing the alias still lands the memo.

    The fix touches the reverse mapping only. Were it to dedupe the registry
    instead, every alias in the fleet's memo history would stop resolving.
    """
    from coordinator_core.ops.fleet import _memo_resolver

    importlib.reload(_memo_resolver)
    inbox, repo_path, _ = _memo_resolver.resolve_receiver_inbox(
        "project-example-orchestration-hub-em"
    )
    assert repo_path is not None
    assert os.path.samefile(str(repo_path), str(collided_registry))


from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.ops.handoff_archive_transition as _op  # noqa: F401 — fires @register_op
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_archive_transition import (
    _APPLY_MINTED_SUCCESSOR,
    _handler as _archive_transition_handler,
)
from coordinator_core.test_archive_stamp import _init_repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_OP_NAME = "handoff.archive_transition"
# is in SUSPENDED_OPS and absent from the registry (kill ledger K-109/K-022),
# Negative-spec: do NOT reinstate a `_OP_NAME not in _REGISTRY` skip. Registry


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


def _seed_predecessor(
    repo: Path, name: str, handoff_id: str | None = None, continued_into: str | None = None,
) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'title: "Test Predecessor {name}"',
        "created: 2026-01-01",
        "branch: work/test/2026-01-01",
        "status: open",
        'predecessor: "none"',
    ]
    if handoff_id:
        lines.append(f'handoff_id: "{handoff_id}"')
    if continued_into:
        lines.append(f'continued_into: "{continued_into}"')
    lines.append("deployment_state: active")
    fm = "\n".join(lines) + "\n"
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _seed_successor(
    repo: Path,
    name: str,
    predecessor: str | None,
    predecessor_id: str | None = None,
    claimed: bool = False,
) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'title: "Test Successor {name}"',
        "created: 2026-01-01",
        "branch: work/test/2026-01-01",
        "status: claimed" if claimed else "status: open",
    ]
    if predecessor is not None:
        lines.append(f'predecessor: "{predecessor}"')
    if predecessor_id:
        lines.append(f'predecessor_id: "{predecessor_id}"')
    if claimed:
        lines.append('claimed_by: "test-session"')
        lines.append('claimed_at: "2026-01-01T00:00:00Z"')
    lines.append("deployment_state: active")
    fm = "\n".join(lines) + "\n"
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _supersede(repo: Path, predecessor_rel: str, continued_into: str, attested: bool) -> dict:
    """`attested` sets the process-local attestation ContextVar, exactly as
    `baton_assemble.apply`'s d6 does -- it is NOT a param, deliberately.

    A parameter would be forgeable through `housekeeping.cycle`'s verbatim
    passthrough of `params["transition"]`; see `_APPLY_MINTED_SUCCESSOR`'s own
    docstring. `test_a_supplied_attested_succession_param_is_ignored` below is
    the pin on that.
    """
    common_dir = _common_dir(repo)
    params = {
        "handoff_path": predecessor_rel,
        "mode": "supersede",
        "continued_into": continued_into,
    }
    if not attested:
        return _run(_archive_transition_handler(params, common_dir))
    token = _APPLY_MINTED_SUCCESSOR.set(continued_into)
    try:
        return _run(_archive_transition_handler(params, common_dir))
    finally:
        _APPLY_MINTED_SUCCESSOR.reset(token)


def test_admits_a_never_claimed_predecessor_and_a_freshly_minted_successor(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_predecessor(repo, "never-claimed.md", handoff_id="hnd-pred-abcdef")
    _seed_successor(
        repo, "successor.md",
        predecessor="state/handoffs/never-claimed.md",
        predecessor_id="hnd-pred-abcdef",
        claimed=False,
    )

    result = _supersede(
        repo, "state/handoffs/never-claimed.md", "state/handoffs/successor.md", attested=True
    )

    assert result["exit_code"] == 0, result
    assert result["superseded"] is True, result


def test_a_supplied_attested_succession_param_is_ignored(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_predecessor(repo, "never-claimed.md", handoff_id="hnd-pred-abcdef")
    _seed_successor(
        repo, "successor.md",
        predecessor="state/handoffs/never-claimed.md",
        predecessor_id="hnd-pred-abcdef",
        claimed=False,
    )

    result = _run(
        _archive_transition_handler(
            {
                "handoff_path": "state/handoffs/never-claimed.md",
                "mode": "supersede",
                "continued_into": "state/handoffs/successor.md",
                "attested_succession": True,
            },
            _common_dir(repo),
        )
    )

    assert result["exit_code"] == 1, result
    assert result["superseded"] is False, result
    assert "was never claimed or shipped" in result["error"], result
    assert "section 7.3" not in result["error"], (
        "the admission check must not even run for a param-only caller"
    )


def test_the_attestation_cannot_be_redirected_to_another_successor(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_predecessor(repo, "never-claimed.md", handoff_id="hnd-pred-abcdef")
    _seed_successor(
        repo, "successor.md",
        predecessor="state/handoffs/never-claimed.md",
        predecessor_id="hnd-pred-abcdef",
        claimed=False,
    )
    _seed_successor(
        repo, "other.md",
        predecessor="state/handoffs/never-claimed.md",
        predecessor_id="hnd-pred-abcdef",
        claimed=False,
    )

    token = _APPLY_MINTED_SUCCESSOR.set("state/handoffs/successor.md")
    try:
        result = _run(
            _archive_transition_handler(
                {
                    "handoff_path": "state/handoffs/never-claimed.md",
                    "mode": "supersede",
                    "continued_into": "state/handoffs/other.md",
                },
                _common_dir(repo),
            )
        )
    finally:
        _APPLY_MINTED_SUCCESSOR.reset(token)

    assert result["exit_code"] == 1, result
    assert result["superseded"] is False, result


def test_a_predecessor_with_no_handoff_id_is_not_refused_for_that_alone(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_predecessor(repo, "no-id.md", handoff_id=None)
    _seed_successor(
        repo, "successor.md",
        predecessor="state/handoffs/no-id.md",
        predecessor_id=None,
        claimed=False,
    )

    result = _supersede(
        repo, "state/handoffs/no-id.md", "state/handoffs/successor.md", attested=True
    )

    assert result["exit_code"] == 0, result
    assert result["superseded"] is True, result


def test_clause1_refuses_when_continued_into_does_not_resolve(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_predecessor(repo, "never-claimed.md")

    result = _supersede(
        repo, "state/handoffs/never-claimed.md", "state/handoffs/no-such-file.md", attested=True
    )

    assert result["exit_code"] == 1, result
    assert result["superseded"] is False, result
    assert "clause 1" in (result["error"] or ""), result


def test_clause3_refuses_on_name_matched_not_identity_checked_edge(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_predecessor(repo, "never-claimed.md", handoff_id="hnd-pred-abcdef")
    # Claimed, but its `predecessor:` names a DIFFERENT file — a name match
    _seed_successor(
        repo, "unrelated-successor.md",
        predecessor="state/handoffs/some-other-parent.md",
        claimed=True,
    )

    result = _supersede(
        repo, "state/handoffs/never-claimed.md", "state/handoffs/unrelated-successor.md",
        attested=True,
    )

    assert result["exit_code"] == 1, result
    assert result["superseded"] is False, result
    assert "clause 3" in (result["error"] or ""), result


def test_clause3_refuses_on_predecessor_id_disagreement(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_predecessor(repo, "never-claimed.md", handoff_id="hnd-pred-abcdef")
    _seed_successor(
        repo, "successor.md",
        predecessor="state/handoffs/never-claimed.md",
        predecessor_id="some-other-id",
        claimed=True,
    )

    result = _supersede(
        repo, "state/handoffs/never-claimed.md", "state/handoffs/successor.md", attested=True
    )

    assert result["exit_code"] == 1, result
    assert result["superseded"] is False, result
    assert "clause 3" in (result["error"] or ""), result


def test_clause4_refuses_when_predecessor_already_carries_a_different_edge(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_predecessor(
        repo, "never-claimed.md", handoff_id="hnd-pred-abcdef",
        continued_into="state/handoffs/already-recorded.md",
    )
    _seed_successor(
        repo, "successor.md",
        predecessor="state/handoffs/never-claimed.md",
        predecessor_id="hnd-pred-abcdef",
        claimed=True,
    )

    result = _supersede(
        repo, "state/handoffs/never-claimed.md", "state/handoffs/successor.md", attested=True
    )

    assert result["exit_code"] == 1, result
    assert result["superseded"] is False, result
    assert "clause 4" in (result["error"] or ""), result


def test_attested_succession_false_reproduces_todays_exact_refusal(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_predecessor(repo, "never-claimed.md", handoff_id="hnd-pred-abcdef")
    _seed_successor(
        repo, "successor.md",
        predecessor="state/handoffs/never-claimed.md",
        predecessor_id="hnd-pred-abcdef",
        claimed=True,
    )
    rel_id = "state/handoffs/never-claimed.md"

    without_param = _supersede(repo, rel_id, "state/handoffs/successor.md", attested=False)
    explicit_false = _run(_archive_transition_handler(
        {
            "handoff_path": rel_id,
            "mode": "supersede",
            "continued_into": "state/handoffs/successor.md",
            "attested_succession": False,
        },
        _common_dir(repo),
    ))

    expected_error = (
        f"mode='supersede' refused: {rel_id} was never claimed or shipped "
        "(DR-242: a successor-named child is not evidence of succession; "
        "nothing to supersede)"
    )
    for result in (without_param, explicit_false):
        assert result["exit_code"] == 1, result
        assert result["superseded"] is False, result
        assert result["error"] == expected_error, result


def test_claimed_or_shipped_at_path_unwidened_by_a_bare_successor_named_child(tmp_path):
    from coordinator_core.archival import claimed_or_shipped_at_path

    repo = tmp_path / "repo"
    _init_repo(repo)
    pred = _seed_predecessor(repo, "never-claimed.md", handoff_id="hnd-pred-abcdef")
    _seed_successor(
        repo, "hopeful-child.md",
        predecessor="state/handoffs/never-claimed.md",
        predecessor_id="hnd-pred-abcdef",
        claimed=False,
    )

    assert claimed_or_shipped_at_path(str(pred)) is False

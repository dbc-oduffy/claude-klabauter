"""
coordinator_core.ops.tests.test_archive_transition_attested_succession

Pins C1 (docs/plans/2026-09-12-supersede-admits-an-apply-minted-success.md):
`handoff.archive_transition`'s `mode="supersede"` block admits an attested
succession over a never-claimed predecessor ONLY when the caller supplies
`attested_succession=True` AND every one of DR-242 Amendment A2 section 7.3's
four admission clauses holds (docs/decisions/DR-242-successor-named-child-is-
not-evidence-of-succ.md § 7.3). Covers:

  - Admission when all four clauses are satisfied.
  - One test per clause proving its individual failure still refuses.
  - `attested_succession=False` reproduces today's exact refusal text
    byte-for-byte (the plan's own "behaves byte-identically to today"
    requirement).
  - `coordinator_core.archival.claimed_or_shipped_at_path` itself stays
    unwidened: a bare successor-named child (never claimed) does not flip a
    predecessor's own claimed-or-shipped verdict merely by existing.
"""

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
# NO registry-presence skip here, deliberately. `handoff.archive_transition`
# is in SUSPENDED_OPS and absent from the registry (kill ledger K-109/K-022),
# and `test_supersede_archives_atomically.py` skips on that because it
# dispatches THROUGH the registry. These tests do not: they call `_handler`
# as a library import, which is the same surviving route the live path takes
# (`baton_assemble.apply`'s d6 -> `housekeeping.cycle` ->
# `handoff_archive_transition._handler`, see that handler's own "ROUTED
# THROUGH `housekeeping.cycle`" note). Copying the sibling's guard here
# disabled this entire file -- it reported "1 skipped" inside an otherwise
# green summary while the behaviour it pins went unexercised, which is how
# the first cut of this plan shipped a fix that did not fix the reported bug.
#
# Negative-spec: do NOT reinstate a `_OP_NAME not in _REGISTRY` skip. Registry
# presence is not this file's reachability condition. If `_handler` itself is
# ever deleted, the import at the top fails loudly, which is the correct
# signal.


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
    """A live, NEVER-claimed-or-shipped predecessor (status: open, no claim
    fields, deployment_state: active) — the shape every test here refuses or
    conditionally admits."""
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
    """A live successor. `predecessor` is the repo-relative path it names via
    its own `predecessor:` field (None omits the field entirely). `claimed`
    controls whether IT is itself `claimed_or_shipped_at_path` (clause 2)."""
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


# ---------------------------------------------------------------------------
# Admission — all four clauses satisfied
# ---------------------------------------------------------------------------


def test_admits_a_never_claimed_predecessor_and_a_freshly_minted_successor(tmp_path):
    """The DoE-claude case, and the whole point of the change.

    The successor here is NEVER CLAIMED -- the freshly-minted shape apply's d6
    actually presents. A2 section 7.3 clause 2 ("the successor is itself
    claimed-or-shipped"), implemented literally, refuses this; that is the
    defect the first cut of this plan shipped past, going green because its
    suite only ever seeded an already-claimed successor. Clause 2 is discharged
    by provenance on this door. If a literal clause-2 check is ever reinstated,
    this goes red.

    (Collapsed from two byte-equivalent tests per overengineering review.)
    """
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
    """THE security pin. A caller-supplied parameter must not open this door.

    `housekeeping.cycle` forwards `params["transition"]` verbatim into this
    handler, so `python -m coordinator_core.invoke housekeeping.cycle` can put
    any key it likes in front of it. That was reached and confirmed by probe on
    2026-09-12 against the first cut, which read the flag off `params` -- it
    would have let any caller supersede a never-claimed predecessor by naming a
    speculative child that merely carried matching `predecessor`/
    `predecessor_id` text, which is DR-242 section 3's hole reopened.

    The attestation is a process-local ContextVar precisely so a separate
    process cannot set it. Here the ContextVar is NOT set and the param IS
    supplied: the refusal must be the ordinary DR-242 one.
    """
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
    """In-process, the token names ONE successor and admits only that one.

    A bare boolean would admit whatever `continued_into` happened to be stamped;
    the token is the path apply minted, and the op requires them to match.
    """
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
    """DR-102's backfill grandfathered `archive/handoffs/`, and an archived
    predecessor is mode='supersede'"'"'s common call shape -- so a pre-backfill
    vintage legitimately carries no `handoff_id`. Refusing there would reproduce
    the stranding this amendment closes, for that predecessor shape alone.

    The path-identity leg of clause 3 carries the edge in that case; the id pair
    binds only when there IS an id. (Code-review P2.)
    """
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


# ---------------------------------------------------------------------------
# Per-clause refusal
# ---------------------------------------------------------------------------


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
    # to the candidate is never supplied, so the identity check fails.
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
        predecessor_id="some-other-id",  # disagrees with the predecessor's own handoff_id
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


# ---------------------------------------------------------------------------
# attested_succession=False — byte-identical to today's refusal
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# claimed_or_shipped_at_path itself stays unwidened
# ---------------------------------------------------------------------------


def test_claimed_or_shipped_at_path_unwidened_by_a_bare_successor_named_child(tmp_path):
    """A hopeful successor-named child existing on disk must NOT, by itself,
    flip `claimed_or_shipped_at_path` for the predecessor it names — that
    predicate reads only the candidate's OWN frontmatter (DR-242 § 2
    Negative-spec) and this plan's Anti-scope forbids widening it. Exercised
    against the live predicate, not a source diff."""
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

"""Both commit routes attach BOTH trailers, measured per route rather than on
a sample of one (AC5, docs/plans/2026-08-30-the-c-door-sends-the-callers-
session-identity.md).

THE DEFECT. The two routes disagreed, each in its own direction, and the
disagreement is what made the trailer unusable as an attribution key:

  * through `ceremony.commit_v2` -- NEITHER trailer. That route lands via
    `commit_paths`' `commit-tree` plumbing, which fires no git hooks, so the
    `prepare-commit-msg` attach point never ran and nothing replaced it.
  * through the older, hook-driven route -- `Session-Id` attached,
    `Deliverable-Id` not. The commits in the census window that DO carry a
    `Deliverable-Id` carry it because a human typed it into the message,
    which is not attachment and does not generalise.

Absent on one route and wrong on another means nothing downstream can tell
the two apart FROM THE TRAILER ALONE, so anything measuring per-session or
per-deliverable scope from it -- review-scope, brightline, close-out
measurement -- is measuring something else.

WHY A PARITY ASSERTION AND NOT TWO INDEPENDENT ONES. A single-route probe
certifies whichever route it happened to pick; that is exactly how the
disagreement survived. `test_both_routes_agree_on_the_same_trailer_set`
below is therefore the load-bearing test: it fails if EITHER route drifts,
including toward a future route that resolves correctly but differently. The
per-route tests exist to say WHICH one moved when parity breaks.

NEGATIVE SPEC -- what this file exists to stop coming back:
  * A route growing its own trailer-resolution copy instead of calling the
    shared decision function. That is the shape the census caught, and
    `test_commit_v2_reaches_the_shared_applier` pins the one route that had
    no attach point at all.
  * A fix that attaches `Session-Id` and quietly leaves `Deliverable-Id`
    alone. Both trailers, or the assertion is not AC5's.
  * Reading a green here as covering the WARM identity question. It does
    not: these tests run cold on purpose (see `apply_missing_trailers`'s
    warm branch, pinned by `test_commit_trailers_warm_identity.py`).
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.git.commit_trailers import (
    apply_missing_trailers,
    compute_missing_trailer_args,
    trailer_values_from_argv,
)

# Real git is load-bearing for the same reason the sibling suite says it is:
# `compute_missing_trailer_args` resolves through `git rev-parse --git-dir`,
# which no mocked seam reproduces.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SID = "34343434-3434-4343-8343-343434343434"
_DLV = "dlv-both-routes-attach-both-trailers"

REPO_ROOT = Path(__file__).resolve().parents[3]

_BARE_MESSAGE = "chore: a commit whose author typed no trailer\n"


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """A repo where BOTH trailers resolve, so an omission is a real finding.

    A fixture where only one of the two is resolvable cannot tell "the route
    dropped it" from "there was nothing to attach" -- the ambiguity that let
    the `Deliverable-Id` half of this defect sit unnoticed.
    """
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init", "-q"], r)
    _git(["config", "user.email", "t@t.example"], r)
    _git(["config", "user.name", "t"], r)

    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    shape_dir = r / ".git" / "coordinator-sessions" / _SID
    shape_dir.mkdir(parents=True, exist_ok=True)
    (shape_dir / "session-shape.json").write_text(
        json.dumps({"pickup": {"deliverable_id": _DLV}}), encoding="utf-8"
    )
    return r


def _msg_file(repo: Path, text: str) -> Path:
    p = repo / "MSG"
    p.write_text(text, encoding="utf-8")
    return p


def _trailers_of(message: str) -> dict:
    return {
        m.group(1): m.group(2)
        for m in re.finditer(
            r"^(Session-Id|Deliverable-Id): (.+)$", message, flags=re.MULTILINE
        )
    }


def _route_hook_driven(repo: Path) -> dict:
    """The older route's decision, reached the way the hook reaches it.

    `coordinator/bin/coordinator-prepare-commit-msg.py` and
    `ceremony.git_native._apply_trailers` both bottom out on
    `compute_missing_trailer_args` over the message FILE, which is why this
    is the route's real decision point and not a stand-in for it.
    """
    argv = compute_missing_trailer_args(_msg_file(repo, _BARE_MESSAGE), repo)
    return _trailers_of("\n".join(trailer_values_from_argv(argv)))


def _route_commit_v2(repo: Path) -> dict:
    """`ceremony.commit_v2`'s decision, reached through its own attach point.

    That route commits via `commit_paths`' `commit-tree` plumbing and fires
    no git hooks, so `apply_missing_trailers` is its ONLY attach point -- the
    thing whose absence made this route carry neither trailer.
    """
    return _trailers_of(apply_missing_trailers(_BARE_MESSAGE, repo))


def test_hook_driven_route_attaches_both_trailers(repo):
    got = _route_hook_driven(repo)
    assert got == {"Session-Id": _SID, "Deliverable-Id": _DLV}, (
        "the hook-driven route did not attach both trailers -- historically it "
        f"attached Session-Id and never Deliverable-Id. Got: {got}"
    )


def test_commit_v2_route_attaches_both_trailers(repo):
    got = _route_commit_v2(repo)
    assert got == {"Session-Id": _SID, "Deliverable-Id": _DLV}, (
        "the ceremony.commit_v2 route did not attach both trailers -- "
        "historically it attached NEITHER, because commit-tree fires no hooks "
        f"and nothing replaced the hook attach point. Got: {got}"
    )


def test_both_routes_agree_on_the_same_trailer_set(repo):
    """The load-bearing one: a single-route probe certifies the wrong route.

    This is the assertion the row asked for in the words it asked for --
    verify PER ROUTE, never on a sample of one. It fails if either route
    drifts, including toward a value that is individually plausible.
    """
    hook = _route_hook_driven(repo)
    v2 = _route_commit_v2(repo)
    assert hook == v2, (
        "the two commit routes disagree on the trailers they attach for the "
        f"same commit in the same repo: hook-driven={hook}, commit_v2={v2}. "
        "Nothing downstream can tell a missing trailer from a wrong one, which "
        "is what made the attribution key unusable."
    )


def test_commit_v2_reaches_the_shared_applier():
    """A route must not grow its own copy of the resolution.

    Structural, not behavioural, on purpose: the behavioural tests above pass
    the moment a route resolves correctly BY ANY MEANS, including a private
    reimplementation that then drifts. The census defect was two routes each
    deciding for itself; the fix was one shared decision, and this is the
    assertion that the sharing survives.
    """
    src = (REPO_ROOT / "coordinator_core" / "ops" / "ceremony" / "commit_v2.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "apply_missing_trailers" in called, (
        "ceremony.commit_v2 no longer calls apply_missing_trailers. That route "
        "fires no git hooks, so this is its only trailer attach point -- "
        "removing the call returns it to attaching NEITHER trailer, silently."
    )

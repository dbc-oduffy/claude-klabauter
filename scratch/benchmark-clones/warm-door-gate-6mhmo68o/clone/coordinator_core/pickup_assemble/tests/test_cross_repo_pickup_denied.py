"""
coordinator_core.pickup_assemble.tests.test_cross_repo_pickup_denied — C9
(docs/plans/2026-08-19-batons-unify-into-one-successor.md § C9).

Purpose: proves the PM ruling (D-G) that a pickup EM from a different repo
than the artifact is DENIED BY NAME — `apply()` now runs the existing
containment bound (`apply_base.assert_in_repo_root`, the same primitive
every dispatch handler already calls) once, up front, and turns its
`OutOfRepoPath` into a distinguishable `reason: "cross_repo_pickup_denied"`
report rather than letting a foreign path surface as a generic
`APPLY_EXIT_PARTIAL_MUTATION` path error from whichever directive happened
to touch it first.

Coverage:
  - a foreign-repo artifact path is denied with the named reason
    (`APPLY_EXIT_CLAIM_DENIED`, `report["reason"] == "cross_repo_pickup_denied"`)
  - a malformed (not-found) path still gets the ordinary path-error refusal
    (`APPLY_EXIT_TRANSPORT_FAIL`, no `reason` key) — the two are
    distinguishable
  - an inbox memo living in THIS repo's own `cross-repo/inbox/` is NOT
    denied — the check keys on containment under `root`, never on the
    string "cross-repo" appearing in a path

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_cross_repo_pickup_denied.py -q
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

import coordinator_core.pickup_assemble.apply as pa_apply
from coordinator_core.pickup_assemble import BriefResult

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _make_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(parents=True, exist_ok=True)


def _patch_brief(monkeypatch, decision_object: dict[str, Any], exit_code: int = 0) -> None:
    def _fake_brief(
        artifact_path: str,
        *,
        decisions: Optional[dict[str, Any]] = None,
        repo_root: Optional[Path] = None,
    ) -> BriefResult:
        return BriefResult(decision_object, exit_code)

    monkeypatch.setattr(pa_apply, "brief", _fake_brief)
    # No session-scoped decision file to read for these fixtures — degrade
    # to an empty dispositions map (this module's own documented "absent
    # file is never fatal" contract), never touch the real one on disk.
    monkeypatch.setattr(
        pa_apply, "_read_session_dispositions", lambda root, sid, artifact_path: {}
    )


class TestForeignRepoDenied:
    def test_foreign_repo_handoff_path_denied_by_name(self, tmp_path, monkeypatch):
        this_repo = tmp_path / "this-repo"
        foreign_repo = tmp_path / "foreign-repo"
        _make_repo(this_repo)
        _make_repo(foreign_repo)
        foreign_handoff = foreign_repo / "state" / "handoffs" / "2026-08-19-foo.md"
        foreign_handoff.parent.mkdir(parents=True, exist_ok=True)
        foreign_handoff.write_text("---\nstatus: open\n---\nbody\n", encoding="utf-8")

        decision_object = {
            "directives": [],
            "judgment_points": [],
            "artifact": {
                "path": str(foreign_handoff),
                "classification": "handoff",
                "frontmatter": {},
            },
        }
        _patch_brief(monkeypatch, decision_object)

        exit_code, report = pa_apply.apply(
            str(foreign_handoff), session_id="sess-cross-repo", repo_root=this_repo
        )

        assert exit_code == pa_apply.APPLY_EXIT_CLAIM_DENIED
        assert report["reason"] == "cross_repo_pickup_denied"
        assert str(foreign_repo) in report["error"] or str(foreign_handoff) in report["error"]
        assert report["landed"] == []


class TestMalformedPathDistinguishable:
    def test_not_found_path_gets_ordinary_path_error_not_named_denial(self, tmp_path, monkeypatch):
        this_repo = tmp_path / "this-repo"
        _make_repo(this_repo)

        exit_code, report = pa_apply.apply(
            "state/handoffs/does-not-exist.md",
            session_id="sess-local",
            repo_root=this_repo,
        )

        assert exit_code == pa_apply.APPLY_EXIT_TRANSPORT_FAIL
        assert "reason" not in report


class TestInRepoInboxMemoNotDenied:
    def test_inbox_memo_in_this_repo_is_not_denied(self, tmp_path, monkeypatch):
        this_repo = tmp_path / "this-repo"
        _make_repo(this_repo)
        # A clean run's scoped-commit tail (`APPLY_EXIT_OK` commits) shells a
        # real `git add`/`git commit` against `repo_root` — irrelevant to
        # what THIS test asserts (that the cross-repo check does not fire
        # for an in-repo artifact), so it is stubbed out rather than paying
        # for a real git init just to exercise unrelated commit plumbing.
        monkeypatch.setattr(pa_apply, "_scoped_commit", lambda *a, **k: None)
        inbox_memo = this_repo / "cross-repo" / "inbox" / "2026-08-19-memo.md"
        inbox_memo.parent.mkdir(parents=True, exist_ok=True)
        inbox_memo.write_text("---\nfrom: a\nto: b\nstatus: open\n---\nbody\n", encoding="utf-8")

        decision_object = {
            "directives": [],
            "judgment_points": [],
            "artifact": {
                "path": "cross-repo/inbox/2026-08-19-memo.md",
                "classification": "memo",
                "frontmatter": {},
            },
        }
        _patch_brief(monkeypatch, decision_object)

        exit_code, report = pa_apply.apply(
            "cross-repo/inbox/2026-08-19-memo.md",
            session_id="sess-local",
            repo_root=this_repo,
        )

        assert report.get("reason") != "cross_repo_pickup_denied"
        assert exit_code != pa_apply.APPLY_EXIT_CLAIM_DENIED


# Full handoff frontmatter, deliberately not the stub the classes above use:
# `drop` resolves the artifact ITSELF rather than reading `brief`'s decision
# object, so an incomplete record classifies `ambiguous` and returns a
# transport failure before containment is ever consulted — a green test
# proving nothing.
_FOREIGN_HANDOFF_FM = """---
title: "Foo"
created: 2026-08-19
branch: work/test/2026-08-19
predecessor: "none"
category: infra
summary: "a foreign-repo baton"
kind: session-handoff
status: open
deployment_state: in_flight
---

body
"""


def _seed_foreign_handoff(foreign_repo: Path) -> Path:
    path = foreign_repo / "state" / "handoffs" / "2026-08-19-foo.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_FOREIGN_HANDOFF_FM, encoding="utf-8")
    return path


class TestDropGetsTheSameNamedDenial:
    """`drop` is `apply`'s inverse and D-G binds it identically. Before this,
    only the forward verb carried the denial. Reproduced against the pre-fix
    `drop` (containment bound stubbed out): a foreign-repo drop raised an
    uncaught `apply_base.OutOfRepoPath` out of `cs_unclaim_handoff` — a
    stack trace where `apply` gives a reason — AND did so having already
    reported `released: True`, i.e. the claim was gone. Hence the gate sits
    ahead of `_session_identity` and both primitives: a drop that will be
    denied must release nothing.

    The reason string is its own (`cross_repo_drop_denied`), so a report
    never conflates which verb refused.
    """

    def test_foreign_repo_drop_denied_by_name(self, tmp_path):
        this_repo = tmp_path / "this-repo"
        foreign_repo = tmp_path / "foreign-repo"
        _make_repo(this_repo)
        _make_repo(foreign_repo)
        foreign_handoff = _seed_foreign_handoff(foreign_repo)

        exit_code, report = pa_apply.drop(
            str(foreign_handoff), session_id="sess-cross-repo", repo_root=this_repo
        )

        assert exit_code == pa_apply.APPLY_EXIT_CLAIM_DENIED
        assert report["reason"] == "cross_repo_drop_denied"
        assert str(foreign_handoff) in report["error"]
        # Nothing released or stamped on the way to the denial — the pre-fix
        # path reported `released: True` before blowing up.
        assert report["unclaimed"] is None
        assert "released" not in report

    def test_relative_escape_is_denied_too_not_only_an_absolute_path(self, tmp_path):
        """`../foreign-repo/...` classifies exactly the way an in-repo path
        does, so the denial must key on RESOLVED containment rather than on
        the supplied string being absolute."""
        this_repo = tmp_path / "this-repo"
        foreign_repo = tmp_path / "foreign-repo"
        _make_repo(this_repo)
        _make_repo(foreign_repo)
        _seed_foreign_handoff(foreign_repo)

        exit_code, report = pa_apply.drop(
            "../foreign-repo/state/handoffs/2026-08-19-foo.md",
            session_id="sess-cross-repo",
            repo_root=this_repo,
        )

        assert exit_code == pa_apply.APPLY_EXIT_CLAIM_DENIED
        assert report["reason"] == "cross_repo_drop_denied"

    def test_inbox_memo_in_this_repo_is_not_denied_on_drop(self, tmp_path, monkeypatch):
        this_repo = tmp_path / "this-repo"
        _make_repo(this_repo)
        # Same stub, same reason, as the sibling class's in-repo case: a
        # non-denied drop shells a real `git add`/`git commit` against
        # `repo_root`, which is unrelated to what this test asserts.
        monkeypatch.setattr(pa_apply, "_scoped_commit", lambda *a, **k: None)
        inbox_memo = this_repo / "cross-repo" / "inbox" / "2026-08-19-memo.md"
        inbox_memo.parent.mkdir(parents=True, exist_ok=True)
        inbox_memo.write_text(
            "---\nfrom: a\nto: b\nstatus: open\n---\nbody\n", encoding="utf-8"
        )

        exit_code, report = pa_apply.drop(
            "cross-repo/inbox/2026-08-19-memo.md",
            session_id="sess-local",
            repo_root=this_repo,
        )

        assert report.get("reason") != "cross_repo_drop_denied"
        assert exit_code != pa_apply.APPLY_EXIT_CLAIM_DENIED


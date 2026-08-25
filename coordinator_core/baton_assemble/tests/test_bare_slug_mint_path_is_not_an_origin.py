"""Regression test for the 2026-08-25 break-class fix: a bare-slug MINT PATH
that is already occupied is this run's own prior attempt, never a lineage
origin.

Reproduces the live break recorded in bug backlog
`2026-08-25-spinoff-brief-then-apply-mints-two-batons-and-adopts-the-stub-as-
origin.yaml` (session f15266ee, 2026-08-25). Two `baton-assemble apply spinoff
<slug>` calls eight seconds apart -- the first's stdout swallowed by a
`Select-Object -First 120` pipe, so the operator could not see it had already
landed -- left TWO `pickup_ready` batons on disk for one topic:

  - `state/handoffs/<date>-<slug>.md`            (attempt 1, untouched scaffold)
  - `state/handoffs/<date>_<HHMMSS>_<slug>.md`   (attempt 2, authored, committed)

and attempt 2 carried attempt 1 as its OWN `origin_handoff`/`origin_handoff_id`
-- provenance pointing at a file a tidy-up deletes, which is worse than absent
provenance because nothing reads as missing.

Two causes, one ordering bug. `resolve_lineage` tested `_artifact_fm_path.is_file()`
BEFORE `was_bare_slug`, so the occupied mint path was read as frontmatter and its
`handoff_id` satisfied the `kind == "spinoff"` branch's "is this a genuine
pre-existing origin baton" gate; separately, `apply spinoff` had no convergence
path at all (kind=handoff reaches one through the predecessor-side
`continued_into` record a spinoff never writes), so the re-run's collision
ladder minted beside the first file rather than onto it.

Spec backlink: `state/bug-backlog/2026-08-25-spinoff-brief-then-apply-mints-two-
batons-and-adopts-the-stub-as-origin.yaml`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.test_baton_assemble import _write_artifact

_THIS_RUN_SESSION = "sid-this-run-bare-slug-mint"
_PEER_SESSION = "sid-a-peer-session"
_SLUG = "track-touched-files-cheaper-rebuild"


@pytest.fixture(autouse=True)
def _this_run_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _THIS_RUN_SESSION)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _mint_rel() -> str:
    """The path `_normalize_artifact_path` derives from the bare slug -- the
    single value both the occupied-path fixture and the assertions key on, so
    the test can never drift from the normalizer it exercises."""
    return ba._normalize_artifact_path(_SLUG)


def _occupy_mint_path(
    root: Path,
    *,
    session: str = _THIS_RUN_SESSION,
    kind: str = "spinoff",
    extra: list[str] | None = None,
) -> Path:
    return _write_artifact(
        root / _mint_rel(),
        [
            f"kind: {kind}",
            "handoff_id: hnd-prior-attempt-a3ef09",
            "deliverable_id: dlv-prior-attempt-a97733",
            f"authoring_session: {session}",
            "pickup_ready: true",
            *(extra or []),
        ],
    )


class TestOccupiedMintPathIsNeverAnOrigin:
    """The half that corrupted provenance: whoever occupies the mint path, its
    frontmatter must not reach `origin_handoff`/`origin_handoff_id`."""

    def test_own_prior_attempt_is_not_adopted_as_origin(self, tmp_path):
        _occupy_mint_path(tmp_path)

        lineage = ba.resolve_lineage("spinoff", _SLUG, tmp_path)

        assert lineage["origin_handoff"] is None
        assert lineage["origin_handoff_id"] is None

    def test_peer_session_artifact_is_not_adopted_as_origin_either(self, tmp_path):
        """A same-slug baton belonging to ANOTHER session is not this run's
        residue -- but it is not an origin either. The decline routes to a
        fresh mint, never to a stamped provenance edge."""
        _occupy_mint_path(tmp_path, session=_PEER_SESSION)

        lineage = ba.resolve_lineage("spinoff", _SLUG, tmp_path)

        assert lineage["origin_handoff"] is None
        assert lineage["origin_handoff_id"] is None


class TestBareSlugReplayConvergesOntoOnePath:
    """The half that minted a second baton: re-running the identical bare-slug
    call must re-use this run's own prior output."""

    def test_own_prior_attempt_is_adopted_as_output_path(self, tmp_path):
        prior = _occupy_mint_path(tmp_path)

        lineage = ba.resolve_lineage("spinoff", _SLUG, tmp_path)

        assert lineage["adopted_mint_path"] == _mint_rel()
        assert Path(lineage["output_path"]).as_posix() == _mint_rel()
        assert (tmp_path / lineage["output_path"]).resolve() == prior.resolve()

    def test_first_run_is_unchanged(self, tmp_path):
        """Nothing on disk at the mint path: the pre-existing fresh-mint shape,
        byte-identical to before this fix."""
        lineage = ba.resolve_lineage("spinoff", _SLUG, tmp_path)

        assert lineage["adopted_mint_path"] is None
        assert Path(lineage["output_path"]).as_posix() == _mint_rel()

    def test_peer_session_artifact_is_not_adopted_as_output(self, tmp_path):
        """Declining to adopt still costs a second file -- but never one
        authored OVER a peer's baton."""
        _occupy_mint_path(tmp_path, session=_PEER_SESSION)

        lineage = ba.resolve_lineage("spinoff", _SLUG, tmp_path)

        assert lineage["adopted_mint_path"] is None
        assert Path(lineage["output_path"]).as_posix() != _mint_rel()

    def test_different_kind_at_the_same_slug_is_not_adopted(self, tmp_path):
        """A `handoff` and a `spinoff` briefed from one slug collide on a name
        and are different artifacts; adopting across kinds authors one over the
        other."""
        _occupy_mint_path(tmp_path, kind="handoff")

        lineage = ba.resolve_lineage("spinoff", _SLUG, tmp_path)

        assert lineage["adopted_mint_path"] is None
        assert Path(lineage["output_path"]).as_posix() != _mint_rel()

    def test_a_symlink_escaping_state_handoffs_is_not_adopted(self, tmp_path):
        """The containment guard, exercised rather than merely read. A mint
        path that resolves OUTSIDE live `state/handoffs/` must decline: d1
        writes `output_path`, so adopting an escape would author a baton
        wherever the link pointed. Review: coordinator:code-reviewer
        (ab5f5c7c) Finding 5."""
        outside = _write_artifact(
            tmp_path / "elsewhere" / "decoy.md",
            [
                "kind: spinoff",
                "handoff_id: hnd-decoy-000000",
                f"authoring_session: {_THIS_RUN_SESSION}",
            ],
        )
        link = tmp_path / _mint_rel()
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted on this platform/account")

        assert ba._adopt_prior_attempt_mint_path(_mint_rel(), tmp_path, "spinoff") is None

    def test_continued_prior_attempt_is_not_adopted(self, tmp_path):
        """A completed link in a longer chain is not a prior attempt's
        output."""
        _occupy_mint_path(tmp_path, extra=["deployment_state: continued"])

        lineage = ba.resolve_lineage("spinoff", _SLUG, tmp_path)

        assert lineage["adopted_mint_path"] is None
        assert Path(lineage["output_path"]).as_posix() != _mint_rel()


class TestAdoptedReplayEmitsASatisfiedD1:
    """End of the chain: the adopted path must reach the directive table as a
    SKIPPED d1, and the write-over-input backstop must not refuse the one shape
    whose entire purpose is to not write."""

    def test_d1_is_already_satisfied_and_the_backstop_stays_silent(self, tmp_path):
        _occupy_mint_path(tmp_path)
        lineage = ba.resolve_lineage("spinoff", _SLUG, tmp_path)

        directives = ba._build_directives("spinoff", lineage, title="T", root=tmp_path)
        d1 = next(d for d in directives if d["id"] == "d1")

        assert d1["already_satisfied"] is True
        # Would raise ValueError before the fix narrowed it to dispatched
        # directives -- d1's `--out` IS the existing normalized artifact_path.
        ba._assert_no_directive_writes_over_input(directives, _mint_rel(), tmp_path)

    def test_backstop_still_refuses_a_dispatched_write_over_an_input(self, tmp_path):
        """Negative control: the narrowing must not disarm the guard for a
        directive that actually fires."""
        _write_artifact(tmp_path / "docs" / "plans" / "live-input.md", ["plan_id: PID"])
        directives = [
            {
                "id": "d1",
                "cli": "coordinator-doc-new",
                "args": ["--out=docs/plans/live-input.md"],
                "already_satisfied": False,
            }
        ]

        with pytest.raises(ValueError, match="would write its output to"):
            ba._assert_no_directive_writes_over_input(
                directives, "docs/plans/live-input.md", tmp_path
            )

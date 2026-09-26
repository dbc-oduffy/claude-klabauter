
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

    @pytest.mark.spawns_process
    @pytest.mark.cadence
    def test_own_prior_attempt_is_not_adopted_as_origin(self, tmp_path):
        _occupy_mint_path(tmp_path)

        lineage = ba.resolve_lineage("spinoff", _SLUG, tmp_path)

        assert lineage["origin_handoff"] is None
        assert lineage["origin_handoff_id"] is None

    @pytest.mark.spawns_process
    @pytest.mark.cadence
    def test_peer_session_artifact_is_not_adopted_as_origin_either(self, tmp_path):
        _occupy_mint_path(tmp_path, session=_PEER_SESSION)

        lineage = ba.resolve_lineage("spinoff", _SLUG, tmp_path)

        assert lineage["origin_handoff"] is None
        assert lineage["origin_handoff_id"] is None


class TestBareSlugReplayConvergesOntoOnePath:

    @pytest.mark.spawns_process
    @pytest.mark.cadence
    def test_own_prior_attempt_is_adopted_as_output_path(self, tmp_path):
        prior = _occupy_mint_path(tmp_path)

        lineage = ba.resolve_lineage("spinoff", _SLUG, tmp_path)

        assert lineage["adopted_mint_path"] == _mint_rel()
        assert Path(lineage["output_path"]).as_posix() == _mint_rel()
        assert (tmp_path / lineage["output_path"]).resolve() == prior.resolve()

    @pytest.mark.spawns_process
    @pytest.mark.cadence
    def test_first_run_is_unchanged(self, tmp_path):
        lineage = ba.resolve_lineage("spinoff", _SLUG, tmp_path)

        assert lineage["adopted_mint_path"] is None
        assert Path(lineage["output_path"]).as_posix() == _mint_rel()

    @pytest.mark.spawns_process
    @pytest.mark.cadence
    def test_peer_session_artifact_is_not_adopted_as_output(self, tmp_path):
        _occupy_mint_path(tmp_path, session=_PEER_SESSION)

        lineage = ba.resolve_lineage("spinoff", _SLUG, tmp_path)

        assert lineage["adopted_mint_path"] is None
        assert Path(lineage["output_path"]).as_posix() != _mint_rel()

    @pytest.mark.spawns_process
    @pytest.mark.cadence
    def test_different_kind_at_the_same_slug_is_not_adopted(self, tmp_path):
        _occupy_mint_path(tmp_path, kind="handoff")

        lineage = ba.resolve_lineage("spinoff", _SLUG, tmp_path)

        assert lineage["adopted_mint_path"] is None
        assert Path(lineage["output_path"]).as_posix() != _mint_rel()

    def test_a_symlink_escaping_state_handoffs_is_not_adopted(self, tmp_path):
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

    @pytest.mark.spawns_process
    @pytest.mark.cadence
    def test_continued_prior_attempt_is_not_adopted(self, tmp_path):
        _occupy_mint_path(tmp_path, extra=["deployment_state: continued"])

        lineage = ba.resolve_lineage("spinoff", _SLUG, tmp_path)

        assert lineage["adopted_mint_path"] is None
        assert Path(lineage["output_path"]).as_posix() != _mint_rel()


class TestAdoptedReplayEmitsASatisfiedD1:

    @pytest.mark.spawns_process
    @pytest.mark.cadence
    def test_d1_is_already_satisfied_and_the_backstop_stays_silent(self, tmp_path):
        _occupy_mint_path(tmp_path)
        lineage = ba.resolve_lineage("spinoff", _SLUG, tmp_path)

        directives = ba._build_directives("spinoff", lineage, title="T", root=tmp_path)
        d1 = next(d for d in directives if d["id"] == "d1")

        assert d1["already_satisfied"] is True
        ba._assert_no_directive_writes_over_input(directives, _mint_rel(), tmp_path)

    def test_backstop_still_refuses_a_dispatched_write_over_an_input(self, tmp_path):
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

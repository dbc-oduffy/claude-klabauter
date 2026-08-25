"""C10 (docs/plans/2026-08-19-batons-unify-into-one-successor.md § C10):
spinoff authoring must never trip `pickup_assemble.compute_baton_unification_
verdict`'s unification routing.

THE DISCRIMINATION IS THE CLAIM, NOT THE MINT. Unification triggers on
ADOPTING a CLAIMED artifact (`compute_baton_unification_verdict`'s own held-
set read, `pickup_assemble/__init__.py` § (a)); a spinoff is never claimed by
its own author at mint time -- this plan's own predecessor was itself authored
that way, by a session holding a DIFFERENT claimed baton. The fix is not a
`kind == "spinoff"` special case anywhere in the unification-trigger path
(none exists -- `baton_assemble.apply` never imports `pickup_assemble`'s
unification surface at all); it is asserting the underlying property so it
cannot silently drift: a freshly-minted spinoff never carries its own claim.

Spec backlink: this plan's own C5 (`_baton_unification_routing_enabled`,
`pickup_assemble/__init__.py`) is the only unification trigger in this repo;
`coordinator_core/baton_assemble/apply.py`'s `_assert_spinoff_mint_not_self_
claimed` is the property assertion under test here.
"""

from __future__ import annotations

import inspect

import pytest

from coordinator_core import baton_assemble as ba
from coordinator_core.baton_assemble import apply as ba_apply
from coordinator_core.session import claims as session_claims
from coordinator_core.test_baton_assemble import (
    _FAKE_OPERATOR_CONFIG,
    _REPO_MAKIMA_BIN,
    _init_repo,
    _write_artifact,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))
    monkeypatch.setattr(ba_apply, "_resolve_makima_bin", lambda: _REPO_MAKIMA_BIN)


def _seed_handoff_claim(repo_root, session_id: str, basename: str, claimed_at: str) -> None:
    """The durable claim ledger record `_resolve_held_handoff_for_session`
    (and therefore `compute_baton_unification_verdict`'s held-set read)
    consults -- mirrors `test_j_divergent_deliverable_id.py`'s own helper of
    the same name, restated because it has no shared home."""
    claims_dir = repo_root / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    claims_dir.mkdir(parents=True, exist_ok=True)
    (claims_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claims_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")


class TestApplyNeverReachesUnification:
    """Structural half: `apply.py` has no call path into pickup_assemble's
    unification surface at all, for any kind -- so the exclusion cannot drift
    into a special case, because there is nothing to special-case."""

    def test_apply_module_never_imports_the_unification_surface(self):
        """Scans actual `import` statements only (never prose/docstrings, this
        module's own docstrings among them, which legitimately NAME these
        symbols while explaining why they are absent from the code)."""
        import_lines = [
            line.strip()
            for line in inspect.getsource(ba_apply).splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for forbidden in (
            "compute_baton_unification_verdict",
            "_unify_into_successor",
            "_baton_unification_routing_enabled",
        ):
            assert not any(forbidden in line for line in import_lines), (
                f"baton_assemble.apply must never import {forbidden!r} -- "
                "unification is triggered only on pickup_assemble's adopt "
                "path, never from authoring/minting"
            )


class TestFreshSpinoffMintIsNeverSelfClaimed:
    """Unit half: `_warn_if_spinoff_mint_self_claimed` proves, on every
    run, that a spinoff's own mint never lands self-claimed -- the one shape
    that could ever make a held-set read pick it up."""

    def test_an_unclaimed_fresh_mint_passes(self, tmp_path):
        rel = "state/handoffs/2026-08-19-fresh-spinoff.md"
        _write_artifact(tmp_path / rel, ["deliverable_id: DEL-1"])

        ba_apply._warn_if_spinoff_mint_self_claimed(tmp_path, rel)

    def test_a_self_claimed_mint_is_reported_observably(self, tmp_path, capsys):
        """Regression guard: if a future change ever stamped `status:
        claimed`/`claimed_by` onto a spinoff's OWN freshly-minted file, that
        must SURFACE rather than silently opening the unification-on-mint hole
        this chunk exists to keep shut.

        Observable, not fatal, and not a bare `assert` — this check runs AFTER
        the mint has been committed, so raising would destroy the report of a
        mutation already on disk, and a bare `assert` vanishes under `python
        -O`. The structural drift guard is `TestApplyNeverReachesUnification`;
        this is the belt to that braces."""
        rel = "state/handoffs/2026-08-19-self-claimed-spinoff.md"
        _write_artifact(
            tmp_path / rel,
            ["deliverable_id: DEL-1", "status: claimed", "claimed_by: sid-author"],
        )

        ba_apply._warn_if_spinoff_mint_self_claimed(tmp_path, rel)

        err = capsys.readouterr().err
        assert "landed self-claimed" in err
        assert rel in err

    def test_a_missing_file_declines_silently(self, tmp_path):
        """Best-effort: nothing to assert about a file this run did not
        commit."""
        ba_apply._warn_if_spinoff_mint_self_claimed(tmp_path, "state/handoffs/absent.md")


class TestAuthoringASpinoffLeavesTheHeldClaimUntouched:
    """End-to-end half, per this chunk's own spec: author a spinoff while
    holding a claimed baton; assert no unification fired and the held claim
    is unchanged."""

    def _fake_execute_directives(
        self, directives, judgment_points, repo_root, *, decisions=None, composition_budget=None
    ):
        return ba_apply.APPLY_EXIT_OK, {"landed": [d["id"] for d in directives]}

    def test_the_held_claim_survives_a_spinoff_mint_byte_identical(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        session_id = "sid-author-holds-a-claim"
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)

        held_rel = "state/handoffs/2026-08-19-authors-own-held-baton.md"
        held_path = _write_artifact(
            tmp_path / held_rel,
            ["deliverable_id: DEL-HELD", "claimed_by: " + session_id],
        )
        _seed_handoff_claim(tmp_path, session_id, held_path.name, "2026-08-19T09:00:00Z")
        held_ledger = (
            tmp_path / ".git" / "coordinator-sessions" / "handoff-claims" / held_path.name
        )
        held_claim_before = (held_ledger / "session_id").read_text(encoding="utf-8")
        held_text_before = held_path.read_text(encoding="utf-8")

        spinoff_rel = "state/handoffs/2026-08-19-fork-of-held-baton.md"
        spinoff_path = _write_artifact(tmp_path / spinoff_rel, [])

        monkeypatch.setattr(ba_apply, "_execute_directives", self._fake_execute_directives)
        monkeypatch.setattr(
            ba_apply, "_scoped_commit", lambda *a, **kw: "deadbeefcafe"
        )

        def _fake_brief(kind, artifact_path, **kwargs):
            return type(
                "R",
                (),
                {
                    "decision_object": {
                        "directives": [{"id": "d1", "cli": "coordinator-doc-new", "args": []}],
                        "judgment_points": [],
                        "artifact": {
                            "path": artifact_path,
                            "lineage": {"output_path": spinoff_rel},
                        },
                    }
                },
            )()

        monkeypatch.setattr(ba, "brief", _fake_brief)

        exit_code, report = ba_apply.apply(
            "spinoff", "fork-of-held-baton", session_id=session_id, repo_root=tmp_path
        )

        assert exit_code == ba_apply.APPLY_EXIT_OK, report

        # The held baton's claim is untouched -- neither the durable ledger
        # nor its frontmatter mirror moved.
        assert (held_ledger / "session_id").read_text(encoding="utf-8") == held_claim_before
        assert held_path.read_text(encoding="utf-8") == held_text_before

        # No unification successor was minted: only the spinoff's own path
        # exists under state/handoffs/, nothing naming a unification.
        live = sorted(p.name for p in (tmp_path / "state" / "handoffs").glob("*.md"))
        assert live == sorted({held_path.name, spinoff_path.name})

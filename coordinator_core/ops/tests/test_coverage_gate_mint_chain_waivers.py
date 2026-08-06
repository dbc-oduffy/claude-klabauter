"""
coordinator_core.ops.tests.test_coverage_gate_mint_chain_waivers

Tests for C2b (docs/plans/2026-07-31-review-trail-chain-ancestry-discriminator.md):
`coverage.gate`'s `mint_chain_waivers` param — a DAG-mode uncovered-close verdict mints
one chain-ancestry waiver per uncovered chain commit, gated behind an explicit
ceremony-close-only param that defaults to read-only (False) for every diagnostic
invocation.

C10 token-flip fix (2026-08-06): `run_coverage_gate` retired the binary "UNCOVERED"
token in favour of "WARN" for the below-threshold case (see coverage_gate.py's module
docstring C10 note) — "UNCOVERED" can no longer be produced by `run_coverage_gate`
(verified by grep against coordinator_core/coverage.py's verdict assembly: only
"COVERED", "WARN", "INDETERMINATE" are ever assigned). The tests below previously
pinned "UNCOVERED" as the mint-triggering verdict, certifying the exact defect this fix
closes — a guard that could never match a verdict production code actually emits. Cases
now use "WARN" as the realistic uncovered-close verdict; the legacy "UNCOVERED" literal
is additionally kept as live production behaviour on the minting guard (dead in
practice, cheap to keep) and one dedicated case below still exercises it.

Moved here from `coverage.halt_on_uncovered` (C2's original site): that op has NO
production caller anywhere in the tree (verified by grep against every `cc_invoke.route`/
`route_mutation` call site in `coordinator/bin/`) — the ceremony-close path actually
routes through `coverage.gate` (`review-coverage-gate.py`'s
`cc_invoke.route("coverage.gate", ...)`, in turn invoked by
`wsc-coverage-gate-runner.py`'s `coverage-gate` subcommand). A test asserting only "the
op mints when the param is True" over the unreachable op is exactly the gap this move
closes -- see `test_close_path_flag_reaches_minting_op` below for the reachability
assertion this plan's dispatch brief required.

Coverage:
    (a) mint-fires        — mint_chain_waivers=True + DAG-mode UNCOVERED -> waiver
                             files land, keyed under the SAME closing_session_id the
                             op threads to run_coverage_gate (proven via a round-trip
                             through chain_ancestry_waived_shas — the reader's own
                             lookup — not by hand-building the on-disk path).
    (b) default-off       — mint_chain_waivers omitted (default False) -> DAG-mode
                             UNCOVERED mints NOTHING.
    (c) covered-no-mint   — mint_chain_waivers=True but verdict COVERED -> no mint.
    (d) AC5 chokepoint    — an indeterminate `_derive_dag_chain_set` derivation
                             yields empty `node_attribution` and (transitively) zero
                             mints, pinned via >=2 of that function's own early-return
                             sites feeding ONE assertion (not per-site op-layer tests).
    (e) close-path reachability — the flag `wsc-coverage-gate-runner.py`'s ceremony-close
                             caller passes is one `review-coverage-gate.py` actually
                             accepts and forwards to `coverage.gate`, and that op mints
                             on it end-to-end. This is the class of defect the C2 -> C2b
                             move exists to catch: a param that only ever reaches an
                             unreachable op is invisible to a unit test that mocks the
                             op layer directly.

Spec backlink: docs/plans/2026-07-31-review-trail-chain-ancestry-discriminator.md § C2b
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.coverage_gate  # noqa: F401 — fires @register_op

from coordinator_core.chain_ancestry_waivers import chain_ancestry_waived_shas
from coordinator_core.coverage import CoverageResult, _derive_dag_chain_set
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.coverage_gate import _coverage_gate

_OP_NAME = "coverage.gate"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.coverage_gate @register_op did not fire"
)

_CHAIN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BIN_DIR = _REPO_ROOT / "coordinator" / "bin"

# Needed for test (e)'s `import cc_invoke` — mirrors review-coverage-gate.py's own
# sys.path.insert(0, _LIB_DIR) at module scope.
_LIB_DIR = str(_BIN_DIR / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)


def _make_result(
    verdict: str,
    uncovered_shas: list[str] | None = None,
) -> CoverageResult:
    """A real CoverageResult (not a MagicMock) so `.verdict`/`.uncovered_shas`
    behave exactly as the op layer reads them (AC5: keying on the structural
    field, not a re-parsed string).
    """
    shas = uncovered_shas or []
    return CoverageResult(
        verdict=verdict,
        verdict_line=(
            f"range=dag:x.md chain_commits={len(shas)} covered=0 "
            f"uncovered={len(shas)} VERDICT={verdict}"
        ),
        chain_commits=len(shas),
        covered=0,
        uncovered=len(shas),
        exit_code=0 if verdict != "INDETERMINATE" else 2,
        notes=[],
        uncovered_shas=shas,
    )


def _run_op(tmp_path: Path, fake_result: CoverageResult, params: dict) -> dict:
    with patch(
        "coordinator_core.ops.coverage_gate.run_coverage_gate",
        return_value=fake_result,
    ), patch(
        "coordinator_core.ops.coverage_gate._stage_chain_ancestry_waivers",
    ):
        # `_write_artifact` and `_stage_chain_ancestry_waivers` both touch disk
        # under `tmp_path` -- the artifact write is harmless (state/coverage/),
        # the staging call is patched out since these tests are not real git repos.
        return asyncio.run(_coverage_gate(params, repo_root=tmp_path))


# ---------------------------------------------------------------------------
# (a) mint fires, keyed under the closing_session_id, round-tripped via the reader
# ---------------------------------------------------------------------------


def test_mint_fires_on_dag_uncovered_and_reader_finds_it(tmp_path: Path) -> None:
    """WARN is the live below-threshold verdict `run_coverage_gate` actually emits
    (C10) — this is the realistic uncovered chain-terminal close that must mint."""
    shas = ["1111111111111111111111111111111111111a", "2222222222222222222222222222222222222b"]
    fake = _make_result("WARN", shas)
    _run_op(
        tmp_path,
        fake,
        params={
            "from_handoff": "/tmp/some-closing-handoff.md",
            "closing_session_id": _CHAIN_ID,
            "mint_chain_waivers": True,
        },
    )
    # Prove the identity match via the READER's own lookup — not a hand-built path.
    waived = chain_ancestry_waived_shas(str(tmp_path), _CHAIN_ID)
    assert waived == frozenset(shas)
    # A different chain_id must see nothing (AC3's scope-mismatch property).
    assert chain_ancestry_waived_shas(str(tmp_path), "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb") == frozenset()


def test_mint_fires_on_legacy_uncovered_token_too(tmp_path: Path) -> None:
    """The legacy "UNCOVERED" literal is kept on the minting guard alongside "WARN"
    (unreachable via `run_coverage_gate` today per the C10 note, but reinstates
    itself for free if that token is ever reintroduced upstream) — pinned so a
    future refactor of the guard's verdict set does not silently drop it."""
    shas = ["5555555555555555555555555555555555555e"]
    fake = _make_result("UNCOVERED", shas)
    _run_op(
        tmp_path,
        fake,
        params={
            "from_handoff": "/tmp/some-closing-handoff.md",
            "closing_session_id": _CHAIN_ID,
            "mint_chain_waivers": True,
        },
    )
    assert chain_ancestry_waived_shas(str(tmp_path), _CHAIN_ID) == frozenset(shas)


# ---------------------------------------------------------------------------
# (b) default False -> mints nothing, even on DAG-mode UNCOVERED
# ---------------------------------------------------------------------------


def test_default_false_mints_nothing_on_dag_uncovered(tmp_path: Path) -> None:
    shas = ["3333333333333333333333333333333333333c"]
    fake = _make_result("WARN", shas)
    _run_op(
        tmp_path,
        fake,
        params={
            "from_handoff": "/tmp/some-closing-handoff.md",
            "closing_session_id": _CHAIN_ID,
            # mint_chain_waivers omitted -- must default to False/read-only.
        },
    )
    assert chain_ancestry_waived_shas(str(tmp_path), _CHAIN_ID) == frozenset()


# ---------------------------------------------------------------------------
# (c) mint_chain_waivers=True but verdict COVERED -> no mint
# ---------------------------------------------------------------------------


def test_no_mint_on_covered_even_with_param_true(tmp_path: Path) -> None:
    fake = _make_result("COVERED", [])
    _run_op(
        tmp_path,
        fake,
        params={
            "from_handoff": "/tmp/some-closing-handoff.md",
            "closing_session_id": _CHAIN_ID,
            "mint_chain_waivers": True,
        },
    )
    assert chain_ancestry_waived_shas(str(tmp_path), _CHAIN_ID) == frozenset()


# ---------------------------------------------------------------------------
# (d) AC5 chokepoint -- pinned against >=2 of _derive_dag_chain_set's own
# early-return sites, as inputs to ONE assertion (not per-site op-layer tests).
# ---------------------------------------------------------------------------


def test_indeterminate_derivation_yields_empty_attribution_two_early_returns(
    tmp_path: Path,
) -> None:
    """AC5's chokepoint: an indeterminate `_derive_dag_chain_set` call always
    yields an empty `node_attribution` (populated only on the non-early-return
    success path), so `run_coverage_gate`'s DAG branch returns early on
    `dag_result.indeterminate` before `dag_node_attribution` is ever assigned,
    and `coverage.gate`'s mint (keyed on `verdict == "UNCOVERED"`) structurally
    never sees an indeterminate derivation's attribution.

    Exercises two distinct early-return sites of `_derive_dag_chain_set` itself
    (lineage-cycle, and a `resolve_live_session_ids` failure) as two inputs to
    one shared assertion -- not as two independent op-layer tests.
    """
    closing_handoff = str(tmp_path / "closing.md")

    # Site 1: Step-1 lineage-cycle early return.
    with patch(
        "coordinator_core.coverage.walk_forward",
        return_value={"terminatedEarly": "lineage-cycle", "orderedPaths": []},
    ):
        result_lineage_cycle = _derive_dag_chain_set(closing_handoff, str(tmp_path))

    # Site 2: Step-2 resolve_live_session_ids-raises early return (Guard 2 --
    # a failure to enumerate live sessions must fail the whole fixpoint closed,
    # not silently proceed on an empty/false live set).
    with patch(
        "coordinator_core.coverage.walk_forward",
        return_value={"orderedPaths": []},
    ), patch(
        "coordinator_core.coverage.resolve_live_session_ids",
        side_effect=RuntimeError("boom"),
    ):
        result_live_sids_failure = _derive_dag_chain_set(closing_handoff, str(tmp_path))

    assert all(
        r.indeterminate is True and r.node_attribution == {}
        for r in (result_lineage_cycle, result_live_sids_failure)
    )


# ---------------------------------------------------------------------------
# (e) close-path reachability -- the class of defect this move exists to catch.
# ---------------------------------------------------------------------------


def test_close_path_flag_reaches_minting_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ceremony-close caller's flag must be one `review-coverage-gate.py`
    actually accepts and forwards to `coverage.gate` as `mint_chain_waivers`,
    and that op must mint on it -- exercised end-to-end through the real CLI
    argv parser (not a mocked op layer), against the real `coordinator_core`
    seam, so a param wired to an unreachable op (C2's original defect class)
    would fail THIS test even though `test_mint_fires_on_dag_uncovered_and_
    reader_finds_it` above (which calls the op directly) would still pass.

    Real `git init` (not mocked) so `review-coverage-gate.py`'s own
    `git rev-parse --show-toplevel` repo-root resolution succeeds -- this
    test is specifically about the CLI's real argv-to-op wiring, so the git
    subprocess calls it makes on that path are left real too.
    """
    import cc_invoke  # noqa: E402 (sys.path already carries lib/ from module scope below)

    subprocess.run(
        ["git", "init", "-q"],
        cwd=tmp_path,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    closing_handoff = tmp_path / "closing.md"
    closing_handoff.write_text("dummy handoff\n", encoding="utf-8")

    shas = ["4444444444444444444444444444444444444d"]
    fake = _make_result("WARN", shas)

    captured_params: dict = {}

    def _fake_route(op, params, repo_root, legacy_fn, **_kwargs):
        assert op == "coverage.gate"
        captured_params.update(params)
        with patch(
            "coordinator_core.ops.coverage_gate.run_coverage_gate",
            return_value=fake,
        ):
            return asyncio.run(_coverage_gate(params, repo_root=Path(repo_root)))

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "review_coverage_gate_under_test", str(_BIN_DIR / "review-coverage-gate.py")
    )
    module = importlib.util.module_from_spec(spec)

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _CHAIN_ID)
    monkeypatch.chdir(tmp_path)

    with patch.object(cc_invoke, "route", side_effect=_fake_route):
        sys.modules["review_coverage_gate_under_test"] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        rc = module.main(["--from-handoff", str(closing_handoff), "--mint-chain-waivers"])

    # review-coverage-gate.py exits 0 on BOTH COVERED and UNCOVERED (module docstring:
    # "Exit: 0 on COVERED or UNCOVERED (verdict-line shape)") -- the caller
    # (wsc-coverage-gate-runner.py's cmd_coverage_gate) parses stdout for the
    # VERDICT= token rather than relying on this CLI's exit code.
    assert rc == 0
    assert captured_params.get("mint_chain_waivers") is True
    assert captured_params.get("closing_session_id") == _CHAIN_ID
    # The real (unmocked) coverage.gate handler + real (unmocked)
    # record_chain_ancestry_waiver actually minted -- proven via the reader.
    assert chain_ancestry_waived_shas(str(tmp_path), _CHAIN_ID) == frozenset(shas)


# ---------------------------------------------------------------------------
# C7 / AC12 -- the waiver directory must not be gitignored.
# ---------------------------------------------------------------------------


def test_chain_ancestry_waiver_dir_not_gitignored() -> None:
    """AC12: `state/review-trail/chain-ancestry-waivers/` must not be matched
    by .gitignore, unlike `state/coverage/gate-result.json` -- "it lives
    under state/, it will be committed" is not a safe inference for this
    directory (this repo's own .gitignore has state/-scoped entries). Checked
    against THIS repo's real, on-disk .gitignore via `git check-ignore`
    (a real subprocess, not a hand-parsed pattern match, since gitignore glob
    semantics are easy to get subtly wrong by re-deriving them)."""
    probe_rel = (
        "state/review-trail/chain-ancestry-waivers/"
        "deadbeef-dead-4eef-8eef-deadbeefdead/"
        "1111111111111111111111111111111111111a.json"
    )
    proc = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", probe_rel],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # `git check-ignore -q` exits 0 if the path IS ignored, 1 if it is NOT
    # ignored (a truthful "not a match" exit-code contract, not a failure) --
    # this test wants the 1 (not ignored) case.
    assert proc.returncode == 1, (
        f"state/review-trail/chain-ancestry-waivers/ is unexpectedly gitignored "
        f"(git check-ignore exit={proc.returncode}, stdout={proc.stdout!r})"
    )


def test_minted_waiver_states_it_does_not_certify_review(tmp_path) -> None:
    """A minted waiver is self-describing about its own trust basis (schema v2).

    Regression guard for the defect example-retrieval-repo-em reported on 2026-08-04: a
    schema-v1 record carried only sha/chain_id/source_handoff/timestamp, so 18
    of them landed on disk reading as coverage nobody had performed. DR-245
    § "The disclosed limit" already stated the limit in prose, but a decision
    record is not what an auditor opens — the artifact has to carry it.

    Asserts the discriminating fields specifically, not merely that the record
    parses: `certifies_review` false and an `em_disposition` key that is
    PRESENT-and-null rather than absent, which is what lets a reader tell
    "reviewed under chain X" from "closed without reviewing".
    """
    import json

    from coordinator_core.chain_ancestry_waivers import (
        chain_waiver_dir,
        record_chain_ancestry_waiver,
    )

    chain_id = "c624e5d7-c9f6-405c-b660-21493db068e8"
    sha = "0304806cde6ed278508d0d920abd170f7cd2eb44"
    record_chain_ancestry_waiver(
        str(tmp_path), frozenset({sha}), chain_id, "state/handoffs/x.md",
    )

    written = json.loads(
        (chain_waiver_dir(str(tmp_path), chain_id) / ("%s.json" % sha)).read_text()
    )
    assert written["schema_version"] == 2
    assert written["certifies_review"] is False
    assert "em_disposition" in written and written["em_disposition"] is None
    assert written["waived_commit_count"] == 1
    assert "NOT a record that anyone reviewed" in written["reader_note"]


def test_minted_waiver_records_a_supplied_em_disposition(tmp_path) -> None:
    """A disposition supplied by the ceremony-close caller reaches the artifact."""
    import json

    from coordinator_core.chain_ancestry_waivers import (
        chain_waiver_dir,
        record_chain_ancestry_waiver,
    )

    chain_id = "aa11bb22-cc33-dd44-ee55-ff6677889900"
    sha = "1111111111111111111111111111111111111111"
    record_chain_ancestry_waiver(
        str(tmp_path), frozenset({sha}), chain_id, None,
        "ancestry not reviewed by this close",
    )

    written = json.loads(
        (chain_waiver_dir(str(tmp_path), chain_id) / ("%s.json" % sha)).read_text()
    )
    assert written["em_disposition"] == "ancestry not reviewed by this close"
    assert written["certifies_review"] is False

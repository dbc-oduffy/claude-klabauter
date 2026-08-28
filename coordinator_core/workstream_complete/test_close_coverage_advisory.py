"""
coordinator_core.workstream_complete.test_close_coverage_advisory — C2 of
docs/plans/2026-08-27-the-close-tells-the-author-what-is-uncovered.md.

Purpose: proves the close-coverage advisory (`directives_review.py ::
build_close_coverage_advisory_directive`, C1) is structurally incapable of
gating (AC2), silent on every one of D2's three "unavailable" arms (AC3),
register/byte-cap conformant (AC4), and reaches the real dimension
in-process rather than a second implementation (AC7's grep half — the
no-shell-out assertion; the "no duplicated reviewed-set walk" half of AC7
is a repo-wide grep, not something a single test module can assert and is
out of this chunk's scope per its own body).

Consumes (never reimplements):
    coordinator_core.workstream_complete.directives_review ::
        build_close_coverage_advisory_directive, _CLOSE_COVERAGE_ADVISORY_ID
    coordinator_core.workstream_complete.apply :: _execute_directives
        (AC2's own "does this actually land, unblocked" proof — the same
        harness `test_apply.py:613`/`:715` use for `d-coverage-gate`,
        rather than a hand-rolled directive-list walk.)
    coordinator_core.ops.gate_dimension_review :: _review_dimension_check
    coordinator_core.ops.gate_validate_invocable :: Verdict, DimensionResult
    coordinator_core.bash_guards._message_size :: MESSAGE_PROSE_CAP_BYTES

Negative-spec:
    - Does NOT assert on `_review_dimension_check`'s own internal credit
      rules (kind partition, foreign-session narrowing, verdict filter) —
      those are `coverage.py`'s / the dimension's own test surface, not
      this advisory's.
    - Does NOT assert the repo-wide "no duplicated reviewed-set walk" grep
      (AC7's other half) — that is a static, whole-repo check outside a
      single test module's remit; this file only proves the in-process
      call site and the absence of a shell-out to `merge-gate-and-pr.py`.
    - Does NOT widen or re-derive `_review_dimension_check`'s signature —
      every call here matches its real 3-positional-arg contract.

Run scoped only:
    python3 -m pytest coordinator_core/workstream_complete/test_close_coverage_advisory.py -q
Spec backlink: docs/plans/2026-08-27-the-close-tells-the-author-what-is-uncovered.md, chunk C2
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from coordinator_core.bash_guards._message_size import MESSAGE_PROSE_CAP_BYTES
from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    single_invocation_tree_process_time,
)
from coordinator_core.ops import gate_dimension_review
from coordinator_core.ops.gate_validate_invocable import DimensionResult, Verdict
from coordinator_core.workstream_complete import apply as ws_apply
from coordinator_core.workstream_complete import directives_review


def _sibling_directive(id_: str) -> dict[str, Any]:
    """A plain, unrelated directive with no `depends_on` of its own —
    stands in for "the rest of the close" so AC2's "no sibling directive
    gained a `depends_on` edge onto the advisory" assertion has something
    concrete to check."""
    return {"id": id_, "cli": "wsc-close", "args": [], "depends_on": None, "already_satisfied": True}


# ---------------------------------------------------------------------------
# AC2 — the close survives a forced uncovered set.
# Modeled on test_apply.py:613 (`_consumed_handoff_completeness_fixture`)
# and :715 (`test_consumed_handoff_completeness_does_not_gate_coverage_gate_directive`),
# which assert exactly this shape for `d-coverage-gate`: a directive that
# lands unconditionally, with no sibling gaining a `depends_on` edge to it.
# ---------------------------------------------------------------------------


def test_uncovered_set_lands_advisory_without_gating_the_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC2: force the dimension to report a real uncovered set (`Verdict.FAIL`).
    The advisory directive still lands in `report["landed"]`, never in
    `report["blocked"]`, and no sibling directive in the same close carries
    a `depends_on` edge onto it — the advisory's own `depends_on` stays
    `None` throughout, per D3.

    # Review: coordinator:code-reviewer (Finding 1, P1) — the landed/blocked/
    # exit-code assertions below hold regardless of the injected verdict,
    # because `already_satisfied=True` short-circuits `_execute_directives`
    # before any gate is evaluated. The `capsys` assertion is the one check
    # that actually distinguishes "the FAIL mock was reached and mattered"
    # from "the directive shape made this pass regardless" — it asserts the
    # advisory message was really printed for a FAIL verdict, and (via the
    # second call below) that a non-FAIL verdict prints nothing."""

    def fake_check(changed_files: list[str], diff_base: str | None, repo_root: Path | None) -> DimensionResult:
        return DimensionResult(
            dimension="review",
            verdict=Verdict.FAIL,
            detail="uncovered: 1/2 commit(s) touching changed_files carry neither stamp nor receipt (e.g. deadbeefcafe)",
        )

    monkeypatch.setattr(gate_dimension_review, "_review_dimension_check", fake_check)

    advisory_directive = directives_review.build_close_coverage_advisory_directive(
        ["some/file.py"], "abc123..def456", tmp_path
    )

    captured = capsys.readouterr()
    assert directives_review._CLOSE_COVERAGE_ADVISORY_PREFIX in captured.out
    assert "uncovered: 1/2 commit(s)" in captured.out

    sibling = _sibling_directive("d-run-wsc-tail")
    directives = [advisory_directive, sibling]

    assert advisory_directive["depends_on"] is None
    assert advisory_directive["id"] == directives_review._CLOSE_COVERAGE_ADVISORY_ID

    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)
    assert directives_review._CLOSE_COVERAGE_ADVISORY_ID in report["landed"]
    assert directives_review._CLOSE_COVERAGE_ADVISORY_ID not in report["blocked"]
    assert report["failed"] == []
    # No sibling directive in this close (nor the advisory itself) carries a
    # `depends_on` edge pointing at the advisory id.
    for directive in directives:
        depends = directive.get("depends_on")
        if depends is None:
            continue
        depends_list = depends if isinstance(depends, list) else [depends]
        assert directives_review._CLOSE_COVERAGE_ADVISORY_ID not in depends_list, directive

    # Negative direction, same test per Finding 1's suggestion: a non-FAIL
    # verdict prints nothing, distinguishing the FAIL-path print above from
    # "this function always prints regardless of verdict."
    def fake_check_pass(changed_files: list[str], diff_base: str | None, repo_root: Path | None) -> DimensionResult:
        return DimensionResult(dimension="review", verdict=Verdict.PASS, detail="all covered")

    monkeypatch.setattr(gate_dimension_review, "_review_dimension_check", fake_check_pass)
    directives_review.build_close_coverage_advisory_directive(["some/file.py"], "abc123..def456", tmp_path)
    captured = capsys.readouterr()
    assert captured.out == ""


# ---------------------------------------------------------------------------
# AC3 — three arms, three separate injection points, D2's exact wording:
# "UNAVAILABLE, an exception, or a missing store all take the same path".
# ---------------------------------------------------------------------------


def test_silent_when_dimension_returns_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Arm 1: the dimension resolves cleanly to `Verdict.UNAVAILABLE`."""

    def fake_check(changed_files: list[str], diff_base: str | None, repo_root: Path | None) -> DimensionResult:
        return DimensionResult(dimension="review", verdict=Verdict.UNAVAILABLE, detail="diff_base not provided")

    monkeypatch.setattr(gate_dimension_review, "_review_dimension_check", fake_check)

    directive = directives_review.build_close_coverage_advisory_directive(["a.py"], "abc..def", tmp_path)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert directive["already_satisfied"] is True
    assert directive["depends_on"] is None


def test_silent_when_dimension_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Arm 2: the dimension call itself raises — a live dependency outage,
    not a resolved verdict of any kind."""

    def fake_check(changed_files: list[str], diff_base: str | None, repo_root: Path | None) -> DimensionResult:
        raise RuntimeError("simulated dependency outage")

    monkeypatch.setattr(gate_dimension_review, "_review_dimension_check", fake_check)

    directive = directives_review.build_close_coverage_advisory_directive(["a.py"], "abc..def", tmp_path)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert directive["already_satisfied"] is True
    assert directive["depends_on"] is None


def test_silent_when_reviewed_set_store_is_absent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Arm 3: the reviewed-set store is missing in the most literal sense —
    `repo_root` is not a git repository at all, so the dimension's own
    `git log` resolution fails and it degrades to `Verdict.UNAVAILABLE`
    (`gate_dimension_review._review_dimension_check`'s own `rc != 0` leg).
    Deliberately exercises the REAL function with no mock on
    `_review_dimension_check` itself — a single mock standing in for all
    three arms is exactly what this AC forbids; this arm proves the
    silence holds even when the dimension's own git resolution, not a
    test double, is what fails."""
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    directive = directives_review.build_close_coverage_advisory_directive(["a.py"], "abc..def", not_a_repo)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert directive["already_satisfied"] is True
    assert directive["depends_on"] is None


# ---------------------------------------------------------------------------
# AC4 — register conformance + the 220-byte prose cap, against the
# RENDERED string (`_render_close_coverage_advisory_message`), never a
# second measurement path.
# ---------------------------------------------------------------------------

_FORBIDDEN_SUBSTRINGS = (
    "override",
    "bypass",
    "unlock",
    "docs/reference/guard-override-keys",
    ".flag",
    ".sentinel",
    "export ",
    "touch ",
)


def test_rendered_message_is_register_conformant_and_under_the_byte_cap() -> None:
    """AC4: the rendered string is one fact, once, plus (optionally) a
    terse alternative — no B1 self-legitimacy, no B2 repeated fact, no B3
    reassurance wrapper, no B4 apology, and (B6) no override key, sentinel
    filename, or unlock doc pointer in any shape. Measured against
    `MESSAGE_PROSE_CAP_BYTES` (220), the same constant B5 cites rather than
    a second measurement path (D4)."""
    detail = "uncovered: 1/2 commit(s) touching changed_files carry neither a review-trail stamp nor a reviewer sidecar receipt (e.g. deadbeefcafe)"
    message = directives_review._render_close_coverage_advisory_message(detail)

    assert message is not None
    assert len(message.encode("utf-8")) <= MESSAGE_PROSE_CAP_BYTES

    lowered = message.lower()
    for token in _FORBIDDEN_SUBSTRINGS:
        assert token not in lowered, (token, message)

    # B1 self-legitimacy: the message never asserts its own genuineness.
    for phrase in ("this is a real", "not a refusal", "no need to", "don't be alarmed", "harmless", "sorry"):
        assert phrase not in lowered, (phrase, message)

    # B2 repeated fact: the word "uncovered" appears at most once — the
    # message states its one fact once, never restates it for emphasis.
    assert lowered.count("uncovered") <= 1, message

    # The advisory carries a stable, recognizable prefix and nothing after
    # it but the dimension's own plain detail string.
    assert message == f"{directives_review._CLOSE_COVERAGE_ADVISORY_PREFIX}{detail}"


def test_rendered_message_is_none_when_over_the_byte_cap() -> None:
    """The over-cap render path (D2's "silence, not a degraded message")
    — a detail long enough to push the composed string past 220 bytes
    returns `None`, never a truncated string."""
    detail = "x" * (MESSAGE_PROSE_CAP_BYTES + 50)
    assert directives_review._render_close_coverage_advisory_message(detail) is None


# ---------------------------------------------------------------------------
# AC7 (grep half) — the advisory reaches the dimension in-process; no
# shell-out to merge-gate-and-pr.py from this module.
# ---------------------------------------------------------------------------


def test_no_shell_out_to_merge_gate_and_pr() -> None:
    """AC7 grep half: the advisory's own three functions
    (`_render_close_coverage_advisory_message`, `_emit_close_coverage_advisory`,
    `build_close_coverage_advisory_directive`) call no subprocess mechanism
    at all — the advisory reaches `_review_dimension_check` in-process (D1),
    never by shelling out to `merge-gate-and-pr.py` or any other CLI.
    Scoped to just these three functions' own source (not the whole module,
    which has unrelated sibling builders that DO discuss subprocess/git in
    prose) so a docstring elsewhere in the file can never mask a real
    shell-out introduced here."""
    import inspect

    source = "".join(
        inspect.getsource(fn)
        for fn in (
            directives_review._render_close_coverage_advisory_message,
            directives_review._emit_close_coverage_advisory,
            directives_review.build_close_coverage_advisory_directive,
        )
    )
    assert "import subprocess" not in source
    assert "subprocess.run" not in source
    assert "subprocess.call" not in source
    assert "subprocess.Popen" not in source
    assert "os.system" not in source
    assert "merge-gate-and-pr" not in source
    assert "merge_gate_and_pr" not in source


# ---------------------------------------------------------------------------
# AC5 — the advisory's ADDED cost, measured AT THE CLOSE CALLER, never at the
# merge-gate caller. `single_invocation_tree_process_time` (whole
# process-tree job-object accounting) brackets two one-shot child scripts —
# one that calls `build_close_coverage_advisory_directive`, one that does the
# identical import/setup work (including the three modules
# `_emit_close_coverage_advisory` itself lazily imports — see Finding 2,
# below) and stops short of the call — so the delta isolates the advisory's
# own WORK from interpreter start and module import, neither of which the
# advisory's call itself introduces.
#
# NOT the merge-gate figure. The Anti-scope section of this plan is explicit
# that 93.75ms/4 procs was taken at `merge-gate-and-pr.py :: cmd_coverage_gate`
# on a 37-file changeset, and is context, never a substitute, for a figure
# taken at the close caller on the close's own changeset shape. This test
# supplies that missing figure rather than reusing the merge-gate one.
#
# NEEDS RE-MEASUREMENT (coordinator:code-reviewer Finding 2, P2): the figure
# below (93.75ms/2 procs) was taken before the "without" script paid the
# three lazy imports `_emit_close_coverage_advisory` triggers
# (`gate_dimension_review`, `gate_validate_invocable`, `_message_size`), so
# it conflates the advisory's own `git log` cost with first-import cost the
# "without" baseline never paid. The scripts below now import those three
# modules on both sides before the measured call, which changes the true
# delta; the number in this comment has not been re-derived against that
# fix and should not be trusted as the current figure until it is.
#
# ORIGINAL MEASUREMENT (stale, pre-fix; kept for provenance only -- this
# file, 2026-08-28, single sample, k=1 -- single_invocation_
# tree_process_time cannot be batched, module docstring -- Windows job-object
# accounting, +-15.6ms scheduler-tick quantisation applies): close caller,
# 5-file changeset (HEAD~5..HEAD, this branch): without=140.625ms/1 proc,
# with=234.375ms/3 procs, ADDED = 93.75ms process time / 2 procs (the
# dimension's own `git log` plus its conhost, UNCONTROLLED for the lazy-
# import cost noted above). Coincidentally the same process-time figure as
# the merge-gate's 93.75ms/4 procs, on a differently-shaped changeset (5
# files vs 37) and a different proc delta (2 added here vs the merge gate's
# own 4-proc total) -- read as coincidence, not as evidence the two call
# sites share a cost, since they measure different quantities (an ADDED
# delta here vs a TOTAL there).
#
# NOT a small fraction of the 500ms brightline by the letter of AC5's own
# wording (93.75ms is ~18.75% of 500ms) -- clears the brightline itself with
# room, but this is the finding AC5's own body says to surface rather than
# silently accept: "If the added cost is NOT a small fraction of 500ms...
# the handoff's build-vs-don't question reopens rather than resolving by
# default." Surfaced here rather than asserted through a hand-picked
# percentage threshold this test would otherwise be tuned to always pass.
# ---------------------------------------------------------------------------

_AC5_CHILD_PREAMBLE = """
import sys
sys.path.insert(0, {repo_root!r})
from pathlib import Path
"""

_AC5_WITHOUT_ADVISORY = _AC5_CHILD_PREAMBLE + """
from coordinator_core.workstream_complete import directives_review  # noqa: F401
# Review: coordinator:code-reviewer (Finding 2, P2) -- `_emit_close_coverage_advisory`
# lazily imports these three modules only when actually called, so the
# "with" script pays their first-import cost inside the measured window.
# Paying it here too, before the measured region starts, isolates the
# advisory's own WORK (the dimension's `git log` spawn) from module-import
# cost neither side should be charged for.
from coordinator_core.ops.gate_dimension_review import _review_dimension_check  # noqa: F401
from coordinator_core.ops.gate_validate_invocable import Verdict  # noqa: F401
from coordinator_core.bash_guards._message_size import MESSAGE_PROSE_CAP_BYTES  # noqa: F401
for _ in range({iterations}):
    pass
print("OK")
"""

# AMORTISED, not one-shot. Windows reports process time on a 15.625ms
# quantum and one advisory call costs well under that, so a single-call
# delta measures the scheduler, not the code -- observed directly: the
# one-shot form returned +46.875ms on one run and -46.875ms on another for
# identical code, and min-of-5 still went negative. Both scripts now do the
# SAME K iterations and differ only in whether the advisory runs; the
# per-call figure is the delta over K, which lifts the signal above the
# quantum instead of hiding inside it.
_AC5_ITERATIONS = 20

_AC5_WITH_ADVISORY = _AC5_CHILD_PREAMBLE + """
from coordinator_core.workstream_complete import directives_review
from coordinator_core.ops.gate_dimension_review import _review_dimension_check  # noqa: F401
from coordinator_core.ops.gate_validate_invocable import Verdict  # noqa: F401
from coordinator_core.bash_guards._message_size import MESSAGE_PROSE_CAP_BYTES  # noqa: F401
for _ in range({iterations}):
    directives_review.build_close_coverage_advisory_directive(
        {changed_files!r}, {diff_base!r}, Path({repo_root!r})
    )
print("OK")
"""


@pytest.mark.spawns_process
@pytest.mark.skipif(
    not (IS_WINDOWS or IS_DARWIN),
    reason="single_invocation_tree_process_time has no primitive off Windows/Darwin",
)
def test_ac5_advisory_added_cost_measured_at_the_close_caller(tmp_path: Path) -> None:
    """AC5: close-with minus close-without, at the close caller, on this
    repo's own real changeset shape (5 commits, `git diff --name-only
    HEAD~5..HEAD` at authoring time — 5 files touched, named below rather
    than re-derived live, so this test's own git spawn for fixture setup is
    never inside the measured window)."""
    repo_root = Path(__file__).resolve().parents[2]
    assert (repo_root / ".git").exists(), (
        f"expected {repo_root} to be the repo root (parents[2] of this test file)"
    )

    # The real changeset this figure is scoped to (HEAD~5..HEAD at authoring
    # time on this branch) — a fixed, named shape rather than a live re-derive,
    # per this test's own docstring.
    changed_files = [
        "state/audits/data/2026-08-27-adjudication-06/population.json",
        "state/bug-backlog/2026-08-28-three-live-cli-doors-front-a-killed-review-trail-op.yaml",
        "state/bug-backlog/2026-08-28-workstream-completes-review-scale-instruments-answer-for-the-branch-not-the-session.yaml",
        "state/handoffs/2026-08-27-every-bin-name-warm-serves-and-a-classifier-says-so.md",
        "state/handoffs/2026-08-28-the-delegation-grant-is-only-as-strong-a.md",
    ]
    diff_base = "HEAD~5..HEAD"

    without_script = tmp_path / "without_advisory.py"
    without_script.write_text(
        textwrap.dedent(_AC5_WITHOUT_ADVISORY.format(repo_root=str(repo_root), iterations=_AC5_ITERATIONS)),
        encoding="utf-8",
    )
    with_script = tmp_path / "with_advisory.py"
    with_script.write_text(
        textwrap.dedent(
            _AC5_WITH_ADVISORY.format(
                repo_root=str(repo_root),
                changed_files=changed_files,
                diff_base=diff_base,
                iterations=_AC5_ITERATIONS,
            )
        ),
        encoding="utf-8",
    )

    # SAMPLING, and why a single shot is not enough here. Windows reports
    # process time on a 15.625ms scheduler quantum, and the advisory's own
    # work is on that same order. A one-shot delta is therefore quantisation
    # noise wearing a number's clothes: the first run of this test produced
    # +46.875ms (3 ticks) and a later run of the SAME code produced
    # -46.875ms, a physically impossible negative cost. Take the MINIMUM of
    # N samples per side -- the minimum is the least-contaminated estimate of
    # true cost, since scheduler noise and peer load on this box only ever
    # ADD time (see CLAUDE.md's load norm: ~50 concurrent sessions).
    _SAMPLES = 5
    without_runs = [
        single_invocation_tree_process_time(
            [sys.executable, str(without_script)], cwd=str(repo_root)
        )
        for _ in range(_SAMPLES)
    ]
    with_runs = [
        single_invocation_tree_process_time(
            [sys.executable, str(with_script)], cwd=str(repo_root)
        )
        for _ in range(_SAMPLES)
    ]

    for r in without_runs + with_runs:
        assert r["rc"] == 0, r

    without_result = min(without_runs, key=lambda r: r["process_time_ms"])
    with_result = min(with_runs, key=lambda r: r["process_time_ms"])

    total_added_ms = with_result["process_time_ms"] - without_result["process_time_ms"]
    added_process_time_ms = total_added_ms / _AC5_ITERATIONS
    added_procs = (with_result["procs"] - without_result["procs"]) / _AC5_ITERATIONS

    # A negative delta means the instrument, not the code, is being measured:
    # the advisory cannot make the close cheaper. Fail loudly rather than
    # report a number that would launder noise into evidence.
    assert added_process_time_ms >= 0.0, (
        f"AC5 instrument is noise-dominated: min-of-{_SAMPLES} still yields a "
        f"negative added cost ({added_process_time_ms}ms). The figure is not "
        "usable as AC5 evidence; raise _SAMPLES or amortise the advisory's "
        "work across iterations inside one process.",
        without_runs,
        with_runs,
    )

    print(
        "AC5 close-caller measurement "
        f"(changeset=5 files, HEAD~5..HEAD, min of {_SAMPLES}): "
        f"without={without_result['process_time_ms']}ms/{without_result['procs']}procs, "
        f"with={with_result['process_time_ms']}ms/{with_result['procs']}procs, "
        f"added={added_process_time_ms}ms/{added_procs}procs"
    )

    # AC5's own hard floor: clears the 500ms brightline. Whether the added
    # cost is a SMALL FRACTION of it (not merely under it) is not something
    # this test decides by picking a percentage threshold to gate on — that
    # is the reopened build-vs-don't question this module's docstring
    # surfaces as a finding, not a number a hand-tuned assertion should
    # quietly launder into a pass.
    assert added_process_time_ms < 500.0, (
        f"AC5: advisory added {added_process_time_ms}ms at the close caller "
        "— exceeds the 500ms brightline itself, not merely the 'small "
        "fraction' bar; this is a finding to surface (handoff's "
        "build-vs-don't question reopens), not a number to accept",
        with_result,
        without_result,
    )

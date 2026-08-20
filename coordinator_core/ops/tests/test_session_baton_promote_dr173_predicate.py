"""C9 / AC10: DR-173's gate is a predicate, and its note does no blocking.

docs/plans/2026-08-19-gate-notes-are-advisory-blocked-by-derives-readiness.md § C9.

DR-173 (DoE, accepted 2026-08-19) parks a promoted baton whose `category`/
`summary` are unfilled. Its ratified OUTCOME is unchanged by this plan --
`awaiting_gate` + `pickup_ready: false` + a `blocking_notes` reason text --
and only the framing moves: the gating decision was ALREADY two field reads,
and `blocking_notes` was ALREADY output the decision writes rather than input
it reads.

REGRESSION THIS FILE EXISTS TO PIN. C3 re-pointed `--gated-open` at
`blocked_by`, but the promote op still passed its prose reason to that flag,
so a promoted baton came out carrying

    blocked_by:
      - "category and summary are unfilled placeholders"

a `blocked_by` entry fabricated from a sentence. That is forbidden by the
plan's § Anti-scope ("do not force a fake stub id into blocked_by") and is
break-class rather than untidy: the entry can never resolve, so the baton stays
parked FOREVER -- filling in category and summary would not clear it. The fix
is the `--gated-predicate` arm, which parks on the predicate and carries the
reason as advisory prose with no graph edge invented.

Spawn ratchet C2 disposition: STUB. Every assertion here is decided inside
`_scaffold_handoff` itself (the `--gated-predicate` blank-check and the
readiness/notes derivation both live there, not in `main()`'s argv layer),
so the CLI subprocess this file used to shell out to is incidental --
calling `_scaffold_handoff` in-process (same loader idiom as
test_coordinator_doc_new_summary_gated_open.py) proves the identical
property with no clean-interpreter dependency.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
CLI = REPO / "coordinator" / "bin" / "coordinator-doc-new.py"


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_dr173_predicate_test", str(CLI)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_dr173_predicate_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


def _scaffold(tmp_path, *extra):
    kwargs = {}
    args = list(extra)
    while args:
        flag = args.pop(0)
        if flag == "--gated-predicate":
            kwargs["gated_predicate"] = args.pop(0)
        elif flag == "--gate-note":
            kwargs["gate_note"] = args.pop(0)
        elif flag == "--gated-open":
            kwargs["gated_open"] = args.pop(0)
        else:  # pragma: no cover -- fixture discipline, not a real CLI arg
            raise AssertionError(f"unrecognized fixture flag: {flag!r}")
    return _cli._scaffold_handoff(title="promoted baton", branch="b", **kwargs)


class TestDr173RatifiedOutcomeIsPreserved:
    def test_unfilled_fields_still_park_the_baton(self, tmp_path):
        """AC10: the ratified trio, byte-for-byte in behaviour."""
        content = _scaffold(tmp_path, "--gated-predicate", "category and summary are unfilled placeholders")

        assert "deployment_state: awaiting_gate" in content
        assert "pickup_ready: false" in content
        assert "category and summary are unfilled placeholders" in content

    def test_the_reason_rides_in_blocking_notes_not_blocked_by(self, tmp_path):
        """The regression. A prose reason must never become a graph edge."""
        content = _scaffold(tmp_path, "--gated-predicate", "category is an unfilled placeholder")

        assert "blocking_notes:" in content
        assert "blocked_by:" not in content, (
            "the DR-173 reason was minted as a blocked_by entry; that id can never "
            "resolve, so the baton would stay parked even once the fields are filled"
        )

    def test_note_is_not_load_bearing_the_predicate_is(self, tmp_path):
        """C9's whole point: delete the note and the baton stays parked,
        because the empty fields are what park it, not the text describing why.

        Asserted structurally -- the parked state and the note are produced by
        two different things, so stripping the note leaves readiness untouched.
        """
        content = _scaffold(tmp_path, "--gated-predicate", "summary is an unfilled placeholder")
        without_note = "\n".join(
            line for line in content.splitlines() if not line.startswith("blocking_notes:")
        )

        assert "blocking_notes:" not in without_note
        assert "deployment_state: awaiting_gate" in without_note
        assert "pickup_ready: false" in without_note

    def test_both_fields_present_promotes_ordinary(self, tmp_path):
        """The non-gated arm must not regress -- the overwhelming majority."""
        content = _scaffold(tmp_path)

        assert "deployment_state: ready_to_fire" in content
        assert "pickup_ready: true" in content
        assert "blocking_notes:" not in content

    def test_predicate_and_gate_note_together_keep_both_reasons(self, tmp_path):
        """Review (code-reviewer slice C): `gate_note or gated_predicate`
        silently dropped DR-173's ratified reason text when both were
        supplied, leaving the baton parked with no record of what parked it.
        They are two different things — the mechanical condition, and an
        unrelated advisory constraint — so both survive."""
        content = _scaffold(
            tmp_path,
            "--gated-predicate", "category is an unfilled placeholder",
            "--gate-note", "needs a GPU box",
        )

        assert "category is an unfilled placeholder" in content
        assert "needs a GPU box" in content
        assert "deployment_state: awaiting_gate" in content
        assert "pickup_ready: false" in content

    def test_gated_open_and_gate_note_together(self, tmp_path):
        """C3's body requires this combination be legal and tested: a blocked
        baton that also carries an advisory note. It had no test anywhere
        (review: code-reviewer slice C)."""
        content = _scaffold(
            tmp_path,
            "--gated-open", "stb-real-blocker-000001",
            "--gate-note", "needs a macOS box",
        )

        assert "deployment_state: awaiting_gate" in content
        assert "pickup_ready: false" in content
        assert "stb-real-blocker-000001" in content
        assert "needs a macOS box" in content

    def test_gate_note_alone_never_parks_the_baton(self, tmp_path):
        """AC4 at the CLI surface, restated here because it is the assertion
        the whole ruling rests on: prose does not gate."""
        content = _scaffold(tmp_path, "--gate-note", "needs a GPU box")

        assert "deployment_state: ready_to_fire" in content
        assert "pickup_ready: true" in content
        assert "needs a GPU box" in content
        assert "blocked_by:" not in content

    def test_blank_predicate_is_refused_fail_loud(self, tmp_path):
        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            with pytest.raises(SystemExit) as ctx:
                _cli._scaffold_handoff(title="blank", branch="b", gated_predicate="   ")
        assert ctx.value.code == 1
        assert "gated-predicate" in stderr_buf.getvalue()


class TestPromoteOpRoutesToThePredicateArm:
    def test_op_passes_gated_predicate_never_gated_open(self):
        """Pin the routing itself, so a later edit cannot quietly send the
        reason back through --gated-open and re-mint the fake edge."""
        src = (REPO / "coordinator_core" / "ops" / "session_baton_promote.py").read_text(
            encoding="utf-8"
        )
        assert "--gated-predicate" in src
        assert '"--gated-open", ' not in src, (
            "session_baton_promote routes its DR-173 reason through --gated-open "
            "again; that mints an unresolvable blocked_by entry"
        )

"""coordinator_core.ops.docgen.tests.test_c6_conformance — byte-identity conformance
harness against the LIVE ``coordinator-doc-new`` oracle (C6, AC5).

Purpose: for every one of the 22 extracted doc types, invoke this repo's own
in-tree ``coordinator-doc-new`` CLI (``coordinator/bin/coordinator-doc-new.py`` —
migrated in from DoE by commit b644d5a9; resolved repo-root-relative to this
test file, no cross-repo clone lookup, so both sides cannot drift together
unnoticed) with a fixed set of caller-supplied inputs, invoke this repo's
internal ``render.render_document`` with the SAME inputs, redact the closed
volatile-field set per C5 (``volatility.redact_text``) on BOTH sides, and
assert byte-identical remainder.

This is a round-trip CONFORMANCE test, not a happy-path smoke test (DR-210
anti-drift discipline) — every extracted type gets its own assertion, and a
type that cannot be made byte-identical is reported as a per-type finding
(oracle text vs. rendered text diff, plus the DoE-HEAD commit of the template
surface), not silently skipped or loosened to structural-similarity.

Determinism strategy (mirrors C5's hybrid contract):
  - INJECTABLE fields (``deliverable_id``, ``branch``) are driven with the SAME
    literal value on both sides via the oracle's real CLI flags
    (``--deliverable-id``/``--branch``) — see ``volatility.MINT_FIELDS``.
  - REDACT-ONLY fields (``created``, ``date``, ``dispatched_at``, ``spawned_at``,
    ``handoff_id``, ``completion_id``, ``plan_id`` — ``volatility.TIME_FIELDS``)
    have NO CLI carry path; a placeholder is fed to this repo's render (any
    value — it is redacted post-render) and ``volatility.redact_text`` is
    applied to BOTH the oracle's raw output and this repo's rendered output
    before comparison. Redaction is line-oriented (matches on ``key:`` regardless
    of quoting), so the placeholder value's exact text is immaterial.
  - Fields the oracle resolves from repo/session CONTEXT with no CLI override
    and no wall-clock/mint component (``author`` on ``plan`` — resolved via
    ``_resolve_from_repo()``; ``dispatched_by`` on ``run-report`` — resolved
    from the ``CLAUDE_CODE_SESSION_ID`` env var) are NOT volatile — they are
    deterministic for the lifetime of one test process/environment. For
    ``dispatched_by`` this test relies on subprocess env inheritance (the
    child sees the same ``CLAUDE_CODE_SESSION_ID`` this test process resolves
    with the identical fallback). For ``author`` there is no CLI override at
    all, so this test extracts the oracle's own resolved ``author:`` line
    (single narrow exception, documented at its call site below) and feeds
    that literal value into the render side — this is not open-ended
    normalization, it is reading the one caller-resolved value neither side
    can otherwise be handed identically.
  - ``run_id`` (``audit-record``'s ``run_id_placeholder`` field) is a
    ``volatility.TIME_FIELDS`` member and covered by ``volatility.redact_text``
    like every other redact-only field. (Historical note: this test's own
    audit originally found the field missing from C5's ``volatility.py``
    registry and patched it locally via a now-removed ``_LOCAL_EXTRA_REDACT``;
    a code-review pass closed the gap in the real registry instead, per
    ``volatility.py``'s module docstring.)

Spec backlink: pln-strang-12-document-generation--75a7eb § C6 (AC5)
Oracle: coordinator/bin/coordinator-doc-new.py (in-repo, resolved repo-root-relative)
Negative-spec: this module does not vendor a copy of the oracle CLI or any
template text — every run re-resolves and re-invokes the in-repo CLI at its
current on-disk state. The oracle CLI is no longer cross-repo (DoE commit
b644d5a9 migrated the executable surface into this repo); a missing oracle at
the expected path is therefore a broken checkout, not an unavailable optional
dependency — this module fails loud on that condition rather than skipping.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.docgen import template_format as tf
from coordinator_core.ops.docgen import volatility as vol
from coordinator_core.ops.docgen.render import render_document

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

# ---------------------------------------------------------------------------
# Oracle resolution (module-scoped — repo-root-relative, no cross-repo clone
# lookup). The oracle CLI lives in THIS repo as of DoE commit b644d5a9; a
# missing oracle at the expected path is a broken checkout, not an
# unavailable optional dependency, so resolution failure fails the whole
# module loudly rather than skipping.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ORACLE_PATH = _REPO_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"

if not _ORACLE_PATH.is_file():
    pytest.fail(
        f"oracle CLI not found at {_ORACLE_PATH} (resolved repo-root-relative "
        f"from {__file__}) — this is a broken checkout, not an unavailable "
        "optional dependency; the oracle has lived in-repo since DoE commit "
        "b644d5a9",
        pytrace=False,
    )


def _repo_head_sha() -> str:
    """Best-effort repo-HEAD commit SHA for the template surface, for failure diagnostics."""
    try:
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # Best-effort per the docstring above -- falls through to the
        # "<unresolvable>" sentinel rather than failing the diagnostic itself.
        pass
    return "<unresolvable>"


# ---------------------------------------------------------------------------
# Shared literal inputs — short, plain values chosen to avoid incidental
# truncation/escaping edge cases already covered by test_doc_render.py's own
# quoting tests; this module's job is oracle-vs-render identity, not idiom
# coverage in isolation.
# ---------------------------------------------------------------------------

TITLE = "Conformance Test"
BRANCH = "conformance-branch"
DELIVERABLE_ID = "dlv-conformance-test"
INITIATIVE = "conformance-initiative"
REDACT_INPUT = "PENDING-REDACTION"  # fed to render for redact-only fields; exact text is immaterial


def _run_oracle(extra_args: list[str], out_path: Path) -> str:
    """Invoke the live oracle CLI, writing to ``out_path``; return the file text."""
    cmd = [sys.executable, str(_ORACLE_PATH), *extra_args, "--out", str(out_path)]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        f"oracle invocation failed (exit {result.returncode}): {cmd}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return out_path.read_text(encoding="utf-8")


def _extract_line_value(text: str, key: str) -> str:
    """Extract the raw (unquoted) value of a top-level ``key: value`` frontmatter line.

    Narrow helper for the one field (``author`` on ``plan``) with no CLI override —
    see the module docstring's determinism-strategy note.
    """
    m = re.search(rf"^{re.escape(key)}: (.*)$", text, flags=re.MULTILINE)
    assert m is not None, f"no {key!r} line found in oracle output"
    return m.group(1)


# ---------------------------------------------------------------------------
# Per-type spec: (extra CLI args beyond --out, resolved_field_values)
# ---------------------------------------------------------------------------

def _handoff_family_values(placeholder_summary: str, **extra: object) -> dict:
    values = {
        "title": TITLE,
        "created": REDACT_INPUT,
        "branch": BRANCH,
        "placeholder_summary": placeholder_summary,
        "deliverable_id": DELIVERABLE_ID,
        "initiative": INITIATIVE,
        "handoff_id": REDACT_INPUT,
    }
    values.update(extra)
    return values


def _resolved_authoring_session() -> str:
    """The oracle's own `_resolve_session_id()` chain, mapped to the render
    side's truthy/falsy shape.

    `authoring_session` has no CLI override -- `coordinator-doc-new`'s
    `_scaffold_handoff` stamps it from `_resolve_session_id()` and omits the
    field on the `"em-unknown"` sentinel. `""` is what makes the template's
    `optional_omit` drop the line on exactly the same condition, so the two
    sides agree without either learning the other's sentinel.

    Duplicated rather than imported for the reason `_resolve_session_id`'s own
    docstring gives for duplicating it in the first place: that CLI must run
    from a consumer repo where makima's package tree is not importable. This
    conformance test is itself the drift detector if the chains diverge.
    """
    resolved = (
        os.environ.get("COORDINATOR_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or "em-unknown"
    )
    return "" if resolved == "em-unknown" else resolved


def _build_specs() -> dict[str, dict]:
    specs: dict[str, dict] = {}

    specs["handoff"] = {
        "cli": [
            "--type", "handoff", "--title", TITLE, "--branch", BRANCH,
            "--deliverable-id", DELIVERABLE_ID, "--initiative", INITIATIVE,
        ],
        # authoring_session has NO CLI override — the oracle stamps it from
        # `_resolve_session_id()` (COORDINATOR_SESSION_ID > CLAUDE_SESSION_ID >
        # CLAUDE_CODE_SESSION_ID > "em-unknown"), optional-omit on the
        # "em-unknown" sentinel (see _scaffold_handoff's docstring). Resolve
        # with the identical chain here so the render side sees the same
        # truthy/falsy shape the oracle's `if _authoring_session != "em-unknown"`
        # gate produces; "" omits the field the same way "em-unknown" does
        # for the oracle.
        "values": _handoff_family_values(
            "PLACEHOLDER — replace with one-line session summary (≤140 chars)",
            authoring_session=_resolved_authoring_session(),
        ),
    }

    specs["spinoff"] = {
        "cli": [
            "--type", "spinoff", "--title", TITLE, "--branch", BRANCH,
            "--deliverable-id", DELIVERABLE_ID, "--initiative", INITIATIVE,
        ],
        "values": _handoff_family_values(
            "PLACEHOLDER — replace with one-line spinoff summary (≤140 chars)"
        ),
    }

    specs["recovery"] = {
        "cli": [
            "--type", "recovery", "--title", TITLE, "--branch", BRANCH,
            "--deliverable-id", DELIVERABLE_ID, "--initiative", INITIATIVE,
            "--recovers-session", "sid-conformance",
        ],
        "values": {
            "title": TITLE,
            "created": REDACT_INPUT,
            "branch": BRANCH,
            "placeholder_summary": "PLACEHOLDER — replace with one-line recovery summary (≤140 chars)",
            "recovers_session": "sid-conformance",
            "deliverable_id": DELIVERABLE_ID,
            "initiative": INITIATIVE,
            "handoff_id": REDACT_INPUT,
        },
    }

    specs["goal-seed"] = {
        "cli": [
            "--type", "goal-seed", "--title", TITLE, "--branch", BRANCH,
            "--goals", "goal-one,goal-two", "--gate-dependency", "conformance-subsystem gate",
        ],
        "values": {
            "title": TITLE,
            "created": REDACT_INPUT,
            "branch": BRANCH,
            "gate_dependency": "conformance-subsystem gate",
            "placeholder_summary": "PLACEHOLDER — replace with one-line vision-slice summary (≤140 chars)",
            "goals": ["goal-one", "goal-two"],
            "handoff_id": REDACT_INPUT,
        },
    }

    specs["roadmap-seed"] = {
        "cli": [
            "--type", "roadmap-seed", "--title", TITLE, "--branch", BRANCH,
            "--goals", "goal-one,goal-two", "--gate-dependency", "conformance-subsystem gate",
            "--deliverable-id", DELIVERABLE_ID, "--initiative", INITIATIVE,
        ],
        "values": {
            "title": TITLE,
            "created": REDACT_INPUT,
            "branch": BRANCH,
            "gate_dependency": "conformance-subsystem gate",
            "placeholder_summary": "PLACEHOLDER — replace with one-line capability-arc summary (≤140 chars)",
            "deliverable_id": DELIVERABLE_ID,
            "initiative": INITIATIVE,
            "goals": ["goal-one", "goal-two"],
            "handoff_id": REDACT_INPUT,
        },
    }

    _roadmap_id = "rmp-conformance"
    _stub_id = "rmp-conformance-1"
    specs["roadmap-baton"] = {
        "cli": [
            "--type", "roadmap-baton", "--title", TITLE, "--branch", BRANCH,
            "--roadmap-id", _roadmap_id, "--stub-id", _stub_id,
            "--deliverable-id", DELIVERABLE_ID, "--initiative", INITIATIVE,
        ],
        "values": {
            "title": TITLE,
            "created": REDACT_INPUT,
            "branch": BRANCH,
            "roadmap_id": _roadmap_id,
            "stub_id": _stub_id,
            "authoring_session_path": f"state/roadmap/{_roadmap_id}/",
            "placeholder_summary": "PLACEHOLDER — replace with one-line stub summary (≤140 chars)",
            "deliverable_id": DELIVERABLE_ID,
            "initiative": INITIATIVE,
            "handoff_id": REDACT_INPUT,
        },
    }

    specs["plan"] = {
        # author has NO CLI override (_resolve_from_repo() always wins) — extracted
        # from the oracle's own output post-hoc; see the "plan" special-case below.
        "cli": [
            "--type", "plan", "--title", TITLE, "--branch", BRANCH,
            "--deliverable-id", DELIVERABLE_ID, "--initiative", INITIATIVE,
        ],
        "values": {
            "title": TITLE,
            "created": REDACT_INPUT,
            "branch": BRANCH,
            "plan_id": REDACT_INPUT,
            "deliverable_id": DELIVERABLE_ID,
            "initiative": INITIATIVE,
            # "author" filled in by the test after the oracle runs (extracted).
        },
    }

    specs["decision"] = {
        # id has NO CLI override (the oracle always self-allocates via
        # `_allocate_dr_number` against the live docs/decisions/ dir under the
        # invoking cwd's git root) — extracted post-hoc from the oracle's own
        # output after it runs, same narrow pattern as "plan"'s author below.
        "cli": ["--type", "decision", "--title", TITLE],
        "values": {"title": TITLE, "created": REDACT_INPUT},
    }

    specs["audit-record"] = {
        "cli": ["--type", "audit-record", "--title", TITLE, "--system", "conformance-system"],
        "values": {
            "title": TITLE,
            "system": "conformance-system",
            "created": REDACT_INPUT,
            # run_id_placeholder is date-derived with no CLI carry; tracked in
            # volatility.py's registry (_POLICY["audit-record"]) and redacted by
            # volatility.redact_text like every other redact-only field.
            "run_id_placeholder": REDACT_INPUT,
        },
    }

    specs["problem-set"] = {
        "cli": ["--type", "problem-set", "--title", TITLE],
        "values": {"title": TITLE, "created": REDACT_INPUT},
    }

    specs["completion"] = {
        "cli": ["--type", "completion", "--title", TITLE, "--nature", "infra", "--chain", "conformance-chain"],
        "values": {
            "title": TITLE,
            "created": REDACT_INPUT,
            "nature": "infra",
            "chain": "conformance-chain",
            "completion_id": REDACT_INPUT,
        },
    }

    specs["health-status"] = {
        "cli": ["--type", "health-status", "--title", TITLE],
        "values": {"title": TITLE, "created": REDACT_INPUT},
    }

    specs["goal"] = {
        "cli": ["--type", "goal", "--title", TITLE],
        "values": {
            "title": TITLE,
            "created": REDACT_INPUT,
            "goal_id": "goal-conformance-test",
            "goal_id_quoted": '"goal-conformance-test"',
        },
    }

    specs["strategic-self-description"] = {
        "cli": ["--type", "strategic-self-description", "--title", TITLE],
        "values": {"repo_name": TITLE},
    }

    specs["research-synthesis"] = {
        "cli": ["--type", "research-synthesis", "--title", TITLE],
        "values": {"title": TITLE, "created": REDACT_INPUT},
    }

    specs["run-report"] = {
        "cli": [
            "--type", "run-report",
            "--plan", "docs/plans/2026-07-21-conformance.md",
            "--chunk", "c-conformance",
            "--agent-type", "general-purpose",
        ],
        "values": {
            "plan_path": "docs/plans/2026-07-21-conformance.md",
            "chunk_id": "c-conformance",
            "dispatched_at": REDACT_INPUT,
            "dispatched_by": os.environ.get("CLAUDE_CODE_SESSION_ID", "em-unknown"),
            "agent_type": "general-purpose",
        },
    }

    specs["review-findings"] = {
        "cli": [
            "--type", "review-findings",
            "--slice", "Z", "--scope", "coordinator_core/ops/docgen/",
        ],
        "values": {
            "slice_id": "Z",
            "scope": "coordinator_core/ops/docgen/",
            "spawned_at": REDACT_INPUT,
            "lead_session_id": os.environ.get(
                "COORDINATOR_SESSION_ID",
                os.environ.get(
                    "CLAUDE_SESSION_ID", os.environ.get("CLAUDE_CODE_SESSION_ID", "em-unknown")
                ),
            ),
        },
    }

    specs["memo"] = {
        "cli": [
            "--type", "memo", "--title", TITLE,
            "--to", "conformance-receiver-em", "--topic", "conformance-topic",
            "--from-repo", "conformance-sender-em",
        ],
        "values": {
            "title": TITLE,
            "from_id": "conformance-sender-em",
            "to": "conformance-receiver-em",
            "created": REDACT_INPUT,
            "explicit_summary": TITLE,
            "topic": "conformance-topic",
        },
    }

    _plan_stem = "2026-07-21-conformance"
    _plan_path = f"docs/plans/{_plan_stem}.md"
    for sidecar_type in ("review", "prior-art-check", "plan-coverage-check", "docs-check"):
        specs[sidecar_type] = {
            "cli": ["--type", sidecar_type, "--plan", _plan_stem],
            "values": {"plan_path": _plan_path, "created": REDACT_INPUT},
        }

    return specs


_SPECS = _build_specs()


def test_all_22_types_have_a_conformance_spec():
    assert set(_SPECS) == set(tf.available_template_types())


# ---------------------------------------------------------------------------
# Ambient-env-var precedence coverage (Finding 3, review 4f8cc506): the main
# byte-identity loop above only exercises whatever COORDINATOR_SESSION_ID /
# CLAUDE_SESSION_ID / CLAUDE_CODE_SESSION_ID / CLAUDE_CODE_SESSION_ID happen to
# be set in the invoking environment -- in CI, where none of them are set,
# both sides silently resolve to the "em-unknown" -> "" omitted-field branch
# and the truthy branch + inter-var precedence order go untested. These cases
# drive each var explicitly via monkeypatch so both branches, and the
# precedence order itself, are asserted against the live oracle rather than
# left to whatever the ambient environment happens to provide.
# ---------------------------------------------------------------------------

_SESSION_ENV_VARS = ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


@pytest.mark.parametrize(
    "env_overrides",
    [
        {"COORDINATOR_SESSION_ID": "cs-only-session"},
        {"CLAUDE_SESSION_ID": "clsess-only-session"},
        {"CLAUDE_CODE_SESSION_ID": "clcode-only-session"},
        {
            "COORDINATOR_SESSION_ID": "cs-wins-session",
            "CLAUDE_SESSION_ID": "clsess-loses-session",
            "CLAUDE_CODE_SESSION_ID": "clcode-loses-session",
        },
        {
            "CLAUDE_SESSION_ID": "clsess-wins-session",
            "CLAUDE_CODE_SESSION_ID": "clcode-loses-session",
        },
    ],
    ids=[
        "coordinator-session-id-only",
        "claude-session-id-only",
        "claude-code-session-id-only",
        "coordinator-wins-over-both-others",
        "claude-session-id-wins-over-claude-code-session-id",
    ],
)
def test_authoring_session_env_precedence_matches_oracle(env_overrides, monkeypatch, tmp_path):
    for var in _SESSION_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for var, value in env_overrides.items():
        monkeypatch.setenv(var, value)

    out_path = tmp_path / "out.md"
    oracle_text = _run_oracle(_SPECS["handoff"]["cli"], out_path)

    values = _handoff_family_values(
        "PLACEHOLDER — replace with one-line session summary (≤140 chars)",
        authoring_session=_resolved_authoring_session(),
    )
    rendered_text = render_document("handoff", values)
    if not rendered_text.endswith("\n"):
        rendered_text += "\n"

    oracle_redacted = vol.redact_text(oracle_text, "handoff")
    rendered_redacted = vol.redact_text(rendered_text, "handoff")
    assert oracle_redacted == rendered_redacted


@pytest.mark.parametrize(
    "env_overrides",
    [
        {"COORDINATOR_SESSION_ID": "cs-only-rf"},
        {"CLAUDE_SESSION_ID": "clsess-only-rf"},
        {"CLAUDE_CODE_SESSION_ID": "clcode-only-rf"},
        {
            "COORDINATOR_SESSION_ID": "cs-wins-rf",
            "CLAUDE_SESSION_ID": "clsess-loses-rf",
            "CLAUDE_CODE_SESSION_ID": "clcode-loses-rf",
        },
        {
            "CLAUDE_SESSION_ID": "clsess-wins-rf",
            "CLAUDE_CODE_SESSION_ID": "clcode-loses-rf",
        },
    ],
    ids=[
        "coordinator-session-id-only",
        "claude-session-id-only",
        "claude-code-session-id-only",
        "coordinator-wins-over-both-others",
        "claude-session-id-wins-over-claude-code-session-id",
    ],
)
def test_lead_session_id_env_precedence_matches_oracle(env_overrides, monkeypatch, tmp_path):
    """Same 3-var precedence chain as `authoring_session` (Finding 3's own
    caveat: this pattern pre-exists for `review-findings`'s `lead_session_id`
    at lines 413-416 and was not introduced by this diff; extending the same
    coverage here is cheap since the spec already resolves it identically)."""
    for var in _SESSION_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for var, value in env_overrides.items():
        monkeypatch.setenv(var, value)

    out_path = tmp_path / "out.md"
    oracle_text = _run_oracle(_SPECS["review-findings"]["cli"], out_path)

    values = dict(_SPECS["review-findings"]["values"])
    values["lead_session_id"] = os.environ.get(
        "COORDINATOR_SESSION_ID",
        os.environ.get(
            "CLAUDE_SESSION_ID", os.environ.get("CLAUDE_CODE_SESSION_ID", "em-unknown")
        ),
    )
    rendered_text = render_document("review-findings", values)
    if not rendered_text.endswith("\n"):
        rendered_text += "\n"

    oracle_redacted = vol.redact_text(oracle_text, "review-findings")
    rendered_redacted = vol.redact_text(rendered_text, "review-findings")
    assert oracle_redacted == rendered_redacted


@pytest.mark.parametrize("doc_type", sorted(_SPECS))
def test_byte_identical_to_live_oracle(doc_type, tmp_path):
    spec = _SPECS[doc_type]
    out_path = tmp_path / "out.md"
    oracle_text = _run_oracle(spec["cli"], out_path)

    values = dict(spec["values"])
    if doc_type == "plan":
        # Narrow exception (module docstring): no CLI override exists for
        # `author` (`_resolve_from_repo()` always wins) — extract the oracle's
        # own resolved value and feed it identically to the render side.
        values["author"] = _extract_line_value(oracle_text, "author")
    elif doc_type == "decision":
        # Same shape: no CLI override exists for `id` (the oracle always
        # self-allocates via `_allocate_dr_number`) — extract the oracle's own
        # resolved id and feed it identically to the render side. This sidesteps
        # a real (if narrow) race: both sides scanning the same live
        # docs/decisions/ dir independently could diverge if a concurrent
        # process lands a new DR file between the two scans; reading the
        # oracle's own resolved value instead of re-deriving it makes this
        # test's comparison immune to that race by construction.
        values["id"] = _extract_line_value(oracle_text, "id")

    rendered_text = render_document(doc_type, values)
    # The oracle's main() ensures exactly one trailing newline at WRITE time
    # (`if not content.endswith("\n"): fh.write("\n")`) — a write-time behaviour
    # outside any `_scaffold_*` builder's own return value. render_document is
    # deliberately pure (AC6: no write), so it does not itself append this.
    # Mirror the write-time normalization here so the comparison is between two
    # "as persisted to disk" forms, not silently accepting a bare trailing-
    # newline difference as a content divergence.
    if not rendered_text.endswith("\n"):
        rendered_text += "\n"

    oracle_redacted = vol.redact_text(oracle_text, doc_type)
    rendered_redacted = vol.redact_text(rendered_text, doc_type)

    if oracle_redacted != rendered_redacted:
        pytest.fail(
            f"byte-identity conformance FAILED for doc_type={doc_type!r} "
            f"(repo-HEAD={_repo_head_sha()}):\n"
            f"--- oracle (redacted) ---\n{oracle_redacted}\n"
            f"--- rendered (redacted) ---\n{rendered_redacted}\n"
        )

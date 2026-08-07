"""
test_cross_repo_memo_c6.py — C6 round-trip and fixture-behavioral tests.

Spec backlink: docs/plans/2026-05-30-pickup-cross-repo-memo-fork.md § C6

Two parts:

  C6a — kind-contract round-trip (binds AC4/AC5/AC6 at the integration level).
    For each kind ∈ {ask, consult, fyi, proposal} AND the no-kind case:
      1. Drive the CLI to write a memo to a temp receiver dir.
      2. Assert the written file validates schema-GREEN under cross-repo-memo.yaml
         using schema.js (the same validator that lint-frontmatter.js uses).
      3. Assert the surfacing helper (workday-start-cross-repo-memo-surface.py)
         bands it correctly:
           ask/consult/proposal → appears before a fyi memo in output (urgent band)
           fyi                  → appears after ask/consult (quiet band), carries [fyi] marker
           no-kind              → bands as ask (urgent, no [fyi] marker)
    Claim: proves the kind contract agrees end-to-end across C2/C3/C4.

  C6b — memo-fixture behavioral assertion (binds AC2/AC7).
    1. Positive: a memo fixture with status=actioned + decision=accepted + decision_note
       validates schema-GREEN.
    2. Positive (fyi variant): a memo with status=actioned + actioned_note (no decision)
       validates schema-GREEN.
    3. Negative-spec (AC2): applying handoff-schema mutations to a memo produces
       schema-INVALID results:
         - status: consumed   → REJECTED (not in memo status enum)
         - status: active     → REJECTED (not in memo status enum)
         - deployment_state: in_flight → REJECTED (unknown required field remains required;
           the schema.js cross-field rule fires because memo's required fields
           are missing — we verify any schema error results, or check status enum)
       This is the mechanical proof that "the handoff active→consumed mutation
       must never be applied to a memo."

Run with: python3 -m pytest coordinator/bin/test_cross_repo_memo_c6.py
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile
import textwrap

import pytest

# Native schema-validation seam (replaces the Node lib/schema.js shell-out
# retired in the 2026-07-22 de-node cutover). Repo root must be on sys.path
# for the coordinator_core package import to resolve, regardless of invocation
# cwd. Three dirnames, not two: this file lives at <repo>/coordinator/bin/, so
# two lands on <repo>/coordinator/ — a directory with no coordinator_core in it.
# The off-by-one was invisible under a serial run, where cwd is already the repo
# root and satisfies the import, and surfaced only under `-n auto`, where xdist
# workers do not carry cwd on sys.path and the module failed to collect at all.
_REPO_ROOT_FOR_IMPORT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _REPO_ROOT_FOR_IMPORT not in sys.path:
    sys.path.insert(0, _REPO_ROOT_FOR_IMPORT)
from coordinator_core.frontmatter.schema_validate import (  # noqa: E402
    load_schemas,
    parse_frontmatter,
    validate_frontmatter_obj,
)
from coordinator_core.win_portability import no_console_creationflags

# ---------------------------------------------------------------------------
# Test infrastructure (mirrors test_cross_repo_memo.py conventions)
# ---------------------------------------------------------------------------



def _bin_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _script_path() -> str:
    return os.path.join(_bin_dir(), "cross-repo-memo")


def _surface_helper_path() -> str:
    return os.path.join(_bin_dir(), "workday-start-cross-repo-memo-surface.py")


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(_bin_dir()))


def _contract_schemas_dir() -> str:
    """Native replacement for the retired lib/schema.js + schemas/ pairing.

    coordinator_core/contract/ vendors cross-repo-memo.schema.json as a
    GENERATED PROJECTION (x-generated-by: coordinator_core.contract.emit_memo_schema)
    of the same shape schema.js used to validate — the frontmatter-schema-driven
    base-required/enum checks plus the schema_validate._MEMO_CROSS_FIELD_RULES
    lifecycle rules run underneath validate_frontmatter_obj(). This is the SAME
    behavioral SSOT the JS validator delegated to before the 2026-07-22 de-node
    cutover deleted coordinator/bin/lib/schema.js — see CLAUDE.md § Runtime
    conventions ("Query/read layer is fully native").
    """
    return os.path.join(_repo_root(), "coordinator_core", "contract")


def _python() -> str:
    return sys.executable


# Standalone-script helpers mirror test_cross_repo_memo.py by design (no shared import); keep in sync if either changes.
# Review: code-reviewer F8 — deliberate duplication noted; these helpers are not accidentally repeated.
def _run_dispatcher(args: list[str], env: dict, stdin_text: str = "") -> subprocess.CompletedProcess:
    """Invoke cross-repo-memo CLI via subprocess (Windows-compatible)."""
    return subprocess.run(
        [_python(), _script_path()] + args,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        input=stdin_text,
    )


def _make_mock_machine_local(tmpdir: str, return_value: str) -> str:  # mirrors test_cross_repo_memo.py by design
    """Create a stub machine-local Python script that returns return_value."""
    stub_path = os.path.join(tmpdir, "_mock_machine_local.py")
    escaped = return_value.replace("\\", "\\\\")
    script = textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        print("{escaped}")
        sys.exit(0)
    """)
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(script)
    return stub_path


# ---------------------------------------------------------------------------
# Real-op seam plumbing (A8 strangler cutover) — mirrors test_cross_repo_memo.py
# / test_cross_repo_memo_draft.py's identically-named helpers. C6a drives the
# CLI's flag-only send path (`--to`/`--topic`/`--title`, no subcommand), which
# post-cutover dispatches through cc_invoke.route_mutation onto the REAL
# claude-klabauter memo.send op — MACHINE_LOCAL_IMPL-only mocking no longer satisfies
# it (the op reads registry.toml directly via COORDINATOR_SETTINGS_HOME,
# bypassing MACHINE_LOCAL_IMPL entirely). A fixture-resolvable CLAUDE_KLABAUTER_ROOT
# and an isolated registry.toml are both required; CLAUDE_KLABAUTER_ROOT-unresolvable
# machines SKIP loud (never silently degrade) via `skip_test`.
# ---------------------------------------------------------------------------

TESTS_SKIPPED = 0
SKIPS: list[str] = []


def skip_test(name: str, reason: str) -> None:
    """Record a LOUD skip — printed and tallied separately from pass/fail,
    never silent. Used only when the real claude-klabauter op seam is genuinely
    unresolvable on this machine (CLAUDE_KLABAUTER_ROOT unresolvable) — mirrors
    test_cross_repo_memo.py / test_cross_repo_memo_draft.py's identically-named
    helper."""
    global TESTS_SKIPPED
    TESTS_SKIPPED += 1
    msg = f"  SKIP: {name} — {reason}"
    SKIPS.append(msg)
    print(msg)


def _resolve_test_claude_klabauter_root() -> str | None:
    """Resolve CLAUDE_KLABAUTER_ROOT for real-op (send) subcommand tests.

    Routes through the SAME cc_invoke._resolve_claude_klabauter_root() four-rung ladder
    test_cross_repo_memo.py's identically-named helper uses (env var -> pointer
    file -> machine-local registry entry -> coordinator_core.invoke importable),
    so all three sibling test files degrade identically across machines.
    Returns None (never raises) when genuinely unresolvable, so callers SKIP
    loud instead of silently degrading."""
    lib_dir = os.path.join(_bin_dir(), "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import cc_invoke  # noqa: E402 (late import after sys.path manipulation)
    try:
        return cc_invoke._resolve_claude_klabauter_root()
    except RuntimeError:
        return None


def _write_registry_toml(settings_home: str, entries: dict[str, str]) -> None:
    """Write an ISOLATED machine-local registry.toml under settings_home mapping
    each repos.<key> -> path — the exact surface claude-klabauter's memo.send op reads
    directly via stdlib tomllib (COORDINATOR_SETTINGS_HOME/machine-local/
    registry.toml). Distinct from MACHINE_LOCAL_IMPL, which only affects this
    CLI's OWN (example-doctrine-repo-side) machine-local lookups."""
    import json as _json
    reg_dir = os.path.join(settings_home, "machine-local")
    os.makedirs(reg_dir, exist_ok=True)
    with open(os.path.join(reg_dir, "registry.toml"), "w", encoding="utf-8") as f:
        for key, path in entries.items():
            f.write(f'"{key}" = {_json.dumps(path)}\n')


def _repo_key_for(to: str) -> str:
    """Mirror memo_send.py's convention_repo_key (strip trailing '-em', dashes->
    underscores, prefix 'repos.') for the isolated registry.toml a real-op test
    writes."""
    suffix = to[:-3] if to.endswith("-em") else to
    return "repos." + suffix.replace("-", "_")


def _today() -> str:
    return datetime.date.today().isoformat()






# ---------------------------------------------------------------------------
# Schema validation helper — native seam (coordinator_core.frontmatter.schema_validate)
#
# Replaces the retired Node lib/schema.js shell-out (deleted in the 2026-07-22
# de-node cutover — see coordinator_core/DIRECTORY.md and CLAUDE.md § Runtime
# conventions). load_schemas()/parse_frontmatter()/validate_frontmatter_obj()
# are direct Python ports of schema.js's loadSchemas/parseFrontmatter/
# validateFrontmatter (same module docstring cites the JS line numbers ported
# from), so this exercises the same validation shape+cross-field behavior the
# retired JS path did — just no Node runtime required for claude-klabauter's own work.
# ---------------------------------------------------------------------------


def _validate_memo_file(file_path: str) -> dict:
    """Validate a memo file against the cross-repo-memo schema.

    Returns the {ok, errors} dict from validate_frontmatter_obj(), which
    dispatches to the same JSON-Schema shape validation + cross-field rules
    (schema_validate._MEMO_CROSS_FIELD_RULES) the retired schema.js path used.
    Raises RuntimeError if the cross-repo-memo schema cannot be resolved, so
    callers that historically treated a Node/module-resolution failure as
    "validation infra broken, not a memo defect" keep that same skip-loud shape.
    """
    try:
        schemas = load_schemas(_contract_schemas_dir())
        schema = schemas["cross-repo-memo"]
    except (KeyError, OSError, ValueError) as exc:
        raise RuntimeError(f"cross-repo-memo schema unresolvable: {exc}") from exc

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    parsed = parse_frontmatter(content)
    frontmatter = parsed.get("frontmatter") or {}
    return validate_frontmatter_obj(frontmatter, schema)


def _run_surface_helper(inbox_dir: str, mock_today: str = "") -> str:
    """Run workday-start-cross-repo-memo-surface.py against inbox_dir and return output."""
    env = {**os.environ, "CROSS_REPO_INBOX_DIR": inbox_dir}
    if mock_today:
        env["MOCK_TODAY"] = mock_today
    result = subprocess.run(
        [sys.executable, _surface_helper_path()],
        env=env,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    return result.stdout


def _write_memo_fixture(inbox_dir: str, filename: str, frontmatter_body: str) -> str:
    """Write a raw memo fixture directly to inbox_dir/<filename>.

    frontmatter_body is the YAML content between the --- delimiters.
    Returns the full path to the written file.
    """
    os.makedirs(inbox_dir, exist_ok=True)
    file_path = os.path.join(inbox_dir, filename)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(f"---\n{frontmatter_body}\n---\n\nMemo body.\n")
    return file_path


# ---------------------------------------------------------------------------
# C6a — kind-contract round-trip
#
# For each kind ∈ {ask, consult, fyi, <absent>}:
#   1. CLI writes a memo to a temp receiver dir.
#   2. Written file validates schema-GREEN.
#   3. Surfacing helper bands it correctly.
#
# The banding assertion for the fyi vs. urgent band requires at least two
# memos in the inbox so the sort order is observable.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind, expected_band, test_num", [
    ("ask",      "urgent", "T1"),
    ("consult",  "urgent", "T2"),
    ("fyi",      "quiet",  "T3"),
    (None,       "urgent", "T4"),  # absent kind → defaults to ask (urgent)
    ("proposal", "urgent", "T5"),  # proposal is action-requiring → urgent band
])
def test_c6a_kind_roundtrip_schema_and_band(
    kind: str | None,
    expected_band: str,  # "urgent" or "quiet"
    test_num: str,
) -> None:
    """Drive CLI → validate schema-GREEN → verify surface banding for one kind value."""
    kind_label = kind if kind is not None else "<absent>"
    name = f"C6a-{test_num} — kind={kind_label}: CLI→schema-GREEN, surfaces in {expected_band} band"

    today = _today()
    # Use MOCK_TODAY = today so age reads "0 days old" and no stale flag fires.
    mock_today = today

    claude_klabauter_root = _resolve_test_claude_klabauter_root()
    if claude_klabauter_root is None:
        skip_test(name, "CLAUDE_KLABAUTER_ROOT unresolvable on this machine — cannot exercise the real memo.send op the flag-only path now dispatches through")
        return

    with tempfile.TemporaryDirectory() as receiver_tmpdir, \
         tempfile.TemporaryDirectory() as claude_home_tmpdir, \
         tempfile.TemporaryDirectory() as stub_tmpdir:

        mock_impl = _make_mock_machine_local(stub_tmpdir, receiver_tmpdir)
        # A8 strangler cutover: the flag-only send path now dispatches through
        # cc_invoke.route_mutation onto the real memo.send op, which reads
        # registry.toml directly via COORDINATOR_SETTINGS_HOME — wire an
        # isolated registry.toml registering the receiver alongside CLAUDE_KLABAUTER_ROOT.
        _write_registry_toml(claude_home_tmpdir, {_repo_key_for("example-retrieval-repo-em"): receiver_tmpdir})
        env = {
            "MACHINE_LOCAL_IMPL": mock_impl,
            "CLAUDE_HOME": claude_home_tmpdir,
            "COORDINATOR_SETTINGS_HOME": claude_home_tmpdir,
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root,
        }

        # Build CLI args — include --kind only when a value is provided.
        cli_args = [
            "--to", "example-retrieval-repo-em",
            "--topic", f"kind-test-{kind_label.replace('<', '').replace('>', '')}",
            "--title", f"Kind={kind_label} Test Memo",
            # DEC-1 (2026-07-2x): memo.send makes --summary a required send-time
            # field alongside --kind — omit-and-derive is no longer permitted.
            "--summary", f"C6a kind-contract round-trip test for kind={kind_label}.",
        ]
        if kind is not None:
            cli_args.extend(["--kind", kind])

        # ask/proposal (and absent-kind, which defaults to ask) require a
        # well-formed scoped_to since C2/C3 (2026-07-21) — consult/fyi are
        # exempt. Add it here so the test continues to exercise the
        # CLI->schema-GREEN->banding path rather than tripping the new
        # fail-closed refusal.
        if kind is None or kind in ("ask", "proposal"):
            cli_args.extend([
                "--scoped-to-artifact", "coordinator/bin/lib/schema.js",
                "--scoped-to-sha", "abc1234",
                "--scoped-to-seam", "cross-field-validation",
            ])

        result = _run_dispatcher(cli_args, env=env, stdin_text="This is the memo body.\n")

        # AC8 (2026-08-04): the receiver_tmpdir here is a plain (non-git)
        # directory, so delivery lands but the commit leg degrades — exit 2
        # is the documented "delivered uncommitted" outcome, not a failure
        # (see test_cross_repo_memo.py::test_non_git_receiver_proceeds, the
        # sibling test this file's CLI-driving test predates).
        if result.returncode not in (0, 2):
            raise AssertionError(f"{name}: " + (f"CLI failed (exit {result.returncode}): {result.stderr}"))

        # Locate the written file. Receiver filename is <date>-<from>-<topic>.md;
        # scan by topic suffix so the test is environment-agnostic.
        topic_slug = f"kind-test-{kind_label.replace('<', '').replace('>', '')}"
        inbox_dir = os.path.join(receiver_tmpdir, "cross-repo", "inbox")
        import glob as _glob
        matches = _glob.glob(os.path.join(inbox_dir, f"*-{topic_slug}.md"))
        if not matches:
            raise AssertionError(f"{name}: " + (f"Expected written file not found in {inbox_dir} (pattern *-{topic_slug}.md)"))
        written_file = matches[0]

        # --- 2. Schema validation (AC5) ---
        try:
            validation = _validate_memo_file(written_file)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"Schema validation failed to run: {exc}"))

        if not validation.get("ok"):
            errs = validation.get("errors", [])
            raise AssertionError(f"{name}: " + (f"Schema validation FAILED (expected GREEN): {errs}"))

        # --- 3. Surface banding (AC6) ---
        # inbox_dir already assigned above when locating the written file.

        # Write a companion fyi memo so we can compare line order.
        # For the fyi test case, write a companion ask memo.
        companion_kind = "fyi" if expected_band == "urgent" else "ask"
        companion_filename = f"{today}-companion-{companion_kind}.md"
        companion_fm = textwrap.dedent(f"""\
            title: "Companion {companion_kind} Memo"
            from: "claude-central-em"  # Review: code-reviewer — F1: fixture updated to match post-flip _sender_em_id() canonical id
            to: "example-retrieval-repo-em"
            created: {today}
            status: open
            kind: {companion_kind}
        """).strip()
        _write_memo_fixture(inbox_dir, companion_filename, companion_fm)

        surface_output = _run_surface_helper(inbox_dir, mock_today=mock_today)

        # Find line numbers for each memo in output.
        lines = surface_output.splitlines()
        our_line_num = next(
            (i + 1 for i, l in enumerate(lines) if f"Kind={kind_label}" in l),
            None
        )
        companion_line_num = next(
            (i + 1 for i, l in enumerate(lines) if f"Companion {companion_kind}" in l),
            None
        )

        if our_line_num is None:
            raise AssertionError(f"{name}: " + (f"Our memo not found in surface output:\n{surface_output}"))
        if companion_line_num is None:
            raise AssertionError(f"{name}: " + (f"Companion memo not found in surface output:\n{surface_output}"))

        if expected_band == "urgent":
            # ask/consult/proposal/no-kind → should appear BEFORE the fyi companion.
            if our_line_num < companion_line_num:
                pass  # correct
            else:
                raise AssertionError(f"{name}: " + (f"Expected urgent memo (line {our_line_num}) to appear before "
                    f"fyi companion (line {companion_line_num}) in output:\n{surface_output}"))
            # Urgent memos should NOT carry [fyi] marker.
            our_line_text = lines[our_line_num - 1]
            if "[fyi]" in our_line_text:
                raise AssertionError(f"{name}: " + (f"Urgent memo should not carry [fyi] marker; got: {our_line_text!r}"))
        else:
            # fyi → should appear AFTER the ask companion.
            if our_line_num > companion_line_num:
                pass  # correct
            else:
                raise AssertionError(f"{name}: " + (f"Expected fyi memo (line {our_line_num}) to appear after "
                    f"ask companion (line {companion_line_num}) in output:\n{surface_output}"))
            # Quiet memos should carry [fyi] marker.
            our_line_text = lines[our_line_num - 1]
            if "[fyi]" not in our_line_text:
                raise AssertionError(f"{name}: " + (f"Quiet (fyi) memo should carry [fyi] marker; got: {our_line_text!r}"))



# ---------------------------------------------------------------------------
# C6b — memo-fixture behavioral assertion (binds AC2/AC7)
#
# Note on M0 already-actioned short-circuit (SKILL.md Memo Branch M0, added per code-reviewer F1):
#   M0 is a prose-branch behavior verified by SKILL prose, not by schema validation.
#   An already-actioned memo is schema-valid (status=actioned passes the memo enum).
#   C6b's schema tests intentionally do not cover M0 — there is nothing schema-level
#   to assert. The guard is behavioral: the EM reads it as read-only context and stops.
#   Schema tests assert what schema.js rejects; M0's stop condition is agent behavior.
#   Review: code-reviewer F5 — behavioral-only M0 coverage noted here.
#
# Tests:
#   1. Positive: status=actioned + decision=accepted + decision_note → schema-GREEN.
#   2. Positive (fyi variant): status=actioned + actioned_note (no decision) → schema-GREEN.
#   3. Negative: status=consumed → schema-INVALID (not in memo status enum).
#   4. Negative: status=active → schema-INVALID (not in memo status enum).
#   5. Negative: deployment_state: in_flight present → schema-INVALID (unknown field causes
#      the memo to be validated against memo schema which does not have required fields
#      expected — or the memo status is still wrong; we assert the validator rejects it).
#
# Note on C6b test 5 (deployment_state):
#   The memo schema has no 'deployment_state' field at all. Adding 'deployment_state'
#   to a memo does NOT by itself cause schema.js to reject it (schema.js does not
#   reject unknown optional fields — only required-field absence and enum violations
#   cause errors). Therefore C6b test 5 proves the COMBINATION: a file with
#   deployment_state is a HANDOFF (not a memo), and when we drop its proper handoff
#   required fields (status: active → consumed flip), the memo validator would
#   reject status: consumed. The key negative assertion is AC2: the validator
#   DOES reject status: consumed (not in memo enum), which is the actual mutation
#   a /pickup handoff branch would apply. deployment_state alone with status: open
#   would pass memo schema (unknown fields are tolerated); the test correctly uses
#   status: consumed as the pivot.
# ---------------------------------------------------------------------------

def _build_base_memo_fm(
    status: str,
    today: str,
    extra_fields: dict[str, str] | None = None,
) -> str:
    """Build minimal valid memo frontmatter for the given status.

    No `kind:` field is set here, so these fixtures default to kind=ask
    (schema.js's absent-kind default) and therefore require a well-formed
    `scoped_to` since C2 (2026-07-21) — added unconditionally so C6b's
    positive fixtures continue to validate schema-GREEN.
    """
    lines = [
        f'title: "C6b Test Memo"',
        f'from: "claude-central-em"',  # Review: code-reviewer — F1: fixture updated to match post-flip _sender_em_id() canonical id
        f'to: "example-retrieval-repo-em"',
        f'created: {today}',
        f'status: {status}',
        f'delivery_mode: receiver-repo',
        f'summary: "A brief summary for testing."',
        f'scoped_to:',
        f'  artifact: "coordinator/bin/lib/schema.js"',
        f'  sha: "abc1234"',
        f'  seam: "cross-field-validation"',
    ]
    if extra_fields:
        for k, v in extra_fields.items():
            lines.append(f'{k}: {v}')
    return "\n".join(lines)


def test_c6b_actioned_ask_validates_green() -> None:
    """C6b-1 (AC7): status=actioned + decision=accepted + decision_note + realized_by validates schema-GREEN.

    This is the 'ask' terminal state after /pickup adjudicate-and-own.
    realized_by is REQUIRED when status=actioned and decision=accepted (claim-of-record rule,
    2026-06-23 example-game-repo B3 incident — prevents a second session from re-realizing the same memo).
    Valid shapes: sentinel "inline", a plan/task path (contains "/"), or a hex commit SHA.
    Spec backlink: bin/lib/schema.js cross-field rule ~line 844.
    """
    name = "C6b-1 (AC7) — actioned+decision+decision_note+realized_by (claim-of-record) memo validates schema-GREEN"
    today = _today()

    fm = _build_base_memo_fm(
        status="actioned",
        today=today,
        extra_fields={
            "decision": "accepted",
            "decision_note": '"Applied the requested change."',
            "realized_by": '"inline"',
        },
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        inbox_dir = os.path.join(tmpdir, "cross-repo", "inbox")
        file_path = _write_memo_fixture(inbox_dir, f"{today}-c6b-test1.md", fm)
        try:
            validation = _validate_memo_file(file_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"Schema validation failed to run: {exc}"))

        if not validation.get("ok"):
            errs = validation.get("errors", [])
            raise AssertionError(f"{name}: " + (f"Schema validation FAILED (expected GREEN): {errs}"))



def test_c6b_actioned_fyi_validates_green() -> None:
    """C6b-2 (AC7): status=actioned + actioned_note (no decision field) validates schema-GREEN.

    This is the 'fyi' terminal state: receiver acknowledges only.
    No 'decision' field is required or expected for fyi acknowledgement.
    """
    name = "C6b-2 (AC7) — actioned+actioned_note (fyi variant) validates schema-GREEN"
    today = _today()

    fm = _build_base_memo_fm(
        status="actioned",
        today=today,
        extra_fields={
            "actioned_note": '"noted — informational"',
        },
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        inbox_dir = os.path.join(tmpdir, "cross-repo", "inbox")
        file_path = _write_memo_fixture(inbox_dir, f"{today}-c6b-test2.md", fm)
        try:
            validation = _validate_memo_file(file_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"Schema validation failed to run: {exc}"))

        if not validation.get("ok"):
            errs = validation.get("errors", [])
            raise AssertionError(f"{name}: " + (f"Schema validation FAILED (expected GREEN): {errs}"))



def test_c6b_status_consumed_rejected() -> None:
    """C6b-3 (AC2): status=consumed is NOT in the memo status enum → schema-INVALID.

    'consumed' is a HANDOFF status (active → consumed). Applying it to a memo
    is the exact mutation the /pickup handoff branch would wrongly apply. The
    validator must REJECT it. This is the mechanical proof of AC2.
    """
    name = "C6b-3 (AC2) — status=consumed is REJECTED by memo schema (handoff mutation negative-spec)"
    today = _today()

    fm = _build_base_memo_fm(status="consumed", today=today)

    with tempfile.TemporaryDirectory() as tmpdir:
        inbox_dir = os.path.join(tmpdir, "cross-repo", "inbox")
        file_path = _write_memo_fixture(inbox_dir, f"{today}-c6b-consumed.md", fm)
        try:
            validation = _validate_memo_file(file_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"Schema validation failed to run: {exc}"))

        if validation.get("ok"):
            raise AssertionError(f"{name}: " + ("Schema validation returned OK (expected REJECT) for status=consumed. "
                "This means the handoff 'active→consumed' mutation would pass memo "
                "schema validation — a VIOLATION of AC2."))

        # Confirm the error mentions status and the invalid value.
        errs = validation.get("errors", [])
        errs_str = str(errs)
        if "status" not in errs_str.lower() and "consumed" not in errs_str.lower():
            raise AssertionError(f"{name}: " + (f"Rejected but error does not mention 'status'/'consumed': {errs}"))



def test_c6b_status_active_rejected() -> None:
    """C6b-4 (AC2): status=active is NOT in the memo status enum → schema-INVALID.

    'active' is the HANDOFF initial status. It is not a valid memo status.
    Verifies that the memo schema enum properly excludes handoff-only values.
    """
    name = "C6b-4 (AC2) — status=active is REJECTED by memo schema (handoff status negative-spec)"
    today = _today()

    fm = _build_base_memo_fm(status="active", today=today)

    with tempfile.TemporaryDirectory() as tmpdir:
        inbox_dir = os.path.join(tmpdir, "cross-repo", "inbox")
        file_path = _write_memo_fixture(inbox_dir, f"{today}-c6b-active.md", fm)
        try:
            validation = _validate_memo_file(file_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"Schema validation failed to run: {exc}"))

        if validation.get("ok"):
            raise AssertionError(f"{name}: " + ("Schema validation returned OK (expected REJECT) for status=active. "
                "Handoff 'active' status must not be valid for memo schema."))

        errs = validation.get("errors", [])
        errs_str = str(errs)
        if "status" not in errs_str.lower() and "active" not in errs_str.lower():
            raise AssertionError(f"{name}: " + (f"Rejected but error does not mention 'status'/'active': {errs}"))



def test_c6b_combined_handoff_mutation_rejected() -> None:
    """C6b-5 (AC2): combined handoff mutation (status=consumed + deployment_state) is REJECTED.

    This is the complete set of mutations /pickup would apply to a handoff:
    - status: active → consumed
    - deployment_state: in_flight

    Applied to a memo file, the validator must reject it. The pivot assertion
    is status=consumed (not in memo enum); deployment_state is an additional
    signal that this is not a memo (but unknown optional fields alone do not
    cause rejection in schema.js).
    """
    name = (
        "C6b-5 (AC2) — combined handoff mutation (status=consumed + deployment_state) "
        "REJECTED by memo schema"
    )
    today = _today()

    fm = _build_base_memo_fm(
        status="consumed",
        today=today,
        extra_fields={
            "deployment_state": "in_flight",
        },
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        inbox_dir = os.path.join(tmpdir, "cross-repo", "inbox")
        file_path = _write_memo_fixture(inbox_dir, f"{today}-c6b-handoff-mut.md", fm)
        try:
            validation = _validate_memo_file(file_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"Schema validation failed to run: {exc}"))

        if validation.get("ok"):
            raise AssertionError(f"{name}: " + ("Schema validation returned OK (expected REJECT) for the combined handoff "
                "mutation. The /pickup handoff branch must NOT be applied to memos."))



# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

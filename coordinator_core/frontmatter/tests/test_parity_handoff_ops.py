"""
coordinator_core.frontmatter.tests.test_parity_handoff_ops

Parity harness — CLI byte-parity between claude-klabauter's native handoff op handlers and
FROZEN GOLDEN snapshots of the legacy JS CLIs (example-doctrine-repo coordinator/bin/) they
were ported from, via coordinator_core.testing.golden (de-node Gate A, C3).

Covered operations (each captured ONCE, CAPTURE_GOLDENS=1, against the live
Node.js oracle CLI, then committed under `_goldens/parity_handoff_ops/`):

  consume   — handoff-transition.js consume
  supersede — handoff-transition.js supersede
  ship      — handoff-transition.js ship
  stamp     — stamp-shipped-in.js

Ordinary runs shell out ONLY to the Python op handlers and diff their resulting
on-disk content against the committed golden — no node / example-doctrine-repo checkout
needed at test time.

TestHandoffPhaseCrossFieldParity is a distinct LIVE-ORACLE-turned-golden shape
(not a mutation-parity harness like the above): its golden freezes the
lint-frontmatter.js --file --json cross-field-rule-logic verdict (H-CROSS-EXEC-1/2
from example-doctrine-repo's CROSS_FIELD_RULES['handoff']) so claude-klabauter's validate_frontmatter() can be
diffed against it without a live node/example-doctrine-repo dependency at test time. This is
a drift guard on rule LOGIC, which check_schema_drift() (byte-comparing
schema.json shape) does not cover.

TestOvercapSummaryRejectionParity / TestEmptySessionIdParity freeze the JS CLI's
rejection exit code (rc != 0) as a golden fact — the fixture content itself never
changes on rejection (both paths must leave the file byte-identical to the
original), so there is nothing else oracle-derived to freeze for those cases.

normalize — normalize-handoff-frontmatter.js (legacy_normalize path) — is NOT
converted here: TestNormalizeParity below is already retired (skip-marked) since
the finish-strangler collapse (chunk C6) removed the JS-dispatch seam it compared
against; there is no live JS side left, so it was never in this suite's node-live
surface and needs no golden conversion.

Method: for each mutation-parity test case, two copies of the same fixture file
are written to separate temp directories.  Under `CAPTURE_GOLDENS=1`, the JS CLI
is run on the first copy and its resulting content is frozen as the golden;
ordinarily, only the Python op handler runs (on the second copy), and its
resulting on-disk content is diffed against the committed golden.

Parity contract:
  - Byte-identical resulting file content for all fields EXCEPT deliverable_id.
  - AC6: deliverable_id is internally minted — assert structural shape only
    (dlv-<slug>-<6hex>), NOT byte-equality of the suffix.
  - AC5: consumed_at and session_id are op INPUTS — they are pinned to fixed
    values in both paths so byte-equality holds for those fields.

Negative-spec: none of the fixture bodies or lint-frontmatter.js --json output
below embed a tmp_path-derived absolute prefix — frontmatter content has no path
fields, and lint-frontmatter.js's JSON `file` key is `repoRel` (relative to repo
root, see example-doctrine-repo coordinator/bin/lint-frontmatter.js:72), not an absolute
path — so no tmp_path normalization step is needed before freezing here (unlike
`coordinator_core/tests/test_dag_js_parity.py`'s `--format paths`/`json` CLI
output, which does embed absolute paths and requires `_normalize_paths_output`/
`_normalize_json_tmp_path`). Verified empirically at conversion time by
inspecting a captured golden for path leakage before committing it.

Regenerating goldens (deliberate, reviewed action only — see Decisions in
docs/plans/2026-07-21-parity-suites-freeze-to-goldens.md):
    CAPTURE_GOLDENS=1 python3 -m pytest coordinator_core/frontmatter/tests/test_parity_handoff_ops.py -q
(requires `node` on PATH and the example-doctrine-repo sibling checkout to be resolvable.)

Run (from the repo root):
  python3 -m pytest coordinator_core/frontmatter/tests/test_parity_handoff_ops.py -q

Spec backlinks:
  example-doctrine-repo: coordinator/bin/handoff-transition.js
  example-doctrine-repo: coordinator/bin/stamp-shipped-in.js
  example-doctrine-repo: coordinator/bin/normalize-handoff-frontmatter.js
  example-doctrine-repo: coordinator/bin/lint-frontmatter.js
  Port sources: coordinator_core/ops/handoff_transition.py
                coordinator_core/ops/handoff_stamp.py
                coordinator_core/ops/handoff_normalize.py
  Pattern reference: coordinator_core/tests/test_dag_js_parity.py (C1, the
                      reference conversion this chunk (C3) copies).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

import pytest

from coordinator_core.testing.doe_root import resolve_doe_root
from coordinator_core.testing.golden import assert_matches_golden, is_capturing, load_golden

# Real-git spawn is load-bearing: fixtures build a real git repo
# (`_init_git_repo`/`_common_dir`) so ops under test resolve the ACTUAL
# git-common-dir/worktree layout, not a mocked path -- this parity suite's
# whole purpose is proving real behaviour against the JS oracle. Per-test
# repos for isolation.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_GOLDEN_NAMESPACE = "parity_handoff_ops"

# ---------------------------------------------------------------------------
# JS CLI paths (example-doctrine-repo sibling repo) — resolved lazily and ONLY consulted
# under CAPTURE_GOLDENS=1 (see `_require_oracle` below). Never gates an
# ordinary (non-capture) run — that skip-on-missing shape is exactly the
# silent-green hazard this de-node conversion closes (see module docstring
# and coordinator_core/testing/golden.py's own negative-spec).
# ---------------------------------------------------------------------------

_DOE_COORDINATOR = Path(resolve_doe_root() or "/doe-root-unresolved") / "coordinator"
_DOE_BIN = _DOE_COORDINATOR / "bin"
_TRANSITION_JS = _DOE_BIN / "handoff-transition.js"
_STAMP_JS = _DOE_BIN / "stamp-shipped-in.js"
_NORMALIZE_JS = _DOE_BIN / "normalize-handoff-frontmatter.js"
_LINT_JS = _DOE_BIN / "lint-frontmatter.js"
_DOE_SCHEMAS_DIR = _DOE_COORDINATOR / "schemas"


def _require_oracle() -> str:
    """Return the resolved `node` executable path, or raise loudly.

    Called ONLY from CAPTURE_GOLDENS=1 recapture paths. A recapture with no
    node/oracle to capture from is a hard user error, not a skip — ordinary
    (non-capturing) test runs never call this function.
    """
    node = shutil.which("node")
    missing = [
        str(p) for p in (_TRANSITION_JS, _STAMP_JS, _NORMALIZE_JS, _LINT_JS)
        if not p.is_file()
    ]
    if node is None or missing:
        raise RuntimeError(
            "CAPTURE_GOLDENS=1 recapture requires `node` on PATH and the example-doctrine-repo "
            "sibling checkout's coordinator/bin/*.js CLIs to be resolvable — neither "
            f"is needed for an ordinary (non-capture) run. node={node!r} missing={missing!r}"
        )
    return node

# claude-klabauter's own vendored copy of handoff.schema.json — the drift-checked schema
# validate_frontmatter() actually validates against (see check_schema_drift /
# TestSchemaFilesExist in test_schema_validate.py). Live-oracle parity means the
# LIVE example-doctrine-repo JS rule engine vs claude-klabauter's Python re-implementation, both consulted
# fresh per assertion — not a frozen transcription of either side's output.
_CLAUDE_KLABAUTER_HANDOFF_SCHEMA = Path(__file__).parent.parent / "schemas" / "handoff.schema.json"

# ---------------------------------------------------------------------------
# Python op imports (fires @register_op side-effects)
# ---------------------------------------------------------------------------

import coordinator_core.ops.handoff_transition  # noqa: F401
import coordinator_core.ops.handoff_stamp       # noqa: F401
import coordinator_core.ops.handoff_normalize    # noqa: F401

from coordinator_core.ops.handoff_transition import _handler as _transition_handler
from coordinator_core.ops.handoff_stamp import _handler as _stamp_handler
from coordinator_core.ops.handoff_normalize import _handler as _normalize_handler
from coordinator_core.frontmatter.primitives import split_frontmatter, read_fm_field
from coordinator_core.frontmatter.schema_validate import validate_frontmatter

# ---------------------------------------------------------------------------
# AC5 — pinned op inputs (consumed_at / session_id)
# ---------------------------------------------------------------------------

_PINNED_AT = "2026-01-02T10:00:00Z"          # ISO timestamp; ':' forces YAML quoting
_PINNED_SESSION = "session-parity-test-abc"   # no structural chars → unquoted

# Portable subprocess creation flags (popup-safe-env-suppressed):
# CREATE_NO_WINDOW on Windows, 0 (no-op) on macOS/Linux.
_CFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------------------
# AC6 — deliverable_id structural shape
# ---------------------------------------------------------------------------

_DLV_SHAPE_RE = re.compile(r"^dlv-.+-[0-9a-f]{6}$")

# ---------------------------------------------------------------------------
# Fixture content
# ---------------------------------------------------------------------------

# Pre-cutoff basic handoff — no category/summary requirement (created 2026-01-01).
# title, created, branch, status, predecessor are all present so schema validation
# passes for consume/supersede/ship ops.
_BASIC_FIXTURE = """\
---
title: Parity Test Handoff
created: 2026-01-01
branch: work/test/2026-01-01
status: active
predecessor: none
---

# Parity Test Handoff

Body text for parity testing.
"""

# Basic fixture with an HTML comment preamble (installer-seeded baton shape).
# Tests that preamble+HTML-comment blocks are preserved verbatim through all ops.
_PREAMBLE_FIXTURE = """\
<!-- example_retrieval_repo_setup baton v2 -->
<!-- generated: 2026-01-01 -->
---
title: Parity Preamble Handoff
created: 2026-01-01
branch: work/test/2026-01-01
status: active
predecessor: none
---

# Parity Preamble Handoff

Body text.
"""

# Post-cutoff handoff with over-cap summary (141 chars > 140-char limit).
# Used for rejection-parity tests: both JS and Python must return non-zero.
_OVERCAP_SUMMARY = "A" * 141
_OVERCAP_FIXTURE = (
    "---\n"
    "title: Parity Overcap Handoff\n"
    "created: 2026-06-01\n"
    "branch: work/test/2026-06-01\n"
    "status: active\n"
    "predecessor: none\n"
    "category: infra\n"
    f'summary: "{_OVERCAP_SUMMARY}"\n'
    "---\n"
    "\n# Parity Overcap Handoff\n\nBody text.\n"
)

# Normalize target — all six drift conditions present.
#   Rule 1: created has ISO time component
#   Rule 2: pickup_ready is quoted "true"
#   Rule 3: category absent (title has no recognizable keyword → uncategorized)
#   Rule 4: summary absent (will be backfilled from H1)
#   Rule 5: deliverable_id absent (AC6: minted — structural check only)
#   Rule 6: initiative absent (will be inserted as null)
_NORMALIZE_FIXTURE = """\
---
title: Parity Normalize Target
created: 2026-07-01T12:34:56Z
branch: work/test/2026-07-01
pickup_ready: "true"
status: active
predecessor: none
---

# Parity Normalize Target Body Heading

Body text.
"""

# Normalize fixture with HTML comment preamble — tests preamble preservation.
_NORMALIZE_PREAMBLE_FIXTURE = """\
<!-- installer-provenance: parity-test -->
---
title: Parity Normalize With Preamble
created: 2026-07-01T09:00:00Z
branch: work/test/2026-07-01
status: active
predecessor: none
---

# Parity Normalize With Preamble

Body text.
"""

# ---------------------------------------------------------------------------
# Git repo helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    """Initialise a minimal git repo at path (silent, no initial commit needed)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        capture_output=True, check=True,
        creationflags=_CFLAGS,  # popup-safe-env-suppressed
    )
    for cfg_args in (
        ["user.email", "parity@test.local"],
        ["user.name", "Parity Test"],
        ["commit.gpgsign", "false"],
    ):
        subprocess.run(
            ["git", "config"] + cfg_args,
            cwd=str(path), capture_output=True, check=True,
            creationflags=_CFLAGS,  # popup-safe-env-suppressed
        )


def _common_dir(repo_root: Path) -> Path:
    """Return the resolved git common dir for a repo (repo_root/.git for standard)."""
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=str(repo_root), capture_output=True, check=True,
        creationflags=_CFLAGS,  # popup-safe-env-suppressed
    )
    return Path(result.stdout.decode().strip()).resolve()


# ---------------------------------------------------------------------------
# Async runner helper
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio dependency."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# JS CLI runner helpers
# ---------------------------------------------------------------------------


def _js_transition(
    verb: str,
    handoff_path: Path,
    *,
    session_id: Optional[str] = None,
    at: Optional[str] = None,
) -> Tuple[int, str, str]:
    """Run handoff-transition.js <verb> and return (exit_code, stdout, stderr).

    CAPTURE_GOLDENS=1 recapture path ONLY — never called on an ordinary run.
    """
    cmd = [_require_oracle(), str(_TRANSITION_JS), verb, "--handoff", str(handoff_path)]
    if session_id is not None:
        cmd += ["--session-id", session_id]
    if at is not None:
        cmd += ["--at", at]
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        creationflags=_CFLAGS,  # popup-safe-env-suppressed
    )
    return result.returncode, result.stdout, result.stderr


def _js_stamp(handoff_path: Path, sha: str) -> Tuple[int, str, str]:
    """Run stamp-shipped-in.js and return (exit_code, stdout, stderr).

    CAPTURE_GOLDENS=1 recapture path ONLY — never called on an ordinary run.
    """
    cmd = [_require_oracle(), str(_STAMP_JS), "--handoff", str(handoff_path), "--sha", sha]
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        creationflags=_CFLAGS,  # popup-safe-env-suppressed
    )
    return result.returncode, result.stdout, result.stderr


def _js_lint_file(repo_root: Path, file_path: Path) -> Tuple[int, str, str]:
    """Run lint-frontmatter.js --file <file_path> --root <repo_root> --json.

    Exit 0 = schema-valid (or no matching schema — treated as valid by this
    caller since all fixtures below are constructed to match the handoff
    schema); exit 1 = schema/cross-field violation. Mirrors handoff.stamp_phase's
    AC3 live-oracle contract: shells to the LIVE example-doctrine-repo lint-frontmatter.js
    CROSS_FIELD_RULES['handoff'].

    CAPTURE_GOLDENS=1 recapture path ONLY — never called on an ordinary run
    (see TestHandoffPhaseCrossFieldParity, which diffs against the frozen
    golden the rest of the time).
    """
    cmd = [
        _require_oracle(), str(_LINT_JS),
        "--file", str(file_path),
        "--root", str(repo_root),
        "--json",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        creationflags=_CFLAGS,  # popup-safe-env-suppressed
    )
    return result.returncode, result.stdout, result.stderr


def _js_normalize(repo_root: Path) -> Tuple[int, str, str]:
    """Run normalize-handoff-frontmatter.js --write with legacy path forced.

    CLAUDE_KLABAUTER_ROOT is set to a non-existent directory so resolveClaudeKlabauterRoot() returns
    the fake path (Rung 1 short-circuits to env var).  PYTHONSAFEPATH=1 prevents
    Python 3.11+ from adding '' (cwd) to sys.path — without it, seamPresent()'s
    `python3 -c 'import coordinator_core.client'` succeeds because cwd resolves to
    the project root, routing the JS CLI to the native Python client instead of the
    legacy_normalize() text-transform under test.

    With both guards set, seamPresent() returns False → legacy_normalize() runs, and
    we compare the JS text-transform core against the Python port.
    """
    # Review: code-reviewer — F5: PYTHONSAFEPATH silently vacuous on Python < 3.11; skip if < 3.11
    # so we don't compare Python-routed-to-Python instead of JS-text-transform vs Python port.
    if sys.version_info < (3, 11):
        pytest.skip(
            "PYTHONSAFEPATH requires Python ≥ 3.11 — normalize parity test would be "
            "vacuous on 3.10 (seam check may succeed via cwd sys.path). Skipping.",
        )
    env = {
        **os.environ,
        "CLAUDE_KLABAUTER_ROOT": str(repo_root / "__fake_claude_klabauter_nonexistent"),
        # PYTHONSAFEPATH=1: prevents '' (cwd) from being prepended to sys.path so
        # `import coordinator_core.client` fails in the seamPresent() check even when
        # pytest is invoked from inside the project directory.  Requires Python ≥ 3.11.
        "PYTHONSAFEPATH": "1",
    }
    cmd = [
        _require_oracle(), str(_NORMALIZE_JS),
        "--write", "--root", str(repo_root),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, env=env,
        creationflags=_CFLAGS,  # popup-safe-env-suppressed
    )
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# Fixture-writing helpers
# ---------------------------------------------------------------------------


def _write_handoff(path: Path, content: str) -> None:
    """Write handoff content to path, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_handoff_fixture(fm: dict, *, heading: str, body: str = "Body text.") -> str:
    """Render an fm dict + body into handoff frontmatter text (plain scalars only).

    All values used by the C3 handoff_phase fixtures below are plain strings
    with no YAML-structural characters (':', quotes, etc.), so unquoted
    'key: value' lines round-trip identically through both the JS YAML parser
    and PyYAML — no serialize_yaml_scalar quoting guard is needed here (unlike
    the SHA-quoting fixtures in TestStampParity above).
    """
    lines = ["---"]
    for key, value in fm.items():
        if value is None:
            lines.append(f"{key}: null")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {heading}")
    lines.append("")
    lines.append(body)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Golden helpers
# ---------------------------------------------------------------------------


def _mutation_golden_content(case: str, capture_fn) -> str:
    """Return the golden resulting-file-content text for `case`.

    Under CAPTURE_GOLDENS=1, calls `capture_fn()` (which must run the live JS
    CLI on its own js-side fixture copy, assert its exit code, and return the
    resulting file content) and freezes the result as the golden. Ordinarily,
    `capture_fn` is never invoked — the committed golden is loaded instead. No
    tmp_path normalization is applied here: see module docstring negative-spec
    (mutation output fields carry no path values).
    """
    if is_capturing():
        content = capture_fn()
        assert_matches_golden(content, _GOLDEN_NAMESPACE, case, kind="text")
        return content
    return load_golden(_GOLDEN_NAMESPACE, case, kind="text").decode("utf-8")


def _rejection_golden_rc(case: str, capture_fn) -> int:
    """Return the golden JS exit code for a rejection-parity `case`.

    Under CAPTURE_GOLDENS=1, calls `capture_fn()` (which must run the live JS
    CLI, assert it left the fixture file byte-unchanged, and return its exit
    code) and freezes {"rc": <code>} as the golden. Ordinarily, `capture_fn`
    is never invoked — only the frozen rc is used, to assert the native
    Python side's exit-code contract agrees without any live node call.
    """
    if is_capturing():
        rc = capture_fn()
        assert_matches_golden({"rc": rc}, _GOLDEN_NAMESPACE, case, kind="json")
        return rc
    return load_golden(_GOLDEN_NAMESPACE, case, kind="json")["rc"]


def _cross_field_golden(case: str, capture_fn) -> dict:
    """Return the golden lint-frontmatter.js --json verdict for `case`.

    Under CAPTURE_GOLDENS=1, calls `capture_fn()` (which must run the live
    lint-frontmatter.js --file --json oracle and return its parsed JSON
    verdict alongside a bool `valid` flag) and freezes the result. Ordinarily,
    `capture_fn` is never invoked — the committed golden verdict is diffed
    against claude-klabauter's validate_frontmatter() output instead. No tmp_path
    normalization needed: lint-frontmatter.js's `file` key is `repoRel`
    (relative to repo root — see example-doctrine-repo coordinator/bin/lint-frontmatter.js:72),
    not an absolute path (see module docstring negative-spec).
    """
    if is_capturing():
        verdict = capture_fn()
        assert_matches_golden(verdict, _GOLDEN_NAMESPACE, case, kind="json")
        return verdict
    return load_golden(_GOLDEN_NAMESPACE, case, kind="json")


# ---------------------------------------------------------------------------
# Content comparison helpers
# ---------------------------------------------------------------------------


def _consume_golden_to_new_vocab(text: str) -> str:
    """Translate a frozen consume golden (old status:consumed/consumed_at/consumed_by)
    to the DR-084 vocabulary the Python writer now emits (status:claimed/claimed_at/
    claimed_by). The JS oracle predates DR-084 and is never recaptured, so the
    committed golden is byte-identical modulo this rename.
    """
    text = re.sub(r"^status: consumed$", "status: claimed", text, flags=re.MULTILINE)
    text = re.sub(r"^consumed_at:", "claimed_at:", text, flags=re.MULTILINE)
    text = re.sub(r"^consumed_by:", "claimed_by:", text, flags=re.MULTILINE)
    return text


def _ship_golden_to_new_vocab(text: str) -> str:
    """Translate a frozen ship golden (old status:active, untouched by ship) to the
    DR-084 P4-narrowed vocabulary (status:open). ship's own JS oracle output never
    reads consumed_at/consumed_by (those fields are absent from the ship fixtures),
    so only the status token itself needs the rename — same never-recaptured
    rationale as _consume_golden_to_new_vocab above.
    """
    return re.sub(r"^status: active$", "status: open", text, flags=re.MULTILINE)


def _supersede_golden_to_new_vocab(text: str, continued_into: str) -> str:
    """Translate a frozen supersede golden (old status:consumed/deployment_state:
    abandoned) to DR-084 vocabulary (status:claimed/deployment_state:continued +
    continued_into). deployment_state:abandoned has retired; continued is the only
    automated-writer-eligible replacement and requires continued_into as positive
    succession proof, so this also inserts that field (absent from the pre-DR-084
    golden by construction).
    """
    text = re.sub(r"^status: consumed$", "status: claimed", text, flags=re.MULTILINE)
    text = re.sub(
        r"^deployment_state: abandoned$",
        f"deployment_state: continued\ncontinued_into: {continued_into}",
        text,
        flags=re.MULTILINE,
    )
    return text


def _get_fm(path: Path) -> str:
    """Return the fm_text of the file at path, asserting valid frontmatter exists."""
    content = path.read_text(encoding="utf-8")
    split = split_frontmatter(content)
    assert split is not None, f"no parseable frontmatter in {path}"
    return split.fm_text


def _field(fm: str, key: str) -> Optional[str]:
    """Return trimmed raw value for key in fm, or None."""
    return read_fm_field(fm, key)


def _assert_byte_equal_except_dlv(
    js_content: str,
    py_content: str,
    *,
    context: str = "",
) -> None:
    """Assert JS and Python outputs are byte-identical excluding the deliverable_id line.

    Strips the deliverable_id line from both before comparison (AC6 — id is
    internally minted with time/pid/random entropy → suffix differs).  All other
    content must be exactly equal.
    """
    def _strip_dlv(text: str) -> str:
        return re.sub(r"^deliverable_id:.*\n", "", text, flags=re.MULTILINE)

    js_stripped = _strip_dlv(js_content)
    py_stripped = _strip_dlv(py_content)
    assert js_stripped == py_stripped, (
        f"JS/Python output mismatch{' (' + context + ')' if context else ''}.\n"
        f"JS output:\n{js_stripped!r}\n\nPython output:\n{py_stripped!r}"
    )


def _assert_dlv_shape(fm: str, *, label: str) -> None:
    """Assert deliverable_id (when present) has the structural shape dlv-<slug>-<6hex>."""
    dlv = _field(fm, "deliverable_id")
    if dlv is None:
        return
    # Review: code-reviewer — F11: explicit single-pair removal to avoid stripping ''-quoted values
    raw = dlv[1:-1] if (dlv.startswith("'") and dlv.endswith("'")) else dlv
    assert _DLV_SHAPE_RE.match(raw), (
        f"{label}: deliverable_id '{raw}' does not match dlv-<slug>-<6hex> shape"
    )


# ---------------------------------------------------------------------------
# Tests — consume parity
# ---------------------------------------------------------------------------


class TestConsumeParity:
    """handoff-transition.js consume (golden) vs Python _transition_handler consume."""

    def test_consume_basic_fixture(self, tmp_path):
        """Byte-equal consume on a basic pre-cutoff handoff (no preamble)."""
        py_repo = tmp_path / "py_repo"
        _init_git_repo(py_repo)
        py_file = py_repo / "state" / "handoffs" / "basic.md"
        _write_handoff(py_file, _BASIC_FIXTURE)

        def _capture() -> str:
            js_repo = tmp_path / "js_repo"
            _init_git_repo(js_repo)
            js_file = js_repo / "state" / "handoffs" / "basic.md"
            _write_handoff(js_file, _BASIC_FIXTURE)
            js_rc, js_out, js_err = _js_transition(
                "consume", js_file,
                session_id=_PINNED_SESSION,
                at=_PINNED_AT,
            )
            assert js_rc == 0, f"JS consume failed (rc={js_rc}):\n{js_err}"
            return js_file.read_text(encoding="utf-8")

        expected = _consume_golden_to_new_vocab(_mutation_golden_content("consume_basic", _capture))

        py_result = _run(_transition_handler(
            {
                "verb": "consume",
                "handoff_path": str(py_file),
                "session_id": _PINNED_SESSION,
                "at": _PINNED_AT,
            },
            repo_root=_common_dir(py_repo),
        ))
        assert py_result["exit_code"] == 0, (
            f"Python consume failed: {py_result.get('error')}"
        )

        py_content = py_file.read_text(encoding="utf-8")
        assert py_content == expected, (
            "consume basic: golden mismatch.\n"
            f"golden:\n{expected!r}\n\nPython:\n{py_content!r}"
        )

        # Field-level spot-checks
        fm = _get_fm(py_file)
        assert _field(fm, "status") == "claimed"
        assert _field(fm, "deployment_state") == "in_flight"
        assert _PINNED_AT in (_field(fm, "claimed_at") or "")
        assert _field(fm, "claimed_by") == _PINNED_SESSION

    def test_consume_preamble_preserved(self, tmp_path):
        """Consume preserves HTML-comment preamble byte-identically (both paths)."""
        py_repo = tmp_path / "py_repo"
        _init_git_repo(py_repo)
        py_file = py_repo / "state" / "handoffs" / "preamble.md"
        _write_handoff(py_file, _PREAMBLE_FIXTURE)

        def _capture() -> str:
            js_repo = tmp_path / "js_repo"
            _init_git_repo(js_repo)
            js_file = js_repo / "state" / "handoffs" / "preamble.md"
            _write_handoff(js_file, _PREAMBLE_FIXTURE)
            js_rc, _, js_err = _js_transition(
                "consume", js_file,
                session_id=_PINNED_SESSION,
                at=_PINNED_AT,
            )
            assert js_rc == 0, f"JS consume (preamble) failed: {js_err}"
            return js_file.read_text(encoding="utf-8")

        expected = _consume_golden_to_new_vocab(_mutation_golden_content("consume_preamble", _capture))

        py_result = _run(_transition_handler(
            {
                "verb": "consume",
                "handoff_path": str(py_file),
                "session_id": _PINNED_SESSION,
                "at": _PINNED_AT,
            },
            repo_root=_common_dir(py_repo),
        ))
        assert py_result["exit_code"] == 0, (
            f"Python consume (preamble) failed: {py_result.get('error')}"
        )

        py_content = py_file.read_text(encoding="utf-8")
        assert py_content == expected, (
            "consume preamble: golden mismatch.\n"
            f"golden:\n{expected!r}\n\nPython:\n{py_content!r}"
        )
        assert "<!-- example_retrieval_repo_setup baton v2 -->" in py_content
        assert "<!-- generated: 2026-01-01 -->" in py_content


# ---------------------------------------------------------------------------
# Tests — supersede parity
# ---------------------------------------------------------------------------


class TestSupersedeParity:
    """handoff-transition.js supersede (golden) vs Python _transition_handler supersede."""

    def test_supersede_basic_fixture(self, tmp_path):
        """Byte-equal supersede on a basic pre-cutoff handoff.

        Fixture is pre-claimed (status: claimed, not the shared _BASIC_FIXTURE's
        status: active) to satisfy the DR-242 gate the dispatcher's supersede
        verb enforces (`claimed_or_shipped_at_path` —
        `coordinator_core/ops/handoff_transition.py`): supersede now refuses a
        handoff that was never claimed or shipped, since a successor-named
        child is not evidence of succession. This is a local variant, not an
        edit to the shared `_BASIC_FIXTURE` (other test classes below rely on
        its status: active starting value). The byte-equality assertion is
        unaffected: `_supersede`'s status write is idempotent on an
        already-claimed record (`_status_is` treats "claimed" as already at
        target), so the rendered output matches the frozen golden exactly as
        it did when the fixture started at status: active.
        """
        py_repo = tmp_path / "py_repo"
        _init_git_repo(py_repo)
        py_file = py_repo / "state" / "handoffs" / "basic.md"
        _supersede_fixture = _BASIC_FIXTURE.replace("status: active", "status: claimed")
        _write_handoff(py_file, _supersede_fixture)

        def _capture() -> str:
            js_repo = tmp_path / "js_repo"
            _init_git_repo(js_repo)
            js_file = js_repo / "state" / "handoffs" / "basic.md"
            _write_handoff(js_file, _supersede_fixture)
            js_rc, _, js_err = _js_transition("supersede", js_file)
            assert js_rc == 0, f"JS supersede failed: {js_err}"
            return js_file.read_text(encoding="utf-8")

        _continued_into = "state/handoffs/successor.md"
        expected = _supersede_golden_to_new_vocab(
            _mutation_golden_content("supersede_basic", _capture), _continued_into
        )

        py_result = _run(_transition_handler(
            {
                "verb": "supersede",
                "handoff_path": str(py_file),
                "continued_into": _continued_into,
            },
            repo_root=_common_dir(py_repo),
        ))
        assert py_result["exit_code"] == 0, (
            f"Python supersede failed: {py_result.get('error')}"
        )

        py_content = py_file.read_text(encoding="utf-8")
        assert py_content == expected, (
            "supersede basic: golden mismatch.\n"
            f"golden:\n{expected!r}\n\nPython:\n{py_content!r}"
        )

        fm = _get_fm(py_file)
        assert _field(fm, "status") == "claimed"
        assert _field(fm, "deployment_state") == "continued"
        assert _field(fm, "continued_into") == _continued_into
        assert _field(fm, "claimed_at") is None, "supersede must NOT write claimed_at"
        assert _field(fm, "claimed_by") is None, "supersede must NOT write claimed_by"

    # test_supersede_preamble_preserved retired (DR-242, 2026-07-29): unlike
    # test_supersede_basic_fixture above, this case cannot be fixed by
    # pre-claiming the fixture. The DR-242 gate's precondition oracle,
    # `claimed_or_shipped_at_path` -> `_frontmatter()`
    # (`coordinator_core/tests/_baton_dag_oracle.py`), only recognizes
    # frontmatter when the file's bytes literally start with "---"
    # (`text.startswith("---")`); `_PREAMBLE_FIXTURE`'s whole point is an
    # HTML-comment preamble BEFORE that delimiter (the installer-seeded
    # baton shape). So for this fixture shape, `_frontmatter()` always
    # returns "" regardless of status/claimed_at/deployment_state, and the
    # gate always refuses — no fixture state can satisfy both the
    # byte-equality assertion and the DR-242 precondition at once, which is
    # the fallback condition the dispatch brief named for retiring rather
    # than fixturing. (Flagged upstream, not fixed here: this also means the
    # DR-242 gate would incorrectly refuse a genuinely-claimed real handoff
    # that happens to carry a leading comment preamble — out of scope for
    # this dispatch, which is confined to this test file.) Preamble
    # preservation itself remains covered for every other verb this file
    # parity-tests: test_consume_preamble_preserved, test_ship_preamble_preserved,
    # test_stamp_preamble_preserved, test_normalize_preamble_preserved.


# ---------------------------------------------------------------------------
# Tests — ship parity
# ---------------------------------------------------------------------------


class TestShipParity:
    """handoff-transition.js ship (golden) vs Python _transition_handler ship."""

    def test_ship_basic_fixture(self, tmp_path):
        """Byte-equal ship on a basic pre-cutoff handoff (no shipped_in required)."""
        py_repo = tmp_path / "py_repo"
        _init_git_repo(py_repo)
        py_file = py_repo / "state" / "handoffs" / "h.md"
        _write_handoff(py_file, _BASIC_FIXTURE.replace("status: active", "status: open"))

        def _capture() -> str:
            js_repo = tmp_path / "js_repo"
            _init_git_repo(js_repo)
            js_file = js_repo / "state" / "handoffs" / "h.md"
            _write_handoff(js_file, _BASIC_FIXTURE)
            js_rc, _, js_err = _js_transition("ship", js_file)
            assert js_rc == 0, f"JS ship failed: {js_err}"
            return js_file.read_text(encoding="utf-8")

        expected = _ship_golden_to_new_vocab(_mutation_golden_content("ship_basic", _capture))

        py_result = _run(_transition_handler(
            {"verb": "ship", "handoff_path": str(py_file)},
            repo_root=_common_dir(py_repo),
        ))
        assert py_result["exit_code"] == 0, (
            f"Python ship failed: {py_result.get('error')}"
        )

        py_content = py_file.read_text(encoding="utf-8")
        assert py_content == expected, (
            "ship basic: golden mismatch.\n"
            f"golden:\n{expected!r}\n\nPython:\n{py_content!r}"
        )

        fm = _get_fm(py_file)
        assert _field(fm, "deployment_state") == "shipped"
        assert _field(fm, "status") == "open", "ship must NOT change status"

    def test_ship_preamble_preserved(self, tmp_path):
        """Ship preserves HTML-comment preamble byte-identically."""
        py_repo = tmp_path / "py_repo"
        _init_git_repo(py_repo)
        py_file = py_repo / "state" / "handoffs" / "h.md"
        _write_handoff(py_file, _PREAMBLE_FIXTURE.replace("status: active", "status: open"))

        def _capture() -> str:
            js_repo = tmp_path / "js_repo"
            _init_git_repo(js_repo)
            js_file = js_repo / "state" / "handoffs" / "h.md"
            _write_handoff(js_file, _PREAMBLE_FIXTURE)
            js_rc, _, js_err = _js_transition("ship", js_file)
            assert js_rc == 0, f"JS ship (preamble) failed: {js_err}"
            return js_file.read_text(encoding="utf-8")

        expected = _ship_golden_to_new_vocab(_mutation_golden_content("ship_preamble", _capture))

        py_result = _run(_transition_handler(
            {"verb": "ship", "handoff_path": str(py_file)},
            repo_root=_common_dir(py_repo),
        ))
        assert py_result["exit_code"] == 0

        py_content = py_file.read_text(encoding="utf-8")
        assert py_content == expected
        assert "<!-- example_retrieval_repo_setup baton v2 -->" in py_content


# ---------------------------------------------------------------------------
# Tests — stamp parity (SHA quoting variants)
# ---------------------------------------------------------------------------

# Fixture for stamp: already has consumed_at so shipped_in anchors after it.
# Deliberately old-vocabulary (status:consumed/consumed_at/consumed_by) — this is
# a JS-parity fixture (the JS oracle predates DR-084) AND it exercises handoff_stamp's
# archived-schema consumed_at fallback anchor (claimed_at absent → anchors on
# consumed_at instead; see coordinator_core/ops/handoff_stamp.py). The new-vocabulary
# claimed_at anchor path is covered separately below (test_stamp_claimed_at_anchor,
# Python-only — no JS golden, since claimed_at postdates the JS oracle).
_STAMP_BASE_FIXTURE = """\
---
title: Parity Stamp Test
created: 2026-01-01
branch: work/test/2026-01-01
status: consumed
consumed_at: '2026-01-02T10:00:00Z'
consumed_by: session-stamp-test
predecessor: none
---

# Parity Stamp Test

Body text.
"""

# New-vocabulary variant (status:claimed/claimed_at/claimed_by) — used only by
# test_stamp_claimed_at_anchor (Python-only, no JS golden).
_STAMP_CLAIMED_FIXTURE = """\
---
title: Parity Stamp Test
created: 2026-01-01
branch: work/test/2026-01-01
status: claimed
claimed_at: '2026-01-02T10:00:00Z'
claimed_by: session-stamp-test
predecessor: none
---

# Parity Stamp Test

Body text.
"""


class TestStampParity:
    """stamp-shipped-in.js (golden) vs Python _stamp_handler — SHA quoting variants.

    Three quoting guards (from stamp-shipped-in.js serializeYamlScalar + F0 fix):
      - '#'-in-SHA: structural char → quoted
      - all-numeric SHA: YAML parses as integer → quoted (F0 integer-coerce defense)
      - scientific-notation SHA (e.g. '1958e194'): YAML 1.1 float → quoted (F0)
    """

    def _run_both(
        self, tmp_path: Path, sha: str, case: str, *, filename: str = "stamp.md",
        fixture: str = _STAMP_BASE_FIXTURE,
    ) -> str:
        """Stamp the py-side fixture, diff against the golden for `case`, return py_content."""
        py_repo = tmp_path / "py_repo"
        _init_git_repo(py_repo)
        py_file = py_repo / "state" / "handoffs" / filename
        _write_handoff(py_file, fixture)

        def _capture() -> str:
            js_repo = tmp_path / "js_repo"
            _init_git_repo(js_repo)
            js_file = js_repo / "state" / "handoffs" / filename
            _write_handoff(js_file, fixture)
            js_rc, _, js_err = _js_stamp(js_file, sha)
            assert js_rc == 0, f"JS stamp (sha={sha!r}) failed: {js_err}"
            return js_file.read_text(encoding="utf-8")

        expected = _mutation_golden_content(case, _capture)
        # kind is now REQUIRED on the Python side (DR-096, example-doctrine-repo
        # 2026-07-26/27 follow-up) — the JS oracle predates shipped_in_kind
        # entirely and never writes it, so the golden's SHA-quoting-only
        # content gets one line inserted (immediately after shipped_in:,
        # matching the handler's own lockstep-insert contract) before the
        # byte-for-byte compare below. This test's SUBJECT is SHA quoting,
        # not shipped_in_kind — the insertion keeps that subject unchanged
        # while still exercising the real (now kind-required) handler.
        shipped_in_line = next(
            ln for ln in expected.splitlines(keepends=True)
            if ln.lstrip().startswith("shipped_in:")
        )
        expected = expected.replace(
            shipped_in_line, shipped_in_line + "shipped_in_kind: ship-commit\n", 1
        )

        py_result = _run(_stamp_handler(
            {"handoff_path": str(py_file), "sha": sha, "kind": "ship-commit"},
            repo_root=_common_dir(py_repo),
        ))
        assert py_result["exit_code"] == 0, (
            f"Python stamp (sha={sha!r}) failed: {py_result.get('error')}"
        )

        py_content = py_file.read_text(encoding="utf-8")
        assert py_content == expected, (
            f"stamp {case}: golden mismatch.\ngolden:\n{expected!r}\n\nPython:\n{py_content!r}"
        )
        return py_content

    def test_stamp_hash_in_sha(self, tmp_path):
        """SHA with '#' is single-quoted by both paths (#-in-SHA defense)."""
        sha = "abc#def123"
        py_c = self._run_both(tmp_path, sha, "stamp_hash_in_sha")
        assert f"shipped_in: '{sha}'" in py_c

    def test_stamp_all_numeric_sha(self, tmp_path):
        """All-numeric SHA is single-quoted (F0 integer-coerce defense)."""
        sha = "274671833"
        py_c = self._run_both(tmp_path, sha, "stamp_all_numeric_sha")
        assert f"shipped_in: '{sha}'" in py_c

    def test_stamp_scientific_sha(self, tmp_path):
        """Scientific-notation SHA ('1958e194') is single-quoted (YAML 1.1 float defense)."""
        sha = "1958e194"
        py_c = self._run_both(tmp_path, sha, "stamp_scientific_sha")
        assert f"shipped_in: '{sha}'" in py_c

    def test_stamp_normal_hex_sha(self, tmp_path):
        """Normal hex SHA (letters + digits, no structural chars) is unquoted."""
        sha = "a1b2c3d4e5f6"
        py_c = self._run_both(tmp_path, sha, "stamp_normal_hex_sha")
        # Unquoted — no single-quotes around the SHA value
        assert f"shipped_in: {sha}" in py_c

    def test_stamp_preamble_preserved(self, tmp_path):
        """Stamp preserves HTML-comment preamble byte-identically."""
        preamble_fixture = (
            "<!-- installer-provenance: parity-test -->\n"
            + _STAMP_BASE_FIXTURE
        )
        sha = "deadbeef"
        py_c = self._run_both(
            tmp_path, sha, "stamp_preamble",
            filename="h.md", fixture=preamble_fixture,
        )
        assert "<!-- installer-provenance: parity-test -->" in py_c

    def test_stamp_claimed_at_anchor(self, tmp_path):
        """New-vocabulary (claimed_at) records anchor shipped_in after claimed_at.

        Python-only — no JS golden (claimed_at postdates the JS oracle, which only
        ever wrote consumed_at; see _STAMP_BASE_FIXTURE's fallback-anchor coverage).
        """
        py_repo = tmp_path / "py_repo"
        _init_git_repo(py_repo)
        py_file = py_repo / "state" / "handoffs" / "claimed.md"
        _write_handoff(py_file, _STAMP_CLAIMED_FIXTURE)

        py_result = _run(_stamp_handler(
            {"handoff_path": str(py_file), "sha": "deadbeef", "kind": "ship-commit"},
            repo_root=_common_dir(py_repo),
        ))
        assert py_result["exit_code"] == 0, (
            f"Python stamp (claimed_at anchor) failed: {py_result.get('error')}"
        )

        py_content = py_file.read_text(encoding="utf-8")
        assert "claimed_at: '2026-01-02T10:00:00Z'\nshipped_in: deadbeef\n" in py_content


# ---------------------------------------------------------------------------
# Tests — normalize parity (six rules)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "normalize-handoff-frontmatter.js legacy path was intentionally removed in the "
        "finish-strangler collapse (chunk C6) — coordinator_core.invoke seam no longer "
        "dispatches handoff.normalize to JS. Python's _normalize_handler is now the sole "
        "implementation; there is no JS side left to compare against, so this JS-vs-Python "
        "parity class is retired. Parity was already proven while both implementations "
        "coexisted; kept skipped (not deleted) so the historical intent stays greppable."
    )
)
class TestNormalizeParity:
    """normalize-handoff-frontmatter.js (legacy path) vs Python _normalize_handler.

    AC6 applies: deliverable_id suffix is minted from time/pid/random entropy →
    structural shape check only (dlv-<slug>-<6hex>).  All other fields compared
    byte-identically (deliverable_id LINE stripped before comparison).

    CLAUDE_KLABAUTER_ROOT env override forces JS CLI onto the legacy_normalize() path so we
    compare the JS text-transform core (not JS-routing-to-Python-routing-to-Python).
    """

    def _run_both(
        self, tmp_path: Path, fixture_content: str, filename: str = "target.md"
    ) -> Tuple[str, str]:
        """Write fixture to two repos, normalize both, return (js_content, py_content)."""
        js_repo = tmp_path / "js_repo"
        py_repo = tmp_path / "py_repo"
        _init_git_repo(js_repo)
        _init_git_repo(py_repo)

        js_file = js_repo / "state" / "handoffs" / filename
        py_file = py_repo / "state" / "handoffs" / filename
        _write_handoff(js_file, fixture_content)
        _write_handoff(py_file, fixture_content)

        js_rc, js_out, js_err = _js_normalize(js_repo)
        assert js_rc == 0, (
            f"JS normalize failed (rc={js_rc}):\n"
            f"stdout={js_out!r}\nstderr={js_err!r}"
        )

        py_result = _run(_normalize_handler(
            {"write": True},
            repo_root=_common_dir(py_repo),
        ))
        assert py_result["exit_code"] == 0, (
            f"Python normalize failed: {py_result.get('errors')}"
        )

        return js_file.read_text(encoding="utf-8"), py_file.read_text(encoding="utf-8")

    def test_normalize_six_rules_parity(self, tmp_path):
        """All six normalize rules produce byte-identical output (excluding dlv suffix)."""
        js_c, py_c = self._run_both(tmp_path, _NORMALIZE_FIXTURE)

        _assert_byte_equal_except_dlv(js_c, py_c, context="normalize six rules")

        # AC6: both deliverable_ids must have correct structural shape.
        js_split = split_frontmatter(js_c)
        py_split = split_frontmatter(py_c)
        assert js_split is not None and py_split is not None
        _assert_dlv_shape(js_split.fm_text, label="JS normalize")
        _assert_dlv_shape(py_split.fm_text, label="Python normalize")

        # Spot-check individual rule outputs via Python (verified equal to JS above).
        fm = py_split.fm_text
        assert _field(fm, "created") == "2026-07-01", "Rule 1: created ISO time stripped"
        assert _field(fm, "pickup_ready") == "true", "Rule 2: pickup_ready unquoted"
        assert _field(fm, "category") is not None, "Rule 3: category backfilled"
        assert _field(fm, "summary") is not None, "Rule 4: summary backfilled"
        assert _field(fm, "deliverable_id") is not None, "Rule 5: deliverable_id minted"
        assert _field(fm, "initiative") == "null", "Rule 6: initiative inserted as null"

    def test_normalize_rule1_created_strip(self, tmp_path):
        """Rule 1: created ISO time component stripped to bare YYYY-MM-DD — parity."""
        fixture = (
            "---\n"
            "title: Rule1 Test\n"
            "created: 2026-05-28T11:16:48Z\n"
            "branch: work/test/2026-05-28\n"
            "status: active\n"
            "predecessor: none\n"
            "---\n\n# Rule1 Test\n\nBody.\n"
        )
        js_c, py_c = self._run_both(tmp_path, fixture, filename="rule1.md")
        _assert_byte_equal_except_dlv(js_c, py_c, context="normalize rule1")
        fm = split_frontmatter(py_c).fm_text
        assert _field(fm, "created") == "2026-05-28", "created must be bare YYYY-MM-DD"

    def test_normalize_rule2_pickup_ready_quoted_true(self, tmp_path):
        """Rule 2: pickup_ready: \"true\" (double-quoted) → bare bool true — parity."""
        fixture = (
            '---\n'
            'title: Rule2a Test\n'
            'created: 2026-01-01\n'
            'branch: work/test/2026-01-01\n'
            'status: active\n'
            'pickup_ready: "true"\n'
            'predecessor: none\n'
            '---\n\n# Rule2a Test\n\nBody.\n'
        )
        js_c, py_c = self._run_both(tmp_path, fixture, filename="rule2a.md")
        _assert_byte_equal_except_dlv(js_c, py_c, context="normalize rule2 quoted-true")
        fm = split_frontmatter(py_c).fm_text
        assert _field(fm, "pickup_ready") == "true", "pickup_ready must be bare true"

    def test_normalize_rule2_pickup_ready_quoted_false(self, tmp_path):
        """Rule 2: pickup_ready: \"false\" → bare bool false — parity."""
        fixture = (
            '---\n'
            'title: Rule2b Test\n'
            'created: 2026-01-01\n'
            'branch: work/test/2026-01-01\n'
            'status: active\n'
            'pickup_ready: "false"\n'
            'predecessor: none\n'
            '---\n\n# Rule2b Test\n\nBody.\n'
        )
        js_c, py_c = self._run_both(tmp_path, fixture, filename="rule2b.md")
        _assert_byte_equal_except_dlv(js_c, py_c, context="normalize rule2 quoted-false")
        fm = split_frontmatter(py_c).fm_text
        assert _field(fm, "pickup_ready") == "false", "pickup_ready must be bare false"

    def test_normalize_rule3_category_backfill_uncategorized(self, tmp_path):
        """Rule 3: category absent + no keyword in title → backfilled as uncategorized — parity."""
        fixture = (
            "---\n"
            "title: Generic Title With No Category Keyword\n"
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: active\n"
            "predecessor: none\n"
            "---\n\n# Generic Title With No Category Keyword\n\nBody.\n"
        )
        js_c, py_c = self._run_both(tmp_path, fixture, filename="rule3a.md")
        _assert_byte_equal_except_dlv(js_c, py_c, context="normalize rule3 uncategorized")
        fm = split_frontmatter(py_c).fm_text
        assert _field(fm, "category") == "uncategorized"

    def test_normalize_rule3_category_backfill_infra_keyword(self, tmp_path):
        """Rule 3: 'install' keyword in title → category backfilled as infra — parity."""
        fixture = (
            "---\n"
            "title: Plugin Install Automation\n"
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: active\n"
            "predecessor: none\n"
            "---\n\n# Plugin Install Automation\n\nBody.\n"
        )
        js_c, py_c = self._run_both(tmp_path, fixture, filename="rule3b.md")
        _assert_byte_equal_except_dlv(js_c, py_c, context="normalize rule3 infra")
        fm = split_frontmatter(py_c).fm_text
        assert _field(fm, "category") == "infra"

    def test_normalize_rule4_summary_backfill_from_h1(self, tmp_path):
        """Rule 4: summary absent → backfilled from first H1 in body — parity."""
        fixture = (
            "---\n"
            "title: Some Handoff Title\n"
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: active\n"
            "predecessor: none\n"
            "---\n\n# Extracted From H1 Heading\n\nBody text here.\n"
        )
        js_c, py_c = self._run_both(tmp_path, fixture, filename="rule4.md")
        _assert_byte_equal_except_dlv(js_c, py_c, context="normalize rule4 summary H1")
        fm = split_frontmatter(py_c).fm_text
        summary = _field(fm, "summary")
        assert summary is not None, "summary must be backfilled"
        assert "Extracted From H1 Heading" in (summary or "")

    def test_normalize_rule5_deliverable_id_shape_parity(self, tmp_path):
        """Rule 5 (AC6): both paths mint dlv-<slug>-<6hex>; suffixes may differ."""
        fixture = (
            "---\n"
            "title: No DLV Yet\n"
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: active\n"
            "predecessor: none\n"
            "---\n\n# No DLV Yet\n\nBody.\n"
        )
        filename = "rule5-dlv-shape.md"
        js_c, py_c = self._run_both(tmp_path, fixture, filename=filename)

        js_fm = split_frontmatter(js_c).fm_text
        py_fm = split_frontmatter(py_c).fm_text

        js_dlv = (_field(js_fm, "deliverable_id") or "").strip("'")
        py_dlv = (_field(py_fm, "deliverable_id") or "").strip("'")

        assert js_dlv, "JS: deliverable_id must be minted"
        assert py_dlv, "Python: deliverable_id must be minted"

        slug = "rule5-dlv-shape"  # stem of filename
        assert js_dlv.startswith(f"dlv-{slug}-"), (
            f"JS deliverable_id '{js_dlv}' must start with 'dlv-{slug}-'"
        )
        assert py_dlv.startswith(f"dlv-{slug}-"), (
            f"Python deliverable_id '{py_dlv}' must start with 'dlv-{slug}-'"
        )
        assert _DLV_SHAPE_RE.match(js_dlv), f"JS dlv '{js_dlv}' not dlv-<slug>-<6hex>"
        assert _DLV_SHAPE_RE.match(py_dlv), f"Python dlv '{py_dlv}' not dlv-<slug>-<6hex>"

        # Suffixes will differ (time/pid/random entropy) — that is expected and correct (AC6).
        js_suffix = js_dlv.rsplit("-", 1)[-1]
        py_suffix = py_dlv.rsplit("-", 1)[-1]
        assert re.fullmatch(r"[0-9a-f]{6}", js_suffix), f"JS suffix '{js_suffix}' not 6hex"
        assert re.fullmatch(r"[0-9a-f]{6}", py_suffix), f"Python suffix '{py_suffix}' not 6hex"

    def test_normalize_rule6_initiative_null(self, tmp_path):
        """Rule 6: initiative absent → inserted as 'null' (D9 present-as-null) — parity."""
        fixture = (
            "---\n"
            "title: Rule6 Test\n"
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: active\n"
            "predecessor: none\n"
            "---\n\n# Rule6 Test\n\nBody.\n"
        )
        js_c, py_c = self._run_both(tmp_path, fixture, filename="rule6.md")
        _assert_byte_equal_except_dlv(js_c, py_c, context="normalize rule6 initiative null")
        fm = split_frontmatter(py_c).fm_text
        assert _field(fm, "initiative") == "null", "initiative must be present-as-null"

    def test_normalize_preamble_preserved(self, tmp_path):
        """HTML-comment preamble is preserved byte-identically through normalize."""
        js_c, py_c = self._run_both(
            tmp_path, _NORMALIZE_PREAMBLE_FIXTURE,
            filename="preamble-norm.md",
        )
        _assert_byte_equal_except_dlv(js_c, py_c, context="normalize preamble preserved")
        assert "<!-- installer-provenance: parity-test -->" in py_c

    def test_normalize_idempotent_on_already_clean(self, tmp_path):
        """Second run on an already-normalized file is a no-op on both paths (D1 carry)."""
        clean_fixture = (
            "---\n"
            "title: Already Clean\n"
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "pickup_ready: false\n"
            "category: infra\n"
            "summary: A clean summary.\n"
            "deliverable_id: dlv-already-clean-abc123\n"
            "initiative: null\n"
            "status: active\n"
            "predecessor: none\n"
            "---\n\n# Already Clean\n\nBody.\n"
        )
        js_c, py_c = self._run_both(tmp_path, clean_fixture, filename="clean.md")

        # Both outputs must be byte-identical to the original (no drift detected)
        assert js_c == clean_fixture, (
            f"JS normalize modified already-clean file.\nGot:\n{js_c!r}"
        )
        assert py_c == clean_fixture, (
            f"Python normalize modified already-clean file.\nGot:\n{py_c!r}"
        )


# ---------------------------------------------------------------------------
# Tests — over-cap summary REJECTION parity (consume / supersede)
# ---------------------------------------------------------------------------


class TestOvercapSummaryRejectionParity:
    """Both JS and Python must reject a >140-char summary with non-zero exit (post-cutoff).

    The schema validation gate (D4) runs after in-memory mutations but BEFORE the
    file write — both paths must: (a) return non-zero and (b) leave the file unchanged.
    The fixture content never changes on rejection by construction, so the only
    oracle-derived fact frozen as a golden here is the JS exit code (rc != 0).
    """

    def test_consume_rejects_overcap_summary_parity(self, tmp_path):
        """JS rc!=0 (golden) AND Python exit_code!=0 AND both leave file unchanged."""
        py_repo = tmp_path / "py_repo"
        _init_git_repo(py_repo)

        # Review: code-reviewer (Finding 3) — relocated from repo root ("h.md") to
        # state/handoffs/ so this exercises the overcap-summary validation
        # rejection it's named for, not the path-containment guard (which now
        # confines handoff_path to state/handoffs/ and would reject a repo-root
        # fixture first, silently testing the wrong code path).
        py_file = py_repo / "state" / "handoffs" / "h.md"
        _write_handoff(py_file, _OVERCAP_FIXTURE)
        py_original = py_file.read_text(encoding="utf-8")

        def _capture() -> int:
            js_repo = tmp_path / "js_repo"
            _init_git_repo(js_repo)
            js_file = js_repo / "state" / "handoffs" / "h.md"
            _write_handoff(js_file, _OVERCAP_FIXTURE)
            js_original = js_file.read_text(encoding="utf-8")
            js_rc, _, js_err = _js_transition(
                "consume", js_file,
                session_id=_PINNED_SESSION,
                at=_PINNED_AT,
            )
            assert js_rc != 0, (
                f"JS consume must reject overcap summary (rc={js_rc}). stderr={js_err!r}"
            )
            assert js_file.read_text(encoding="utf-8") == js_original, (
                "JS: overcap rejection must leave file unchanged"
            )
            return js_rc

        js_rc = _rejection_golden_rc("consume_overcap_rejection", _capture)
        assert js_rc != 0

        py_result = _run(_transition_handler(
            {
                "verb": "consume",
                "handoff_path": str(py_file),
                "session_id": _PINNED_SESSION,
                "at": _PINNED_AT,
            },
            repo_root=_common_dir(py_repo),
        ))
        assert py_result["exit_code"] != 0, (
            f"Python consume must reject overcap summary; got {py_result!r}"
        )
        assert py_result["applied"] is False
        assert py_file.read_text(encoding="utf-8") == py_original, (
            "Python: overcap rejection must leave file unchanged"
        )

    def test_supersede_rejects_overcap_summary_parity(self, tmp_path):
        """supersede also rejects post-cutoff overcap summary on both paths."""
        py_repo = tmp_path / "py_repo"
        _init_git_repo(py_repo)

        # Review: code-reviewer (Finding 3) — relocated to state/handoffs/, see note above.
        py_file = py_repo / "state" / "handoffs" / "h.md"
        _write_handoff(py_file, _OVERCAP_FIXTURE)
        py_original = py_file.read_text(encoding="utf-8")

        def _capture() -> int:
            js_repo = tmp_path / "js_repo"
            _init_git_repo(js_repo)
            js_file = js_repo / "state" / "handoffs" / "h.md"
            _write_handoff(js_file, _OVERCAP_FIXTURE)
            js_original = js_file.read_text(encoding="utf-8")
            js_rc, _, js_err = _js_transition("supersede", js_file)
            assert js_rc != 0, (
                f"JS supersede must reject overcap summary (rc={js_rc})"
            )
            assert js_file.read_text(encoding="utf-8") == js_original
            return js_rc

        js_rc = _rejection_golden_rc("supersede_overcap_rejection", _capture)
        assert js_rc != 0

        py_result = _run(_transition_handler(
            {"verb": "supersede", "handoff_path": str(py_file)},
            repo_root=_common_dir(py_repo),
        ))
        assert py_result["exit_code"] != 0, (
            f"Python supersede must reject overcap summary; got {py_result!r}"
        )
        assert py_file.read_text(encoding="utf-8") == py_original


# ---------------------------------------------------------------------------
# Tests — empty session-id FAIL-LOUD parity (the Staff Engineer P2)
# ---------------------------------------------------------------------------


class TestEmptySessionIdParity:
    """Both JS and Python must reject empty session-id (the Staff Engineer P2 fail-loud).

    Invariant: never write `consumed_by:` empty — it would corrupt the claim-gate
    idempotency check and the audit trail the freeze hook rests on.
    """

    def test_empty_session_id_both_non_zero(self, tmp_path):
        """JS rc!=0 (golden) AND Python exit_code!=0 — both leave file unchanged."""
        py_repo = tmp_path / "py_repo"
        _init_git_repo(py_repo)

        # Review: code-reviewer (Finding 3) — relocated to state/handoffs/ so this
        # exercises the empty-session-id validation rejection, not the path guard.
        py_file = py_repo / "state" / "handoffs" / "h.md"
        _write_handoff(py_file, _BASIC_FIXTURE)
        py_original = py_file.read_text(encoding="utf-8")

        def _capture() -> int:
            js_repo = tmp_path / "js_repo"
            _init_git_repo(js_repo)
            js_file = js_repo / "state" / "handoffs" / "h.md"
            _write_handoff(js_file, _BASIC_FIXTURE)
            js_original = js_file.read_text(encoding="utf-8")
            js_rc, _, js_err = _js_transition(
                "consume", js_file,
                session_id="",
                at=_PINNED_AT,
            )
            assert js_rc != 0, (
                f"JS consume: empty session-id must return non-zero (rc={js_rc})"
            )
            assert js_file.read_text(encoding="utf-8") == js_original, (
                "JS: empty session-id must NOT write the file"
            )
            return js_rc

        js_rc = _rejection_golden_rc("empty_session_id_rejection", _capture)
        assert js_rc != 0

        py_result = _run(_transition_handler(
            {
                "verb": "consume",
                "handoff_path": str(py_file),
                "session_id": "",
                "at": _PINNED_AT,
            },
            repo_root=_common_dir(py_repo),
        ))
        assert py_result["exit_code"] != 0, (
            f"Python consume: empty session-id must return non-zero; got {py_result!r}"
        )
        assert py_result["applied"] is False
        assert py_file.read_text(encoding="utf-8") == py_original, (
            "Python: empty session-id must NOT write the file"
        )

    def test_whitespace_only_session_id_both_non_zero(self, tmp_path):
        """Whitespace-only session-id is also rejected by both paths."""
        py_repo = tmp_path / "py_repo"
        _init_git_repo(py_repo)

        # Review: code-reviewer (Finding 3) — relocated to state/handoffs/, see note above.
        py_file = py_repo / "state" / "handoffs" / "h.md"
        _write_handoff(py_file, _BASIC_FIXTURE)

        def _capture() -> int:
            js_repo = tmp_path / "js_repo"
            _init_git_repo(js_repo)
            js_file = js_repo / "state" / "handoffs" / "h.md"
            _write_handoff(js_file, _BASIC_FIXTURE)
            js_original = js_file.read_text(encoding="utf-8")
            js_rc, _, js_err = _js_transition(
                "consume", js_file,
                session_id="   ",
                at=_PINNED_AT,
            )
            assert js_rc != 0, (
                f"JS: whitespace-only session-id must return non-zero (rc={js_rc})"
            )
            assert js_file.read_text(encoding="utf-8") == js_original
            return js_rc

        js_rc = _rejection_golden_rc("whitespace_session_id_rejection", _capture)
        assert js_rc != 0

        py_result = _run(_transition_handler(
            {
                "verb": "consume",
                "handoff_path": str(py_file),
                "session_id": "   ",
                "at": _PINNED_AT,
            },
            repo_root=_common_dir(py_repo),
        ))
        assert py_result["exit_code"] != 0, (
            f"Python: whitespace-only session-id must return non-zero; got {py_result!r}"
        )


# ---------------------------------------------------------------------------
# Tests — handoff_phase / execution-authorization cross-field parity (C3)
#
# LIVE ORACLE, not frozen fixtures: each case below shells to the live example-doctrine-repo
# lint-frontmatter.js (which applies CROSS_FIELD_RULES['handoff'] including
# H-CROSS-EXEC-1/2) on every test run and asserts claude-klabauter's validate_frontmatter()
# verdict agrees. check_schema_drift() only byte-compares schema.json shape; it
# does NOT cover the schema.js rule-logic gap this class guards. Plain
# assert-based parity (not xfail(strict)) — example-doctrine-repo's H-CROSS-EXEC-1/2 rules and
# claude-klabauter's _cf_execution_stamp_required / _cf_handoff_phase_kind_gate
# ports are freshly landed in lockstep with no pre-existing divergence.
# ---------------------------------------------------------------------------


class TestHandoffPhaseCrossFieldParity:
    """claude-klabauter validate_frontmatter() vs a frozen golden of lint-frontmatter.js's
    live H-CROSS-EXEC-1/2 verdict — handoff_phase."""

    def _assert_parity(
        self, tmp_path: Path, fm: dict, *, heading: str, filename: str, case: str,
    ) -> None:
        """Write fm to a real repo, diff claude-klabauter's validator against the golden verdict
        for `case` (only reaches the live JS oracle under CAPTURE_GOLDENS=1)."""
        repo = tmp_path / "repo"
        _init_git_repo(repo)

        handoff_file = repo / "state" / "handoffs" / filename
        _write_handoff(handoff_file, _build_handoff_fixture(fm, heading=heading))

        def _capture() -> dict:
            js_rc, js_out, js_err = _js_lint_file(repo, handoff_file)
            assert js_rc in (0, 1), (
                f"lint-frontmatter.js --file exited with usage-error code {js_rc} "
                f"(expected 0=valid or 1=violation): stdout={js_out!r} stderr={js_err!r}"
            )
            return {"valid": js_rc == 0}

        golden = _cross_field_golden(case, _capture)
        js_valid = golden["valid"]

        py_errors = validate_frontmatter(fm, _CLAUDE_KLABAUTER_HANDOFF_SCHEMA)
        py_valid = len(py_errors) == 0

        assert py_valid == js_valid, (
            f"parity mismatch for {filename}: golden (example-doctrine-repo lint-frontmatter.js) "
            f"valid={js_valid} vs claude-klabauter valid={py_valid} (errors={py_errors!r})"
        )

    def test_cutoff_exempt_execution_no_stamp_both_valid(self, tmp_path):
        """Pre-2026-07-17 execution handoff with no four-field stamp — both VALID (cutoff exempt)."""
        fm = {
            "title": "Parity Exec Cutoff Exempt",
            "created": "2026-06-01",
            "branch": "work/test/2026-06-01",
            "status": "open",
            "predecessor": "none",
            "kind": "session-handoff",
            "category": "infra",
            "summary": "Cutoff exempt execution handoff with no stamp",
            "handoff_phase": "execution",
        }
        self._assert_parity(
            tmp_path, fm,
            heading="Parity Exec Cutoff Exempt",
            filename="cutoff-exempt.md",
            case="cutoff_exempt_execution_no_stamp",
        )

    def test_post_cutoff_execution_missing_stamp_both_invalid(self, tmp_path):
        """Post-cutoff execution handoff missing the four-field stamp — both INVALID (H-CROSS-EXEC-1)."""
        fm = {
            "title": "Parity Exec Post Cutoff Missing Stamp",
            "created": "2026-07-17",
            "branch": "work/test/2026-07-17",
            "status": "open",
            "predecessor": "none",
            "kind": "session-handoff",
            "category": "infra",
            "summary": "Post cutoff missing stamp fields",
            "handoff_phase": "execution",
        }
        self._assert_parity(
            tmp_path, fm,
            heading="Parity Exec Post Cutoff Missing Stamp",
            filename="post-cutoff-missing-stamp.md",
            case="post_cutoff_execution_missing_stamp",
        )

    def test_handoff_phase_wrong_kind_both_invalid(self, tmp_path):
        """handoff_phase present with kind != session-handoff — both INVALID (H-CROSS-EXEC-2)."""
        fm = {
            "title": "Parity Phase Wrong Kind",
            "created": "2026-06-01",
            "branch": "work/test/2026-06-01",
            "status": "open",
            "predecessor": "none",
            "handoff_phase": "continuation",
        }
        self._assert_parity(
            tmp_path, fm,
            heading="Parity Phase Wrong Kind",
            filename="phase-wrong-kind.md",
            case="handoff_phase_wrong_kind",
        )

    def test_continuation_no_stamp_both_valid(self, tmp_path):
        """Continuation-phase handoff with no four-field stamp — both VALID (stamp is execution-only)."""
        fm = {
            "title": "Parity Continuation No Stamp",
            "created": "2026-06-01",
            "branch": "work/test/2026-06-01",
            "status": "open",
            "predecessor": "none",
            "kind": "session-handoff",
            "category": "infra",
            "summary": "Continuation handoff with no stamp",
            "handoff_phase": "continuation",
        }
        self._assert_parity(
            tmp_path, fm,
            heading="Parity Continuation No Stamp",
            filename="continuation-no-stamp.md",
            case="continuation_no_stamp",
        )

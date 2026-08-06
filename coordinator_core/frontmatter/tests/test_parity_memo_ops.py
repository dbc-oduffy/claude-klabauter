"""
coordinator_core.frontmatter.tests.test_parity_memo_ops

Parity harness — proves byte-identical frontmatter output between a FROZEN GOLDEN
snapshot of the legacy JS CLI (example-doctrine-repo coordinator/bin/memo-transition.js) and the
Claude-klabauter-native Python op handler for all three memo lifecycle verbs:

  claim   — open → in_progress
  action  — in_progress → actioned
  release — in_progress → open

Method (de-node Gate A, 2026-07-21 conversion, C4): each case's oracle output was
captured ONCE (CAPTURE_GOLDENS=1, against the live Node.js oracle) and committed under
`_goldens/parity_memo_ops/`, via `coordinator_core.testing.golden`. Ordinary runs never
invoke node — they run the native Python op handler and diff its resulting on-disk
content (and, for the validation-fail arm, its exit_code) against the committed golden.
This is the same pattern `coordinator_core/tests/test_dag_js_parity.py` established as
the reference conversion for this sweep; see that module's docstring and
`coordinator_core/testing/golden.py`'s for the shared `is_capturing()` /
`assert_matches_golden` contract.

Parity contract:
  - Byte-identical resulting file content across all three verbs × fixture matrix.
  - No structural exclusions (unlike handoff parity tests, memos have no minted IDs).

Validation-fail arm (mirrors TestOvercapSummaryRejectionParity in test_parity_handoff_ops.py):
  - One reject-parity fixture per reachable cross-field rule.
  - Each fixture asserts: node-rc (frozen golden) == native-exit_code AND on-disk-unchanged.

Grandfather SKIP:
  - Pre-cutoff memo (created < 2026-05-22) is verified to be ACCEPTED by both paths
    even when it would otherwise fail validation (summary > 120 chars). (This case
    remains `xfail(strict=True)` — see `test_grandfather_skip_pre_cutoff` — a known
    cross-repo contract drift unrelated to the golden conversion; the frozen golden
    simply pins the SAME divergent oracle output that made the live-oracle comparison
    xfail before this conversion.)

All-digit realized_by:
  - realized_by="12345678" is quoted in both paths (JS serializeYamlScalar SHA-as-int guard).

Note on _memo_cf_in_progress_needs_picked_up_by:
  - This cross-field rule fires for status=in_progress, but claim always writes picked_up_by
    and action/release both transition OUT of in_progress (post-mutation status is never
    in_progress on a successful path).  The rule is unreachable via normal verb invocations
    as a rejection-producing guard.  The TestClaimCollisionParity class tests the related
    in_progress-collision rejection (claim on already-in_progress memo without picked_up_by),
    which is the closest reachable failure mode.

No 'node' needed to run this suite — it is only ever invoked during an explicit
CAPTURE_GOLDENS=1 recapture (see `_require_oracle`/`_js_memo` below), never on an
ordinary run. A missing golden fixture is a hard failure (`GoldenMissingError`), never
a skip — see `coordinator_core/testing/golden.py` module docstring negative-spec.

Regenerating goldens (deliberate, reviewed action only):
    CAPTURE_GOLDENS=1 python3 -m pytest coordinator_core/frontmatter/tests/test_parity_memo_ops.py -q
(requires `node` on PATH and the example-doctrine-repo sibling checkout to be resolvable.)

Run (from the repo root):
  python3 -m pytest coordinator_core/frontmatter/tests/test_parity_memo_ops.py -v

Spec backlinks:
  example-doctrine-repo: coordinator/bin/memo-transition.js (parity oracle, frozen — not read at
    ordinary test-run time)
  Port source: coordinator_core/ops/memo_transition.py
  Plan: docs/plans/2026-07-06-memo-transition-native-python-port.md § C4, AC7
  De-node conversion: docs/plans/2026-07-21-parity-suites-freeze-to-goldens.md § C4
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple

import pytest

from coordinator_core.testing.doe_root import resolve_doe_root
from coordinator_core.testing.golden import assert_matches_golden, is_capturing, load_golden

# ---------------------------------------------------------------------------
# JS CLI path (example-doctrine-repo sibling repo) — only ever consulted during an explicit
# CAPTURE_GOLDENS=1 recapture (see `_require_oracle`/`_js_memo`).  Not resolved (and
# not required) on an ordinary run.
# ---------------------------------------------------------------------------

# 2026-07-28: the module-level `pytestmark = pytest.mark.pending_fix` demotion
# that used to sit here is RETIRED, so this suite runs in both tiers again. It
# was demoted on 2026-07-22 (72db824d) for exactly one red case, since fixed;
# the whole module is green. Recorded on-file so a future reader can tell the
# marker was deliberately retired rather than accidentally dropped.

_NODE = shutil.which("node")
_DOE_BIN = Path(resolve_doe_root() or "/doe-root-unresolved") / "coordinator" / "bin"
_MEMO_JS = _DOE_BIN / "memo-transition.js"

_GOLDEN_NAMESPACE = "parity_memo_ops"


def _require_oracle() -> None:
    """Raise loudly if a CAPTURE_GOLDENS=1 recapture is attempted without node/the
    oracle available — never a skip (see coordinator_core/testing/golden.py module
    docstring negative-spec). Only ever called from inside an `is_capturing()` branch."""
    if _NODE is None or not _MEMO_JS.is_file():
        raise RuntimeError(
            "CAPTURE_GOLDENS=1 recapture requires `node` on PATH and the example-doctrine-repo "
            f"sibling checkout ({_MEMO_JS}) to be resolvable — neither is needed for "
            "an ordinary (non-capture) run of this suite."
        )


# ---------------------------------------------------------------------------
# Python op import (fires @register_op side-effects)
# ---------------------------------------------------------------------------

import coordinator_core.ops.memo_transition  # noqa: F401
from coordinator_core.ops.memo_transition import _handler as _memo_handler
from coordinator_core.frontmatter.primitives import (
    read_fm_field,
    read_fm_field_unquoted,
    split_frontmatter,
)
from coordinator_core.ops.fleet._memo_summary import _SUMMARY_MAX_CHARS

# ---------------------------------------------------------------------------
# Pinned op inputs (claim requires session_id + at)
# ---------------------------------------------------------------------------

_PINNED_SESSION = "session-parity-memo-test"
_PINNED_AT = "2026-01-02T10:00:00Z"         # ':' forces YAML quoting on picked_up_at
_OTHER_SESSION = "other-session-holder"       # for collision tests
_CTX = None                                   # show_top scope: no ctx.repo_root needed

# Portable subprocess flags (CREATE_NO_WINDOW on Windows, 0 on macOS/Linux).
_CFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------------------
# Fixture content
# ---------------------------------------------------------------------------

# Fresh memo — status=open, no preamble.  Valid post-cutoff (2026-06-01 > 2026-05-22).
_FRESH_MEMO = """\
---
kind: fyi
status: open
from: sender-session
summary: A test memo for parity testing.
created: 2026-06-01
---
"""

# Pre-claimed memo — status=in_progress with matching session (for action/release tests).
# Must exactly match what claim() produces on _FRESH_MEMO with _PINNED_SESSION/_PINNED_AT.
_IN_PROGRESS_MEMO = """\
---
kind: fyi
status: in_progress
picked_up_at: '2026-01-02T10:00:00Z'
picked_up_by: session-parity-memo-test
from: sender-session
summary: A test memo for parity testing.
created: 2026-06-01
---
"""

# Pre-claimed by a DIFFERENT session (for claim-collision tests).
_IN_PROGRESS_OTHER_MEMO = """\
---
kind: fyi
status: in_progress
picked_up_at: '2026-01-01T09:00:00Z'
picked_up_by: other-session-holder
from: sender-session
summary: A test memo for parity testing.
created: 2026-06-01
---
"""

# Already-in_progress but NO picked_up_by (manually crafted — for "in_progress without
# picked_up_by" collision test; the rule _memo_cf_in_progress_needs_picked_up_by is
# unreachable via normal verb ops as a post-mutation reject, but the collision check
# itself produces a consistent rejection that we can test for parity).
_IN_PROGRESS_NO_PICKED_UP_BY_MEMO = """\
---
kind: fyi
status: in_progress
from: sender-session
summary: A test memo for parity testing.
created: 2026-06-01
---
"""

# Already-actioned memo (for action idempotent-no-op and collision tests).
_ACTIONED_MEMO = """\
---
kind: fyi
status: actioned
picked_up_at: '2026-01-02T10:00:00Z'
picked_up_by: session-parity-memo-test
decision: accepted
realized_by: docs/plans/test.md
from: sender-session
summary: A test memo for parity testing.
created: 2026-06-01
---
"""

# Review: code-reviewer (F3) — note-mode idempotency fixture.
# Already-actioned memo in note-mode (actioned_note only — no decision/realized_by).
# Field order matches what action() produces starting from _IN_PROGRESS_MEMO:
#   status → actioned; actioned_note inserted after status.
_ACTIONED_NOTE_TEXT = "Noted for tracking."  # must match actioned_note field in fixture below
_ACTIONED_NOTE_MEMO = """\
---
kind: fyi
status: actioned
actioned_note: Noted for tracking.
picked_up_at: '2026-01-02T10:00:00Z'
picked_up_by: session-parity-memo-test
from: sender-session
summary: A test memo for parity testing.
created: 2026-06-01
---
"""

# Fresh memo with HTML-comment preamble (installer-seeded baton shape).
_PREAMBLE_MEMO = """\
<!-- example_retrieval_repo_setup baton v2 -->
<!-- generated: 2026-01-01 -->
---
kind: fyi
status: open
from: sender-session
summary: A preamble memo for parity testing.
created: 2026-06-01
---
"""

# In-progress memo with HTML-comment preamble.
_IN_PROGRESS_PREAMBLE_MEMO = """\
<!-- example_retrieval_repo_setup baton v2 -->
<!-- generated: 2026-01-01 -->
---
kind: fyi
status: in_progress
picked_up_at: '2026-01-02T10:00:00Z'
picked_up_by: session-parity-memo-test
from: sender-session
summary: A preamble memo for parity testing.
created: 2026-06-01
---
"""

# Over-cap summary memo (121 chars > 120-char memo cap) — post-cutoff.
# Claim on this must REJECT (summary_length_cap rule fires post-mutation).
_OVERCAP_SUMMARY = "B" * 121
_OVERCAP_MEMO = (
    "---\n"
    "kind: fyi\n"
    "status: open\n"
    "from: sender-session\n"
    f'summary: "{_OVERCAP_SUMMARY}"\n'
    "created: 2026-06-01\n"
    "---\n"
)

# Invalid kind memo — kind=ack is NOT in the valid enum (ask|consult|fyi|proposal).
# Claim on this must REJECT (kind_enum rule fires post-mutation).
_INVALID_KIND_MEMO = """\
---
kind: ack
status: open
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""

# Delivery mode central-only without 'to:' field.
# Claim on this must REJECT (central_only_requires_to rule fires post-mutation).
_CENTRAL_ONLY_MISSING_TO_MEMO = """\
---
kind: fyi
delivery_mode: central-only
status: open
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""

# Pre-cutoff memo (created < 2026-05-22) with overcap summary.
# Claim on this must SUCCEED — grandfather __skip__ short-circuits all cross-field rules.
_PRE_CUTOFF_OVERCAP_MEMO = (
    "---\n"
    "kind: fyi\n"
    "status: open\n"
    "from: sender-session\n"
    f'summary: "{_OVERCAP_SUMMARY}"\n'
    "created: 2026-05-01\n"
    "---\n"
)


# ---------------------------------------------------------------------------
# Git repo helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    """Initialise a minimal git repo at path (silent, no initial commit needed)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        capture_output=True, check=True,
        creationflags=_CFLAGS,
    )
    for cfg_args in (
        ["user.email", "parity@test.local"],
        ["user.name", "Parity Test"],
        ["commit.gpgsign", "false"],
    ):
        subprocess.run(
            ["git", "config"] + cfg_args,
            cwd=str(path), capture_output=True, check=True,
            creationflags=_CFLAGS,
        )


# ---------------------------------------------------------------------------
# Async runner helper
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio dependency."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# JS CLI runner helper — CAPTURE_GOLDENS=1 recapture path ONLY.  Every call site is
# gated behind `is_capturing()` (see `_capture_or_load_case` /
# `_capture_or_load_rejection_case` below) — never invoked on an ordinary run.
# ---------------------------------------------------------------------------


def _js_memo(
    verb: str,
    memo_path: Path,
    *,
    session_id: Optional[str] = None,
    at: Optional[str] = None,
    decision: Optional[str] = None,
    decision_note: Optional[str] = None,
    realized_by: Optional[str] = None,
    actioned_note: Optional[str] = None,
    distill_fate: Optional[str] = None,
    in_repo_capture: Optional[str] = None,
) -> Tuple[int, str, str]:
    """Run memo-transition.js <verb> and return (exit_code, stdout, stderr)."""
    cmd = [str(_NODE), str(_MEMO_JS), verb, "--memo", str(memo_path)]
    if session_id is not None:
        cmd += ["--session-id", session_id]
    if at is not None:
        cmd += ["--at", at]
    if decision is not None:
        cmd += ["--decision", decision]
    if decision_note is not None:
        cmd += ["--decision-note", decision_note]
    if realized_by is not None:
        cmd += ["--realized-by", realized_by]
    if actioned_note is not None:
        cmd += ["--actioned-note", actioned_note]
    if distill_fate is not None:
        cmd += ["--distill-fate", distill_fate]
    if in_repo_capture is not None:
        cmd += ["--in-repo-capture", in_repo_capture]
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        creationflags=_CFLAGS,
    )
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# Golden capture/load helpers
#
# No `tmp_path` leakage hazard here (unlike test_dag_js_parity.py / test_records_query
# _parity.py): every memo fixture is a static string constant, and no field written by
# claim/action/release ever embeds an absolute tmp_path — realized_by/in_repo_capture
# values used in these tests are all static relative-looking strings. So the captured
# {"rc", "content"} JSON needs no normalization step before freezing/comparing.
# ---------------------------------------------------------------------------


def _capture_or_load_case(
    case: str,
    verb: str,
    js_file: Path,
    **kwargs,
) -> Tuple[int, str]:
    """Return (rc, resulting_file_content) for one memo-transition.js verb invocation.

    Under CAPTURE_GOLDENS=1, runs the live oracle against js_file (already seeded with
    the test's starting fixture) and freezes {"rc": ..., "content": ...} as a committed
    golden. Otherwise (ordinary run — no node needed), loads the committed golden. See
    coordinator_core/testing/golden.py module docstring for the CAPTURE_GOLDENS contract.
    """
    if is_capturing():
        _require_oracle()
        js_rc, _out, js_err = _js_memo(verb, js_file, **kwargs)
        content = js_file.read_text(encoding="utf-8")
        assert_matches_golden(
            json.dumps({"rc": js_rc, "content": content}),
            _GOLDEN_NAMESPACE, case, kind="json",
        )
        return js_rc, content
    golden = load_golden(_GOLDEN_NAMESPACE, case, kind="json")
    return golden["rc"], golden["content"]


def _capture_or_load_rejection_case(
    case: str,
    verb: str,
    js_file: Path,
    js_original: str,
    **kwargs,
) -> int:
    """Return the oracle's rc for one rejection-arm fixture.

    Under CAPTURE_GOLDENS=1, runs the live oracle, verifies it rejected (rc != 0) and
    left js_file unchanged, then freezes {"rc": rc} as the committed golden. Otherwise
    (ordinary run — no node needed) loads the committed rc from the golden.
    """
    if is_capturing():
        _require_oracle()
        js_rc, _out, js_err = _js_memo(verb, js_file, **kwargs)
        assert js_rc != 0, f"JS must reject for {case} (rc={js_rc}):\n{js_err}"
        assert js_file.read_text(encoding="utf-8") == js_original, (
            f"JS: {case} rejection must leave file unchanged"
        )
        assert_matches_golden(json.dumps({"rc": js_rc}), _GOLDEN_NAMESPACE, case, kind="json")
        return js_rc
    return load_golden(_GOLDEN_NAMESPACE, case, kind="json")["rc"]


# ---------------------------------------------------------------------------
# Fixture-writing helper
# ---------------------------------------------------------------------------


def _write_memo(path: Path, content: str) -> None:
    """Write memo content to path (creating parent dirs), then commit it to HEAD.

    Every fixture memo is written into a repo built by `_init_git_repo` and is
    tracked here immediately, mirroring `coordinator/bin/cross-repo-memo`'s
    `_commit_delivered_memo`: production commits a delivered memo before any
    transition verb ever runs against it, so an untracked/no-HEAD memo is not a
    state a verb op observes in practice. `git_native.commit_authored_content`
    (the entrypoint the Python op uses to commit its own writes) deliberately
    refuses a path absent from HEAD — it exists for in-place mutation of an
    already-tracked file, not to first-commit one — so leaving the fixture
    untracked was a fixture bug, not a production gap.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "test",
           "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "test",
           "GIT_COMMITTER_EMAIL": "t@t"}
    repo = subprocess.run(
        ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True, creationflags=_CFLAGS,
    ).stdout.strip()
    rel = str(path.relative_to(Path(repo)))
    subprocess.run(
        ["git", "-C", repo, "add", "--", rel],
        check=True, capture_output=True, creationflags=_CFLAGS,
    )
    subprocess.run(
        ["git", "-C", repo, "commit", "-m", "deliver memo", "--", rel],
        check=True, capture_output=True, creationflags=_CFLAGS, env=env,
    )


# ---------------------------------------------------------------------------
# Tests — claim parity
# ---------------------------------------------------------------------------


class TestClaimParity:
    """memo-transition.js claim (frozen golden) vs Python _memo_handler claim."""

    def _setup_repos(self, tmp_path: Path) -> Tuple[Path, Path]:
        """Return (js_file, py_file) — both in separate git repos under cross-repo/inbox/."""
        js_repo = tmp_path / "js_repo"
        py_repo = tmp_path / "py_repo"
        _init_git_repo(js_repo)
        _init_git_repo(py_repo)
        js_file = js_repo / "cross-repo" / "inbox" / "memo.md"
        py_file = py_repo / "cross-repo" / "inbox" / "memo.md"
        return js_file, py_file

    def test_claim_basic_fixture(self, tmp_path):
        """Byte-equal claim on a fresh open memo (no preamble)."""
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _FRESH_MEMO)
        _write_memo(py_file, _FRESH_MEMO)

        js_rc, js_content = _capture_or_load_case(
            "claim_basic_fixture", "claim", js_file,
            session_id=_PINNED_SESSION, at=_PINNED_AT,
        )
        assert js_rc == 0, f"JS claim failed (rc={js_rc})"

        py_result = _run(_memo_handler(
            {"verb": "claim", "memo": str(py_file), "session_id": _PINNED_SESSION, "at": _PINNED_AT},
            _CTX,
        ))
        assert py_result["exit_code"] == 0, (
            f"Python claim failed: {py_result.get('error')}"
        )
        assert py_result["applied"] is True

        py_content = py_file.read_text(encoding="utf-8")
        assert js_content == py_content, (
            "claim basic: byte-mismatch.\n"
            f"JS (golden):\n{js_content!r}\n\nPython:\n{py_content!r}"
        )

        # Field-level spot-checks on Python output.
        split = split_frontmatter(py_content)
        assert split is not None
        fm = split.fm_text
        assert read_fm_field(fm, "status") == "in_progress"
        assert _PINNED_AT in (read_fm_field(fm, "picked_up_at") or "")
        assert read_fm_field(fm, "picked_up_by") == _PINNED_SESSION

        # Review: code-reviewer (F4) — lock claim(FRESH_MEMO) → _IN_PROGRESS_MEMO byte-for-byte.
        # action/release idempotency tests use _IN_PROGRESS_MEMO as a pre-claimed starting
        # fixture; if claim() output ever diverges (e.g. field-ordering change in primitives),
        # those tests would silently exercise a non-claim-produced state.
        assert py_content == _IN_PROGRESS_MEMO, (
            "claim basic: Python output must match _IN_PROGRESS_MEMO byte-for-byte.\n"
            f"Got:\n{py_content!r}\n\nExpected:\n{_IN_PROGRESS_MEMO!r}"
        )

    def test_claim_idempotent_noop(self, tmp_path):
        """Claim on already-in_progress memo with same session → no-op, exit 0, file unchanged."""
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _IN_PROGRESS_MEMO)
        _write_memo(py_file, _IN_PROGRESS_MEMO)

        py_original = py_file.read_text(encoding="utf-8")

        js_rc, js_content = _capture_or_load_case(
            "claim_idempotent_noop", "claim", js_file,
            session_id=_PINNED_SESSION, at=_PINNED_AT,
        )
        assert js_rc == 0, f"JS claim idempotent failed (rc={js_rc})"
        assert js_content == _IN_PROGRESS_MEMO, (
            "JS (golden): idempotent claim must leave file unchanged"
        )

        py_result = _run(_memo_handler(
            {"verb": "claim", "memo": str(py_file), "session_id": _PINNED_SESSION, "at": _PINNED_AT},
            _CTX,
        ))
        assert py_result["exit_code"] == 0, (
            f"Python claim idempotent failed: {py_result.get('error')}"
        )
        assert py_result["applied"] is False, "idempotent claim must have applied=False"
        assert py_file.read_text(encoding="utf-8") == py_original, (
            "Python: idempotent claim must leave file unchanged"
        )

    def test_claim_collision(self, tmp_path):
        """Claim on already-in_progress memo held by different session → both reject."""
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _IN_PROGRESS_OTHER_MEMO)
        _write_memo(py_file, _IN_PROGRESS_OTHER_MEMO)

        py_original = py_file.read_text(encoding="utf-8")

        js_rc = _capture_or_load_rejection_case(
            "claim_collision", "claim", js_file, _IN_PROGRESS_OTHER_MEMO,
            session_id=_PINNED_SESSION, at=_PINNED_AT,
        )
        assert js_rc != 0, f"JS claim must reject collision (rc={js_rc})"

        py_result = _run(_memo_handler(
            {"verb": "claim", "memo": str(py_file), "session_id": _PINNED_SESSION, "at": _PINNED_AT},
            _CTX,
        ))
        assert py_result["exit_code"] != 0, (
            f"Python claim must reject collision; got {py_result!r}"
        )
        assert py_result["applied"] is False
        assert py_file.read_text(encoding="utf-8") == py_original, (
            "Python: collision rejection must leave file unchanged"
        )

        assert js_rc == py_result["exit_code"], (
            f"node-rc ({js_rc}) != native-exit_code ({py_result['exit_code']}) on collision"
        )

    def test_claim_preamble_preserved(self, tmp_path):
        """Claim preserves HTML-comment preamble byte-identically."""
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _PREAMBLE_MEMO)
        _write_memo(py_file, _PREAMBLE_MEMO)

        js_rc, js_content = _capture_or_load_case(
            "claim_preamble_preserved", "claim", js_file,
            session_id=_PINNED_SESSION, at=_PINNED_AT,
        )
        assert js_rc == 0, f"JS claim (preamble) failed (rc={js_rc})"

        py_result = _run(_memo_handler(
            {"verb": "claim", "memo": str(py_file), "session_id": _PINNED_SESSION, "at": _PINNED_AT},
            _CTX,
        ))
        assert py_result["exit_code"] == 0, (
            f"Python claim (preamble) failed: {py_result.get('error')}"
        )

        py_content = py_file.read_text(encoding="utf-8")
        assert js_content == py_content, (
            "claim preamble: byte-mismatch.\n"
            f"JS (golden):\n{js_content!r}\n\nPython:\n{py_content!r}"
        )
        assert "<!-- example_retrieval_repo_setup baton v2 -->" in py_content
        assert "<!-- generated: 2026-01-01 -->" in py_content


# ---------------------------------------------------------------------------
# Tests — action parity
# ---------------------------------------------------------------------------


class TestActionParity:
    """memo-transition.js action (frozen golden) vs Python _memo_handler action."""

    def _setup_repos(self, tmp_path: Path) -> Tuple[Path, Path]:
        js_repo = tmp_path / "js_repo"
        py_repo = tmp_path / "py_repo"
        _init_git_repo(js_repo)
        _init_git_repo(py_repo)
        js_file = js_repo / "cross-repo" / "inbox" / "memo.md"
        py_file = py_repo / "cross-repo" / "inbox" / "memo.md"
        return js_file, py_file

    def test_action_basic_fixture(self, tmp_path):
        """Byte-equal action (decision=accepted+realized_by) on in_progress memo."""
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _IN_PROGRESS_MEMO)
        _write_memo(py_file, _IN_PROGRESS_MEMO)

        js_rc, js_content = _capture_or_load_case(
            "action_basic_fixture", "action", js_file,
            decision="accepted", realized_by="docs/plans/test.md",
        )
        assert js_rc == 0, f"JS action failed (rc={js_rc})"

        py_result = _run(_memo_handler(
            {
                "verb": "action", "memo": str(py_file),
                "decision": "accepted", "realized_by": "docs/plans/test.md",
            },
            _CTX,
        ))
        assert py_result["exit_code"] == 0, (
            f"Python action failed: {py_result.get('error')}"
        )
        assert py_result["applied"] is True

        py_content = py_file.read_text(encoding="utf-8")
        assert js_content == py_content, (
            "action basic: byte-mismatch.\n"
            f"JS (golden):\n{js_content!r}\n\nPython:\n{py_content!r}"
        )

        split = split_frontmatter(py_content)
        assert split is not None
        fm = split.fm_text
        assert read_fm_field(fm, "status") == "actioned"
        assert read_fm_field(fm, "decision") == "accepted"
        assert read_fm_field(fm, "realized_by") == "docs/plans/test.md"

    def test_action_idempotent_noop(self, tmp_path):
        """Action on already-actioned memo with same disposition → no-op, exit 0."""
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _ACTIONED_MEMO)
        _write_memo(py_file, _ACTIONED_MEMO)

        py_original = py_file.read_text(encoding="utf-8")

        js_rc, js_content = _capture_or_load_case(
            "action_idempotent_noop", "action", js_file,
            decision="accepted", realized_by="docs/plans/test.md",
        )
        assert js_rc == 0, f"JS action idempotent failed (rc={js_rc})"
        assert js_content == _ACTIONED_MEMO

        py_result = _run(_memo_handler(
            {
                "verb": "action", "memo": str(py_file),
                "decision": "accepted", "realized_by": "docs/plans/test.md",
            },
            _CTX,
        ))
        assert py_result["exit_code"] == 0, (
            f"Python action idempotent failed: {py_result.get('error')}"
        )
        assert py_result["applied"] is False
        assert py_file.read_text(encoding="utf-8") == py_original

    def test_action_note_mode_idempotent_noop(self, tmp_path):
        """Action (note mode) re-actioned with same actioned_note → no-op, exit 0, file unchanged.

        Review: code-reviewer (F3) — covers the note-mode idempotency branch
        (_action's _unquote_yaml_scalar comparison on actioned_note, line ~396-397).
        The decision-mode idempotency is covered by test_action_idempotent_noop.
        A byte-divergence between JS and Python in note-mode idempotency detection
        would go undetected without this fixture.
        """
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _ACTIONED_NOTE_MEMO)
        _write_memo(py_file, _ACTIONED_NOTE_MEMO)

        py_original = py_file.read_text(encoding="utf-8")

        js_rc, js_content = _capture_or_load_case(
            "action_note_mode_idempotent_noop", "action", js_file,
            actioned_note=_ACTIONED_NOTE_TEXT,
        )
        assert js_rc == 0, f"JS action note-mode idempotent failed (rc={js_rc})"
        assert js_content == _ACTIONED_NOTE_MEMO, (
            "JS (golden): note-mode idempotent action must leave file unchanged"
        )

        py_result = _run(_memo_handler(
            {
                "verb": "action", "memo": str(py_file),
                "actioned_note": _ACTIONED_NOTE_TEXT,
            },
            _CTX,
        ))
        assert py_result["exit_code"] == 0, (
            f"Python action note-mode idempotent failed: {py_result.get('error')}"
        )
        assert py_result["applied"] is False, "idempotent note-mode action must have applied=False"
        assert py_file.read_text(encoding="utf-8") == py_original, (
            "Python: note-mode idempotent action must leave file unchanged"
        )

        assert js_rc == py_result["exit_code"], (
            f"node-rc ({js_rc}) != native-exit_code ({py_result['exit_code']}) on note-mode idempotent"
        )

    def test_action_collision(self, tmp_path):
        """Action on already-actioned memo with DIFFERENT disposition → both reject."""
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _ACTIONED_MEMO)
        _write_memo(py_file, _ACTIONED_MEMO)

        py_original = py_file.read_text(encoding="utf-8")

        # Use a different disposition than what's in the fixture (declined vs accepted).
        js_rc = _capture_or_load_rejection_case(
            "action_collision", "action", js_file, _ACTIONED_MEMO,
            decision="declined",
        )
        assert js_rc != 0, "JS action must reject re-action with different disposition"

        py_result = _run(_memo_handler(
            {"verb": "action", "memo": str(py_file), "decision": "declined"},
            _CTX,
        ))
        assert py_result["exit_code"] != 0, (
            f"Python action must reject re-action; got {py_result!r}"
        )
        assert py_result["applied"] is False
        assert py_file.read_text(encoding="utf-8") == py_original

        assert js_rc == py_result["exit_code"], (
            f"node-rc ({js_rc}) != native-exit_code ({py_result['exit_code']}) on re-action collision"
        )

    def test_action_preamble_preserved(self, tmp_path):
        """Action preserves HTML-comment preamble byte-identically."""
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _IN_PROGRESS_PREAMBLE_MEMO)
        _write_memo(py_file, _IN_PROGRESS_PREAMBLE_MEMO)

        js_rc, js_content = _capture_or_load_case(
            "action_preamble_preserved", "action", js_file,
            decision="accepted", realized_by="docs/plans/test.md",
        )
        assert js_rc == 0, f"JS action (preamble) failed (rc={js_rc})"

        py_result = _run(_memo_handler(
            {
                "verb": "action", "memo": str(py_file),
                "decision": "accepted", "realized_by": "docs/plans/test.md",
            },
            _CTX,
        ))
        assert py_result["exit_code"] == 0, (
            f"Python action (preamble) failed: {py_result.get('error')}"
        )

        py_content = py_file.read_text(encoding="utf-8")
        assert js_content == py_content, (
            "action preamble: byte-mismatch.\n"
            f"JS (golden):\n{js_content!r}\n\nPython:\n{py_content!r}"
        )
        assert "<!-- example_retrieval_repo_setup baton v2 -->" in py_content

    def test_action_all_digit_realized_by_quoted(self, tmp_path):
        """realized_by='12345678' (all digits, valid hex SHA) is QUOTED in both paths.

        Locks JS↔Python byte-parity: node's serializeYamlScalar (schema.js) quotes an
        all-numeric string via its `/^[0-9]+$/` branch — a deliberate SHA-as-int guard
        (an unquoted all-digit / `[eE]` value would parse as an integer / scientific
        notation and corrupt the SHA). Python must match by writing realized_by with
        numeric_quoting=True, so both paths emit `realized_by: '12345678'`.
        """
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _IN_PROGRESS_MEMO)
        _write_memo(py_file, _IN_PROGRESS_MEMO)

        all_digit_sha = "12345678"  # 8 hex chars — valid SHA-like realized_by

        js_rc, js_content = _capture_or_load_case(
            "action_all_digit_realized_by_quoted", "action", js_file,
            decision="accepted", realized_by=all_digit_sha,
        )
        assert js_rc == 0, f"JS action (all-digit realized_by) failed (rc={js_rc})"

        py_result = _run(_memo_handler(
            {
                "verb": "action", "memo": str(py_file),
                "decision": "accepted", "realized_by": all_digit_sha,
            },
            _CTX,
        ))
        assert py_result["exit_code"] == 0, (
            f"Python action (all-digit realized_by) failed: {py_result.get('error')}"
        )

        py_content = py_file.read_text(encoding="utf-8")
        assert js_content == py_content, (
            "action all-digit realized_by: byte-mismatch.\n"
            f"JS (golden):\n{js_content!r}\n\nPython:\n{py_content!r}"
        )
        # Assert quoted — JS's serializeYamlScalar quotes all-digit values (SHA-as-int
        # guard); Python matches via numeric_quoting=True.
        assert f"realized_by: '{all_digit_sha}'" in py_content, (
            f"realized_by must be quoted (JS serializeYamlScalar SHA-as-int guard); "
            f"got:\n{py_content!r}"
        )

    def test_action_all_digit_decision_note_quoted(self, tmp_path):
        """decision_note='90210' (all digits, plausible free-text numeric note) is QUOTED
        in both paths.

        Locks JS↔Python byte-parity for the field written via `insert_fm_field`/
        `replace_fm_field` at the decision_note call site: node's serializeYamlScalar
        (schema.js) quotes an all-numeric string unconditionally on every field it
        serializes — there is no separate opt-in flag. Python must match by writing
        decision_note with numeric_quoting=True, so both paths emit
        `decision_note: '90210'`.
        """
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _IN_PROGRESS_MEMO)
        _write_memo(py_file, _IN_PROGRESS_MEMO)

        all_digit_note = "90210"  # plausible free-text numeric decision_note

        js_rc, js_content = _capture_or_load_case(
            "action_all_digit_decision_note_quoted", "action", js_file,
            decision="declined", decision_note=all_digit_note,
        )
        assert js_rc == 0, f"JS action (all-digit decision_note) failed (rc={js_rc})"

        py_result = _run(_memo_handler(
            {
                "verb": "action", "memo": str(py_file),
                "decision": "declined", "decision_note": all_digit_note,
            },
            _CTX,
        ))
        assert py_result["exit_code"] == 0, (
            f"Python action (all-digit decision_note) failed: {py_result.get('error')}"
        )

        py_content = py_file.read_text(encoding="utf-8")
        assert js_content == py_content, (
            "action all-digit decision_note: byte-mismatch.\n"
            f"JS (golden):\n{js_content!r}\n\nPython:\n{py_content!r}"
        )
        # Assert quoted — JS's serializeYamlScalar quotes all-digit values unconditionally;
        # Python matches via numeric_quoting=True.
        assert f"decision_note: '{all_digit_note}'" in py_content, (
            f"decision_note must be quoted (JS serializeYamlScalar all-digit guard); "
            f"got:\n{py_content!r}"
        )

    def test_action_all_digit_actioned_note_quoted(self, tmp_path):
        """actioned_note='42' (all digits, plausible free-text numeric note, note-mode
        action) is QUOTED in both paths.

        Locks JS↔Python byte-parity for the field written via `insert_fm_field`/
        `replace_fm_field` at the actioned_note call site (consult/fyi shape, no
        decision): node's serializeYamlScalar (schema.js) quotes an all-numeric string
        unconditionally on every field it serializes — there is no separate opt-in flag.
        Python must match by writing actioned_note with numeric_quoting=True, so both
        paths emit `actioned_note: '42'`.
        """
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _IN_PROGRESS_MEMO)
        _write_memo(py_file, _IN_PROGRESS_MEMO)

        all_digit_note = "42"  # plausible free-text numeric actioned_note

        js_rc, js_content = _capture_or_load_case(
            "action_all_digit_actioned_note_quoted", "action", js_file,
            actioned_note=all_digit_note,
        )
        assert js_rc == 0, f"JS action (all-digit actioned_note) failed (rc={js_rc})"

        py_result = _run(_memo_handler(
            {
                "verb": "action", "memo": str(py_file),
                "actioned_note": all_digit_note,
            },
            _CTX,
        ))
        assert py_result["exit_code"] == 0, (
            f"Python action (all-digit actioned_note) failed: {py_result.get('error')}"
        )

        py_content = py_file.read_text(encoding="utf-8")
        assert js_content == py_content, (
            "action all-digit actioned_note: byte-mismatch.\n"
            f"JS (golden):\n{js_content!r}\n\nPython:\n{py_content!r}"
        )
        # Assert quoted — JS's serializeYamlScalar quotes all-digit values unconditionally;
        # Python matches via numeric_quoting=True.
        assert f"actioned_note: '{all_digit_note}'" in py_content, (
            f"actioned_note must be quoted (JS serializeYamlScalar all-digit guard); "
            f"got:\n{py_content!r}"
        )


# ---------------------------------------------------------------------------
# Tests — distill_fate / in_repo_capture parity (Finding #11, C3)
#
# Closes a scope-drop found while porting memo-transition.js to native Python
# (BIG_PORT item memo-transition, Wave B): the pre-existing native port predated
# the JS oracle's distill_fate/in_repo_capture stamp-at-source fields and silently
# dropped them on write, even though the example-doctrine-repo-side strangler facade
# (coordinator/lib/memo-transition-facade.sh) already forwarded them.
# Spec backlink: docs/plans/2026-07-12-distill-rebuild-claude-klabauter-reliant.md § C3
# ---------------------------------------------------------------------------


class TestActionDistillFateParity:
    """memo-transition.js action --distill-fate/--in-repo-capture (frozen golden) vs
    Python _memo_handler."""

    def _setup_repos(self, tmp_path: Path) -> Tuple[Path, Path]:
        js_repo = tmp_path / "js_repo"
        py_repo = tmp_path / "py_repo"
        _init_git_repo(js_repo)
        _init_git_repo(py_repo)
        js_file = js_repo / "cross-repo" / "inbox" / "memo.md"
        py_file = py_repo / "cross-repo" / "inbox" / "memo.md"
        return js_file, py_file

    def test_action_decision_shape_with_ratification_byte_equal(self, tmp_path):
        """decision=accepted + realized_by + distill_fate=ratification + in_repo_capture."""
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _IN_PROGRESS_MEMO)
        _write_memo(py_file, _IN_PROGRESS_MEMO)

        js_rc, js_content = _capture_or_load_case(
            "action_decision_shape_with_ratification_byte_equal", "action", js_file,
            decision="accepted", realized_by="inline",
            distill_fate="ratification", in_repo_capture="docs/decisions/DR-999-fixture.md",
        )
        assert js_rc == 0, f"JS action (distill_fate) failed (rc={js_rc})"

        py_result = _run(_memo_handler(
            {
                "verb": "action", "memo": str(py_file),
                "decision": "accepted", "realized_by": "inline",
                "distill_fate": "ratification",
                "in_repo_capture": "docs/decisions/DR-999-fixture.md",
            },
            _CTX,
        ))
        assert py_result["exit_code"] == 0, (
            f"Python action (distill_fate) failed: {py_result.get('error')}"
        )
        assert py_result["applied"] is True

        py_content = py_file.read_text(encoding="utf-8")
        assert js_content == py_content, (
            "action distill_fate/in_repo_capture: byte-mismatch.\n"
            f"JS (golden):\n{js_content!r}\n\nPython:\n{py_content!r}"
        )
        assert "distill_fate: ratification" in py_content
        assert "in_repo_capture: docs/decisions/DR-999-fixture.md" in py_content

    def test_action_note_shape_with_ephemeral_byte_equal(self, tmp_path):
        """Consult/fyi shape (actioned_note only) + distill_fate=ephemeral (no in_repo_capture requirement)."""
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _IN_PROGRESS_MEMO)
        _write_memo(py_file, _IN_PROGRESS_MEMO)

        js_rc, js_content = _capture_or_load_case(
            "action_note_shape_with_ephemeral_byte_equal", "action", js_file,
            actioned_note="acknowledged", distill_fate="ephemeral",
        )
        assert js_rc == 0, f"JS action (ephemeral) failed (rc={js_rc})"

        py_result = _run(_memo_handler(
            {
                "verb": "action", "memo": str(py_file),
                "actioned_note": "acknowledged", "distill_fate": "ephemeral",
            },
            _CTX,
        ))
        assert py_result["exit_code"] == 0, (
            f"Python action (ephemeral) failed: {py_result.get('error')}"
        )

        py_content = py_file.read_text(encoding="utf-8")
        assert js_content == py_content, (
            "action ephemeral note-shape: byte-mismatch.\n"
            f"JS (golden):\n{js_content!r}\n\nPython:\n{py_content!r}"
        )

    def test_action_ratification_without_in_repo_capture_both_reject(self, tmp_path):
        """distill_fate=ratification without in_repo_capture → both JS and Python reject
        (cross-field rule, port of schema.js:2246-2254 / _memo_cf_distill_fate)."""
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _IN_PROGRESS_MEMO)
        _write_memo(py_file, _IN_PROGRESS_MEMO)
        py_original = py_file.read_text(encoding="utf-8")

        js_rc = _capture_or_load_rejection_case(
            "action_ratification_without_in_repo_capture_both_reject", "action", js_file,
            _IN_PROGRESS_MEMO,
            decision="declined", distill_fate="ratification",
        )
        assert js_rc != 0, "JS must reject ratification without in_repo_capture"

        py_result = _run(_memo_handler(
            {
                "verb": "action", "memo": str(py_file),
                "decision": "declined", "distill_fate": "ratification",
            },
            _CTX,
        ))
        assert py_result["exit_code"] != 0, (
            f"Python must reject ratification without in_repo_capture; got {py_result!r}"
        )
        assert py_result["applied"] is False
        assert py_file.read_text(encoding="utf-8") == py_original

        assert js_rc == py_result["exit_code"], (
            f"node-rc ({js_rc}) != native-exit_code ({py_result['exit_code']}) on ratification-missing-capture"
        )

    def test_action_ratification_with_claude_pointer_both_reject(self, tmp_path):
        """Finding #12: a ~/.claude memory pointer always fails the ratification rule,
        on both the JS oracle and the native Python port."""
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _IN_PROGRESS_MEMO)
        _write_memo(py_file, _IN_PROGRESS_MEMO)
        py_original = py_file.read_text(encoding="utf-8")

        js_rc = _capture_or_load_rejection_case(
            "action_ratification_with_claude_pointer_both_reject", "action", js_file,
            _IN_PROGRESS_MEMO,
            decision="declined",
            distill_fate="ratification", in_repo_capture="~/.claude/state/lessons/foo.yaml",
        )
        assert js_rc != 0, "JS must reject a ~/.claude in_repo_capture pointer"

        py_result = _run(_memo_handler(
            {
                "verb": "action", "memo": str(py_file),
                "decision": "declined", "distill_fate": "ratification",
                "in_repo_capture": "~/.claude/state/lessons/foo.yaml",
            },
            _CTX,
        ))
        assert py_result["exit_code"] != 0, (
            f"Python must reject a ~/.claude in_repo_capture pointer; got {py_result!r}"
        )
        assert py_result["applied"] is False
        assert py_file.read_text(encoding="utf-8") == py_original

        assert js_rc == py_result["exit_code"], (
            f"node-rc ({js_rc}) != native-exit_code ({py_result['exit_code']}) on claude-pointer rejection"
        )


# ---------------------------------------------------------------------------
# Tests — release parity
# ---------------------------------------------------------------------------


class TestReleaseParity:
    """memo-transition.js release (frozen golden) vs Python _memo_handler release."""

    def _setup_repos(self, tmp_path: Path) -> Tuple[Path, Path]:
        js_repo = tmp_path / "js_repo"
        py_repo = tmp_path / "py_repo"
        _init_git_repo(js_repo)
        _init_git_repo(py_repo)
        js_file = js_repo / "cross-repo" / "inbox" / "memo.md"
        py_file = py_repo / "cross-repo" / "inbox" / "memo.md"
        return js_file, py_file

    def test_release_basic_fixture(self, tmp_path):
        """Byte-equal release on an in_progress memo (removes picked_up fields)."""
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _IN_PROGRESS_MEMO)
        _write_memo(py_file, _IN_PROGRESS_MEMO)

        js_rc, js_content = _capture_or_load_case("release_basic_fixture", "release", js_file)
        assert js_rc == 0, f"JS release failed (rc={js_rc})"

        py_result = _run(_memo_handler(
            {"verb": "release", "memo": str(py_file)},
            _CTX,
        ))
        assert py_result["exit_code"] == 0, (
            f"Python release failed: {py_result.get('error')}"
        )
        assert py_result["applied"] is True

        py_content = py_file.read_text(encoding="utf-8")
        assert js_content == py_content, (
            "release basic: byte-mismatch.\n"
            f"JS (golden):\n{js_content!r}\n\nPython:\n{py_content!r}"
        )

        split = split_frontmatter(py_content)
        assert split is not None
        fm = split.fm_text
        assert read_fm_field(fm, "status") == "open"
        assert read_fm_field(fm, "picked_up_by") is None, "release must remove picked_up_by"
        assert read_fm_field(fm, "picked_up_at") is None, "release must remove picked_up_at"

    def test_release_idempotent_noop(self, tmp_path):
        """Release on already-open memo → no-op, exit 0, file unchanged."""
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _FRESH_MEMO)
        _write_memo(py_file, _FRESH_MEMO)

        py_original = py_file.read_text(encoding="utf-8")

        js_rc, js_content = _capture_or_load_case("release_idempotent_noop", "release", js_file)
        assert js_rc == 0, f"JS release idempotent failed (rc={js_rc})"
        assert js_content == _FRESH_MEMO

        py_result = _run(_memo_handler(
            {"verb": "release", "memo": str(py_file)},
            _CTX,
        ))
        assert py_result["exit_code"] == 0, (
            f"Python release idempotent failed: {py_result.get('error')}"
        )
        assert py_result["applied"] is False
        assert py_file.read_text(encoding="utf-8") == py_original

    def test_release_collision(self, tmp_path):
        """Release on a memo NOT in in_progress (e.g., actioned) → both reject."""
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _ACTIONED_MEMO)
        _write_memo(py_file, _ACTIONED_MEMO)

        py_original = py_file.read_text(encoding="utf-8")

        js_rc = _capture_or_load_rejection_case(
            "release_collision", "release", js_file, _ACTIONED_MEMO,
        )
        assert js_rc != 0, "JS release must reject on actioned memo"

        py_result = _run(_memo_handler(
            {"verb": "release", "memo": str(py_file)},
            _CTX,
        ))
        assert py_result["exit_code"] != 0, (
            f"Python release must reject on actioned memo; got {py_result!r}"
        )
        assert py_result["applied"] is False
        assert py_file.read_text(encoding="utf-8") == py_original

        assert js_rc == py_result["exit_code"], (
            f"node-rc ({js_rc}) != native-exit_code ({py_result['exit_code']}) on release collision"
        )

    def test_release_preamble_preserved(self, tmp_path):
        """Release preserves HTML-comment preamble byte-identically."""
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _IN_PROGRESS_PREAMBLE_MEMO)
        _write_memo(py_file, _IN_PROGRESS_PREAMBLE_MEMO)

        js_rc, js_content = _capture_or_load_case("release_preamble_preserved", "release", js_file)
        assert js_rc == 0, f"JS release (preamble) failed (rc={js_rc})"

        py_result = _run(_memo_handler(
            {"verb": "release", "memo": str(py_file)},
            _CTX,
        ))
        assert py_result["exit_code"] == 0, (
            f"Python release (preamble) failed: {py_result.get('error')}"
        )

        py_content = py_file.read_text(encoding="utf-8")
        assert js_content == py_content, (
            "release preamble: byte-mismatch.\n"
            f"JS (golden):\n{js_content!r}\n\nPython:\n{py_content!r}"
        )
        assert "<!-- example_retrieval_repo_setup baton v2 -->" in py_content


# ---------------------------------------------------------------------------
# Tests — validation-fail rejection parity
#
# Each fixture exercises one reachable cross-field rule or validation gate.
# Pattern: source the oracle's rc from the frozen golden (loaded, never invoked, on
# an ordinary run); run native on py_file; assert:
#   (a) node-rc (golden) == native-exit_code  (identical rejection outcome)
#   (b) py_file unchanged                     (no partial write on rejection)
#
# Reachable rules tested here:
#   - summary_length_cap (>120 chars): NOT a rejection case on the receive path — see
#     test_receive_overcap_summary_truncated_and_warned for the ratified absorb-and-warn
#     behaviour (2026-07-22 supersedes 2026-07-13's reject-canon on receive only; sender-side
#     compose/send still rejects/caps at source).
#   - kind_enum (invalid kind=ack): via claim on open memo with kind=ack
#   - central_only_requires_to: via claim on open memo with delivery_mode=central-only, no to:
#   - actioned+accepted missing realized_by: via action --decision accepted (early pre-read check)
#   - actioned+accepted malformed realized_by: via action --decision accepted + bad realized_by
#     (cross-field rule fires post-mutation: _memo_cf_actioned_decision_requires_realized_by)
#   - in_progress collision (claim on in_progress without picked_up_by): demonstrates
#     the collision-rejection parity for in_progress state; see module docstring for why
#     _memo_cf_in_progress_needs_picked_up_by is unreachable as a post-mutation reject.
#   - grandfather SKIP: pre-cutoff memo with overcap summary → both ACCEPT (exit 0)
# ---------------------------------------------------------------------------


class TestValidationRejectionParity:
    """Cross-field rule rejection parity — node-rc (frozen golden) == native-exit_code
    AND file unchanged."""

    def _setup_repos(self, tmp_path: Path) -> Tuple[Path, Path]:
        js_repo = tmp_path / "js_repo"
        py_repo = tmp_path / "py_repo"
        _init_git_repo(js_repo)
        _init_git_repo(py_repo)
        js_file = js_repo / "cross-repo" / "inbox" / "memo.md"
        py_file = py_repo / "cross-repo" / "inbox" / "memo.md"
        return js_file, py_file

    def _assert_rejection_parity(
        self,
        py_file: Path,
        js_rc: int, py_result: dict,
        py_original: str,
        label: str,
    ) -> None:
        """Assert parity of rejection: same exit_code AND py_file unchanged."""
        assert js_rc != 0, (
            f"JS (golden) must reject for {label} (got rc={js_rc})"
        )
        assert py_result["exit_code"] != 0, (
            f"Python must reject for {label}; got {py_result!r}"
        )
        assert py_result["applied"] is False
        assert py_file.read_text(encoding="utf-8") == py_original, (
            f"Python: {label} rejection must leave file unchanged"
        )
        assert js_rc == py_result["exit_code"], (
            f"node-rc ({js_rc}) != native-exit_code ({py_result['exit_code']}) for {label}"
        )

    def test_receive_overcap_summary_truncated_and_warned(self, tmp_path, capsys):
        """Claim on memo with >120-char summary → claude-klabauter ABSORBS it, JS golden still rejects.

        This surface has ratcheted through three rulings; read all three before touching it
        again, since the second one already got mistaken for a bug by an agent that only saw
        the first:

        1. 2026-07-13 (`2914a382`, "claude-klabauter-canonical-memo-summary-reject"): claude-klabauter ratified
           canonical REJECT for the DR-210 x-repo memo cap — "fail-loud over silent truncation
           of a human-authored capped field; claude-klabauter is the DR-210 owner of the x-repo memo
           system." At that point Python and the (then-current) JS oracle both rejected, so the
           parity assertion held both ways.
        2. 2026-07-22 (`ba858ae6`, actioning claude-central-em's cross-repo Ask 2,
           `cross-repo/inbox/2026-07-22-claude-central-em-two-asks-installer-seed-and-memo-stamp-
           normalization.md`): claude-klabauter deliberately SUPERSEDED rule (1) on the receive path only.
           An over-cap `summary:` is cosmetic; hard-failing it strands an otherwise-good inbound
           memo at the receiver — and unlike the sender, the receiver cannot fix the sender's
           file without hand-editing another repo's memo. `_normalize_oversize_summary`
           (`coordinator_core/ops/memo_transition.py`) now truncates-and-WARNS ahead of the
           validation gate in claim/action/release; the gate itself
           (`schema_validate._memo_cf_summary_length_cap`) stays strict for every other path
           (sender-side compose/send still reject/cap at source, per DEC-1 in
           `coordinator_core/contract/cross-repo-memo.schema.json`).
        3. This split is why rule (1)'s "no silent truncation" is NOT violated by rule (2):
           (2) truncates loudly — it emits a stderr warning naming the memo path and the
           original length — so the "silent" half of (1)'s prohibition never applied to it.
           The two rulings target different failure shapes (author-fixable vs. not) and
           deliberately do not collide.

        The JS golden here is frozen from the pre-(2) oracle and stays rc!=0 forever — it is
        a retiring, test-only Node artifact (de-node cutover completed 2026-07-22) and was
        never authoritative over this receive-path ruling; claude-klabauter owns this surface per (1).
        So this test does NOT assert JS/Python parity — it asserts Python's ratified,
        permanent behaviour directly. If you are reading this because the test looks "wrong"
        against the JS golden: it is supposed to diverge. Only rules (1)-(3) above can change
        that, not a golden recapture.

        Memo cap is 120 chars (distinct from handoff cap of 140).
        """
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _OVERCAP_MEMO)
        _write_memo(py_file, _OVERCAP_MEMO)

        js_rc = _capture_or_load_rejection_case(
            "reject_overcap_summary", "claim", js_file, _OVERCAP_MEMO,
            session_id=_PINNED_SESSION, at=_PINNED_AT,
        )
        assert js_rc != 0, (
            f"JS golden (frozen, pre-Ask-2 oracle) must still reject; got rc={js_rc}"
        )

        py_result = _run(_memo_handler(
            {"verb": "claim", "memo": str(py_file), "session_id": _PINNED_SESSION, "at": _PINNED_AT},
            _CTX,
        ))

        assert py_result["exit_code"] == 0, (
            f"Python must ABSORB (truncate-and-warn), not reject, an over-cap summary on "
            f"receive (ruling 2, 2026-07-22); got {py_result!r}"
        )
        assert py_result["applied"] is True

        py_split = split_frontmatter(py_file.read_text(encoding="utf-8"))
        new_summary = read_fm_field_unquoted(py_split.fm_text, "summary")
        assert len(new_summary) == _SUMMARY_MAX_CHARS, (
            f"on-disk summary must be truncated to the {_SUMMARY_MAX_CHARS}-char cap; "
            f"got length {len(new_summary)}"
        )
        assert new_summary.endswith("…"), "truncated summary must be ellipsis-terminated"
        assert _OVERCAP_SUMMARY.startswith(new_summary[:-1]), (
            "truncated summary must be a prefix of the original, not a rewrite"
        )

        stderr = capsys.readouterr().err
        assert str(py_file) in stderr, "warning must name the memo path"
        assert str(len(_OVERCAP_SUMMARY)) in stderr, "warning must name the original length"
        assert "truncat" in stderr, "warning must say what happened"

    def test_reject_invalid_kind(self, tmp_path):
        """Claim on memo with kind=ack → both reject (kind_enum rule).

        'ack' is NOT a valid kind — valid set is: ask|consult|fyi|proposal.
        Rule fires post-mutation in validateMemoFrontmatter.
        """
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _INVALID_KIND_MEMO)
        _write_memo(py_file, _INVALID_KIND_MEMO)
        py_original = py_file.read_text(encoding="utf-8")

        js_rc = _capture_or_load_rejection_case(
            "reject_invalid_kind", "claim", js_file, _INVALID_KIND_MEMO,
            session_id=_PINNED_SESSION, at=_PINNED_AT,
        )
        py_result = _run(_memo_handler(
            {"verb": "claim", "memo": str(py_file), "session_id": _PINNED_SESSION, "at": _PINNED_AT},
            _CTX,
        ))

        self._assert_rejection_parity(
            py_file, js_rc, py_result, py_original,
            label="invalid kind=ack",
        )

    def test_reject_central_only_missing_to(self, tmp_path):
        """Claim on delivery_mode=central-only memo without 'to:' → both reject.

        Rule fires post-mutation in validateMemoFrontmatter (_memo_cf_central_only_requires_to).
        """
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _CENTRAL_ONLY_MISSING_TO_MEMO)
        _write_memo(py_file, _CENTRAL_ONLY_MISSING_TO_MEMO)
        py_original = py_file.read_text(encoding="utf-8")

        js_rc = _capture_or_load_rejection_case(
            "reject_central_only_missing_to", "claim", js_file, _CENTRAL_ONLY_MISSING_TO_MEMO,
            session_id=_PINNED_SESSION, at=_PINNED_AT,
        )
        py_result = _run(_memo_handler(
            {"verb": "claim", "memo": str(py_file), "session_id": _PINNED_SESSION, "at": _PINNED_AT},
            _CTX,
        ))

        self._assert_rejection_parity(
            py_file, js_rc, py_result, py_original,
            label="delivery_mode=central-only missing to:",
        )

    def test_reject_in_progress_without_picked_up_by(self, tmp_path):
        """Claim on in_progress memo without picked_up_by → both reject (collision).

        _memo_cf_in_progress_needs_picked_up_by is unreachable as a post-mutation
        rejection (claim always writes picked_up_by; action/release exit in_progress).
        This test exercises the closest reachable failure mode: claim on an in_progress
        memo with no picked_up_by → collision detection fires (held by '(empty)'),
        both paths return exit_code=1, file unchanged.
        """
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _IN_PROGRESS_NO_PICKED_UP_BY_MEMO)
        _write_memo(py_file, _IN_PROGRESS_NO_PICKED_UP_BY_MEMO)
        py_original = py_file.read_text(encoding="utf-8")

        js_rc = _capture_or_load_rejection_case(
            "reject_in_progress_without_picked_up_by", "claim", js_file,
            _IN_PROGRESS_NO_PICKED_UP_BY_MEMO,
            session_id=_PINNED_SESSION, at=_PINNED_AT,
        )
        py_result = _run(_memo_handler(
            {"verb": "claim", "memo": str(py_file), "session_id": _PINNED_SESSION, "at": _PINNED_AT},
            _CTX,
        ))

        self._assert_rejection_parity(
            py_file, js_rc, py_result, py_original,
            label="in_progress without picked_up_by (collision)",
        )

    def test_reject_actioned_accepted_missing_realized_by(self, tmp_path):
        """action --decision accepted without --realized-by → both reject.

        Pre-read arg-level check (not the cross-field rule): both node and native
        fail before the file is read, exit_code=1, file unchanged.
        """
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _IN_PROGRESS_MEMO)
        _write_memo(py_file, _IN_PROGRESS_MEMO)
        py_original = py_file.read_text(encoding="utf-8")

        # Call node WITHOUT --realized-by (so node's early check fires).
        js_rc = _capture_or_load_rejection_case(
            "reject_actioned_accepted_missing_realized_by", "action", js_file, _IN_PROGRESS_MEMO,
            decision="accepted",
        )
        py_result = _run(_memo_handler(
            {"verb": "action", "memo": str(py_file), "decision": "accepted"},
            _CTX,
        ))

        self._assert_rejection_parity(
            py_file, js_rc, py_result, py_original,
            label="actioned+accepted missing realized_by",
        )

    def test_reject_actioned_accepted_malformed_realized_by(self, tmp_path):
        """action --decision accepted --realized-by badref → both reject.

        'badref' is not 'inline', contains no '/', and is not a valid hex SHA.
        The cross-field rule _memo_cf_actioned_decision_requires_realized_by fires
        post-mutation (after status→actioned, before write) → both reject, file unchanged.
        """
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _IN_PROGRESS_MEMO)
        _write_memo(py_file, _IN_PROGRESS_MEMO)
        py_original = py_file.read_text(encoding="utf-8")

        malformed_ref = "badref"  # not inline, no /, not hex — triggers malformed check
        js_rc = _capture_or_load_rejection_case(
            "reject_actioned_accepted_malformed_realized_by", "action", js_file, _IN_PROGRESS_MEMO,
            decision="accepted", realized_by=malformed_ref,
        )
        py_result = _run(_memo_handler(
            {"verb": "action", "memo": str(py_file), "decision": "accepted", "realized_by": malformed_ref},
            _CTX,
        ))

        self._assert_rejection_parity(
            py_file, js_rc, py_result, py_original,
            label=f"actioned+accepted malformed realized_by={malformed_ref!r}",
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Distinct from test_receive_overcap_summary_truncated_and_warned's ruling (that "
            "one is a settled, permanent divergence — this one is an open quoting-style bug): "
            "example-doctrine-repo's normalizeMemoSummary() re-serializes the grandfathered summary without YAML "
            "quotes, diverging from claude-klabauter's Python truncated-and-quoted serialization. Both "
            "sides truncate the grandfathered summary (normalizeMemoSummary runs unconditionally "
            "on both paths, ahead of the grandfather __skip__ check) — the mismatch is quoting "
            "style only, not accept-vs-reject. expected-fail vs the frozen golden (last-known-good "
            "live-JS-oracle capture) until example-doctrine-repo reconciles its quoting. XPASS means example-doctrine-repo reconciled "
            "— remove this marker. See memo 2026-07-13 claude-klabauter-canonical-memo-summary-reject."
        ),
    )
    def test_grandfather_skip_pre_cutoff(self, tmp_path):
        """Pre-cutoff memo (created < 2026-05-22) with overcap summary — both ACCEPT.

        Grandfather __skip__ rule short-circuits all cross-field validation for memos
        with created < 2026-05-22.  Even though summary > 120 chars would normally reject,
        the grandfather rule returns {__skip__: True} before summary_length_cap fires.
        Both node and native exit 0; file IS modified (claim applied).
        Byte-identical output verifies parity of the grandfather skip behaviour.
        """
        js_file, py_file = self._setup_repos(tmp_path)
        _write_memo(js_file, _PRE_CUTOFF_OVERCAP_MEMO)
        _write_memo(py_file, _PRE_CUTOFF_OVERCAP_MEMO)

        js_rc, js_content = _capture_or_load_case(
            "grandfather_skip_pre_cutoff", "claim", js_file,
            session_id=_PINNED_SESSION, at=_PINNED_AT,
        )
        assert js_rc == 0, (
            f"JS (golden) claim must ACCEPT pre-cutoff memo (grandfather skip); rc={js_rc}"
        )

        py_result = _run(_memo_handler(
            {"verb": "claim", "memo": str(py_file), "session_id": _PINNED_SESSION, "at": _PINNED_AT},
            _CTX,
        ))
        assert py_result["exit_code"] == 0, (
            f"Python claim must ACCEPT pre-cutoff memo (grandfather skip); got {py_result!r}"
        )
        assert py_result["applied"] is True

        assert js_rc == py_result["exit_code"], (
            f"node-rc ({js_rc}) != native-exit_code ({py_result['exit_code']}) for grandfather skip"
        )

        # Byte-identical output confirms both paths apply the same mutation.
        py_content = py_file.read_text(encoding="utf-8")
        assert js_content == py_content, (
            "grandfather skip: byte-mismatch (claim applied differently).\n"
            f"JS (golden):\n{js_content!r}\n\nPython:\n{py_content!r}"
        )

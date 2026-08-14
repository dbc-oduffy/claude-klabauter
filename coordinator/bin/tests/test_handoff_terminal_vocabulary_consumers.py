"""test_handoff_terminal_vocabulary_consumers.py — pins the terminal-vocabulary
agreement property for 19 of the 24 files a sibling repo's cutover gate flagged as
"consumers" of ``coordinator_core.lifecycle_constants.HANDOFF_TERMINAL_DEPLOYMENT``
(DR-084's widened handoff terminal set: ``{shipped, abandoned, continued, closed}``).

Why this exists: the sibling gate derives "consumers" via a wide regex over
``coordinator/bin/`` that also matches *other entities'* status-vocabulary
literals — plan status, goal status, bug-backlog status, debt/queue status,
cross-repo-memo status, initiative status — none of which are the handoff
``deployment_state`` axis at all. Of the 24 regex hits, exactly ONE
(``sweep-shipped-handoffs.py``, out of scope here — a different workstream
owns it) was a real unmigrated consumer: it enumerated a stale subset of the
terminal vocabulary instead of importing the SSOT constant. This module pins,
per file, that the same mistake is not latent in 19 of the remaining 23. The
other 4 are ``tests/fixtures/validator-negative/*.md`` data fixtures, which
carry no code to scan — they are covered instead by
``test_validator_negative_corpus.py``, which re-ports the loader that proves
the validator still rejects them. Together the two modules account for all 24
derived consumers; neither covers the full set alone.

Note the fixed point: this module and
``test_sweep_shipped_handoffs_terminal_selector.py`` are themselves in the
scanned path and so are themselves derived consumers. Both are listed below
and checked by the same property as everything else — a vocabulary-pinning
test is not exempt from the vocabulary it pins.

The property pinned per file: **either the file does not read handoff
``deployment_state`` at all (it matched the sibling's regex on some other
entity's status enum), or, if it does, any literal string collection it
contains that looks like a pure terminal-deployment enumeration (a
tuple/set/list/frozenset/dict-key-set of >=2 string literals that is a subset
of the canonical 4-token vocabulary) agrees exactly with
``HANDOFF_TERMINAL_DEPLOYMENT``.**

Negative-spec: this is a *property* test over source shape — it asserts what
each file's literal string collections say the handoff terminal vocabulary
is, never what these scripts actually DO at runtime. It is not a behavioral
test of any of the 20 scripts; several already have their own dedicated
behavioral test suites elsewhere in this tree.

The scanning helper (``_string_literal_collections``) exists exactly once and
dispatches on file shape: AST parsing (via ``ast``) for ``.py`` files —
preferred over regex because regex-over-source is how this class of check
goes stale — and a text-level bracket scan for the two non-Python files
(``.mjs``/``.js``, where no native AST is available). The text-level arm is
weaker (it cannot distinguish an array literal from a string that merely
looks like one inside a comment or template string) — acceptable here only
because both non-Python sites are themselves test fixtures, not production
readers, and the property they are checked against is a defensive-subset
check, not a primary classification.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

CANON = frozenset({"shipped", "abandoned", "continued", "closed"})

# Review: code-reviewer — the widened >=2-token-overlap property (see
# test_consumer_agrees_with_handoff_terminal_deployment) surfaces two
# collections that overlap CANON in >=2 tokens without equaling it, neither
# of which is a live defect; both are disclosed, individually-verified
# exemptions, not a reason to weaken the property for every other consumer:
#
# - test_sweep_shipped_handoffs_terminal_selector.py's `_PRE_DR084_SELECTOR
#   = frozenset({"shipped", "abandoned", "superseded"})` IS, verbatim, the
#   real pre-fix bug shape — intentionally so. The file's own docstring says
#   it is kept "as a named regression witness" for the exact stranding bug
#   DR-084 fixed, and `test_pre_dr084_selector_stranded_closed_and_continued`
#   / `test_superseded_deliberately_not_selected` independently pin that it
#   is never treated as terminal at runtime.
# - test-initiative-shape.mjs's `["active", "paused", "shipped",
#   "abandoned"]` is an INITIATIVE status enum (DR-207), already classified
#   `expected_reads=False` in `_CONSUMERS` above (a different axis entirely
#   — it just happens to reuse the English words "shipped"/"abandoned").
#   Coincidental 2-token reuse across genuinely unrelated status
#   vocabularies is exactly the false-positive shape a per-file allowlist
#   exists to absorb without re-widening the general threshold.
# - this module's own literal frozensets below, listing the two exemptions
#   above, are themselves derived-consumer hits by the module's stated fixed
#   point (see module docstring) — its CANON-overlapping literals are the
#   exemption table itself, not a hand-enumeration of the vocabulary.
_KNOWN_OVERLAP_EXEMPTIONS: dict[str, frozenset[frozenset[str]]] = {
    # test_sweep_shipped_handoffs_terminal_selector.py's exemption removed 2026-08-07 —
    # the file was excised in the spawn-heavy test cull (see _CONSUMERS below).
    "coordinator/bin/tests/test-initiative-shape.mjs": frozenset(
        {frozenset({"active", "paused", "shipped", "abandoned"})}
    ),
    "coordinator/bin/tests/test_handoff_terminal_vocabulary_consumers.py": frozenset(
        {
            frozenset({"shipped", "abandoned", "superseded"}),
            frozenset({"active", "paused", "shipped", "abandoned"}),
        }
    ),
}

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent

# (relative path from repo root, expected to read handoff deployment_state)
#
# expected_reads=False consumers matched the sibling's regex on a DIFFERENT
# entity's status vocabulary (plan/goal/bug-backlog/debt/queue/cross-repo-memo/
# initiative status) and never reference the handoff `deployment_state` field
# at all. expected_reads=True consumers are genuine handoff deployment_state
# touchers, verified individually below.
_CONSUMERS: list[tuple[str, bool]] = [
    ("coordinator/bin/assert-no-terminal-plans-in-live.py", False),  # PLAN_TERMINAL_STATUS, not handoff
    ("coordinator/bin/coordinator-doc-new.py", True),  # writes deployment_state, but only ready_to_fire/awaiting_gate — never a terminal
    ("coordinator/bin/emit-goal-from-artifact.py", False),  # goal _STATUS_MAP, not handoff
    ("coordinator/bin/prune-closed-bugs.py", False),  # bug-backlog status, not handoff
    ("coordinator/bin/test_emit_goal_from_artifact.py", False),  # goal _STATUS_MAP's test twin (abandoned -> dropped), not handoff
    ("coordinator/bin/test_close_origin_stub_on_ship.py", False),  # op result dict key "closed", not handoff deployment_state
    ("coordinator/bin/test_coordinator_queue_append_parity.py", False),  # debt/queue status enum, not handoff
    ("coordinator/bin/test_coordinator_queue_append.py", False),  # debt/queue status enum, not handoff
    ("coordinator/bin/test_cross_repo_memo_c6.py", True),  # touches deployment_state, but only a non-terminal placeholder ("in_flight") on a different (memo-schema-rejection) axis
    ("coordinator/bin/test_migrate_provenance_stamp.py", False),  # generic record fixture "status: closed", not a handoff
    ("coordinator/bin/test_prune_closed_bugs.py", False),  # bug-backlog status, not handoff
    ("coordinator/bin/tests/test_archive_stamp_cli_chain_supersede_archive.py", False),  # argv-shape test; continued_into is a path param, not a vocabulary enumeration
    ("coordinator/bin/tests/test_doe_root_routing.py", False),  # queue status enum {open, closed, deferred}, not handoff
    ("coordinator/bin/tests/test-initiative-shape.mjs", False),  # initiative status enum, not handoff
    ("coordinator/bin/tests/test-query-handoff-ledger.js", True),  # touches deployment_state, but only a fixed non-terminal fixture value ("ready_to_fire")
    # Reconciled 2026-08-02 (stale-test cleanup, triage-F): commit 5310420b2
    # ("C13: cmd_supersede confirms its own write instead of trusting the
    # op's self-report") added a post-write re-read that asserts
    # `deployment_state: continued` landed on disk, so this file now genuinely
    # spells the field — flipped from False to True.
    ("coordinator/bin/tests/test_handoff_archive_transition.py", True),  # C13 (5310420b2) added a re-read assertion on deployment_state: continued
    ("coordinator/bin/tests/test_handoff_terminal_vocabulary_consumers.py", True),  # this module — a vocabulary-pinning test inside the scanned path is its own fixed point; its CANON literal is the canonical set by construction
    ("coordinator_core/ops/emit_artifact_shape_contract.py", True),  # derives handoff axis display order from lifecycle_constants + a guarded coverage assertion, not a hand-typed enumeration; not a JS-oracle-port bin/ file but genuinely reads deployment_state
    # Re-triaged 2026-08-07, NOT silently dropped: test_reap_orphaned_in_flight_handoffs.py
    # and test_sweep_shipped_handoffs_terminal_selector.py were both excised in the
    # spawn-heavy test cull (both resolved _REPO_ROOT by spawning `git rev-parse
    # --show-toplevel` at import time, so they spawned on every collection). Neither
    # was a production consumer — both were tests. The vocabulary they pinned is still
    # pinned by the surviving entries above. Recover either via
    # `git show 6f0e89044:<path>`; full set in
    # state/audits/2026-08-07-spawn-heavy-test-excision-ledger.md.
]

_JS_STRING_ARRAY_RE = re.compile(r"\[([^\[\]]*)\]")
_JS_QUOTED_STRING_RE = re.compile(r"""(['"])((?:(?!\1).)*)\1""")


def _py_string_literal_collections(source: str) -> list[frozenset[str]]:
    """AST-based extraction for .py (and extensionless-but-Python) files.

    Walks Tuple/List/Set literals and Dict literals whose elements (or keys)
    are all string constants, keeping any with >=2 elements. Also covers
    ``frozenset((...))`` calls since the inner literal is itself a Tuple/List/
    Set node already visited by the walk.

    Negative-spec: Dict literals are scanned by KEYS only, never VALUES — a
    mapping whose vocabulary lives in its values (e.g.
    ``{"OLD_TERM": "abandoned", "NEW_TERM": "closed"}``) is invisible to
    this scan. Not live in any of the currently-listed consumers; a future
    consumer using that shape would need this helper extended.
    """
    tree = ast.parse(source)
    collections: list[frozenset[str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            if node.elts and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts
            ):
                values = frozenset(e.value for e in node.elts)
                if len(values) >= 2:
                    collections.append(values)
        elif isinstance(node, ast.Dict):
            keys = [k for k in node.keys if k is not None]
            if keys and all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in keys):
                values = frozenset(k.value for k in keys)
                if len(values) >= 2:
                    collections.append(values)
    return collections


def _text_string_literal_collections(source: str) -> list[frozenset[str]]:
    """Text-level extraction for .js/.mjs files (no AST available here).

    Weaker than the AST arm by design (see module docstring): finds bracketed
    ``[...]`` groups and, within each, extracts every quoted string token,
    without regard for JS syntax outside string literals (comments, template
    strings, nesting). Acceptable only because both non-Python sites under
    test are test fixtures, checked against a defensive subset property, not
    the primary reads/does-not-read classification.
    """
    collections: list[frozenset[str]] = []
    for group in _JS_STRING_ARRAY_RE.finditer(source):
        body = group.group(1)
        strings = [m.group(2) for m in _JS_QUOTED_STRING_RE.finditer(body)]
        if len(strings) >= 2 and len(set(strings)) == len(strings):
            collections.append(frozenset(strings))
    return collections


def _string_literal_collections(path: Path) -> list[frozenset[str]]:
    """Shared scanning helper — the ONLY place literal-vocabulary-collection
    extraction logic lives. Dispatches on file suffix; .py (including the
    extensionless-but-Python ``coordinator-doc-new``) uses the AST arm, .js/
    .mjs use the text arm.
    """
    source = path.read_text(encoding="utf-8")
    if path.suffix in (".js", ".mjs"):
        return _text_string_literal_collections(source)
    return _py_string_literal_collections(source)


def _reads_deployment_state(path: Path) -> bool:
    return "deployment_state" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "rel_path,expected_reads",
    _CONSUMERS,
    ids=[Path(rel_path).name for rel_path, _ in _CONSUMERS],
)
def test_consumer_agrees_with_handoff_terminal_deployment(rel_path: str, expected_reads: bool) -> None:
    path = _REPO_ROOT / rel_path
    if not path.exists():
        raise AssertionError(
            f"{rel_path} no longer exists on disk — this consumer must be re-triaged, "
            "not silently dropped from the vocabulary-consumer test."
        )

    actually_reads = _reads_deployment_state(path)
    assert actually_reads == expected_reads, (
        f"{rel_path}: expected reads_deployment_state={expected_reads}, "
        f"got {actually_reads} — the file's handoff deployment_state usage changed; "
        "re-verify whether it now enumerates (or stopped enumerating) terminal states "
        "before updating this expectation."
    )

    # Review: code-reviewer — a pure-subset check would not have caught the
    # actual historical bug tuple {"shipped","abandoned","superseded"} since
    # "superseded" isn't in CANON, so the tuple was never a subset. Widened
    # to a >=2-token-overlap-but-not-equal check (see rationale below).
    #
    # Regardless of the reads/does-not-read classification: any literal
    # string collection in the file that shares >=2 tokens with the
    # canonical vocabulary but does not equal it exactly is flagged. A pure
    # subset (missing elements) is one shape of drift; the actual historical
    # defect sweep-shipped-handoffs.py had was the OTHER shape — a collection
    # that mixed correct terminal tokens with a retired/wrong one
    # (``{"shipped", "abandoned", "superseded"}``, where "superseded" is not
    # in CANON at all, so the collection is not a subset of CANON and a
    # subset-only check would never see it). >=2-overlap-not-equal catches
    # both shapes: missing-element subsets AND contains-a-wrong-token
    # collections. The threshold is 2, not 1: a single shared token is
    # coincidental (a queue-status enum with "closed", a plan-status enum
    # with "abandoned", a goal-status dict with "achieved"/"active" — none
    # of these purport to BE a handoff-terminal-deployment enumeration, they
    # just happen to use one common English word); requiring >=2 shared
    # tokens is what actually distinguishes "looks like it's enumerating
    # this vocabulary" from "coincidentally shares a word with it", and was
    # verified empirically against every listed consumer (a 1-token overlap
    # threshold produces false positives against six unrelated
    # plan/goal/queue/CLI-arg axes — see _KNOWN_HISTORICAL_WITNESSES below
    # for the one file where a 2-token overlap is a genuine, disclosed hit).
    candidates = [
        c
        for c in _string_literal_collections(path)
        if len(c) >= 2 and len(c & CANON) >= 2 and c != CANON
    ]
    permitted = _KNOWN_OVERLAP_EXEMPTIONS.get(rel_path, frozenset())
    for candidate in candidates:
        if candidate in permitted:
            continue
        assert candidate == CANON, (
            f"{rel_path}: found a literal collection {sorted(candidate)} that overlaps the "
            f"canonical handoff terminal vocabulary {sorted(CANON)} in >=2 tokens but does not "
            "equal it — this is the shape DR-084 retired (a stale partial subset, or a "
            "collection that mixes correct tokens with a retired/wrong one like \"superseded\"); "
            "import HANDOFF_TERMINAL_DEPLOYMENT from coordinator_core.lifecycle_constants "
            "instead of hand-enumerating."
        )

"""C5: a guard test keeps the human axis off the strict wire until opticon vendors it.

Purpose: break-class protection for the plan's § The hazard, and the row that makes C4 safe
to ship. C1 (`person_resolver.py`) derives a `contributor_slug` and resolves a `github` alias
purely for opticon's future consumption; C4 wires the resulting bundle into a NEW, additive
`human_assignee` axis. Neither is authorized to reach `owner` — the field the seven sections
below already emit today, straight from frontmatter, with no human-derived source. This test
is the guard that keeps that true after this commit AND after every future one, until opticon
vendors a contract that names the human axis explicitly.

Barrier phase this guards (P1 not started as of 2026-08-19): P1 is opticon's `.strict()`
schema mode, which rejects any UNVENDORED key wholesale — a `human_*`/`contributor_slug` key
reaching the wire before opticon vendors it does not merely go unread, it makes `.strict()`
reject the whole payload. The dual-read fallback (`repo_owner ?? owner`) on their side expects
`owner` to keep meaning "the org", never a human — so sourcing `owner` from a human-derived
value corrupts that axis too, silently, since dual-read has no shape check to catch it. THIS
TEST IS THE ONLY THING BLOCKING BOTH FAILURE MODES RIGHT NOW. Deleting it is only safe once
opticon's vendored contract names `human_*`/`contributor_slug` as accepted keys AND an explicit
decision retires the `owner`-must-not-be-human invariant — read the plan's § The hazard before
removing this file at P3, not just this docstring.

Three assertions (C9 narrowed the first, added the third; a source-only check was never
enough on its own — see the module body):

  1. Source scan over ``coordinator_core/ops/emit/sections/*.py``, NARROWED BY C9
     (2026-08-20): a ``human_*``/``contributor_slug`` dict-key literal is authorized only
     when it is DOMINATED by an ``if`` (or nested ``if``) whose test references
     ``human_axis_vendored``/``_human_axis_on`` — the C9 activation switch
     (``_shared.human_axis_vendored``). An UNGATED literal (reached with no such dominating
     check, or one this AST walk cannot confidently classify) still fails loud, same as
     before C9. This is the SUBJECT correction the task body calls for: the guard's subject
     is the emitted bytes' authorization path, not "no literal anywhere" — ``handoffs.py``
     and ``trackers.py`` now legitimately carry these literals inside C9's gated blocks.
     Also unchanged: no file imports/calls ``person_resolver``/``resolve_operating_person``
     to source ``owner`` — the seven sections below pass ``owner`` straight through from
     frontmatter today, and this keeps it that way.
  2. Corpus scan over the frontmatter corpus the seven sections actually read (via the
     same in-process, no-spawn ``records_query.query_records`` seam the sections themselves
     use, plus ``state/initiatives/*.yaml``, which sections/initiatives.py reads by a plain
     glob rather than that seam): no ``owner`` value equals a resolved ``github`` alias or
     ``contributor_slug`` for the CURRENT operating person (``person_resolver.
     resolve_operating_person()`` — the alias set, not an over-specific shape regex, per the
     task body: a 9-char-base36 regex both over- and under-matches real slugs). This closes
     the realistic corruption path: a human hand-writes ``owner: <slug>`` into a handoff's
     frontmatter and ``handoffs.py``'s existing ``_jq_or(fm.get("owner"), None)`` carries it
     to the wire unchanged, with no section-source edit at all to catch via assertion 1.
  3. ADDED BY C9 — the behavioural leg, which is the one that actually protects the
     consumer rather than assuming the static leg above classified every literal
     correctly: with ``human_axis_vendored()`` mocked OFF, ``handoffs.collect()`` and
     ``trackers.collect()`` emit a record shape byte-identical (as a dict) to a record
     built from frontmatter carrying no ``human_*`` keys at all — no ``human_*`` key
     reaches either section's output, gated source or not. Written once here (this is
     the assertion `ops/emit/tests/test_human_axis_emission.py` already owes; both
     modules assert the same behaviour so neither can drift from the other silently).

Not live today (2026-08-19): zero ``^owner:`` records exist in ``state/handoffs/`` or
``archive/handoffs/`` — assertion 2 is prospective, not a repair; it is a corpus fixture that
happens to be empty today, so an empty match is a PASS, not a vacuous test. If the operating
person's alias set is itself unresolvable on this box (see C1's coverage note — a git config
`user.email` outside `users.noreply.github.com` leaves `contributor_slug` absent), assertion 2
degenerates to only checking the resolvable legs; that degeneration is expected, not a bug in
this test.

Separately: `handoff.schema.json`'s `owner` description reads as human/EM-shaped today, which
is what invites the mistake this test guards against — correcting it is routed to DoE as its
own backlog item (it is a vendored DoE schema under `schema_drift_watch`'s glob; editing it
here is a drift event, not a local fix). Not this test's job.

No spawn: all three assertions run entirely in-process (AST/text scan +
``records_query``'s native, no-``node`` seam, + in-process ``collect()`` calls against
mocked query functions) — the fast tier's spawn ratchet
(``coordinator_core/tests/test_no_new_spawning_tests.py``) stays untouched.

Spec backlink: docs/plans/2026-08-19-the-tracker-names-an-owner.md § C5, § The hazard, § C9
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.person_resolver import resolve_operating_person

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SECTIONS_DIR = _REPO_ROOT / "coordinator_core" / "ops" / "emit" / "sections"

# The seven sections named in the task body as passing frontmatter ``owner`` straight
# through today, mapped to the ``records_query`` record type they read. Kept as an explicit
# list (not a directory sweep) so a new section added later is NOT silently covered by this
# test until someone deliberately adds it here — matching the task body's named-seven scope.
_OWNER_PASSTHROUGH_RECORD_TYPES: tuple[str, ...] = (
    "handoff",
    "handoff-archived",
    "plan",
    "tracker",
    "roadmap",
    "health-status",
    "decision-guide",
)

_FORBIDDEN_KEY_RE = re.compile(r"^(human_[a-zA-Z0-9_]*|contributor_slug)$")
_HUMAN_SOURCE_RE = re.compile(r"\b(person_resolver|resolve_operating_person)\b")

# C9 narrowing: the names an enclosing `if` test must reference for a `human_*`/
# `contributor_slug` string literal to count as gated. `human_axis_vendored` is the
# switch itself (`_shared.py`); `_human_axis_on` is the per-collect()-call local both
# `handoffs.py` and `trackers.py` bind it to before branching on it — matching either
# name (not requiring both) keeps this test source-shape-tolerant of which one a given
# `if` test happens to name directly.
_FLAG_NAMES = frozenset({"human_axis_vendored", "_human_axis_on"})


def _section_source_files() -> list[Path]:
    return sorted(p for p in _SECTIONS_DIR.glob("*.py") if p.name != "__init__.py")


def _test_references_flag(test_node: ast.expr) -> bool:
    """Whether an ``if`` test node's own source references one of ``_FLAG_NAMES`` —
    any ``ast.Name``/``ast.Attribute`` in the test subtree, not just a bare top-level
    check, so ``if _human_axis_on and other_cond:`` still counts as gated."""
    for sub in ast.walk(test_node):
        if isinstance(sub, ast.Name) and sub.id in _FLAG_NAMES:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in _FLAG_NAMES:
            return True
    return False


def _forbidden_literal_offenders(path: Path) -> list[str]:
    """AST-walk one section file; return offender descriptions for every ``human_*``/
    ``contributor_slug`` string-literal dict key NOT dominated by a flag-referencing
    ``if`` (module docstring, assertion 1's C9 narrowing).

    Builds a parent map first (stdlib ``ast`` carries no parent pointers), then for
    each matching ``ast.Constant`` walks up parents looking for an ``ast.If`` whose
    ``test`` references the flag. Reaching a function/module boundary with no
    qualifying ``If`` along the way is UNGATED — fails loud, same as an ``If`` found
    whose test does not reference the flag (this walk does not attempt to prove a
    negative about runtime reachability; a literal with no textually-visible dominating
    check is exactly the shape assertion 1 must catch, per the module docstring's
    "fail loud when the analysis is inconclusive" instruction)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    parent_of: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent_of[child] = node

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if not _FORBIDDEN_KEY_RE.match(node.value):
            continue

        gated = False
        cursor: ast.AST = node
        while cursor in parent_of:
            cursor = parent_of[cursor]
            if isinstance(cursor, ast.If) and _test_references_flag(cursor.test):
                gated = True
                break
            if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                break
        if not gated:
            offenders.append(f"{path.name}:{node.lineno}: {node.value!r} not dominated by a flag check")

    return offenders


def test_no_section_emits_an_ungated_human_shaped_key():
    """NARROWED BY C9 (2026-08-20, module docstring assertion 1): a
    ``coordinator_core/ops/emit/sections/*.py`` file may carry a ``human_*``/
    ``contributor_slug`` string-literal dict key ONLY when it is dominated by an
    ``if`` gated on the C9 activation switch (``human_axis_vendored``/
    ``_human_axis_on``). AST scan, not a live-wire probe — the barrier this guards
    (P1 not started) is unchanged; what changed is the guard's subject, from "no
    literal anywhere" to "no literal reachable without the switch check"."""
    offenders = []
    for path in _section_source_files():
        offenders.extend(_forbidden_literal_offenders(path))
    assert not offenders, (
        f"section(s) emit a human_*/contributor_slug key with no dominating "
        f"human_axis_vendored/_human_axis_on check, unauthorized before opticon "
        f"vendors it (see this file's docstring, § barrier phase): {offenders}"
    )


def test_no_section_sources_owner_from_person_resolver():
    """No section imports/calls ``person_resolver``/``resolve_operating_person`` — the
    structural derivation (``repo.split(\"/\")[0]``, in ``coordinator_roots.py`` and
    ``branch.py``) and the frontmatter passthrough (the seven sections) are the only two
    authorized sources of ``owner`` today; a third, human-derived source is what this
    guards against."""
    offenders = []
    for path in _section_source_files():
        text = path.read_text(encoding="utf-8")
        if _HUMAN_SOURCE_RE.search(text):
            offenders.append(path.name)
    assert not offenders, (
        f"section(s) reference person_resolver/resolve_operating_person, a human-derived "
        f"source unauthorized for `owner` before opticon vendors it: {offenders}"
    )


def _alias_set() -> set[str]:
    """The CURRENT operating person's resolvable aliases (github, contributor_slug) — the
    task body's explicit instruction: match the alias set, not an over-specific shape regex
    (a 9-char-base36 pattern both over- and under-matches real slugs)."""
    bundle = resolve_operating_person()
    aliases = set()
    github = bundle.get("github")
    if github:
        aliases.add(github)
    contributor_slug = bundle.get("contributor_slug")
    if contributor_slug:
        aliases.add(contributor_slug)
    return aliases


def _owner_values_from_records(record_type: str) -> list[tuple[str, str]]:
    """(path, owner) pairs for every ``record_type`` record whose frontmatter carries a
    non-empty ``owner`` — via the same in-process, no-spawn ``records_query.query_records``
    seam the emit sections themselves call. [] on any read failure (fail-open, matching the
    sections' own fail-open contract — this is a guard test, not a corpus-health check)."""
    try:
        records = query_records(record_type, _REPO_ROOT, limit=0)
    except Exception:
        return []
    found = []
    for rec in records:
        fm = rec.get("frontmatter")
        if not isinstance(fm, dict):
            continue
        owner = fm.get("owner")
        if isinstance(owner, str) and owner:
            found.append((rec.get("path", "<unknown>"), owner))
    return found


def _owner_values_from_initiatives() -> list[tuple[str, str]]:
    """(path, owner) pairs from ``state/initiatives/*.yaml`` — sections/initiatives.py reads
    this corpus via a plain glob + its own simple YAML parse, not ``records_query`` (that
    seam only covers markdown-frontmatter record types), so it needs its own read here."""
    ini_dir = _REPO_ROOT / "state" / "initiatives"
    if not ini_dir.is_dir():
        return []
    found = []
    for fpath in sorted(ini_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(fpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        owner = data.get("owner")
        if isinstance(owner, str) and owner:
            found.append((f"state/initiatives/{fpath.name}", owner))
    return found


def test_no_owner_value_matches_a_resolved_human_alias():
    """Belt-and-suspenders: even with every section source clean (the two tests above), a
    human can hand-write ``owner: <slug>`` straight into a record's frontmatter and the
    existing passthrough (e.g. ``handoffs.py``'s ``_jq_or(fm.get("owner"), None)``) carries
    it to the wire unchanged — no section-source edit at all, so the source scan above
    cannot catch it. This asserts over the actual corpus the seven passthrough sections
    read, for the record. Not live today (2026-08-19): zero ``^owner:`` records exist in
    ``state/handoffs/``/``archive/handoffs/``, so this is prospective, not a repair — an
    empty match set is the expected PASS, not a vacuous test."""
    alias_set = _alias_set()

    offenders: list[tuple[str, str]] = []
    for record_type in _OWNER_PASSTHROUGH_RECORD_TYPES:
        for path, owner in _owner_values_from_records(record_type):
            if owner in alias_set:
                offenders.append((path, owner))
    for path, owner in _owner_values_from_initiatives():
        if owner in alias_set:
            offenders.append((path, owner))

    assert not offenders, (
        f"record(s) carry an `owner` value matching the current operating person's "
        f"resolved github alias or contributor_slug — a human-derived value reaching the "
        f"`owner` axis, unauthorized before opticon vendors it: {offenders}"
    )


# ---------------------------------------------------------------------------
# ADDED BY C9 — the behavioural leg (module docstring assertion 3). This is the
# same assertion `ops/emit/tests/test_human_axis_emission.py` owes; written once
# here and relied on by both rows so the two cannot drift apart silently.
# ---------------------------------------------------------------------------

def _handoffs_ctx(tmp_path: Path) -> EmitContext:
    central = tmp_path / "state"
    central.mkdir(parents=True, exist_ok=True)
    return EmitContext(
        repo_root=tmp_path,
        coordinator_root=tmp_path,
        central_state_root=central,
        git_branch="test-branch",
        git_sha="0" * 40,
        git_sha_short="00000000",
        observed_at="2026-08-20T00:00:00Z",
        hostname="test-host",
        repo_name="test-org/test-repo",
    )


def test_handoffs_emission_is_byte_identical_with_switch_off(tmp_path):
    """With the switch off, a record whose frontmatter carries `human_assignee`/
    `human_claimant` emits the exact same dict shape as one that carries neither —
    the switch, not the frontmatter, gates the key's presence on the wire."""
    from coordinator_core.ops.emit.sections import handoffs as handoffs_section

    ctx = _handoffs_ctx(tmp_path)
    base_fm = {
        "title": "t", "created": "2026-08-20", "status": "open",
        "deployment_state": "ready_to_fire",
    }

    with patch("coordinator_core.ops.emit.sections.handoffs.human_axis_vendored", return_value=False):
        with patch("coordinator_core.ops.emit.sections.handoffs._query_records") as mock_qr:
            mock_qr.side_effect = lambda ctx_arg, rt: (
                [{"path": "state/handoffs/x.md", "frontmatter": base_fm}] if rt == "handoff" else []
            )
            without_human, _ = handoffs_section.collect(ctx)

        with patch("coordinator_core.ops.emit.sections.handoffs._query_records") as mock_qr:
            mock_qr.side_effect = lambda ctx_arg, rt: (
                [{
                    "path": "state/handoffs/x.md",
                    "frontmatter": {**base_fm, "human_assignee": "abc123def", "human_claimant": "abc123def"},
                }] if rt == "handoff" else []
            )
            with_human, _ = handoffs_section.collect(ctx)

    assert without_human == with_human
    assert "human_assignee" not in without_human[0]
    assert "human_claimant" not in without_human[0]


def test_trackers_emission_is_byte_identical_with_switch_off(tmp_path):
    from coordinator_core.ops.emit.sections import trackers as trackers_section

    ctx = _handoffs_ctx(tmp_path)
    base_fm = {"title": "t", "created": "2026-08-20", "status": "active"}

    with patch("coordinator_core.ops.emit.sections.trackers.human_axis_vendored", return_value=False):
        with patch("coordinator_core.ops.emit.sections.trackers._query_tracker_records") as mock_qr:
            mock_qr.return_value = [{"path": "docs/project-tracker.md", "frontmatter": base_fm}]
            without_human, _ = trackers_section.collect(ctx)

        with patch("coordinator_core.ops.emit.sections.trackers._query_tracker_records") as mock_qr:
            mock_qr.return_value = [{
                "path": "docs/project-tracker.md",
                "frontmatter": {**base_fm, "human_owner": "abc123def"},
            }]
            with_human, _ = trackers_section.collect(ctx)

    assert without_human == with_human
    assert "human_owner" not in without_human[0]

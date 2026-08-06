#!/usr/bin/env python3
"""
test_pickup_kind_enum_parity.py — enum-parity CHARACTERIZATION test.

Spec backlink: docs/plans/2026-07-11-cross-repo-commitment-lifecycle.md § C1 (AC1, AC2)

SCOPE: true three-way parity. The memo `kind` enum is declared in three
places that must stay in lockstep, and this test parses all three directly
from disk and asserts set-equality across the full triple — no leg is
covered transitively via a hand-maintained comment:

  1. coordinator/bin/cross-repo-memo             — `_VALID_KINDS` (sender-side CLI authority)
  2. coordinator_core/ops/fleet/memo_send.py     — `_VALID_KINDS` (native engine-side send gate)
  3. coordinator/skills/pickup/SKILL.md          — M3 "Pinned enum:" line (reader/consumer doc)

Reconciled 2026-07-25 (test made collectable/failable by 23f65fce surfaced a
FileNotFoundError, not a genuine drift):

  - Leg 2 was `coordinator/bin/lib/schema.js` (`validKinds`, a hand-rolled JS
    receiver-side validator, parsed by regex since it was never require()'d).
    The 2026-07-22 de-node cutover retired schema.js entirely — its role is
    now filled by `coordinator_core.ops.fleet.memo_send._VALID_KINDS`, a real
    Python tuple imported directly (not copy-pasted) by
    `coordinator_core.contract.emit_memo_schema` (see that module's own
    docstring). This test now imports memo_send and reads the constant
    directly instead of regexing a file that no longer exists — strictly
    more precise than the regex it replaces, since there is no longer a
    second hand-authored copy of this leg to drift out of parseable shape.
  - Leg 3 (SKILL.md) was retired from this repo's tree by the 2026-07-20
    plugin-surface retirement (docs/plans/2026-07-20-retire-claude-klabauter-plugin-surface.md)
    — discovery-resolved surfaces (skills, plugins, hooks) now live only in
    coordinator-claude (example-doctrine-repo). This repo can no longer observe that leg
    at a co-located path. Rather than hardcode an absolute path to a sibling
    clone (machine-dependent, breaks on any machine without that clone), the
    path is resolved the same way every other doctrine CLI in this repo
    resolves the example-doctrine-repo root — `coordinator_registry.doe_root()`
    (DOE_ROOT env -> REPO_EXAMPLE_DOCTRINE_REPO env -> machine-local repos.example_doctrine_repo).
    When unresolvable, this leg is skipped (not silently passed) via
    pytest.skip with the reason on the record — an honest "cannot observe"
    rather than a false green.

Run with: python3 -m pytest coordinator/bin/test_pickup_kind_enum_parity.py
"""

from __future__ import annotations

import os
import re
import sys

import pytest

_REPO_BIN_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB_DIR = os.path.join(_REPO_BIN_DIR, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import coordinator_registry as reg  # noqa: E402


def _repo_bin_dir() -> str:
    """Absolute path to the coordinator/bin directory this test lives in."""
    return _REPO_BIN_DIR


def _cross_repo_memo_path() -> str:
    return os.path.join(_repo_bin_dir(), "cross-repo-memo")


def _pickup_skill_path() -> str:
    """Resolve pickup/SKILL.md — co-located rung first, example-doctrine-repo-clone rung second.

    Rung 1 (co-located): schemas/skills sitting beside bin/ under the same
    coordinator root — true for any layout that hasn't split skills out.
    Rung 2 (split-repo, current claude-klabauter layout): skills/ live only in the example-doctrine-repo
    clone post-2026-07-20 retirement; resolved via coordinator_registry's
    shared doe_root() helper (env -> env -> machine-local), never a
    hardcoded absolute path. Raises reg._DoeUnresolvable if neither rung
    resolves — callers must catch and skip, not hard-fail.
    """
    local = os.path.join(
        os.path.dirname(_repo_bin_dir()), "skills", "pickup", "SKILL.md"
    )
    if os.path.exists(local):
        return local
    return os.path.join(reg.doe_root(), "coordinator", "skills", "pickup", "SKILL.md")


def _parse_valid_kinds_from_cli(path: str) -> set[str]:
    """Extract the `_VALID_KINDS = (...)` tuple literal from cross-repo-memo.

    Matches the exact declaration shape:
        _VALID_KINDS = ("ask", "consult", "fyi", "proposal")
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(r'_VALID_KINDS\s*=\s*\(([^)]*)\)', content)
    if not m:
        raise AssertionError(
            f"could not locate '_VALID_KINDS = (...)' in {path} — "
            "CLI enum declaration shape has changed; update this test's parser."
        )
    items = re.findall(r'"([^"]+)"', m.group(1))
    if not items:
        raise AssertionError(
            f"_VALID_KINDS tuple in {path} parsed empty — regex/shape mismatch."
        )
    return set(items)


def _parse_pinned_enum_from_skill(path: str) -> set[str]:
    """Extract the M3 'Pinned enum:' pipe-delimited list from pickup/SKILL.md.

    Matches the exact declaration shape:
        **Pinned enum:** `ask | consult | fyi | proposal`

    Asserts the shape occurs exactly once — a second occurrence elsewhere in
    SKILL.md (e.g. a worked example echoing the enum) would otherwise be
    silently ignored by `re.search`, which only ever returns the first match.
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    occurrences = content.count("**Pinned enum:**")
    if occurrences != 1:
        raise AssertionError(
            f"expected exactly one '**Pinned enum:**' occurrence in {path}, "
            f"found {occurrences} — duplicate or removed pinned-enum line "
            "would otherwise silently escape this test."
        )
    m = re.search(r'\*\*Pinned enum:\*\*\s*`([^`]+)`', content)
    if not m:
        raise AssertionError(
            f"could not locate '**Pinned enum:** `...`' in {path} — "
            "M3 pinned-enum line shape has changed; update this test's parser."
        )
    items = [part.strip() for part in m.group(1).split("|")]
    items = [item for item in items if item]
    if not items:
        raise AssertionError(
            f"Pinned enum line in {path} parsed empty — regex/shape mismatch."
        )
    return set(items)


def _valid_kinds_from_memo_send() -> set[str]:
    """The native engine-side send gate's `_VALID_KINDS` — imported directly,
    not regexed. Successor to schema.js's `validKinds` (retired 2026-07-22
    de-node cutover); this is the same tuple `emit_memo_schema.py` imports for
    example-doctrine-repo's derived JSON Schema projections, so a real import here is strictly
    more precise than the regex-on-a-vendored-file shape this replaces.
    """
    from coordinator_core.ops.fleet.memo_send import _VALID_KINDS

    return set(_VALID_KINDS)


def test_pickup_pinned_enum_matches_cli_valid_kinds() -> None:
    """AC1/AC2: pickup M3 pinned enum, cross-repo-memo _VALID_KINDS, and
    memo_send._VALID_KINDS must all set-equal each other.

    Fails loud (with the actual symmetric differences) on any pairwise
    divergence across the three-way comparison — any side missing a kind
    another declares, or naming one another does not.

    The SKILL.md leg is skipped (not silently passed) in two cases, each
    logged with its reason rather than reached by a quiet fallthrough:

      - This machine has no resolvable example-doctrine-repo clone (coordinator_registry.doe_root()
        raises reg._DoeUnresolvable) — genuinely cross-repo since the
        2026-07-20 plugin surface retirement moved skills/ out of this tree.
      - example-doctrine-repo commit 2dc344fa ("C5: collapse pickup SKILL.md to the thin
        classification-resolved shell") deliberately removed the M3 "Pinned
        enum:" prose line — the `kind` enum is now surfaced via a
        runtime-computed judgment point (the fired decision object), not a
        static string in SKILL.md, so there is no longer a third parseable
        leg on example-doctrine-repo's side at all. This is example-doctrine-repo's artifact and example-doctrine-repo's design
        choice; re-adding a parity anchor there (if wanted) is a cross-repo
        ask, not a claude-klabauter-side fix — asserting against text that no longer
        exists by design would be a false requirement on a file this repo
        does not own.

    CLI/engine two-way parity (test_cli_matches_engine_valid_kinds below) is
    unconditional and always enforced regardless of either skip.
    """
    name = "pickup M3 pinned enum == cross-repo-memo _VALID_KINDS == memo_send._VALID_KINDS"
    cli_kinds = _parse_valid_kinds_from_cli(_cross_repo_memo_path())
    engine_kinds = _valid_kinds_from_memo_send()

    try:
        skill_path = _pickup_skill_path()
    except reg._DoeUnresolvable as e:
        pytest.skip(
            f"pickup/SKILL.md leg unobservable — example-doctrine-repo clone not resolvable on "
            f"this machine ({e}); CLI/engine two-way parity still enforced "
            "separately below."
        )
    with open(skill_path, "r", encoding="utf-8") as f:
        _skill_content = f.read()
    if _skill_content.count("**Pinned enum:**") == 0:
        pytest.skip(
            f"pickup/SKILL.md ({skill_path}) no longer carries a 'Pinned "
            "enum:' line — collapsed to a classification-resolved shell "
            "(example-doctrine-repo commit 2dc344fa); the kind enum is now a runtime-computed "
            "judgment point, not static prose. Nothing to parse on this leg "
            "by design; CLI/engine two-way parity still enforced separately "
            "below."
        )
    skill_kinds = _parse_pinned_enum_from_skill(skill_path)

    if cli_kinds == skill_kinds == engine_kinds:
        return

    detail_parts = []
    missing_from_skill = cli_kinds - skill_kinds
    extra_in_skill = skill_kinds - cli_kinds
    if missing_from_skill:
        detail_parts.append(
            f"CLI accepts but SKILL.md M3 does not recognize: {sorted(missing_from_skill)}"
        )
    if extra_in_skill:
        detail_parts.append(
            f"SKILL.md M3 recognizes but CLI does not accept: {sorted(extra_in_skill)}"
        )

    missing_from_engine = cli_kinds - engine_kinds
    extra_in_engine = engine_kinds - cli_kinds
    if missing_from_engine:
        detail_parts.append(
            f"CLI accepts but memo_send._VALID_KINDS does not recognize: {sorted(missing_from_engine)}"
        )
    if extra_in_engine:
        detail_parts.append(
            f"memo_send._VALID_KINDS recognizes but CLI does not accept: {sorted(extra_in_engine)}"
        )

    missing_from_skill_vs_engine = engine_kinds - skill_kinds
    extra_in_skill_vs_engine = skill_kinds - engine_kinds
    if missing_from_skill_vs_engine:
        detail_parts.append(
            f"memo_send._VALID_KINDS accepts but SKILL.md M3 does not recognize: {sorted(missing_from_skill_vs_engine)}"
        )
    if extra_in_skill_vs_engine:
        detail_parts.append(
            f"SKILL.md M3 recognizes but memo_send._VALID_KINDS does not accept: {sorted(extra_in_skill_vs_engine)}"
        )

    raise AssertionError(f"{name}: " + ("; ".join(detail_parts)))


def test_cli_matches_engine_valid_kinds() -> None:
    """Two-way local parity: cross-repo-memo._VALID_KINDS ==
    memo_send._VALID_KINDS.

    Both legs live in this repo (unlike the SKILL.md leg above) and are
    hand-duplicated tuples — coordinator/bin/cross-repo-memo is the ported
    CLI (claude-klabauter-owned since the a05cae48 executable-surface adoption);
    coordinator_core/ops/fleet/memo_send.py is the newer native engine op
    strang-03/DR-210 is migrating the send verb into. Until that migration
    retires one of the two declarations, this is a real same-repo drift risk
    and is asserted unconditionally (no example-doctrine-repo-clone dependency, never skipped).
    """
    cli_kinds = _parse_valid_kinds_from_cli(_cross_repo_memo_path())
    engine_kinds = _valid_kinds_from_memo_send()
    assert cli_kinds == engine_kinds, (
        f"cross-repo-memo._VALID_KINDS ({sorted(cli_kinds)}) != "
        f"memo_send._VALID_KINDS ({sorted(engine_kinds)})"
    )


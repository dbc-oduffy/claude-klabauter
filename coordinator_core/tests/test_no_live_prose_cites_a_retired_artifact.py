"""
coordinator_core.tests.test_no_live_prose_cites_a_retired_artifact -- static
gate proving no live production module under coordinator_core/ or
coordinator/bin/ names an artifact the relocation ledger records as RETIRED or
MOVED, unless that (file, artifact) pair predates this gate and is recorded in
its baseline.

MOTIVATING INCIDENT (2026-08-30, this repo). ``ops/ceremony/post_commit_tail.py``
went on describing a dead caller (K-046, `c07062c99`, 2026-08-23) in the present
tense, and three readers plus a PM sweep were each misled by it in turn before
the trace was corrected. Full incident:
``docs/research/spike-verdicts/2026-08-30-baton-ship-stamp-inside-a-500ms-close.md``
§ CORRECTION, and kill-ledger K-046/K-116. The brightline already names the rule
this violates -- "No conclusion rests on 'a caller exists'" -- but a rule with
no artifact behind it is the thing the north star exists to replace. This
module is that artifact.

WHY THE RELOCATION LEDGER IS THE NAME SET. It is the one place a deliberate
death is already recorded, in machine-readable form, as a precondition of
``plugin_health.bin_inventory_gate`` passing. So the set of dead names needs no
heuristic, no guessing, and no maintenance of its own: retiring something adds
it to the ledger, which arms this gate against every module still describing it.
A naive "every filename cited in prose must exist on disk" gate was measured
first and rejected -- 984 sites over 427 names, most of them illustrative
placeholders (``foo.py``, ``bar.py``) or bin scripts cited by ``.py`` where only
a ``.cmd`` twin exists. That is the false-positive flood
``test_no_forked_frontmatter_key_regex.py`` already documents the cost of.

THE BASELINE IS DEBT, NOT AMNESTY. ``dead_artifact_citation_baseline.json``
records the (file, artifact) pairs that existed when this gate landed. They are
listed individually and re-checked by ``test_baseline_only_shrinks`` precisely
so they cannot rot invisibly -- the failure mode of
``state/lessons/2026-08-19-a-guard-on-a-deleted-binary-fails-silently-forever.md``,
whose whole lesson is that a guard which silently stops discriminating is worse
than none. Most baselined citations are already correct: they speak in the past
tense ("is deleted", "were removed in the ceremony.wsc_tail kill"). A few are
not, and are tracked as real debt.

WHAT THIS GATE DECLINES:
  - Tense. It cannot tell "X used to do this" from "X does this", and does not
    try -- a gate that guesses at grammar is worse than none. It asserts only
    that a NEW citation of a retired artifact gets a human decision at the
    moment it appears, which is when the author still knows the answer.
  - Test modules and ``/tests/`` directories. A test naming a dead CLI is
    ordinarily asserting it stays dead.
  - Non-Python artifacts, and ledger entries whose ``old_path`` still exists on
    disk (a ``moved`` entry whose source was later re-created is not a corpse).
  - History surfaces -- ``state/kill-ledger.md``, ``archive/``, ``docs/plans/``,
    handoffs. Naming the dead is their job.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LEDGER = _REPO_ROOT / "docs" / "install" / "relocation-ledger.json"
_BASELINE = Path(__file__).resolve().parent / "dead_artifact_citation_baseline.json"

#: Live production surfaces this gate scans. Deliberately not the whole tree:
#: these are the two directories whose prose a reader consults to learn what
#: currently runs.
_SCANNED_ROOTS = ("coordinator_core", "coordinator/bin")

#: Per-CITATION opt-out. A module that must keep naming a corpse writes this
#: token on the same line, or anywhere in the contiguous comment block the
#: citation sits in, naming the artifact: ``gravestone: wsc-tail.py``.
#:
#: This replaced a whole-FILE ``GRAVESTONE NOTICE`` exemption on 2026-08-30, the
#: day both landed. Kira measured that exemption suppressing ZERO pairs across
#: the two files carrying it — a permanent unaudited escape hatch invented in the
#: same session, for the same files, that turned out not to need it. Its first
#: real customer arrived the same afternoon and argued the opposite way: a peer
#: (`90ee922ddd`) added exemplary past-tense prose recording that
#: `coordinator/bin/wsc-tail.py` died at K-046, and this gate flagged it, because
#: the gate is name-based and declines to sniff tense. A whole file is too coarse
#: a thing to silence for one correct sentence; the marker is per-citation so the
#: silence is exactly as wide as the claim it covers.
_GRAVESTONE_MARKER = "gravestone:"

#: Regex word boundary, named so the alternation above reads as prose.
BOUND = r"\b"


def _retired_artifacts() -> dict[str, str]:
    """Basename -> disposition, for every ledger entry recording a retired or
    moved Python artifact whose ``old_path`` no longer exists on disk.

    The on-disk check is what keeps a ``moved`` entry from firing forever: once
    something is moved back, or a same-named file is legitimately re-created at
    the old path, it is no longer a corpse and citing it is no longer a defect.
    """
    ledger = json.loads(_LEDGER.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for entry in ledger.get("entries", []):
        old_path = entry.get("old_path") or ""
        if entry.get("disposition") not in ("retired", "moved"):
            continue
        if not old_path.endswith(".py"):
            continue
        if (_REPO_ROOT / old_path).exists():
            continue
        out[os.path.basename(old_path)] = entry["disposition"]
    return out


def _live_production_modules() -> list[Path]:
    files: list[Path] = []
    for root in _SCANNED_ROOTS:
        for dirpath, _, filenames in os.walk(_REPO_ROOT / root):
            posix = Path(dirpath).as_posix()
            if "__pycache__" in posix or "/tests/" in posix:
                continue
            for name in filenames:
                if name.endswith(".py") and not name.startswith("test_"):
                    files.append(Path(dirpath) / name)
    return files


@lru_cache(maxsize=1)
def _citations() -> frozenset:
    """Every (repo-relative posix path, dead-artifact basename) pair currently
    in live production prose.

    A module carrying an explicit ``GRAVESTONE NOTICE`` is exempt wholesale: it
    has already made the statement this gate exists to force, and re-listing its
    every mention would only make the baseline noisier without making any reader
    better informed.
    """
    dead = _retired_artifacts()
    if not dead:
        return frozenset()
    #: ONE alternation, not one pass per dead name. The first cut ran 28 separate
    #: regex sweeps over every file and took 14.3s; a gate slow enough to be worth
    #: skipping is a gate that gets skipped.
    alternation = "|".join(re.escape(name) for name in sorted(dead))
    pattern = re.compile(BOUND + "(?:" + alternation + ")" + BOUND)
    found = set()  # memoised by the decorator: both tests share one tree walk
    for path in _live_production_modules():
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(_REPO_ROOT).as_posix()
        for match in pattern.finditer(text):
            basename = match.group(0)
            if _marker_covers(text, match.start(), basename):
                continue
            found.add((rel, basename))
    return frozenset(found)


def _marker_covers(text: str, index: int, basename: str) -> bool:
    """True when a ``gravestone: <basename>`` marker governs the citation at
    ``index``.

    Scope is the citation's own line plus the contiguous run of comment lines
    around it — the block a reader takes in as one annotation. Deliberately not
    the whole file (see ``_GRAVESTONE_MARKER``) and deliberately not a fixed line
    window, which would silently stop covering an annotation someone later
    reflows.
    """
    lines = text.splitlines()
    line_no = text.count(chr(10), 0, index)

    def _is_comment(i: int) -> bool:
        return 0 <= i < len(lines) and lines[i].lstrip().startswith("#")

    lo = hi = line_no
    while _is_comment(lo - 1):
        lo -= 1
    while _is_comment(hi + 1):
        hi += 1
    block = chr(10).join(lines[lo : hi + 1])
    if _GRAVESTONE_MARKER not in block:
        return False
    #: The marker must NAME the artifact it silences. A bare `gravestone:` would
    #: re-create the blanket exemption this replaced, one block at a time.
    for raw in block.split(_GRAVESTONE_MARKER)[1:]:
        if basename in raw.split(chr(10))[0]:
            return True
    return False


def _baseline() -> set:
    return {(a, b) for a, b in json.loads(_BASELINE.read_text(encoding="utf-8"))}


def test_no_new_citation_of_a_retired_artifact() -> None:
    """A module may not newly name an artifact the relocation ledger buried.

    Remediation, in preference order: delete the citation if the prose is only
    describing machinery that no longer exists; rewrite it in the past tense
    naming the ledger entry; or, when a module genuinely must keep describing
    the dead thing at length, open it with a ``GRAVESTONE NOTICE`` block saying
    what died, when, and what the live caller is instead (see
    ``ops/ceremony/post_commit_tail.py`` for the shape). Adding the pair to the
    baseline is NOT a remediation -- that file only shrinks.
    """
    new = sorted(_citations() - _baseline())
    assert not new, (
        "live production prose newly cites an artifact the relocation ledger "
        "records as retired/moved:\n"
        + "\n".join("  %s -> %s" % (path, basename) for path, basename in new)
        + "\n\nThe artifact is gone; the prose describing it is not. Delete the "
        "citation, put it in the past tense, or open the module with a "
        "GRAVESTONE NOTICE. Do not add it to the baseline."
    )


def test_baseline_only_shrinks() -> None:
    """Every baselined pair must still be a real citation.

    A baseline entry that no longer matches means the debt was paid -- good, and
    the entry comes out. Left in, it is a line of permanent fiction that makes
    the count meaningless, which is exactly how
    ``state/lessons/2026-08-19-a-guard-on-a-deleted-binary-fails-silently-forever.md``
    describes a guard dying.
    """
    stale = sorted(_baseline() - _citations())
    assert not stale, (
        "baseline lists (file, artifact) pairs that are no longer citations -- "
        "the debt was paid; remove these lines from "
        "coordinator_core/tests/dead_artifact_citation_baseline.json:\n"
        + "\n".join("  %s -> %s" % (path, basename) for path, basename in stale)
    )

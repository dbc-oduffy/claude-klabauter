"""
coordinator_core.execute_plan_assemble.falsifier_shape — the falsifier's
authoring-time SHAPE predicates, pure and import-light.

Purpose: `close_out_and_stamp._evaluate_goal_falsifier_gate` (the terminal
close-out gate) and `roadmap.prep_gate` (the pre-execution mise-prep/
readiness bar) must judge the SAME plan's falsifier placement and t-shirt/
date gating IDENTICALLY (gh-klabauter#71, F23b) -- a plan that clears the
readiness gate and then refuses at close-out over a defect the readiness
gate could have caught cost its author a whole execution round trip before
this module existed. Restating the predicate in each caller risks exactly
the drift a "one definition" reuse rule exists to prevent.

Split out of `close_out_and_stamp.py` rather than imported from it: that
module's own top-level imports pull in git commit machinery, ceremony,
session, and handoff plumbing -- fine for a terminal, once-per-close-out
gate, but `roadmap.prep_gate` is a ZERO-SPAWN, per-plan hot path (its own
module docstring's Budget section) that runs over the whole corpus in one
census. Importing `close_out_and_stamp` from there paid that module's
whole import graph on whichever corpus plan happened to be the first to
need it -- measured: `2026-09-22-silent-swallow-campaign-wave-dispatch.md`
alone crossed the 500ms brightline
(`test_prep_gate.py::test_every_real_plan_holds_the_brightline`) purely
from that one-time cold import landing on it. This module has none of
that: only `re`, `datetime`, and `yaml`.

`close_out_and_stamp.py` imports these names back (re-export, never a
second copy) so every existing caller of e.g. `close_out_and_stamp.
_falsifier_block` keeps working unchanged.

Negative-spec: does NOT read git, does NOT spawn, does NOT read
`exit_criterion_met` or verify a `baseline_ref`'s git-ancestry (those stay
`close_out_and_stamp`'s own business -- see its module docstring's "§ The
goal-falsifier stamp-decision gate").
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any, Optional

import yaml

#: `prime_exit_criterion.falsifier`'s four required, non-blank string keys.
_REQUIRED_FALSIFIER_KEYS = ("how", "baseline_output", "baseline_ref", "expected_when_true")


def _falsifier_block(prime_exit_criterion: Any) -> Optional[dict]:
    """Total, never-raising detection of a real `falsifier` sub-object: a
    non-dict `prime_exit_criterion`, an absent/non-dict `falsifier`, or a
    `falsifier` missing any of its four required non-blank string keys ALL
    return `None` here."""
    if not isinstance(prime_exit_criterion, dict):
        return None
    falsifier = prime_exit_criterion.get("falsifier")
    if not isinstance(falsifier, dict):
        return None
    for key in _REQUIRED_FALSIFIER_KEYS:
        value = falsifier.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
    return falsifier


def _falsifier_misnested(fm: dict) -> bool:
    """Whether a top-level `falsifier:` sibling key -- rather than the
    nested `prime_exit_criterion.falsifier` `_falsifier_block` reads --
    explains an otherwise-`None` falsifier block (gh-klabauter#63). Only a
    genuine non-empty dict counts; a blank or scalar top-level `falsifier:`
    key still reads as plainly absent."""
    top_level = fm.get("falsifier")
    return isinstance(top_level, dict) and bool(top_level)


GRANDFATHER_DATE = "2026-08-27"
"""The literal ISO date the prime-exit-criterion requirement starts binding.
See `close_out_and_stamp.py`'s own docstring for the full ruling this pins."""

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _plan_created_on_or_after_grandfather(fm: dict) -> bool:
    """Whether the plan's `created` places it under the requirement. Fails
    toward GRANDFATHERED (`False`) on anything it cannot read cleanly."""
    created = fm.get("created")
    if isinstance(created, datetime.datetime):
        created = created.date()
    if isinstance(created, datetime.date):
        return created.isoformat() >= GRANDFATHER_DATE
    if not isinstance(created, str):
        return False
    head = created.strip()[:10]
    if not _ISO_DATE_RE.match(head):
        return False
    return head >= GRANDFATHER_DATE


_MPLUS_TSHIRTS = frozenset({"M", "L", "XL", "XXL"})


def _plan_is_m_plus(fm: dict, root: Path) -> bool:
    """Whether the plan's linked sizing object sizes it M or larger. Fails
    toward NOT-M-PLUS (`False`) on an absent, null, unresolvable, or
    unreadable `sizing_object`. Reads at most one file, spawns nothing."""
    sizing_ref = fm.get("sizing_object")
    if not isinstance(sizing_ref, str) or not sizing_ref.strip():
        return False
    sizing_path = root / sizing_ref.strip()
    try:
        doc = yaml.safe_load(sizing_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return False
    if not isinstance(doc, dict):
        return False
    estimate = doc.get("estimate")
    if not isinstance(estimate, dict):
        return False
    tshirt = estimate.get("tshirt")
    return isinstance(tshirt, str) and tshirt.strip().upper() in _MPLUS_TSHIRTS


_EXEMPTION_CLASS = "unfalsifiable-by-observation-doctrine-or-schema-edit"


def _falsifier_exemption(prime_exit_criterion: Any) -> Optional[dict]:
    """Total, never-raising detection of a real `falsifier_exemption`.
    Returns the exemption only when it carries the pinned `class` spelling
    and a non-blank `admission`; anything else reads as "no exemption"."""
    if not isinstance(prime_exit_criterion, dict):
        return None
    exemption = prime_exit_criterion.get("falsifier_exemption")
    if not isinstance(exemption, dict):
        return None
    if exemption.get("class") != _EXEMPTION_CLASS:
        return None
    admission = exemption.get("admission")
    if not isinstance(admission, str) or not admission.strip():
        return None
    return exemption


GOAL_REFUSAL_PRIME_ABSENT = "prime_exit_criterion_absent"
GOAL_REFUSAL_FALSIFIER_ABSENT = "falsifier_absent"
GOAL_REFUSAL_FALSIFIER_MISNESTED = "falsifier_misnested"


def goal_falsifier_defect(fm: dict, root: Path) -> Optional[str]:
    """Arms 0 and 1 of the close-out goal gate, as one decision: the
    `GOAL_REFUSAL_*` reason a plan's frontmatter earns, or `None`.
    Excludes `status_override_*`, which only the terminal gate honours.
    Total: an unreadable date or sizing reads as grandfathered."""
    if not (_plan_created_on_or_after_grandfather(fm) and _plan_is_m_plus(fm, root)):
        return None
    prime = fm.get("prime_exit_criterion")
    if prime is None:
        return GOAL_REFUSAL_PRIME_ABSENT
    if _falsifier_exemption(prime) is not None or _falsifier_block(prime) is not None:
        return None
    if _falsifier_misnested(fm):
        return GOAL_REFUSAL_FALSIFIER_MISNESTED
    return GOAL_REFUSAL_FALSIFIER_ABSENT


_GREP_BARE_WORD_RE = re.compile(
    r"""\bgrep\b(?P<opts>(?:\s+-\S+)*)\s+(?P<q>["']?)(?P<word>[A-Za-z_][\w-]*)(?P=q)(?=\s|$|[|)<>;&])"""
)
_IN_BARE_WORD_RE = re.compile(
    r"""(?P<q>["'])(?P<word>[A-Za-z_][\w-]*)(?P=q)\s+in\s+\w+(?:\.(?:lower|strip|casefold)\(\))*(?![\w.(\[])"""
)
_ANCHORING_GREP_OPTS = re.compile(r"(?:^|\s)(?:-[A-Za-z]*[wx][A-Za-z]*|--word-regexp|--line-regexp)(?=\s|$)")


def falsifier_how_unanchored(how: Any) -> Optional[str]:
    """Advisory, prove-bad-only: the bare word a falsifier `how` decides on by
    unanchored substring (`grep -c prior`, `"prior" in line`), else `None`.
    That match fires on any line merely containing the word, so an
    indeterminate result can read as a pass. Anything not provably that shape
    (anchored pattern, `-w`/`-x`, `==`, a split token list) returns `None`."""
    if not isinstance(how, str):
        return None
    for match in _GREP_BARE_WORD_RE.finditer(how):
        if not _ANCHORING_GREP_OPTS.search(match.group("opts")):
            return match.group("word")
    match = _IN_BARE_WORD_RE.search(how)
    if match is not None:
        return match.group("word")
    return None


#: A bare `disposition_ref`/`baseline_ref` is always a hex commit sha --
#: never a symbolic ref, branch name, or tag. Bounding the shape before ever
#: handing the value to `git rev-parse` is deliberate defense-in-depth.
_DISPOSITION_REF_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")

#: `baseline_ref` alone may additionally carry a `<repo>:<sha>` cross-repo
#: qualifier -- `disposition_ref` never does.
_BASELINE_REF_CROSS_REPO_RE = re.compile(r"^([a-z][a-z0-9_-]*):([0-9a-fA-F]{4,40})$")

#: `disposition_ref` may also be `<repo_key>:<sha>` -- a superset of the
#: vendored plan-tasks schema's `^([a-z][a-z0-9_-]+:)?[0-9a-f]{7,40}$`
#: (key 2+ chars), so a value the schema admits is never MALFORMED at
#: close-out.
_DISPOSITION_REF_QUALIFIED_RE = re.compile(r"^([a-z][a-z0-9_-]+):([0-9a-fA-F]{4,40})$")

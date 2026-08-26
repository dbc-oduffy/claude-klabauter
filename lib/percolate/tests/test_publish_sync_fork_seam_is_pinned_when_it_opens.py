"""The `_import_publish_sync` fork seam must be pinned BEHAVIOURALLY the day it opens.

`coordinator/bin/publish.py :: _resolve_publish_sync_module_path` is keyed on
the SOURCE REPO's `setup/` directory. Claude-klabauter has no `setup/publish_sync.py`
today, so a claude-klabauter-rooted run resolves straight to the engine module
(`coordinator/lib/percolate/publish_sync.py`) and gets whatever the engine
implements for free. `_import_publish_sync`'s own docstring already names the
expiry -- "claude-klabauter has no `setup/` directory of its own BEFORE THIS PLAN'S C1
LANDS ONE".

WHY THIS GUARD EXISTS, measured on the sibling repo 2026-08-26 rather than
imagined here. DoE-claude DOES have a `setup/publish_sync.py`, and it won that
seam for every DoE-rooted run while having silently forked from the engine: it
still parsed `COORDINATOR_OVERRIDE_ORPHAN_SWEEP` as `os.environ.get(...) ==
"1"` long after the engine grew the scoped `=<name>[,<name>...]` form, with no
`_orphan_sweep_override` and no `exempt`/`at_risk` split. The operator-facing
consequence is the point, and it is worse than a stale copy: the top-level
presence preflight's own remediation text rendered `=1` as the ONLY key, and
`=1` disarms the orphan sweep for EVERY orphan in the round, not the one the
operator reasoned about -- a blanket disarm on a destructive path against a
public mirror, reached by following the guard's own instruction. A guard that
names a blanket disarm as its remedy is more dangerous than no guard, because
it is followed. Fixed there at DoE `3e3078bcb`, pinned at DoE `5a7090b7c`.

WHY A SIGNATURE-LEVEL PARITY CHECK DOES NOT CATCH IT. Their existing parity
suite was GREEN throughout the drift, because it compares signatures and never
bodies, and both override parsers are private -- reached only from inside
`sync_mirror`. Signature parity is satisfied by two functions that disagree
about what every input MEANS.

WHAT THIS TEST DOES. Nothing, while the seam is shut: a `setup/publish_sync.py`
that does not exist cannot fork. The moment one lands, this fails and names the
pin that must land with it -- so whoever lands that C1 meets the obligation from
the suite rather than from having read a brief or a cross-session exchange.
Cross-repo tripwire, same finding, carrying the pin shape and the mutation
check: DoE-claude
`coordinator/docs/wiki/coordinator-tripwires/a-per-root-override-forks-behaviourally-under-green-signature-parity.md`.

NEGATIVE SPEC for the pin this test demands, inherited from the sibling's own
hard-won version -- a pin written without these is a pin that passes vacuously:

  - Compare what an operator's env VALUE MEANS, across every arm the parser
    distinguishes: unset, empty, the blanket literal, one name, several names,
    and the whitespace/empty-element shapes a hand-typed list actually arrives
    in. Not signatures, not source text.
  - Canonicalise the container before comparing, so a frozenset-vs-set
    difference is not read as a behavioural difference.
  - MUTATION-CHECK THE PIN. Green proves nothing here -- a green parity suite
    is the whole defect being guarded against. Revert the copy's parser to the
    pre-fix `== "1"` form and confirm the pin goes red before believing it. It
    is easy to write a vacuous one by accident: derive the env-var name wrong
    and both sides read an unset variable, agree on False for every input, and
    pass.

THE GENERAL RULE THIS IS AN INSTANCE OF, and the right page to read before
writing any guard: DoE-claude
`coordinator/docs/wiki/coordinator-tripwires/a-green-that-never-touched-its-subject.md`
-- "a check that cannot fail is not a check; it emits a green nothing
produced." Its anatomy list gained the DERIVED-LOCATOR clause on 2026-08-26
(DoE `ab457339e`) off two independent failures the same afternoon: an env-var
name derived from a symbol name, and this module's own repo-root derived off by
one. Both are distinct from the flaws already on that list, which are flaws in
the SUBJECT or the ASSERTION; this one is a flaw in how the check ADDRESSES its
subject -- the locator resolves nowhere, so both sides read the same nothing and
agree.

Claude-Klabauter's own `state/lessons/2026-08-26-a-green-test-can-be-pinning-nothing-
mutat-4bd02ab3daa0.yaml` names two sibling shapes (the subject moved out from
under a still-true assertion; the guard was never pinned at all) and does NOT
name the derived-locator one -- the tripwire page above is its home, cited here
rather than copied, because a second drifting copy on the subject of copies that
drift is the joke writing itself.
"""

from __future__ import annotations

from pathlib import Path

#: `parents[4]`, counted rather than eyeballed: this file sits at
#: `coordinator/lib/percolate/tests/`, so [0]=tests [1]=percolate [2]=lib
#: [3]=coordinator [4]=repo root. An off-by-one here points the guard at
#: `coordinator/setup/publish_sync.py`, a path that can never exist, and the
#: guard then passes forever against a seam it is not watching. That is not
#: hypothetical -- this module's first form had exactly that bug, and it was
#: caught only by the mutation check the docstring demands, not by the suite.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_FORK_CANDIDATE = _REPO_ROOT / "setup" / "publish_sync.py"

_PIN_MARKER = "override parsers behaviourally against the engine module"


def _pin_exists() -> bool:
    """A pin is any test in this directory declaring itself as the seam's
    behavioural parity pin via the marker string. Matched on content rather
    than filename so the pin can be named whatever its author finds clearest."""
    for candidate in Path(__file__).parent.glob("test_*.py"):
        if candidate == Path(__file__):
            continue
        if _PIN_MARKER in candidate.read_text(encoding="utf-8"):
            return True
    return False


def test_a_claude_klabauter_side_publish_sync_fork_arrives_with_a_behavioural_pin():
    if not _FORK_CANDIDATE.exists():
        return

    assert _pin_exists(), (
        f"{_FORK_CANDIDATE.relative_to(_REPO_ROOT)} now exists, so it -- not the engine "
        "module -- is what `_import_publish_sync` loads for a claude-klabauter-rooted run, and it "
        "can fork from `coordinator/lib/percolate/publish_sync.py` without any signature "
        "changing. Land a behavioural parity pin in this directory alongside it, "
        "declaring itself with the marker string "
        f"{_PIN_MARKER!r}, and read this module's docstring first: it carries the negative "
        "spec (compare meanings across every arm, canonicalise the container, and "
        "mutation-check the pin against the pre-fix `== \"1\"` form -- a pin that stays "
        "green against that form is pinning nothing). Measured precedent: DoE-claude's "
        "copy forked exactly this way under a green signature-parity suite, and its "
        "operator-facing diagnostic then advertised a blanket sweep disarm as the remedy."
    )

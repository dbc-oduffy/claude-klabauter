"""
coordinator_core.reviewer_vocabulary — the closed delegate-reviewer vocabulary.

Purpose: holds the single definition of "which agent types count as a delegate
reviewer", so that every consumer answers that question from one set instead of
its own copy. Imports nothing outside the stdlib — and nothing at all today —
which is the whole point of the module existing.

Why it is not in ``review_trail_write`` any more (C9,
docs/plans/2026-08-27-the-review-gate-measures-the-whole-session.md): the
vocabulary is eight short strings, but ``review_trail_write``'s module body and
dependency closure cost **34.4ms** to import in a process that has already
loaded ``subagent_sandbox.provision_report`` (61.4ms cold, measured k=5 median).
``provision_report._provision`` is a PreToolUse-Agent hook — a fresh interpreter
on EVERY agent dispatch, fleet-wide — and it must consult this vocabulary on
every one of them to decide whether to stamp a review receipt. Paying 34.4ms
per dispatch to read a frozenset is the same defect the governing plan already
rejected at 101.4ms for ``commit_ledger.resolve_owner.resolve_owner_handoff_id``,
reached through a different import.

Measured marginal cost to reach this vocabulary from ``_provision``, n=15 per
placement, median / max:

    ops.review_trail_write (before C9)          34.4ms            blown 34x
    coordinator_core.ops.reviewer_vocabulary    0.746 / 1.158ms   tail BREACHES
    coordinator_core.reviewer_vocabulary        0.106 / 0.535ms   holds

**Why this module is NOT under ``coordinator_core/ops/``, next to the
vocabulary's other consumer.** Importing it there executes
``coordinator_core/ops/__init__.py`` first, which measured ~0.64ms of that
0.746ms — the package init, not this file, was almost the whole cost, and it
pushed the tail past AC12b's 1ms ceiling. Placement is load-bearing here, not
housekeeping: relocating this file into ``ops/`` for tidiness would silently
reintroduce a ceiling breach that only a max-of-n measurement would catch.
``review_trail_write`` pays that init regardless (it lives there), so importing
upward from it costs nothing.

Negative spec — what this module must never grow:

- **No imports beyond the stdlib, and preferably none at all.** The cost profile
  above is the entire reason it exists. A single import of a coordinator module
  can silently reintroduce the 34.4ms this extraction removed, and the caller
  that pays it is a hook nobody profiles. If something here needs a helper,
  inline it.
- **No second, diverging reviewer set.** Consumers reuse ``DELEGATE_REVIEWERS``
  by name. Three independent copies of "what counts as a reviewer" drifting
  apart is the failure this module was extracted to make impossible, not a
  freedom it grants.
- **No move into a package with a non-trivial ``__init__``.** See the table
  above: the same file under ``ops/`` costs 7x more and breaches the bound.
- **No logic.** Data only. A predicate belongs with its caller, where the
  namespace-stripping convention that caller uses is visible.

Membership is load-bearing twice over, and the second one is easy to miss:
admitting an agent type here admits it to ``review_trail_write``'s ``reviewer``
enum AND arms a durable ``commit_ledger.store.mark_reviewed`` write for that
agent type. Kept as ONE set deliberately — ``coverage.py`` credits on the
record's ``kind``, never on reviewer identity, and ``reviewed_by`` stores the
reviewer's NAME, so a consumer that wants to weigh a ``staff-ux`` pass
differently from a ``staff-eng`` one already has the data to. Splitting the sets
would buy a maintained divergence against a consumer that does not exist.

Pinned by ``coordinator_core/ops/tests/test_review_trail_write.py ::
test_delegate_reviewers_arms_the_commit_ledger_mark``, which reads the name
through ``review_trail_write`` — so that test keeps covering this set through
the re-export, and a rename here that broke the alias would go red there.

Spelled BARE, never namespaced: consumers strip a ``coordinator:``/``agent:``
prefix before testing membership (``review_trail_write.normalize_reviewer``,
``provision_report._bare_agent_type``, ``hooks/subagent_review_mark.py::
_bare_type`` all do this for the identical purpose).
"""

#: The closed set of agent types whose dispatch counts as a delegate review.
#: Re-exported by ``review_trail_write`` as ``_DELEGATE_REVIEWERS`` for every
#: existing by-name consumer; new consumers should import it from here.
DELEGATE_REVIEWERS = frozenset(
    {
        "code-reviewer",
        "staff-eng",
        "code-reviewer+staff-eng",
        "eng-director",
        "senior-front-end",
        "staff-ux",
        "staff-data-sci",
        "ubt-compile",
    }
)

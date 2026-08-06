"""
coordinator_core.session.fast_tier_declaration -- the R6 fast-tier
declaration, read in exactly one place.

Purpose: own the ``fast_tier_unscoped_reason`` frontmatter key -- its
spelling, its "what counts as declared" rule (non-empty after strip), and
the frontmatter read that resolves it -- on behalf of the AUTHORITY layer,
so that no shape classifier ever has to.

Two authority-layer consumers read this declaration, and they are the only
two legitimate ones:

  - ``coordinator_core.session.tier_u_gate`` -- the resolve-and-execute CLI
    seam. It owns the R6 declaration EXIT (the decision that a covered
    command proceeds without a Tier-U grant); this module only hands it the
    declaration's text. See ``tier_u_gate._fast_tier_unscoped_declaration_
    covers`` for the exit's narrow reach.
  - ``coordinator_core.bash_guards.check_test_suite_invocation.check`` --
    the ``PreToolUse(Bash)`` hard-deny guard's authority leg, which pairs
    this declaration with its own token-level match test
    (``_matches_declared_fast_test_cmd``) rather than the gate's exact-
    string one.

Spec backlink: DR-088 R6/R7 -- cross-repo/archive/2026-07-25-example-doctrine-repo-em-
dr088-marker-scope-ruling.md.

Why this is its own module rather than a helper inside ``tier_u_gate``:
``tier_u_gate`` imports the shape classifier
(``check_test_suite_invocation``) at module scope, so the classifier module
cannot import ``tier_u_gate`` back to reach a declaration reader living
there -- not even lazily, because R7's structural guard
(``test_tier_u_gate.TestClassifierMustNeverLearnTheDeclarationKey``) is a
rule about KNOWLEDGE, and a classifier module reaching into the gate for
the answer would satisfy the letter of an import check while breaking the
rule it pins. A leaf module both authority layers depend on, and the
classifier does not, is the seam that actually holds: the dependency edges
run authority -> declaration, never shape -> declaration.

Negative-spec:
  - This module NEVER classifies. It answers "what did this repo declare",
    never "what shape is this command" and never "may this caller run it."
    Both of those live with their respective owners above.
  - It is NEVER imported by
    ``coordinator_core.bash_guards.check_test_suite_invocation``'s
    classification surface (``classify_command``, ``classify_runner_
    footprint``, ``_classify_command_core`` and everything they reach).
    Only ``check()``'s authority leg may consume it, and only lazily,
    matching that module's existing discipline for authority lookups
    (``_tier_u_grant``, ``_mutex_holder``). A classifier that returned
    Tier F because a repo declared an exemption would reinstate the
    provenance-laundering the R7 fast-leg fix removed, in a new costume.
  - It does NOT decide what the declaration COVERS. Reach is the
    consumer's call, deliberately: the two consumers apply deliberately
    different match tests (exact resolved-string equality at the CLI seam,
    token-normalized containment at the hook seam) and collapsing them
    here would silently change one of them.
"""

from __future__ import annotations

import os
from typing import Optional

from coordinator_core.resolve_validation_cmd import cs_read_local_md_key

#: The R6 frontmatter key, spelled once. Every other module that needs it
#: goes through the reader below rather than re-spelling it.
FAST_TIER_UNSCOPED_REASON_KEY = "fast_tier_unscoped_reason"


def fast_tier_unscoped_declaration(repo_root: Optional[str]) -> str:
    """The non-empty R6 declaration prose this repo carries in its
    ``coordinator.local.md`` frontmatter, or ``""`` when the key is absent,
    empty, or whitespace-only.

    ``repo_root`` defaults to the process cwd when ``None`` or empty,
    matching every other repo-root-taking helper on this path. The return
    is stripped, so "declared" means exactly "non-empty after strip" for
    both consumers -- the one definition of the term, in one place.
    """
    root = repo_root if repo_root else os.getcwd()
    return cs_read_local_md_key(root, FAST_TIER_UNSCOPED_REASON_KEY).strip()

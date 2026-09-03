"""No write-guard may OFFER an operator a retired cross-repo-memo invocation.

WHY THIS EXISTS. `cross-repo-memo`'s one-shot flag form (`--to/--topic/--title/--body-file`,
and its stdin variant) was retired in favour of the `draft` -> `send` lifecycle. `argparse`
rejects it outright:

    cross-repo-memo: error: unrecognized arguments: --to ... --topic ...

Five write-guards were still emitting that form as their REMEDIATION -- and these guards fire
precisely when an operator hand-rolls a memo file. So the guard caught the anti-pattern and
then handed back a command that errors. That is worse than staying silent: it spends the
operator's time and reads as the guard being broken rather than the advice being stale.

One of those sites was a TEST asserting `"cross-repo-memo --to" in reason` -- it pinned the
dead shape in place rather than catching it, which is why per-site fixes alone do not close
this. Found by `doe-claude-cb`, 2026-08-26, who hit the same class on their own surfaces;
their half is pinned by `test_memo_channel_has_no_one_shot_fallback.py`.

WHY THIS RENDERS MESSAGES RATHER THAN GREPPING THE TREE. The first version of this test
grepped every `.py`/`.md` in the repo for the retired shape. It drowned: the CLI's own error
strings (`"cross-repo-memo: --summary is N chars"`), prose mentioning a flag by name
(`# cross-repo-memo's --body-file`), and bash-guard fixtures using a memo command as
incidental sample text all matched, and none of them offers anybody anything. A grep cannot
distinguish "hands an operator a command to run" from "mentions a flag". Rendering the
guards' actual output can, because a remediation is exactly the thing that gets rendered.

NEGATIVE SPEC. This does not assert the live grammar is CORRECT -- `bin/cross-repo-memo`'s
own `--help` is the authority, and restating it here would be a second source of truth that
drifts. It asserts only that no RENDERED guard message offers a retired shape.
"""

import pathlib
import re

from coordinator_core.write_guards import block_home_dir_memo_delivery as home_dir
from coordinator_core.write_guards import block_oss_mirror_memo_delivery as oss_mirror
from coordinator_core.write_guards import validate_frontmatter_schema_deny as deny

#: `--topic` and `--body-file` are accepted by no verb -- `draft` takes TOPIC positionally.
_DEAD_FLAGS = ("--topic", "--body-file")
_SEND_SHAPED = ("--to", "--title", "--kind", "--summary")
_VERBS = ("draft", "send", "compose", "list", "discard", "reconcile")

#: Matches a rendered invocation line, i.e. one an operator is meant to copy. The colon form
#: (`cross-repo-memo: ...`) is the CLI reporting about itself and is deliberately not one.
_INVOCATION = re.compile(r"cross-repo-memo(?!:)(?!'s)([^\n]*)")


def _retired_offers(message):
    """Every retired invocation this message hands an operator."""
    out = []
    for match in _INVOCATION.finditer(message):
        tail = match.group(1)
        for dead in _DEAD_FLAGS:
            if dead in tail:
                out.append((dead + " accepted by no verb", match.group(0).strip()))
                break
        else:
            has_verb = any(re.search(r"\b" + v + r"\b", tail) for v in _VERBS)
            if not has_verb and any(f in tail for f in _SEND_SHAPED):
                out.append(("send-shaped flags, no verb", match.group(0).strip()))
    return out


def _rendered_guard_messages():
    """(label, text) for every operator-facing memo remediation this repo emits."""
    yield "deny._memo_offer_message", deny._memo_offer_message()
    yield (
        "deny._own_inbox_deny_message",
        deny._own_inbox_deny_message("claude-klabauter-em", "doe-claude-em"),
    )
    yield (
        "deny._own_inbox_deny_message(no-to)",
        deny._own_inbox_deny_message("claude-klabauter-em", None),
    )
    yield (
        "deny._memo_routing_offer_message",
        deny._memo_routing_offer_message("doe-claude-em"),
    )
    yield (
        "deny._memo_routing_offer_message(unresolved)",
        deny._memo_routing_offer_message(None),
    )
    yield "home_dir._deny_reason", home_dir._deny_reason("~/some-memo.md")
    yield "oss_mirror._deny_reason", oss_mirror._deny_reason("<publish-mirror>")


def test_no_guard_offers_a_retired_invocation():
    offenders = []
    for label, message in _rendered_guard_messages():
        for reason, line in _retired_offers(message):
            offenders.append("{}: {}{}      {}".format(label, reason, chr(10), line))

    assert not offenders, (
        "write-guard(s) hand the operator a cross-repo-memo invocation that argparse "
        "rejects:" + chr(10) + "  " + (chr(10) + "  ").join(offenders) + chr(10) * 2
        + "Use the lifecycle instead:" + chr(10)
        + '  cross-repo-memo draft <slug> --to <receiver-em> --title "..."' + chr(10)
        + "  cross-repo-memo send <slug>"
    )


def test_every_guard_message_actually_offers_the_live_lifecycle():
    """The complement, and the reason the test above cannot pass vacuously.

    Deleting a remediation entirely would satisfy `test_no_guard_offers_a_retired_invocation`
    perfectly. A guard that names the anti-pattern and then offers nothing is a smaller
    failure than one offering a dead command, but it is still a failure -- these fire at the
    moment an operator needs the working command most.
    """
    for label, message in _rendered_guard_messages():
        assert "cross-repo-memo draft" in message, label
        assert "cross-repo-memo send" in message, label


def test_the_detector_would_catch_the_shapes_it_is_named_for():
    """Mutation guard: an assertion that cannot fail is the defect it is written against.

    Five guards sat green while emitting a dead command, because nothing rendered them.
    A detector proven only against messages it has already fixed is in that same position.
    """
    for sample in (
        'cross-repo-memo --to doe-claude-em --topic slug --title "x"',
        "cross-repo-memo --to <em> --topic <slug> --body-file body.md",
        'cross-repo-memo --kind proposal --to doe-claude-em --title "x"',
        'cross-repo-memo --to doe-claude-em --title "x" < body.md',
        "cross-repo-memo draft <slug> --to <em> --body-file body.md",
    ):
        assert _retired_offers(sample), sample


def test_the_detector_does_not_flag_the_live_grammar_or_self_reference():
    """Each of these was a false positive in the grep-the-tree first draft."""
    for sample in (
        'cross-repo-memo draft <slug> --to doe-claude-em --title "x"',
        "cross-repo-memo send <slug>",
        "cross-repo-memo compose <slug>",
        "cross-repo-memo list",
        "cross-repo-memo reconcile --apply",
        "cross-repo-memo --supersedes <path> send <topic>",
        "cross-repo-memo --list-receivers",
        "cross-repo-memo --check-addressee doe-claude-em",
        'cross-repo-memo draft <slug> --to <em> --title "x" --kind fyi --summary "s"',
        # The CLI reporting on its own flags -- not an offer to run anything.
        "cross-repo-memo: --summary is 174 chars, cap is 120",
        "cross-repo-memo: --body-file 'x.md' was empty",
        # Prose naming a flag.
        "resolves cross-repo-memo's --to value via the registry",
    ):
        assert not _retired_offers(sample), (sample, _retired_offers(sample))


# --- Live doc surfaces -------------------------------------------------------
#
# The guard-message tests above cover what a GUARD hands an operator. They do not
# cover what a DOC hands one, and `doe-claude-cb` found two live doc surfaces still
# offering the retired form after the code half was clean -- including
# `cross-repo/README.md`, which had it under a heading reading "Legacy one-shot".
#
# That shape is the dangerous one and it is why docs get their own test: a dead
# alternative printed directly beneath a working one is worse than no alternative.
# It is what an EM reaches for when the primary path refuses, which is exactly the
# moment they cannot tell a stale doc from a broken tool. Four sessions lost an
# evening to it on 2026-08-25/26.
#
# The first version of this file exempted `cross-repo/` WHOLESALE, to avoid rewriting
# memos other repos authored. That exemption was too wide and is what let
# `cross-repo/README.md` through -- the README is ours, the inbox bodies are not.
# Scope the exemption to the received bodies, never to the directory.

_REPO_ROOT_DOCS = pathlib.Path(__file__).resolve().parents[3]

#: Surfaces that INSTRUCT, scoped by allow-list rather than by exempting frozen ones.
#:
#: A deny-list was tried twice and failed twice. The first exempted `cross-repo/` wholesale
#: to protect memo bodies other repos wrote, and that is exactly what let
#: `cross-repo/README.md` through -- the README is ours, the inbox bodies are not. The second
#: added `docs/` and drowned in `docs/plans/`, which RECORD what a past session did and are
#: paper trail in the same sense as `archive/`: rewriting one falsifies the record.
#:
#: The property worth pinning is narrow and enumerable -- a doc that TEACHES the memo workflow
#: must not teach a dead command. Reference docs and READMEs teach; plans and research record.
#: An allow-list states that distinction instead of approximating it by subtraction.
_INSTRUCTIONAL_GLOBS = (
    "bin/*.md",
    "state/cross-repo/*.md",
    "state/cross-repo/*/README.md",
    "docs/reference/*.md",
    "*.md",
)


def _instructional_docs():
    seen = set()
    for glob in _INSTRUCTIONAL_GLOBS:
        for path in _REPO_ROOT_DOCS.glob(glob):
            if path.is_file() and path not in seen:
                seen.add(path)
                yield path, path.relative_to(_REPO_ROOT_DOCS)


def test_no_live_doc_offers_a_retired_invocation():
    offenders = []
    for path, rel in _instructional_docs():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "cross-repo-memo" not in text:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for reason, snippet in _retired_offers(line):
                offenders.append(
                    "{}:{}: {}{}      {}".format(rel, lineno, reason, chr(10), snippet)
                )

    assert not offenders, (
        "live doc surface(s) offer a RETIRED cross-repo-memo invocation:"
        + chr(10) + "  " + (chr(10) + "  ").join(offenders) + chr(10) * 2
        + "A dead alternative printed beneath a working one is what an EM reaches for "
        "when the primary refuses. Delete it rather than labelling it legacy."
    )


def test_the_allowlist_covers_the_surfaces_this_was_found_on():
    """Regression pin: both files `doe-claude-cb` found must be in scope.

    The channel README carried the dead form under a heading reading "Legacy
    one-shot" -- a fallback presented as live, directly beneath the working path.
    An allow-list that silently stopped matching it would restore the blind spot
    without failing anything.

    The two channel surfaces moved to `state/cross-repo/` in f24febad50, which
    retired the legacy root outright rather than leaving a second home. The pin
    follows them; naming the retired paths here would fail on their absence, not
    on a narrowed allow-list.
    """
    covered = {rel.as_posix() for _, rel in _instructional_docs()}
    for required in (
        "state/cross-repo/README.md",
        "bin/cross-repo-memo.md",
        "state/cross-repo/inbox/README.md",
        "docs/reference/em-callable-ops.md",
    ):
        assert required in covered, (required, sorted(covered)[:20])


def test_the_allowlist_excludes_the_record_surfaces():
    """Plans and research RECORD; rewriting one falsifies what a session did."""
    covered = {rel.as_posix() for _, rel in _instructional_docs()}
    assert not any(c.startswith("docs/plans/") for c in covered)
    assert not any(c.startswith("docs/research/") for c in covered)
    assert not any(c.startswith("archive/") for c in covered)
    assert not any(c.startswith("tasks/") for c in covered)
    # Memo bodies other repos authored.
    assert not any(
        c.startswith("state/cross-repo/inbox/") and not c.endswith("README.md") for c in covered
    )

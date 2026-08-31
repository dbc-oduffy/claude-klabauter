# -*- coding: utf-8 -*-
"""Baseline falsifier for docs/plans/2026-08-31-prose-flags-travel-as-files-through-the.md.

Enumerates every `coordinator/bin/*.py` entrypoint that has a `.cmd` launcher
sibling and DECLARES a prose-bearing flag as a value-taking parse site, then
prints one line per flag failing Rule 1 (an unconditional, flag-scoped newline
refusal). Exit 1 while any UNREFUSED flag remains, 0 when none do.

WHY "DECLARED" IS THE LOAD-BEARING WORD. A first cut matched any quoted
`"--flag"` token anywhere in the source and reported 15 pairs. Two were false
positives, and both are false in ways worth naming, because a falsifier that
counts non-defects makes its own target unreachable by correct work:

  - `workweek-start-goal-and-priorities --text` has no `add_argument` at all.
    The token is a list element built for `subprocess.run([sys.executable,
    str(append_goal_event), ..., "--text", text])` -- an EMITTER, not a parse
    site. A list-form spawn has no shell and no `.cmd` in it, so there is no
    corrupting transport; the real parse site is `append-goal-event.py`, which
    this probe reports in its own right.
  - `reap-integrated-review-findings --summary` is `action="store_true"`.
    A boolean carries no payload to corrupt.

Both were caught by a coverage reviewer reading the sources this probe only
pattern-matched, and both are the same defect in the instrument: matching a
MENTION rather than a DECLARATION. The `declared()` predicate below is the fix.

// Review: staff-eng (the Staff Engineer) -- the scoring predicate below was corrected
// twice more after that first fix, both caught by staff-eng plan review:
//
// 1. OR-vs-AND (critical). The original scan() marked a flag covered if
//    EITHER it had a `--<flag>-file` sibling OR the WHOLE FILE contained any
//    newline-refusal call -- a disjunction, where the prime exit criterion is
//    a conjunction per flag (refuse AND, where reachable, offer a file leg).
//    An oracle whose predicate is weaker than its criterion cannot falsify
//    it: `total uncovered: 0` was reachable with Rule 1 discharged for none
//    of the flags. Fixed by scoring UNREFUSED (Rule 1, unconditional, gates
//    the exit code) as its own column, independent of any file-leg check.
// 2. Whole-file `refuses` substring (critical). `"refuse_newline_argv" in
//    src` matched anywhere in the file, so ONE refusal call for any flag in
//    an entrypoint marked EVERY prose flag in that file covered -- verified,
//    this would have gone green for `--title` on `cross-repo-memo.py` the
//    moment `--summary` alone was wired, no code touching `--title`. Fixed
//    by `flag_refused()`, which scopes the match to a `refuse_newline_argv(`
//    call naming this specific flag (via `flag_name="--<flag>"` or the flag
//    literal within the call's own parens). The `"contains a newline"` prose
//    fallback is dropped entirely -- it matched help text and docstrings and
//    cannot be flag-scoped.
//
// Rule 2 (a `--<flag>-file` sibling, owed only where a newline/quote is
// legitimately reachable) is NOT gated by this probe. Which flags Rule 2
// applies to is a per-site judgment the plan has not yet reduced to a
// checkable predicate (staff-eng finding: "a quote is legitimately
// reachable" is not a named foreclosure) -- escalated back to the plan
// rather than invented here. `scan()` still reports an UNLEGGED column,
// informationally, so a reader can see which declared prose flags lack a
// file-leg sibling today; it does not gate the exit code.

Kept deliberately static (regex over each CLI's own source) rather than
importing and introspecting: these are ~130 heterogeneous CLIs with argparse,
hand-rolled token loops, and positional grammars, several of which perform real
side effects at import. See `coordinator_core/ceremony_common/
test_argv_prog_slot_contract.py` for the same reasoning on a neighbouring guard.

Run:  python coordinator/bin/tests/prose_transport_probe.py [repo_root]
"""
import os, re, sys
PROSE = ("body","note","notes","reason","message","summary","title",
         "description","text","old-string","new-string","rationale","why",
         "objective")

def declared(src, flag):
    """A flag is DECLARED as a value-taking parse site iff it is either an
    argparse add_argument that is not a boolean, or a hand-rolled token
    comparison. A bare string in a list literal is an EMITTER, not a parse
    site — it builds argv FOR another process and has no transport of its
    own to corrupt.

    // Review: staff-eng (the Staff Engineer) -- the declaration span used to be capped
    // at 400 chars via a non-greedy `.{0,400}?` group, which (a) silently
    // dropped any add_argument call whose text ran longer (this corpus's
    // own multi-line help strings), and (b) being non-greedy, could stop at
    // the FIRST literal `)` inside the span -- e.g. a parenthesised help
    // string ahead of `action=` -- truncating the boolean check away from
    // its own `action=` kwarg. Fixed by bounding the span to the next
    // `add_argument(` call (or EOF) instead of a character budget, and by
    // searching that whole span for `action=store_(true|false)` rather than
    // a bare substring on a truncated slice.
    """
    for m in re.finditer(r'add_argument\(\s*["\']--%s["\']' % re.escape(flag), src):
        start = m.end()
        nxt = re.search(r'add_argument\(', src[start:])
        end = start + nxt.start() if nxt else len(src)
        span = src[start:end]
        if not re.search(r'action\s*=\s*["\']store_(true|false)["\']', span):
            return True
    # hand-rolled: compared against, indexed, or membership-tested
    for pat in (r'==\s*["\']--%s["\']', r'["\']--%s["\']\s*==',
                r'\.index\(\s*["\']--%s["\']', r'["\']--%s["\']\s+in\s',
                r'get\(\s*["\']--%s["\']', r'\[\s*["\']--%s["\']\s*\]'):
        if re.search(pat % re.escape(flag), src):
            return True
    return False

def _refusing_helpers(src):
    """Names of same-file functions whose own body calls `refuse_newline_argv`.

    A file that centralises its refusal in one helper -- which is the shape
    this plan's C1 pushes every entrypoint toward -- passes the flag name to
    the HELPER, and the helper passes a variable to `refuse_newline_argv`.
    Nothing in that file ever writes `refuse_newline_argv(..., "--reason")`,
    so a literal-match predicate reports the flag unrefused while it is in
    fact refused on every invocation.
    """
    names = []
    defs = [(m.start(), m.group(1)) for m in re.finditer(r'^def (\w+)\(', src, re.M)]
    for i, (pos, name) in enumerate(defs):
        end = defs[i + 1][0] if i + 1 < len(defs) else len(src)
        if "refuse_newline_argv" in src[pos:end]:
            names.append(name)
    return names

def flag_refused(src, flag):
    """Rule 1: an unconditional newline refusal SCOPED to this flag.

    Two shapes count, and they are the same claim reached two ways:

      direct    -- `refuse_newline_argv(...)` naming the flag, via
                   `flag_name="--<flag>"` or the flag literal inside the
                   same call's parens.
      delegated -- the flag literal passed to a same-file helper whose own
                   body calls `refuse_newline_argv` (see `_refusing_helpers`).

    Does NOT match a refusal call for a DIFFERENT flag elsewhere in the file
    -- that whole-file disjunction was the instrument's second defect (see
    module docstring), and neither shape above reintroduces it: both require
    this flag's own literal at the call site.

    // Review: staff-eng (the Staff Engineer) found the whole-file OR. The delegated arm
    // was added after HIS correction, because the corrected predicate then
    // reported `archive-stamp-cli --reason` UNREFUSED -- a flag verified at
    // source to be refused on every invocation, through
    // `_resolve_prose(tail, flag)` -> `refuse_newline_argv(inline,
    // flag_name=flag)`. That is a false positive in the GATING column, and
    // it is the expensive direction: the criterion "0 unrefused" becomes
    // unreachable by correct code, and an executor driving toward it would
    // inline a redundant refusal beside a working one to satisfy a regex --
    // the falsifier dictating worse code, which is the failure this
    // instrument exists to prevent. It does not trace call graphs; it credits
    // one level of same-file delegation, which is the only level the shape
    // this plan mandates actually uses.
    """
    direct = r'refuse_newline_argv\([^)]*(?:flag_name\s*=\s*["\']--%s["\']|["\']--%s["\'])' % (
        re.escape(flag), re.escape(flag))
    if re.search(direct, src, re.S):
        return True
    for helper in _refusing_helpers(src):
        delegated = r'\b%s\s*\([^)]*["\']--%s["\']' % (re.escape(helper), re.escape(flag))
        if re.search(delegated, src, re.S):
            return True
    return False

def flag_legged(src, flag):
    """Rule 2 file-leg presence (informational only -- see module docstring:
    which flags Rule 2 applies to is not yet a checkable predicate)."""
    return bool(re.search(r'["\']--%s-file["\']' % re.escape(flag), src))

def scan(root):
    bd = os.path.join(root, "coordinator", "bin")
    cmds = {os.path.splitext(f)[0] for f in os.listdir(bd) if f.endswith(".cmd")}
    unrefused, unlegged = [], []
    for fn in sorted(os.listdir(bd)):
        if not fn.endswith(".py"): continue
        stem = fn[:-3]
        if stem not in cmds: continue
        src = open(os.path.join(bd, fn), encoding="utf-8", errors="replace").read()
        for flag in PROSE:
            if not declared(src, flag): continue
            if not flag_refused(src, flag):
                unrefused.append((stem, "--" + flag))
            if not flag_legged(src, flag):
                unlegged.append((stem, "--" + flag))
    return unrefused, unlegged

unrefused, unlegged = scan(sys.argv[1] if len(sys.argv) > 1 else ".")
for s, f in unrefused: print("UNREFUSED %-42s %s" % (s, f))
for s, f in unlegged: print("UNLEGGED  %-42s %s (informational -- Rule 2 not gated, see docstring)" % (s, f))
print("total uncovered (entrypoint, flag) pairs: %d" % len(unrefused))
sys.exit(1 if unrefused else 0)

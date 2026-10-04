"""BX-16 shape 1 body: `find ... -exec` and `for f in $(find ...)` rewrite.

Split out of `dispatch_checks.py` (hot-path line ratchet). Depends only on
`_rewrite_support` and the tokenizer/classifier, never on `dispatch_checks`.
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._command_tokenizer import (
    segments_from_tokens_with_pipe_flag as _bt_segments_from_tokens_with_pipe_flag,
    token_matches_binary as _bt_token_matches_binary,
)
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _normalize_executable_basename,
)
from coordinator_core.bash_guards._rewrite_support import (
    _advisory,
    _allow_rewrite,
    _bt_python3_invocation,
    _crlf_strip,
    _override,
)
from coordinator_core.bash_guards._shape_classifier import (
    Shape as _BT_Shape,
    classify_command as _bt_classify_command,
)


_FIND_EXEC_TRANSLATABLE_VERBS = frozenset({"rm", "cat", "wc"})

#: Verbs where `-exec VERB ARGS {} +` is byte-identical in effect to running
#: `-exec VERB ARGS {} \;` once per match -- a per-verb property, answered by
#: measurement, never by rule. Deliberately a SEPARATE table from
#: `_FIND_EXEC_TRANSLATABLE_VERBS` above: that table hand-writes a semantic
#: equivalence to a python3 rewrite (a harder, more error-prone claim -- all
#: three shipped entries were found wrong by measurement); this table only
#: asks "does the '+' form's OUTPUT match the concatenation of the ';'
#: form's per-match output", which is the narrower, purely batch-equivalence
#: question. Widening one table by reasoning about the other is the mistake
#: this module comment exists to head off.
#:
#: Measured (GNU findutils 4.11.0, this session, three files, no ARG_MAX
#: pressure) 2026-08-31:
#:   rm, cat, chmod, chown, touch, git add -- `+` output identical to `;`.
#:   head, tail -- `+` prepends '==> path <==' banners the `;` form never
#:     emits.
#:   wc -- `+` appends a grand-total line the `;` form never emits.
#:   grep -- multi-operand `+` prefixes every hit with 'path:' the
#:     single-operand `;` form never emits.
#: Anything not on this allowlist fails safe to the unchanged prose advisory
#: below -- never a guessed batch form.
_FIND_EXEC_BATCH_EQUIVALENT_VERBS = frozenset(
    {"rm", "cat", "chmod", "chown", "touch", "git"}
)


def _bt_parse_find_exec_segment(tokens: List[str]) -> Optional[Dict[str, Any]]:
    """`tokens` is one already-tokenized SEGMENT (post
    `segments_from_tokens_with_pipe_flag`) whose first token is a `find`
    invocation. Returns a parsed
    ``{"path", "name_pattern", "only_files", "exec_argv"}`` dict, or `None`
    if this segment carries no `-exec` this function can confidently
    isolate.

    A `;`-terminated `-exec ARGV ;` is the common case, and its terminator
    is NEVER visible inside `tokens` here: `tokenize_full_command` treats
    `;` as an always-separate punctuation token regardless of the shell's
    OWN escaping (`\\;` and bare `;` tokenize identically), so
    `segments_from_tokens_with_pipe_flag` has already consumed it as a
    segment BOUNDARY before this function ever sees the segment -- the
    segment's own end IS the terminator in that case, there is no
    remaining `;` token to search for. Only the `+`-terminated form
    (`-exec ARGV +`) leaves its terminator inside the segment, since `+` is
    not one of the tokenizer's punctuation/separator characters. So: stop
    `exec_argv` at a literal `+` token if one appears, else take the
    segment's own remainder as `exec_argv` (the semicolon-consumed case)."""
    if "-exec" not in tokens:
        return None
    exec_idx = tokens.index("-exec")
    pred = tokens[1:exec_idx]
    path = "."
    i = 0
    if pred and not pred[0].startswith("-"):
        path = pred[0]
        i = 1
    name_pattern: Optional[str] = None
    only_files = False
    while i < len(pred):
        tok = pred[i]
        if tok == "-name" and i + 1 < len(pred):
            name_pattern = pred[i + 1]
            i += 2
            continue
        if tok == "-type" and i + 1 < len(pred):
            only_files = pred[i + 1] == "f"
            i += 2
            continue
        i += 1
    rest = tokens[exec_idx + 1:]
    plus_idx = rest.index("+") if "+" in rest else None
    exec_argv = rest[:plus_idx] if plus_idx is not None else rest
    terminator = "plus" if plus_idx is not None else "semi"
    # Strip a literal trailing ";" for the rare case it DID survive inside
    # the segment (e.g. a quoted `';'` operand -- tokenize_full_command
    # respects quoting, so a quoted semicolon is one ordinary token, not a
    # separator, and would otherwise be mistaken for part of the invoked
    # command's own arguments).
    if exec_argv and exec_argv[-1] == ";":
        exec_argv = exec_argv[:-1]
    if not exec_argv:
        return None
    return {
        "path": path,
        "name_pattern": name_pattern,
        "only_files": only_files,
        "exec_argv": exec_argv,
        "terminator": terminator,
    }




def _bt_find_exec_python_rewrite(parsed: Dict[str, Any]) -> Optional[str]:
    """Translate a parsed `-exec` invocation into a single `python3 -c`
    one-liner, when the exec'd verb is one of `_FIND_EXEC_TRANSLATABLE_
    VERBS` (rm/cat/wc -- the observed census/cleanup habit: delete matches,
    print matches, count lines across matches). Returns `None` for any
    other verb -- an arbitrary `-exec <binary>` cannot be translated
    without knowing its semantics, and this function never guesses; the
    caller falls back to an advisory rather than a false auto-rewrite."""
    verb_norm = _normalize_executable_basename(parsed["exec_argv"][0])
    if verb_norm not in _FIND_EXEC_TRANSLATABLE_VERBS:
        return None
    path = parsed["path"]
    pattern = parsed["name_pattern"]
    match_expr = (
        # fnmatchcase, not fnmatch: fnmatch.fnmatch() normalizes case via
        # os.path.normcase, which is a no-op on POSIX but lower-cases both
        # sides on Windows -- silently case-INSENSITIVE there, while `find
        # -name` (unlike `-iname`) is case-sensitive on every platform.
        "fnmatch.fnmatchcase(fn, %s)" % json.dumps(pattern) if pattern else "True"
    )
    if verb_norm == "rm":
        # `find -exec rm {} \;` prints NOTHING on success. A progress line
        # here is not a friendlier rewrite, it is a different command: an
        # operator who pipes or diffs this output gets a line the original
        # never produced. Measured against real `find` 2026-08-31 -- real
        # emitted '', this emitted '2 file(s) removed'.
        body = (
            "import fnmatch, os\n"
            "for root, dirs, files in os.walk(%s):\n"
            "    for fn in files:\n"
            "        if %s:\n"
            "            os.remove(os.path.join(root, fn))" % (json.dumps(path), match_expr)
        )
    elif verb_norm == "cat":
        # `cat` CONCATENATES; it appends nothing. `print()` added one
        # newline per file, so N matched files yielded N spurious newlines
        # and a file with no trailing newline was silently given one.
        # Measured 2026-08-31: real 'one\\ntwo\\nthree\\nfour', this
        # 'one\\ntwo\\n\\nthree\\nfour\\n'.
        body = (
            "import fnmatch, os, sys\n"
            "for root, dirs, files in os.walk(%s):\n"
            "    for fn in files:\n"
            "        if %s:\n"
            '            with open(os.path.join(root, fn), encoding="utf-8", errors="replace") as fh:\n'
            "                sys.stdout.write(fh.read())" % (json.dumps(path), match_expr)
        )
    else:  # wc -- only the `-l` (line-count) form is translated
        if "-l" not in parsed["exec_argv"][1:]:
            return None
        # TWO defects here, and the second is the one that matters. `find
        # -exec wc -l {} \;` runs wc PER FILE and prints `<count> <path>`
        # for each; a bare grand total is a different answer to a different
        # question, and a census workflow reading it gets one number where
        # it asked for a breakdown. And the total was itself wrong: `wc -l`
        # counts NEWLINE CHARACTERS, while iterating a file object yields a
        # final unterminated line as a line. Measured 2026-08-31 over two
        # files, one without a trailing newline -- real '2 ./a.txt\\n1
        # ./sub/b.txt\\n', this '4\\n'.
        #
        # Not replicated, deliberately: `wc`'s column padding (which differs
        # between GNU and BSD/msys builds, so there is no single correct
        # spelling) and the native path separator (normalized to `/`, since
        # the command being replaced is a POSIX one whose output uses it on
        # every host). Structure and counts are the contract; cosmetics are
        # not.
        body = (
            "import fnmatch, os\n"
            "for root, dirs, files in os.walk(%s):\n"
            "    for fn in files:\n"
            "        if %s:\n"
            "            p = os.path.join(root, fn)\n"
            '            with open(p, "rb") as fh:\n'
            '                n = sum(chunk.count(b"\\n") for chunk in iter(lambda: fh.read(1 << 20), b""))\n'
            '            print("%%d %%s" %% (n, p.replace(os.sep, "/")))'
            % (json.dumps(path), match_expr)
        )
    return "%s -c %s" % (_bt_python3_invocation(), shlex.quote(body))


def _bt_parse_for_loop_find(tokens: List[str]) -> Optional[Dict[str, Any]]:
    """Parse `for f in $(find <path> [-name <pat>]); do <verb> "$f"; done`.

    shell-doc-ok: quotes the bash loop shape this parser recognizes.

    C5 of `docs/plans/2026-08-31-the-batched-form-the-guard-never-offers.md`.
    The shape forks one process per match exactly as `-exec ... \\;` does, so
    the spawn-budget harm this guard exists to prevent is fully present --
    and until now fully unguarded, because `_bt_parse_find_exec_segment`
    requires a literal `-exec` token a for-loop-wrapped find never carries.
    The falsifier caught the guard's docstring CLAIMING to cover this shape
    (corrected at C3); this closes the coverage that claim asserted.

    DELIBERATELY NARROW, and the narrowness is the design. The plan's own
    `case_against` argued -- correctly -- that general command-substitution
    and loop-body parsing already belongs to
    `guard_grep_via_bash._substitutable_rewrite`, and that building a second
    general parser here would be the parallel-surface mistake. So this does
    not parse loop bodies in general. It recognises ONE canonical shape and
    returns None for everything else: a single-command body, a single
    `$f`-style operand, no pipes, no redirects, no chaining inside the body.
    Anything richer is not a batching question and is not answered here.

    Returns ``{"path", "name_pattern", "verb_argv", "var"}`` or None.
    """
    if not tokens or tokens[0] != "for" or "do" not in tokens or "done" not in tokens:
        return None
    if len(tokens) < 6 or tokens[2] != "in":
        return None

    var = tokens[1]
    do_idx = tokens.index("do")
    done_idx = tokens.index("done")
    if done_idx < do_idx:
        return None

    # --- the iterated command substitution -------------------------------
    head = tokens[3:do_idx]
    while head and head[-1] == ";":
        head = head[:-1]
    if not head or not head[0].startswith("$("):
        return None
    if head[0][2:] != "find":
        return None
    if not head[-1].endswith(")"):
        return None
    find_argv = [head[0][2:]] + head[1:]
    find_argv[-1] = find_argv[-1][:-1]
    if any("$(" in tok for tok in find_argv[1:]):
        return None

    path = None
    name_pattern = None
    i = 1
    while i < len(find_argv):
        tok = find_argv[i]
        if tok == "-name":
            if i + 1 >= len(find_argv):
                return None
            name_pattern = find_argv[i + 1].strip("'\"")
            i += 2
            continue
        if tok == "-type":
            i += 2
            continue
        if tok.startswith("-"):
            # An option this parser does not model -- refuse rather than
            # emit a rewrite that drops it. Silently changing what a
            # command matches is the one failure worse than staying quiet.
            return None
        if path is None:
            path = tok
            i += 1
            continue
        return None
    if path is None:
        return None

    # --- the loop body ---------------------------------------------------
    body = tokens[do_idx + 1:done_idx]
    while body and body[-1] == ";":
        body = body[:-1]
    if not body or ";" in body or "|" in body or "&&" in body:
        return None
    if any(tok in (">", ">>", "<", "&") for tok in body):
        return None

    deref = {"$" + var, "${" + var + "}", '"$' + var + '"', '"${' + var + '}"'}
    operand_idx = [i for i, tok in enumerate(body) if tok.strip('"') in
                   {"$" + var, "${" + var + "}"} or tok in deref]
    if len(operand_idx) != 1 or operand_idx[0] != len(body) - 1:
        # The loop variable must be the FINAL operand -- the same
        # placeholder-final precondition `_bt_find_exec_batch_rewrite`
        # applies to `{}`, and for the identical reason: anything else is
        # not the shape `-exec ... +` is equivalent to.
        return None

    verb_argv = body[:-1]
    if not verb_argv or any("$" in tok for tok in verb_argv):
        return None

    return {
        "path": path,
        "name_pattern": name_pattern,
        "verb_argv": verb_argv,
        "var": var,
    }


def _bt_for_loop_find_batch_rewrite(parsed: Dict[str, Any]) -> Optional[str]:
    """The `-exec ... +` equivalent of a parsed for-loop find, or None.

    Reuses `_FIND_EXEC_BATCH_EQUIVALENT_VERBS` -- C2's MEASURED allowlist --
    rather than minting a second table. A verb off that list gets no offer,
    exactly as it does on the `-exec` side: never a guessed batch.
    """
    verb_argv = parsed["verb_argv"]
    verb_norm = _normalize_executable_basename(verb_argv[0])
    if verb_norm not in _FIND_EXEC_BATCH_EQUIVALENT_VERBS:
        return None
    if verb_norm == "git" and (len(verb_argv) < 2 or verb_argv[1] != "add"):
        return None
    argv = ["find", parsed["path"]]
    if parsed["name_pattern"]:
        argv += ["-name", parsed["name_pattern"]]
    argv += ["-exec"] + list(verb_argv)
    # `{}` and `+` are find's own syntax, never operands to quote --
    # `shlex.join` would emit `'{}'`, which find does not recognise as the
    # placeholder.
    return "%s {} +" % (" ".join(shlex.quote(a) for a in argv),)


def _bt_find_exec_batch_rewrite(tokens: List[str], parsed: Dict[str, Any]) -> Optional[str]:
    r"""Offer the POSIX `+` batched form for a verb this session measured as
    batch-equivalent (`_FIND_EXEC_BATCH_EQUIVALENT_VERBS`), gated on `{}`
    being the FINAL token of the exec'd argv -- `+` only batches when the
    placeholder is last; a `{}` mid-argv (`-exec cmd {} -flag \;`) is not
    this shape and this function returns `None` for it, same as an
    unrecognized verb. Returns `None` (no suggestion, no rewrite) on either
    miss -- never a guessed batch form for a verb not on the allowlist."""
    exec_argv = parsed["exec_argv"]
    if not exec_argv or exec_argv[-1] != "{}":
        return None
    verb_norm = _normalize_executable_basename(exec_argv[0])
    if verb_norm not in _FIND_EXEC_BATCH_EQUIVALENT_VERBS:
        return None
    if verb_norm == "git" and (len(exec_argv) < 2 or exec_argv[1] != "add"):
        # Only `git add` is on the measured allowlist (same shape as `rm`);
        # any other git subcommand is an unmeasured claim this function
        # never guesses at.
        return None
    exec_idx = tokens.index("-exec")
    new_tokens = tokens[: exec_idx + 1] + exec_argv + ["+"]
    return shlex.join(new_tokens)


def check_find_exec_rewrite(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Auto-rewrite or advise on a per-match-fork `find` shape; None otherwise.

    A full-command rewrite (`updatedInput.command`) is only sound when the
    matched segment IS the whole command, so a segment chained with other work
    degrades to an advisory naming that segment. `-exec ... +` is already
    batched and allowed silently.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    if _override("COORDINATOR_ALLOW_FIND_EXEC", payload=payload):
        return None
    classification = _bt_classify_command(cmd)
    if classification.tokens is None:
        return None
    if not (
        classification.has_shape(_BT_Shape.FIND_EXEC_XARGS)
        or classification.has_shape(_BT_Shape.FOR_LOOP)
    ):
        return None

    segments = _bt_segments_from_tokens_with_pipe_flag(classification.tokens)
    single_segment = len(segments) == 1
    _find_exec_note = operator_override_note(
        "COORDINATOR_ALLOW_FIND_EXEC", payload=payload, git_root=git_root
    )

    for_loop = _bt_parse_for_loop_find(classification.tokens)
    if for_loop is not None:
        loop_batch = _bt_for_loop_find_batch_rewrite(for_loop)
        whole_command = classification.tokens[-1] == "done"
        verb = for_loop["verb_argv"][0]
        if loop_batch and whole_command:
            return _allow_rewrite(
                loop_batch,
                (
                    "Auto-rewritten: 'for f in $(find ...); do %s \"$f\"; done' "
                    "forks one process PER MATCH -- the same spawn storm as "
                    "'-exec ... ;'. The POSIX '+' form batches matches into as "
                    "few invocations as ARG_MAX allows, with identical output "
                    "for this verb." % (verb,)
                )
                + (" %s" % _find_exec_note if _find_exec_note else ""),
            )
        if loop_batch:
            return _advisory(
                (
                    "Advisory: 'for f in $(find ...); do %s \"$f\"; done' forks "
                    "one process PER MATCH -- '%s' does the same work in as few "
                    "invocations as ARG_MAX allows, but this loop runs alongside "
                    "OTHER work in the same command, so no full-command "
                    "auto-rewrite is offered." % (verb, loop_batch)
                )
                + (" %s" % _find_exec_note if _find_exec_note else "")
            )
        return _advisory(
            (
                "Advisory: 'for f in $(find ...); do %s \"$f\"; done' forks one "
                "process PER MATCH -- the founding-incident 879-process shape on "
                "Windows. '%s' is not on the measured batch-equivalent verb list, "
                "so no '+' form is offered: batching it could change its output. "
                "A single python3 -c os.walk(...) loop does the enumeration in "
                "one process." % (verb, verb)
            )
            + (" %s" % _find_exec_note if _find_exec_note else "")
        )
    for tokens, _pipe_before in segments:
        if not tokens or not _bt_token_matches_binary(tokens[0], "find"):
            continue
        parsed = _bt_parse_find_exec_segment(tokens)
        if not parsed:
            continue
        if parsed["terminator"] == "plus":
            # Already batched: silent allow.
            continue
        rewrite = _bt_find_exec_python_rewrite(parsed)
        if rewrite and single_segment:
            return _allow_rewrite(
                rewrite,
                (
                    "Auto-rewritten: 'find ... -exec %s ... {} ;' forks one "
                    "process PER MATCH (the founding-incident 879-process shape "
                    "on Windows) -> one python3 process, zero per-match forks."
                    % (parsed["exec_argv"][0],)
                )
                + (" %s" % _find_exec_note if _find_exec_note else ""),
            )
        batch_rewrite = _bt_find_exec_batch_rewrite(tokens, parsed)
        if rewrite and batch_rewrite:
            # The `+` form is a segment-local edit, so it survives chaining.
            return _advisory(
                (
                    "Advisory: 'find ... -exec %s ... {} ;' (segment: %s) "
                    "forks one process PER MATCH -- the POSIX '+' form (%s) "
                    "batches matches with identical output for this verb. "
                    "The python3 rewrite is not offered: this segment runs "
                    "alongside OTHER work, and replacing the whole command "
                    "would drop it."
                    % (parsed["exec_argv"][0], " ".join(tokens), batch_rewrite)
                )
                + (" %s" % _find_exec_note if _find_exec_note else "")
            )
        if rewrite:
            return _advisory(
                (
                    "Advisory: 'find ... -exec %s ... {} ;' (segment: %s) forks "
                    "one process PER MATCH -- the founding-incident 879-process "
                    "shape on Windows. A single python3 -c os.walk(...) loop "
                    "does the same enumeration in one process, but this "
                    "find-exec segment runs alongside OTHER work in the same "
                    "command (a for-loop, a chained command, or both), so no "
                    "full-command auto-rewrite is offered -- replacing the "
                    "whole command would silently drop that other work."
                    % (parsed["exec_argv"][0], " ".join(tokens))
                )
                + (" %s" % _find_exec_note if _find_exec_note else "")
            )
        if batch_rewrite and single_segment:
            return _allow_rewrite(
                batch_rewrite,
                (
                    "Auto-rewritten: 'find ... -exec %s ... {} ;' forks one "
                    "process PER MATCH -- the POSIX '+' form batches matches "
                    "into as few invocations as ARG_MAX allows, with "
                    "identical output for this verb."
                    % (parsed["exec_argv"][0],)
                )
                + (" %s" % _find_exec_note if _find_exec_note else ""),
            )
        if batch_rewrite:
            return _advisory(
                (
                    "Advisory: 'find ... -exec %s ... {} ;' (segment: %s) "
                    "forks one process PER MATCH -- the POSIX '+' form (%s) "
                    "batches matches with identical output for this verb, "
                    "but this find-exec segment runs alongside OTHER work in "
                    "the same command, so no full-command auto-rewrite is "
                    "offered."
                    % (parsed["exec_argv"][0], " ".join(tokens), batch_rewrite)
                )
                + (" %s" % _find_exec_note if _find_exec_note else "")
            )
        return _advisory(
            (
                "Advisory: 'find ... -exec %s ... {} ;' forks one process PER "
                "MATCH -- the founding-incident 879-process shape on Windows. "
                "A single python3 -c os.walk(...) loop does the same "
                "enumeration in one process; this exec'd verb has no known "
                "translation on file, so the rewrite is not offered "
                "automatically."
                % (parsed["exec_argv"][0],)
            )
            + (" %s" % _find_exec_note if _find_exec_note else "")
        )
    return None

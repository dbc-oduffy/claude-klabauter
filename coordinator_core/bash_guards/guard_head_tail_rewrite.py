"""coordinator_core.bash_guards.guard_head_tail_rewrite --
``check_head_tail_plumbing_rewrite`` (BX-16 shape 7 -- MULTI_PROBE_BANNER's
sibling gap for HEAD_TAIL_PLUMBING), extracted out of ``dispatch_checks.py``.

Pure move (M1 F9 widening, 2026-07-29): this module's own logic is unchanged
from its prior home. Extracted alongside ``guard_offer_git_c.py`` (beyond
either source plan's ratified scope, PM-assented per Merge Finding F9) so
`dispatch_checks.py` is wanted by two fewer waves on the critical path.

Shared helpers this module still imports from ``dispatch_checks``/the
package's shared tokenizer/shape-classifier rather than duplicating (this
file's own private helpers below -- ``_bt_head_tail_count``,
``_bt_parse_find_census_segment``, ``_bt_parse_ls_segment``,
``_bt_build_generator_lines``, ``_bt_head_short_circuit_lines``,
``_bt_tail_ring_buffer_lines`` -- are used ONLY by
``check_head_tail_plumbing_rewrite`` and each other): ``_crlf_strip``,
``_override``, ``_advisory``, ``_allow_rewrite``, ``_bt_has_redirection``
(also consumed by ``check_grep_via_bash_rewrite``'s
``_bt_grep_flags_and_operands``, which stays behind),
``_bt_grep_flags_and_operands``/``_GREP_FAMILY_BINARIES_BT`` (also consumed
by ``check_grep_via_bash_rewrite``), ``_bt_python3_invocation`` (also
consumed by ``check_find_exec_rewrite``/``check_grep_via_bash_rewrite``/
``check_multiprobe_banner_rewrite``), and the shared tokenizer/shape
classifier.

Spec backlink: docs/plans/2026-07-29-bash-guard-merged-execution-shape.md M1
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards.dispatch_checks import (
    _GREP_FAMILY_BINARIES_BT,
    _advisory,
    _allow_rewrite,
    _bt_grep_flags_and_operands,
    _bt_has_redirection,
    _bt_python3_invocation,
    _crlf_strip,
    _override,
)
from coordinator_core.bash_guards._command_tokenizer import (
    segments_from_tokens_with_pipe_flag as _bt_segments_from_tokens_with_pipe_flag,
    token_matches_binary as _bt_token_matches_binary,
)
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._shape_classifier import (
    Shape as _BT_Shape,
    classify_command as _bt_classify_command,
)


def _bt_head_tail_count(tokens_rest: List[str]) -> Optional[int]:
    """Parse a `head`/`tail` segment's own argument tokens (everything after
    the binary name) into a line count -- recognized forms only: no args
    (bare `head`/`tail`, default 10), `-N` (old-style numeric flag), or
    `-n N` (two separate tokens). Anything else (a flag this doesn't know,
    `-c` byte-count mode, etc.) returns `None` so the caller advises instead
    of guessing."""
    if not tokens_rest:
        return 10
    if len(tokens_rest) == 1:
        tok = tokens_rest[0]
        if tok.startswith("-n") and tok[2:].isdigit():
            return int(tok[2:])
        if tok.startswith("-") and tok[1:].isdigit():
            return int(tok[1:])
        return None
    if len(tokens_rest) == 2 and tokens_rest[0] == "-n" and tokens_rest[1].isdigit():
        return int(tokens_rest[1])
    return None


def _bt_parse_find_census_segment(tokens: List[str]) -> Optional[Dict[str, Any]]:
    """Parse a `find`-invocation segment with NO `-exec` (that case belongs
    to `check_find_exec_rewrite`, not this one) into a
    ``{"path", "name_pattern", "only_files"}`` census descriptor, or `None`
    if it carries a flag this function does not recognize, OR a shell
    redirection operator (`_bt_has_redirection`) -- never guessed.
    """
    if not tokens or not _bt_token_matches_binary(tokens[0], "find"):
        return None
    if "-exec" in tokens:
        return None
    if _bt_has_redirection(tokens[1:]):
        return None
    pred = tokens[1:]
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
        return None  # an unrecognized find flag -- don't guess a translation
    return {"path": path, "name_pattern": name_pattern, "only_files": only_files}


def _bt_parse_ls_segment(tokens: List[str]) -> Optional[Dict[str, Any]]:
    """Parse a bare `ls`/`ls -1`/`ls -a` segment into a ``{"path"}``
    descriptor, or `None` for any flag this function does not recognize, OR
    a shell redirection operator (`_bt_has_redirection`) -- a redirection
    token is not a path operand (see that helper's own docstring for the
    live incident this guards against: `ls DIR 2>/dev/null | head -40`
    silently took `"2>/dev/null"` as the directory to list)."""
    if not tokens or not _bt_token_matches_binary(tokens[0], "ls"):
        return None
    if _bt_has_redirection(tokens[1:]):
        return None
    path = "."
    for tok in tokens[1:]:
        if tok.startswith("-"):
            if tok not in ("-1", "-a"):
                return None
            continue
        path = tok
    return {"path": path}


def _bt_build_generator_lines(kind: str, parsed: Dict[str, Any]) -> Optional[List[str]]:
    """Return python source LINES that populate an `_out: List[str]`
    variable with the exact stdout lines the upstream generator segment
    (``kind`` in ``{"find", "ls", "grep"}``) would have produced -- always
    SORTED for determinism (`find`'s own directory-traversal order is not
    guaranteed reproducible across a rewrite, so this rewrite deliberately
    trades raw `find` order for a stable one rather than guessing at it)."""
    if kind == "find":
        path = parsed["path"]
        pattern = parsed["name_pattern"]
        only_files = parsed["only_files"]
        match_expr = (
            # fnmatchcase, not fnmatch -- see the identical fix + rationale
            # in `_bt_find_exec_python_rewrite`, above: `find -name` is
            # case-sensitive on every platform, but `fnmatch.fnmatch()`
            # silently isn't on Windows (`os.path.normcase` lower-cases both
            # sides there).
            "fnmatch.fnmatchcase(fn, %s)" % json.dumps(pattern) if pattern else "True"
        )
        entries_expr = "files" if only_files else "files + dirs"
        return [
            "import fnmatch, os",
            "_out = []",
            "for root, dirs, files in os.walk(%s):" % json.dumps(path),
            "    for fn in sorted(%s):" % entries_expr,
            "        if %s:" % match_expr,
            "            _out.append(os.path.join(root, fn))",
        ]
    if kind == "ls":
        path = parsed["path"]
        return [
            "import os",
            "_out = sorted(os.listdir(%s))" % json.dumps(path),
        ]
    if kind == "grep":
        flags = parsed["flags"]
        lines = [
            "import os, re",
            "pat = re.compile(%s%s)"
            % (
                json.dumps(parsed["pattern"]),
                ", re.IGNORECASE" if "i" in flags else "",
            ),
            "targets = %s" % json.dumps(parsed["targets"]),
            "_out = []",
            "for base in targets:",
            "    walk = os.walk(base) if os.path.isdir(base) else "
            '[(os.path.dirname(base) or ".", [], [os.path.basename(base)])]',
            "    for root, dirs, files in walk:",
            "        for fn in sorted(files):",
            "            p = os.path.join(root, fn)",
            "            try:",
            '                with open(p, encoding="utf-8", errors="replace") as fh:',
            "                    lines_ = fh.readlines()",
            "            except OSError:",
            "                continue",
            "            hits = [(i + 1, ln) for i, ln in enumerate(lines_) if pat.search(ln)]",
        ]
        if "l" in flags:
            lines.append("            if hits:")
            lines.append("                _out.append(p)")
        elif "c" in flags:
            lines.append("            if hits:")
            lines.append('                _out.append(p + ":" + str(len(hits)))')
        else:
            lines.append("            for lineno, ln in hits:")
            lines.append(
                '                _out.append(p + ":" + str(lineno) + ":" + ln.rstrip())'
            )
        return lines
    return None  # pragma: no cover -- callers only ever pass a recognized kind


def _bt_head_short_circuit_lines(gen_lines: List[str], n: int) -> List[str]:
    """Wrap `gen_lines` (the flat `_out.append(...)`-populating generator
    body from `_bt_build_generator_lines`, `kind` in ``{"find", "grep"}``)
    so a HEAD slice of the first `n` items stops the underlying tree/file
    walk the moment the n-th item is collected, instead of enumerating every
    remaining item only to discard it in the final `_out[:n]` slice -- the
    fix for the fork-count benchmark's finding that `head -n 5` over a large
    tree walked the WHOLE tree first, growing without a ceiling.

    Requires `n > 0` -- the caller special-cases `n <= 0` separately (there
    is nothing to collect, so the generator body is skipped entirely rather
    than run and then immediately unwound).

    Inserts an `if len(_out) >= n: raise _BtHeadDone()` guard at the SAME
    indentation immediately after every `_out.append(...)` line found (both
    `find`'s and `grep`'s generator bodies append inside nested `for`
    loops -- ANY depth of nesting is covered, since the guard is re-emitted
    at each append site's own indent, not just the outermost loop), then
    wraps the WHOLE body in `try/except _BtHeadDone: pass` so the raise
    unwinds every nesting level at once, cleanly -- a plain `break` cannot
    escape more than its own immediately-enclosing `for`, and this walk may
    be nested two or three loops deep.

    The guard fires strictly AFTER the append that reaches `n`, so `_out`
    holds AT MOST `n` items when the walk stops early (fewer only if the
    walk exhausts before reaching `n`) -- `_out[:n]` in the caller's final
    slice is then a no-op over an already-bounded list, never a behavior
    change: the SEQUENCE of collected items is unaffected by when the walk
    stops, only the amount of unnecessary extra work after item `n` is
    eliminated.

    `ls`'s generator body (`_out = sorted(os.listdir(path))`) carries no
    `_out.append(...)` call to instrument -- returned UNCHANGED (detected via
    the `any(...)` guard below). This is not a gap: `os.listdir` is a single
    syscall with no incremental/partial form, so there is no walk to
    short-circuit for that generator kind -- the caller only routes `find`/
    `grep` bodies through this function for that reason.
    """
    if not any("_out.append(" in line for line in gen_lines):
        return gen_lines
    body: List[str] = []
    for line in gen_lines:
        body.append("    " + line)
        stripped = line.lstrip()
        if stripped.startswith("_out.append("):
            indent = line[: len(line) - len(stripped)]
            body.append("    " + indent + "if len(_out) >= %d:" % n)
            body.append("    " + indent + "    raise _BtHeadDone()")
    return (
        ["class _BtHeadDone(Exception): pass", "try:"]
        + body
        + ["except _BtHeadDone:", "    pass"]
    )


def _bt_tail_ring_buffer_lines(gen_lines: List[str], kind: str, n: int) -> List[str]:
    """Bound the TAIL generator body's peak memory to O(n) via a
    `collections.deque(maxlen=n)` ring buffer, instead of accumulating the
    WHOLE stream into a plain list before slicing the last `n` off the end
    at the caller's `_out[-n:]`. Unlike the head short-circuit above, a tail
    slice genuinely needs to OBSERVE every item -- there is no way to know
    which items are the last `n` without seeing them all -- so this bounds
    memory, not walk time.

    Requires `n > 0` -- the caller special-cases `n <= 0` separately (a
    `deque(maxlen=0)` is legal but pointless; `tail -n 0`'s existing empty-
    list handling already covers it).

    `find`/`grep` generator bodies (see `_bt_build_generator_lines`)
    initialize `_out = []` then `.append(...)` inside nested loops --
    swapping the ONE init line for `_out = collections.deque(maxlen=n)` is
    sufficient: `deque.append` already evicts the oldest entry once
    `maxlen` is reached, so every later `.append(...)` call needs no
    change, and iteration order (oldest to newest) matches the original
    list's order, so the caller can print the deque directly instead of
    re-slicing it.

    `ls`'s body builds the whole line in one assignment
    (`_out = sorted(os.listdir(path))`) -- there is no incremental append to
    swap, so this appends a second line coercing the already-built list into
    a bounded deque afterward. `os.listdir` itself has no partial form (see
    `_bt_head_short_circuit_lines`'s identical note) -- this bounds the
    final RETAINED size, not the syscall itself, which still lists the
    whole directory in one call either way.

    Returns `gen_lines` UNCHANGED if no `_out = []` init line is found for a
    non-`ls` kind (defensive -- every currently recognized `find`/`grep`
    generator body emits exactly one) rather than silently skipping the
    memory bound.
    """
    if kind == "ls":
        return gen_lines + ["_out = collections.deque(_out, maxlen=%d)" % n]
    out: List[str] = []
    replaced = False
    for line in gen_lines:
        if not replaced and line.strip() == "_out = []":
            out.append(line.replace("_out = []", "_out = collections.deque(maxlen=%d)" % n))
            replaced = True
        else:
            out.append(line)
    if not replaced:
        return gen_lines
    return out


def check_head_tail_plumbing_rewrite(cmd: str, session_id: str = "") -> Optional[Dict[str, Any]]:
    """BX-16 shape 7 (BX-8's rewrite target, head/tail half) -- `... | head
    -n N` / `... | tail -n N` truncates a subprocess's output via ANOTHER
    subprocess. A third of all measured forks are this text-plumbing shape
    rather than the question being asked (BX-8's own body) -- the founding
    incident's own flagship example (`find ... -exec sh -c ...` piped
    through `head`) is `check_find_exec_rewrite`'s shape, not this one; this
    check owns the SIMPLE-generator-into-head/tail pipelines
    (`find`/`ls`/`grep` feeding `head`/`tail`, no `-exec`).

    Auto-rewrites to a single `python3 -c` one-liner reproducing the SAME
    generator output and slicing head/tail inside that ONE python3
    subprocess -- one fork replacing the pipeline's two, never zero: the
    rewrite is itself a real fork+exec, and calling it "in-process" (as
    this docstring and three sibling messages in this module did until
    2026-07-29) is the false-capability claim `_alternative_liveness.py`'s
    `_capability_id` docstring already names for this guard --
    when the entire observable output is reproducible -- the upstream stage
    is one of `_bt_parse_find_census_segment`/`_bt_parse_ls_segment`/the
    substitutable-residue grep form (`_bt_grep_flags_and_operands`, same
    rule as `check_grep_via_bash_rewrite`), the pipeline is exactly two
    segments (no further composition after `head`/`tail`), and the
    `head`/`tail` count is one of `_bt_head_tail_count`'s recognized forms.
    Advises with a skeleton, unchanged command, when any stage is not.
    Never denies -- see this section's module comment.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    if _override("COORDINATOR_ALLOW_HEAD_TAIL_PLUMBING"):
        return None
    classification = _bt_classify_command(cmd)
    if classification.tokens is None:
        return None
    if not classification.has_shape(_BT_Shape.HEAD_TAIL_PLUMBING):
        return None

    segments = _bt_segments_from_tokens_with_pipe_flag(classification.tokens)
    if len(segments) != 2:
        return _advisory(
            "Advisory: this pipeline pipes into 'head'/'tail' as part of a "
            "longer chain than this rewrite's conservative two-stage "
            "'generator | head-or-tail' shape covers, so no rewrite is "
            "offered -- and for a chain this long none would help: "
            "reproducing the upstream stages inside a python3 -c would mean "
            "running them as subprocesses anyway (python3 + shell + each "
            "upstream stage), which costs MORE forks than the chain you "
            "wrote, not fewer. Shortening the chain, or asking for less of "
            "its output, is the real saving here. %s"
            % operator_override_note("COORDINATOR_ALLOW_HEAD_TAIL_PLUMBING")
        )
    (up_tokens, up_pipe_before), (ht_tokens, ht_pipe_before) = segments
    if up_pipe_before or not ht_pipe_before or not ht_tokens or not up_tokens:
        return None  # not the `generator | head-or-tail` shape this check owns
    is_head = _bt_token_matches_binary(ht_tokens[0], "head")
    is_tail = _bt_token_matches_binary(ht_tokens[0], "tail")
    if not (is_head or is_tail):
        return None  # HEAD_TAIL_PLUMBING matched on a different segment; not ours

    n = _bt_head_tail_count(ht_tokens[1:])
    if n is None:
        return _advisory(
            "Advisory: '... | %s' truncates output via an extra subprocess -- "
            "this rewrite recognizes bare '%s', '%s -N', and '%s -n N' "
            "line-count forms only; this invocation's own arguments ('%s') "
            "are not one of those, so the rewrite is not offered "
            "automatically. %s"
            % (
                ht_tokens[0],
                ht_tokens[0],
                ht_tokens[0],
                ht_tokens[0],
                " ".join(ht_tokens[1:]),
                operator_override_note("COORDINATOR_ALLOW_HEAD_TAIL_PLUMBING"),
            )
        )

    kind: Optional[str] = None
    parsed: Optional[Dict[str, Any]] = None
    if _bt_token_matches_binary(up_tokens[0], "find"):
        parsed = _bt_parse_find_census_segment(up_tokens)
        kind = "find" if parsed else None
    elif _bt_token_matches_binary(up_tokens[0], "ls"):
        parsed = _bt_parse_ls_segment(up_tokens)
        kind = "ls" if parsed else None
    elif any(_bt_token_matches_binary(up_tokens[0], b) for b in _GREP_FAMILY_BINARIES_BT):
        parsed = _bt_grep_flags_and_operands(up_tokens)
        kind = "grep" if parsed else None

    if kind is None or parsed is None:
        return _advisory(
            "Advisory: '... | %s' truncates a subprocess's output via ANOTHER "
            "subprocess. No rewrite is offered here because none would help, "
            "not because a translation is merely missing from file: this "
            "pipeline's upstream stage ('%s') is not one this guard can "
            "reproduce in Python, so a python3 -c would have to RUN it as a "
            "subprocess -- python3 plus a shell plus the upstream stage, more "
            "forks than the two you wrote, not fewer. %s"
            % (
                ht_tokens[0],
                " ".join(up_tokens),
                operator_override_note("COORDINATOR_ALLOW_HEAD_TAIL_PLUMBING"),
            )
        )

    gen_lines = _bt_build_generator_lines(kind, parsed)
    if gen_lines is None:  # pragma: no cover -- defensive, kind is always recognized here
        return None

    # HEAD: short-circuit the walk once the first `n` items are collected
    # (`_bt_head_short_circuit_lines`) instead of enumerating the whole
    # generator only to discard everything past item `n` -- the fork-count
    # benchmark's finding this fix exists for (`head -n 5` over a large
    # tree walked the WHOLE tree first). `n <= 0` (only reachable via an
    # explicit `-n 0`/`-0` -- bare `head` defaults to 10, see
    # `_bt_head_tail_count`) needs no walk at all: the first 0 items of
    # anything is empty, so the generator body is skipped entirely rather
    # than run and immediately unwound.
    if is_head:
        slice_expr = "_out[:%d]" % n
        if n > 0:
            body_lines = _bt_head_short_circuit_lines(gen_lines, n)
        else:
            body_lines = ["_out = []"]
    # TAIL: the whole stream must still be OBSERVED (there is no way to know
    # which items are the last `n` without seeing them all), so this bounds
    # MEMORY via a fixed-size ring (`_bt_tail_ring_buffer_lines`) rather than
    # walk time -- `_out` is already exactly the last `n` items once the
    # ring buffer is in place, so the final slice collapses to `_out` itself
    # instead of re-slicing a list that was never allowed to grow past `n`.
    elif n > 0:
        slice_expr = "_out"
        body_lines = ["import collections"] + _bt_tail_ring_buffer_lines(gen_lines, kind, n)
    else:
        slice_expr = "[]"  # `tail -n 0` -- Python's `_out[-0:]` would (wrongly) return everything
        body_lines = gen_lines

    script_lines = body_lines + ["for _l in %s:" % slice_expr, "    print(_l)"]
    script = "\n".join(script_lines)
    return _allow_rewrite(
        "%s -c %s" % (_bt_python3_invocation(), shlex.quote(script)),
        "Auto-rewrite: pipe into '%s' forks twice for one answer -- "
        "replaced with one python3 -c reproducing the same output and "
        "slicing head/tail inside that single subprocess. %s"
        % (ht_tokens[0], operator_override_note("COORDINATOR_ALLOW_HEAD_TAIL_PLUMBING")),
    )
